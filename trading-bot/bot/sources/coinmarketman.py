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

RATE-BUDGET (kritisch): Free-Tier = 100 Requests/TAG. Discovery-Scan alle 6h =
4 Board-Reads/Tag; Tiefen-Scoring der Top-K via closed-trades/summary muss
budgetiert werden (K<=15 je Scan -> <=64 Req/Tag gesamt). Niemals je Tick callen.
"""
import json
import logging
import os

import requests

log = logging.getLogger(__name__)

TOKEN_ENV = "COINMARKETMAN_TOKEN"

RANK_FIELDS = ("pnlDay", "pnlWeek", "pnlMonth", "pnlAllTime")


class CMMClient:
    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def token() -> str:
        return os.environ.get(TOKEN_ENV, "").strip()

    def _get(self, path: str, params: dict | None = None):
        tok = self.token()
        if not tok:
            raise RuntimeError(f"{TOKEN_ENV} fehlt in der .env (per /setcmm setzen)")
        r = requests.get(
            f"{self.base_url}/{path.lstrip('/')}",
            params=params or {},
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
            timeout=self.timeout,
        )
        if r.status_code == 401:
            raise RuntimeError("401: Token ungültig/abgelaufen")
        if r.status_code == 429:
            raise RuntimeError("429: Tages-Limit erreicht (Free-Tier: 100 Requests/Tag)")
        if r.status_code >= 400:
            # Der Body sagt bei 400/403 meist EXAKT, welcher Parameter fehlt/falsch
            # ist - ohne ihn debuggen wir blind (gelernt aus der ersten 400-Probe).
            raise RuntimeError(f"{r.status_code} auf /{path.lstrip('/')}: {r.text[:300]}")
        return r.json()

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
