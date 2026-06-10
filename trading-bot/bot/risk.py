"""Risikomanagement: Positionsgröße, Stops und Circuit Breaker.

Kernprinzip: Pro Trade wird höchstens `risk_per_trade` (z.B. 1%) des Kapitals
riskiert. Der Stop liegt `atr_stop_mult` ATRs vom Einstieg entfernt; daraus
ergibt sich die Positionsgröße. Leverage ist nur das Vehikel, um diese Größe
darstellen zu können - nie das Ziel. Ein hartes `max_leverage`-Limit deckelt
die Notional-Größe zusätzlich.
"""

from dataclasses import dataclass

from .config import RiskConfig


@dataclass
class PositionPlan:
    size: float          # Positionsgröße in Coin (z.B. BTC)
    notional: float      # Positionswert in USD
    leverage: int        # zu setzender Leverage (aufgerundet, gedeckelt)
    entry: float
    stop_loss: float
    take_profit: float
    risk_usd: float      # USD-Verlust, wenn der Stop greift


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def plan_position(
        self, equity: float, entry: float, atr_value: float, is_long: bool
    ) -> PositionPlan | None:
        """Berechnet Größe/Stop/TP für einen neuen Trade. None = kein Trade."""
        if equity <= 0 or entry <= 0 or atr_value <= 0:
            return None

        stop_dist = self.cfg.atr_stop_mult * atr_value
        risk_usd = equity * self.cfg.risk_per_trade
        size = risk_usd / stop_dist
        notional = size * entry

        # Leverage-Deckel: Notional darf equity * max_leverage nicht übersteigen
        max_notional = equity * self.cfg.max_leverage
        if notional > max_notional:
            scale = max_notional / notional
            size *= scale
            notional = max_notional
            risk_usd *= scale

        if notional < 10:  # Hyperliquid-Mindestordergröße
            return None

        direction = 1 if is_long else -1
        stop_loss = entry - direction * stop_dist
        take_profit = entry + direction * stop_dist * self.cfg.take_profit_r
        leverage = max(1, min(self.cfg.max_leverage, -(-int(notional) // max(int(equity), 1))))

        return PositionPlan(
            size=size,
            notional=notional,
            leverage=leverage,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_usd=risk_usd,
        )

    def daily_loss_exceeded(self, day_start_equity: float, equity: float) -> bool:
        """Circuit Breaker: True, wenn der Tagesverlust das Limit reißt."""
        if day_start_equity <= 0:
            return False
        drawdown = (day_start_equity - equity) / day_start_equity
        return drawdown >= self.cfg.max_daily_loss

    def total_drawdown_exceeded(self, start_equity: float, equity: float) -> bool:
        """Max-Drawdown-Halt (Prop-Firmen-Regel): Verlust vom Startkapital."""
        if start_equity <= 0:
            return False
        return (start_equity - equity) / start_equity >= self.cfg.max_total_drawdown

    @staticmethod
    def stop_hit(is_long: bool, price: float, stop_loss: float, take_profit: float) -> str | None:
        """Prüft, ob ein Preis Stop-Loss oder Take-Profit auslöst."""
        if is_long:
            if price <= stop_loss:
                return "stop_loss"
            if price >= take_profit:
                return "take_profit"
        else:
            if price >= stop_loss:
                return "stop_loss"
            if price <= take_profit:
                return "take_profit"
        return None
