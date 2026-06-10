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
    def __init__(self, info, addresses: list[str]):
        self.info = info
        self.addresses = addresses

    def snapshot(self, address: str) -> LeaderSnapshot:
        state = self.info.user_state(address)
        equity = float(state["marginSummary"]["accountValue"])
        positions: dict[str, LeaderPosition] = {}
        for p in state.get("assetPositions", []):
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
