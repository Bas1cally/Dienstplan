"""Lighter (zkLighter) als Copy-Quelle: fremde Positionen lesen -> LeaderSnapshot.

Die Probe (probe_dexs.py) hat bestätigt: Lighter gibt die offenen Positionen
JEDES Kontos öffentlich her (GET /api/v1/account?by=index|l1_address). Es gibt
KEIN Ranking-API - deshalb kommen die zu kopierenden Trader aus einer Watchlist
(config lighter.accounts), die der Nutzer aus dem Lighter-Web-Leaderboard füllt.

Feldnamen aus dem offiziellen SDK (AccountPosition): symbol, sign, position
(Betrag), avg_entry_price, position_value; Account: collateral (= Equity-Proxy).
Trotzdem defensiv geparst - erst am echten Konto per /lighter verifizieren,
dann darauf messen. Ausführung bleibt IMMER Hyperliquid.
"""

import logging

from ..copytrade.tracker import LeaderPosition, LeaderSnapshot

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://mainnet.zklighter.elliot.ai"


def _http_get(url: str, params: dict):
    import requests

    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _first_account(data) -> dict:
    """Toleriert {"accounts":[{...}]}, {"account":{...}} oder das Konto direkt."""
    if isinstance(data, dict):
        if isinstance(data.get("accounts"), list) and data["accounts"]:
            return data["accounts"][0]
        if isinstance(data.get("account"), dict):
            return data["account"]
        return data
    if isinstance(data, list) and data:
        return data[0]
    return {}


def _positions_of(acc: dict) -> list:
    for key in ("positions", "position", "openPositions"):
        v = acc.get(key)
        if isinstance(v, list):
            return v
    return []


def _is_long(pos: dict) -> bool:
    """sign-Semantik defensiv: >0/„long"/1 = long, <0/2/„short" = short.
    (Wird am echten Konto per /lighter verifiziert.)"""
    s = pos.get("sign")
    if isinstance(s, str):
        return s.lower().startswith("l") or s in ("1", "+1")
    try:
        return int(s) >= 0 and int(s) != 2
    except (TypeError, ValueError):
        return float(pos.get("position", 0) or 0) >= 0


class LighterClient:
    def __init__(self, base_url: str = DEFAULT_BASE, http=None):
        self.base = base_url.rstrip("/")
        self._http = http or _http_get

    def raw_account(self, ref: str) -> dict:
        by = "l1_address" if str(ref).lower().startswith("0x") else "index"
        return _first_account(self._http(f"{self.base}/api/v1/account",
                                         {"by": by, "value": str(ref)}))

    def snapshot(self, ref: str, map_coin=None) -> LeaderSnapshot:
        acc = self.raw_account(ref)
        equity = float(acc.get("collateral") or acc.get("available_balance") or 0)
        positions: dict[str, LeaderPosition] = {}
        for p in _positions_of(acc):
            size_abs = abs(float(p.get("position") or 0))
            if size_abs == 0:
                continue
            symbol = str(p.get("symbol") or "").upper()
            coin = map_coin(symbol) if map_coin else symbol
            if not coin:
                continue  # auf HL nicht handelbar -> überspringen
            long = _is_long(p)
            positions[coin] = LeaderPosition(
                coin=coin,
                size=size_abs if long else -size_abs,
                entry=float(p.get("avg_entry_price") or 0),
                position_value=abs(float(p.get("position_value") or 0)),
                leverage=1.0,
            )
        return LeaderSnapshot(address=str(ref), equity=equity, positions=positions)


class LighterSource:
    """CopySource-konform: Watchlist als Discovery, Snapshots von Lighter."""

    name = "lighter"

    def __init__(self, cfg, client: LighterClient | None = None):
        self.cfg = cfg                        # LighterConfig
        self.client = client or LighterClient(cfg.base_url)
        self._hl_coins = set(cfg.coins) if cfg.coins else None

    def discover(self, min_score: float = 0) -> list[str]:
        return [str(a) for a in (self.cfg.accounts or [])]

    def snapshot(self, ref: str) -> LeaderSnapshot:
        return self.client.snapshot(ref, map_coin=self.map_coin)

    def map_coin(self, venue_symbol: str) -> str | None:
        # Lighter-Symbole sind meist reine Coins (BTC, ETH, SOL); nur auf HL
        # handelbare zulassen (Whitelist, sonst alles durch).
        sym = venue_symbol.upper().replace("-USD", "").replace("USD", "").strip()
        if self._hl_coins is not None and sym not in self._hl_coins:
            return None
        return sym or None
