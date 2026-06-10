"""Investigator: beliebige Wallets durchleuchten und Market-Maker-Druck messen.

Inspiriert von hl.eco/Investigator - aber nativ aus der Quelle gebaut: Alle
Daten dort stammen aus der öffentlichen Hyperliquid-API, die wir ohnehin
nutzen. Drei Werkzeuge:

  Dossier      - komplette Akte zu einer Adresse: Positionen mit Leverage,
                 Equity, Round-Trip-Metriken, LARP-Verdict (investigate.py)
  Watchlist    - beliebige Wallets (Whales, mutmaßliche MMs) beobachten;
                 Positionswechsel landen im Journal und als Telegram-Alert
  Markt-Puls   - Funding-Rate + Open Interest je Coin: extremes Funding
                 zeigt, auf welcher Seite die Crowd steht (und gegen wen
                 die Market Maker kassieren)
"""

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Funding annualisiert: |Rate| darüber gilt als extrem (Crowd einseitig)
FUNDING_EXTREME_APR = 0.50


@dataclass
class WalletEvent:
    address: str
    coin: str
    kind: str          # open | close | flip | increase | decrease
    old_notional: float
    new_notional: float

    def text(self) -> str:
        side = "LONG" if self.new_notional > 0 or (self.new_notional == 0 and self.old_notional > 0) else "SHORT"
        return (f"{self.address[:8]}… {self.kind.upper()} {self.coin} {side}: "
                f"{self.old_notional:,.0f} → {self.new_notional:,.0f} USD")


class WalletWatcher:
    """Beobachtet Positionsänderungen beliebiger Wallets (Snapshot-Diff)."""

    def __init__(self, info, addresses: list[str], min_notional_change: float = 25_000):
        self.info = info
        self.addresses = [a.lower() for a in addresses]
        self.min_change = min_notional_change
        self._last: dict[str, dict[str, float]] = {}   # addr -> coin -> signiertes Notional

    def poll(self) -> list[WalletEvent]:
        events: list[WalletEvent] = []
        for addr in self.addresses:
            try:
                current = self._positions(addr)
            except Exception:
                log.debug("Watchlist: %s nicht abrufbar", addr, exc_info=True)
                continue
            prev = self._last.get(addr)
            if prev is not None:  # ersten Snapshot nie alerten (alles wäre "neu")
                events.extend(self._diff(addr, prev, current))
            self._last[addr] = current
        return events

    def _positions(self, addr: str) -> dict[str, float]:
        state = self.info.user_state(addr)
        out = {}
        for p in state.get("assetPositions", []):
            pos = p["position"]
            size = float(pos["szi"])
            if size != 0:
                value = abs(float(pos.get("positionValue") or 0))
                out[pos["coin"]] = value if size > 0 else -value
        return out

    def _diff(self, addr: str, prev: dict, cur: dict) -> list[WalletEvent]:
        events = []
        for coin in set(prev) | set(cur):
            old, new = prev.get(coin, 0.0), cur.get(coin, 0.0)
            if abs(new - old) < self.min_change:
                continue
            if old == 0:
                kind = "open"
            elif new == 0:
                kind = "close"
            elif (old > 0) != (new > 0):
                kind = "flip"
            else:
                kind = "increase" if abs(new) > abs(old) else "decrease"
            events.append(WalletEvent(addr, coin, kind, old, new))
        return events


@dataclass
class MarketPulse:
    coin: str
    funding_apr: float      # aktuelle Funding-Rate annualisiert (signiert)
    open_interest: float    # in Coin
    mark: float
    crowded: str = ""       # "long" | "short" | ""

    @property
    def oi_usd(self) -> float:
        return self.open_interest * self.mark


def market_pulse(info, coins: list[str] | None = None) -> list[MarketPulse]:
    """Funding + OI je Coin. Positives Funding = Longs zahlen = Crowd ist long.

    Market-Maker-Lesart: Extremes Funding heißt, die Taker-Crowd drängt auf
    eine Seite und zahlt dafür - die Gegenseite (meist MMs) kassiert. Gegen
    extrem gecrowdete Richtungen einzusteigen ist statistisch teuer.
    """
    meta, ctxs = info.meta_and_asset_ctxs()
    out = []
    for asset, ctx in zip(meta["universe"], ctxs):
        coin = asset["name"]
        if coins and coin not in coins:
            continue
        funding_hourly = float(ctx.get("funding", 0))
        apr = funding_hourly * 24 * 365
        mark = float(ctx.get("markPx") or 0)
        pulse = MarketPulse(
            coin=coin, funding_apr=apr,
            open_interest=float(ctx.get("openInterest", 0)), mark=mark,
        )
        if apr >= FUNDING_EXTREME_APR:
            pulse.crowded = "long"
        elif apr <= -FUNDING_EXTREME_APR:
            pulse.crowded = "short"
        out.append(pulse)
    return out
