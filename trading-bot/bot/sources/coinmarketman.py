"""HyperTracker (CoinMarketMan) API: reichhaltiges Perp-PnL-Leaderboard als
Trader-Quelle. Read-only, JWT-Bearer-Auth.

Der Token steht als COINMARKETMAN_TOKEN in der .env (Secret, gitignored) und wird
LAZY zur Laufzeit gelesen - so wirkt ein per /setcmm gesetzter Token sofort, ohne
Neustart. Nie im Code/Repo hinterlegen.

Spec-Quelle: github.com/Coin-Market-Man/hypertracker-skills (SKILL.md, offiziell) -
die Doku-Website blockt unseren Egress-Proxy, das Repo nicht.

Basis:  https://ht-api.coinmarketman.com/api/external
Boards: GET /leaderboards/perp-pnl   (perp-only - unser Hauptboard, wir kopieren Perps)
        GET /leaderboards/all-pnl    (Perps + Spot + Vaults)
        Pflicht-Query: rankBy + orderBy (pnlAllTime|pnlMonth|pnlWeek|pnlDay,
        beide gleich setzen) und order (asc|desc). limit: 25|50|100.
        all-pnl liefert: totalCount, sumPnl, assetsDistribution, Trader-Array mit
        address, totalValue, pnlAllTime/Month/Week/Day, rank, profile.
Kür:    GET /closed-trades/summary?address=0x... - totalTrades, wins, losses,
        longTrades, shortTrades, avgDuration -> echte Winrate fürs Tiefen-Scoring.

RATE-BUDGET (kritisch): Free-Tier = 100 Requests/TAG JE TOKEN. Discovery-Scan
alle 6h = 4 Board-Reads/Tag; Tiefen-Scoring der Top-K via closed-trades/summary
muss budgetiert werden (K<=15 je Scan -> <=64 Req/Tag gesamt). Niemals je Tick
callen.

TOKEN-ROTATION (Nutzer-Entscheidung 17.07., bewusstes ToS-Risiko akzeptiert):
bis zu MAX_FALLBACK_TOKENS Tokens (COINMARKETMAN_TOKEN, _2, _3, ...) - ist
Token N an seinem 100/Tag-Limit (429), wechselt der Client automatisch auf
Token N+1. NUR bei 429 gewechselt, nie bei 401 (ein kaputter Token wird durch
Rotation nicht repariert). Der aktive Index ist Prozess-weit (Modul-State) und
bewusst NICHT persistiert - ein Neustart/Deploy ist ohnehin der natürliche
Reset-Punkt, und die echten Tages-Budgets laufen serverseitig bei CMM weiter,
unabhängig davon, was wir hier merken.
"""
import json
import logging
import os
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

TOKEN_ENV = "COINMARKETMAN_TOKEN"
MAX_FALLBACK_TOKENS = 5   # COINMARKETMAN_TOKEN (Slot 1) + _2.._5

RANK_FIELDS = ("pnlDay", "pnlWeek", "pnlMonth", "pnlAllTime")

# Prozess-weiter Rotations-Zeiger: welcher Token-Slot (Index in tokens())
# gerade als "noch nicht ausgeschöpft" gilt. Bewusst NICHT pro CMMClient-
# Instanz (die werden an vielen Stellen frisch konstruiert), sonst würde
# jede neue Instanz wieder bei Slot 1 anfangen und ihn erneut vergeblich
# probieren, bis das nächste 429 kommt - reine Verschwendung des ohnehin
# knappen Budgets.
_active_token_idx = 0


class CMMClient:
    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def tokens() -> list[str]:
        """Alle konfigurierten Tokens in Reihenfolge (Haupt-Token zuerst)."""
        out = []
        primary = os.environ.get(TOKEN_ENV, "").strip()
        if primary:
            out.append(primary)
        for i in range(2, MAX_FALLBACK_TOKENS + 1):
            tok = os.environ.get(f"{TOKEN_ENV}_{i}", "").strip()
            if tok:
                out.append(tok)
        return out

    @staticmethod
    def token() -> str:
        """Der aktuell aktive Token (für /cmm-Probe & 'ist überhaupt einer
        gesetzt'-Checks) - der erste, der noch nicht als ausgeschöpft gilt."""
        toks = CMMClient.tokens()
        if not toks:
            return ""
        return toks[min(_active_token_idx, len(toks) - 1)]

    def _get(self, path: str, params: dict | None = None):
        global _active_token_idx
        toks = self.tokens()
        if not toks:
            raise RuntimeError(f"{TOKEN_ENV} fehlt in der .env (per /setcmm setzen)")
        start = min(_active_token_idx, len(toks) - 1)
        for offset in range(len(toks) - start):
            idx = start + offset
            r = requests.get(
                f"{self.base_url}/{path.lstrip('/')}",
                params=params or {},
                headers={"Authorization": f"Bearer {toks[idx]}", "Accept": "application/json"},
                timeout=self.timeout,
            )
            if r.status_code == 401:
                raise RuntimeError(f"401: Token #{idx + 1} ungültig/abgelaufen")
            if r.status_code == 429:
                _active_token_idx = idx + 1
                if idx + 1 < len(toks):
                    log.warning("CMM: Token #%d Tageslimit erreicht - wechsle auf Token #%d",
                               idx + 1, idx + 2)
                else:
                    log.warning("CMM: Token #%d Tageslimit erreicht - keine weiteren "
                               "Tokens konfiguriert (siehe /setcmm)", idx + 1)
                continue
            if r.status_code >= 400:
                # Der Body sagt bei 400/403 meist EXAKT, welcher Parameter fehlt/falsch
                # ist - ohne ihn debuggen wir blind (gelernt aus der ersten 400-Probe).
                raise RuntimeError(f"{r.status_code} auf /{path.lstrip('/')}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"429: alle {len(toks)} Token(s) ausgeschöpft "
                           f"(Free-Tier: 100 Requests/Tag je Token)")

    def leaderboard(self, board: str = "perp-pnl", rank_by: str = "pnlMonth",
                    limit: int = 100, offset: int = 0, order: str = "desc"):
        """Leaderboard `perp-pnl` (perp-only) oder `all-pnl`. rankBy/orderBy/order
        sind PFLICHT (400 sonst); rankBy==orderBy ist die sinnvolle Belegung."""
        if rank_by not in RANK_FIELDS:
            raise ValueError(f"rank_by muss eins von {RANK_FIELDS} sein, nicht {rank_by!r}")
        if limit not in (25, 50, 100):
            limit = min((25, 50, 100), key=lambda v: abs(v - limit))
        return self._get(f"leaderboards/{board}",
                         {"rankBy": rank_by, "orderBy": rank_by, "order": order,
                          "limit": limit, "offset": offset})

    def closed_trades_summary(self, address: str):
        """Winrate-Rohdaten je Wallet: totalTrades, wins, losses, avgDuration.
        ACHTUNG Budget: 1 Request pro Wallet - nur für Top-Kandidaten nutzen."""
        return self._get("closed-trades/summary", {"address": address})


# ---------- Discovery: perp-pnl-Board -> Kandidaten für die Tiefenanalyse ----------
#
# Live-Probe (13.07.2026) hat das Contract bestätigt:
#   {totalCount, data: [{address, age, perpEquity, openValue, openValueLong,
#    exposureRatio, bias, pnlDay/Week/Month/AllTime, rank, volumeDay/Week/Month/
#    AllTime, pnlPercent*}]}  -- ALLE Zahlwerte kommen als Strings.

@dataclass
class CMMCandidate:
    address: str
    equity: float          # perpEquity
    pnl_month: float
    pnl_week: float
    pnl_day: float
    exposure_ratio: float  # openValue / perpEquity (effektiver Hebel)
    volume_month: float
    rank: int


def _f(v) -> float:
    """Die API liefert Zahlen als Strings ('4522388.484516') - defensiv wandeln."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_cmm_candidates(cfg) -> list[CMMCandidate]:
    """KOPIERBARE Kandidaten aus dem perp-pnl-Leaderboard, über MEHRERE
    Zeitfenster (`periods` x `pages` Requests).

    Warum mehrere Fenster: das Monats-Board wird von Positions-Sitzern
    dominiert - die AKTIVEN Richtungs-Trader (viele kleine Treffer) stehen
    dort auf Rang 300+. Auf dem WOCHEN-Board stehen sie vorn, denn wer diese
    Woche vorne ist, hat diese Woche gehandelt. pnlWeek zuerst = Aktive
    bekommen die vorderen Plätze im Kandidaten-Interleave.

    Live-Befund bleibt: Board-Spitzen sind MM/HFT-Whales (408x Umsatz) - daher
    Umsatz-/Hebel-/Equity-Deckel VOR der teuren Analyse. Das echte Qualitäts-
    Gate bleibt die Tiefenanalyse + LARP/Sprint-Gate auf HL-Daten."""
    client = CMMClient(cfg.base_url, cfg.timeout)
    out: list[CMMCandidate] = []
    seen: set[str] = set()
    pages = max(1, int(getattr(cfg, "pages", 1)))
    periods = list(getattr(cfg, "periods", None) or [cfg.period])
    for period in periods:
        for page in range(pages):
            payload = client.leaderboard(board="perp-pnl", rank_by=period,
                                         limit=cfg.limit, offset=page * cfg.limit)
            rows = rows_of(payload)
            if not rows:
                break
            for r in rows:
                if not isinstance(r, dict):
                    continue
                addr = str(r.get("address", "")).strip()
                if not addr.startswith("0x") or addr.lower() in seen:
                    continue
                c = CMMCandidate(
                    address=addr,
                    equity=_f(r.get("perpEquity")),
                    pnl_month=_f(r.get("pnlMonth")),
                    pnl_week=_f(r.get("pnlWeek")),
                    pnl_day=_f(r.get("pnlDay")),
                    exposure_ratio=_f(r.get("exposureRatio")),
                    volume_month=_f(r.get("volumeMonth")),
                    rank=int(_f(r.get("rank"))),
                )
                if not (cfg.min_equity <= c.equity <= cfg.max_equity):
                    continue
                # Fenster-PnL passend zum Board: Wochen-Board -> Wochen-PnL zählt
                window_pnl = c.pnl_week if period == "pnlWeek" else \
                    c.pnl_day if period == "pnlDay" else c.pnl_month
                if window_pnl <= cfg.min_pnl:
                    continue
                if abs(c.exposure_ratio) > cfg.max_exposure:
                    continue
                if c.equity > 0 and c.volume_month / c.equity > cfg.max_turnover:
                    continue
                seen.add(addr.lower())
                out.append(c)
    return out


def rows_of(payload) -> list:
    """Defensiv: die Zeilen-Liste aus verschiedenen möglichen Antwort-Hüllen ziehen
    (Liste direkt, oder {data|rows|leaderboard|results|items|traders: [...]}, oder
    erstbeste Liste im Dict). Bis die Probe das echte Format bestätigt, raten wir
    robust."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "rows", "leaderboard", "results", "items", "traders"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
        for v in payload.values():           # {data: {rows: [...]}} o.ä.
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list):
                        return vv
    return []


def describe_payload(payload, period: str, limit: int) -> str:
    """Beschreibt die ROHE Leaderboard-Struktur (Keys, Beispielzeile) fürs /cmm -
    damit wir das echte Contract sehen, bevor der Parser gebaut wird."""
    rows = rows_of(payload)
    top_type = type(payload).__name__
    top_keys = list(payload.keys())[:12] if isinstance(payload, dict) else "(Liste)"
    out = [f"Antwort-Typ: {top_type} | Top-Keys: {top_keys}",
           f"Zeilen erkannt: {len(rows)}"]
    if rows and isinstance(rows[0], dict):
        r0 = rows[0]
        out.append(f"Zeilen-Keys: {list(r0.keys())}")
        sample = {k: str(v)[:40] for k, v in list(r0.items())[:16]}
        out.append("Beispiel #1:\n" + json.dumps(sample, indent=1, ensure_ascii=False)[:1200])
    elif rows:
        out.append(f"Zeile[0]: {str(rows[0])[:400]}")
    else:
        out.append(f"⚠️ keine Zeilen erkannt. Roh (gekürzt): {str(payload)[:500]}")
    return "\n".join(out)


def probe(base_url: str, period: str = "pnlMonth", limit: int = 25,
          timeout: float = 20.0) -> str:
    """Live-Probe: beide Leaderboards holen und rohe Struktur beschreiben. Läuft
    nur vom VPS (der Sandbox-Egress-Proxy blockt coinmarketman.com).
    Budget: 2 Requests (von 100/Tag im Free-Tier)."""
    client = CMMClient(base_url, timeout)
    out = [f"🔎 <b>CMM-Probe</b> ({period}, limit {limit})"]
    for board in ("perp-pnl", "all-pnl"):
        out.append(f"\n<b>— {board} —</b>")
        try:
            payload = client.leaderboard(board=board, rank_by=period, limit=limit)
            out.append(describe_payload(payload, period, limit))
        except Exception as e:
            out.append(f"⚠️ {str(e)[:280]}")
    return "\n".join(out)
