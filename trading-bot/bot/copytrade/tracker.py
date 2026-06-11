"""Beobachtet die offenen Positionen der Leader-Wallets.

Liefert pro Poll einen vollständigen Snapshot (kein Event-Diffing): Der Copier
gleicht daraus das Ziel-Portfolio ab. Vorteil: verpasste Polls oder Neustarts
korrigieren sich selbst, statt Positionen "zu verlieren".
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class LeaderPosition:
    coin: str
    size: float            # signiert: >0 long, <0 short
    entry: float
    position_value: float  # |Notional| in USD
    leverage: float


@dataclass
class LeaderSnapshot:
    address: str
    equity: float
    positions: dict[str, LeaderPosition]

    def exposure(self, coin: str) -> float:
        """Signierte Allokation in coin relativ zur Equity des Leaders (-x..+x)."""
        pos = self.positions.get(coin)
        if not pos or self.equity <= 0:
            return 0.0
        sign = 1.0 if pos.size > 0 else -1.0
        return sign * pos.position_value / self.equity


class LeaderTracker:
    def __init__(self, info, addresses: list[str], dexs: list[str] | None = None):
        self.info = info
        self.addresses = addresses
        self.dexs = dexs or [""]  # Haupt-DEX + Builder-DEXs (Aktien, Gold, Öl)

    def snapshot(self, address: str) -> LeaderSnapshot:
        equity = 0.0
        raw_positions: list = []
        for dex in self.dexs:
            try:
                state = self.info.user_state(address, dex=dex) if dex else self.info.user_state(address)
            except Exception:
                log.debug("Leader %s: DEX %r nicht abrufbar", address[:10], dex, exc_info=True)
                continue
            equity += float(state["marginSummary"]["accountValue"])
            raw_positions.extend(state.get("assetPositions", []))
        positions: dict[str, LeaderPosition] = {}
        for p in raw_positions:
            pos = p["position"]
            size = float(pos["szi"])
            if size == 0:
                continue
            positions[pos["coin"]] = LeaderPosition(
                coin=pos["coin"],
                size=size,
                entry=float(pos.get("entryPx") or 0),
                position_value=abs(float(pos.get("positionValue") or 0)),
                leverage=float(pos.get("leverage", {}).get("value", 1)),
            )
        return LeaderSnapshot(address=address, equity=equity, positions=positions)

    def snapshot_all(self) -> list[LeaderSnapshot]:
        snaps = []
        for addr in self.addresses:
            try:
                snaps.append(self.snapshot(addr))
            except Exception:
                log.exception("Snapshot fehlgeschlagen für %s - überspringe diese Runde", addr)
        return snaps
