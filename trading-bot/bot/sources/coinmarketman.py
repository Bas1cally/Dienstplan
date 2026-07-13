"""HyperTracker (CoinMarketMan) API: reichhaltiges Perp-PnL-Leaderboard als
Trader-Quelle. Read-only, JWT-Bearer-Auth.

Der Token steht als COINMARKETMAN_TOKEN in der .env (Secret, gitignored) und wird
LAZY zur Laufzeit gelesen - so wirkt ein per /setcmm gesetzter Token sofort, ohne
Neustart. Nie im Code/Repo hinterlegen.

Doku:   https://docs.coinmarketman.com/
Basis:  https://ht-api.coinmarketman.com/api/external
Board:  GET /leaderboards/all-pnl?pnl=pnlMonth&limit=100&offset=0&order=desc
        pnl: pnlDay | pnlWeek | pnlMonth | pnlAllTime (Sortierfeld)
        Zeilen (laut Doku): Wallet-Adresse, Wallet-Alter, Perp-Equity, offenes
        Notional, Long-Anteil, Exposure-Ratio, Directional Bias, pnlDay/Week/
        Month/AllTime, Volumen je Fenster, Rang. Exakte JSON-Keys per /cmm-Probe
        bestätigen, BEVOR der Parser (CoinMarketManSource) scharf geht.
"""
import json
import logging
import os

import requests

log = logging.getLogger(__name__)

TOKEN_ENV = "COINMARKETMAN_TOKEN"


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
        if r.status_code == 403:
            raise RuntimeError("403: kein Zugriff (Free-Tier deckt diesen Endpoint nicht?)")
        if r.status_code == 429:
            raise RuntimeError("429: Rate-Limit (Free-Tier) erreicht")
        r.raise_for_status()
        return r.json()

    def leaderboard(self, period: str = "pnlMonth", limit: int = 100,
                    offset: int = 0, order: str = "desc"):
        """Perp-PnL-Leaderboard, sortiert nach `period`."""
        return self._get("leaderboards/all-pnl",
                         {"pnl": period, "limit": limit, "offset": offset, "order": order})


def rows_of(payload) -> list:
    """Defensiv: die Zeilen-Liste aus verschiedenen möglichen Antwort-Hüllen ziehen
    (Liste direkt, oder {data|rows|leaderboard|results|items: [...]}, oder erstbeste
    Liste im Dict). Bis die Probe das echte Format bestätigt, raten wir robust."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "rows", "leaderboard", "results", "items"):
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
    out = [f"🔎 <b>CMM-Probe</b> ({period}, limit {limit})",
           f"Antwort-Typ: {top_type} | Top-Keys: {top_keys}",
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
    """Live-Probe: Leaderboard holen und rohe Struktur beschreiben. Läuft nur vom
    VPS (der Sandbox-Egress-Proxy blockt coinmarketman.com)."""
    payload = CMMClient(base_url, timeout).leaderboard(period=period, limit=limit)
    return describe_payload(payload, period, limit)
