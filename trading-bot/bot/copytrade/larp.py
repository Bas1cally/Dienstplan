"""LARP-Filter: trennt echte Trader von Glückstreffern und Blendern.

"LARPer" auf dem Leaderboard erkennt man an wiederkehrenden Mustern:
  - Ein einziger Mega-Trade trägt fast den ganzen PnL (Lucky Punch)
  - Wenige Trades / kurze Historie (Zufall nicht ausschließbar)
  - Nur eine gute Woche im Fenster (Momentum-Glück, kein Edge)
  - Extremer Drawdown unterwegs (Überhebelung - irgendwann kommt die Null)
  - Reines Scalping (für Copy-Trading unbrauchbar: unser Lag frisst den Edge)

Jedes Gate ist ein hartes K.O. - der Score spielt erst danach eine Rolle.
"""

import logging
from dataclasses import dataclass, field

from .analyzer import TraderMetrics

log = logging.getLogger(__name__)


@dataclass
class LarpConfig:
    min_round_trips: int = 30            # Mindestanzahl abgeschlossener Trades
    min_active_days: int = 10            # an wie vielen Tagen wurde gehandelt
    max_single_trade_share: float = 0.40 # größter Trade max. 40% des Brutto-Gewinns
    min_profitable_week_share: float = 0.60
    max_drawdown: float = 0.25           # 25% relativ zum Konto = überhebelt
    min_median_holding_minutes: float = 30.0  # Scalper aussortieren


@dataclass
class LarpVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)


class LarpFilter:
    def __init__(self, cfg: LarpConfig | None = None):
        self.cfg = cfg or LarpConfig()

    def check(self, m: TraderMetrics) -> LarpVerdict:
        c = self.cfg
        reasons: list[str] = []

        if m.net_pnl <= 0:
            reasons.append("netto unprofitabel (nach Fees)")
        if m.round_trips < c.min_round_trips:
            reasons.append(f"nur {m.round_trips} Round-Trips (< {c.min_round_trips})")
        if m.active_days < c.min_active_days:
            reasons.append(f"nur {m.active_days} aktive Tage (< {c.min_active_days})")
        if m.max_trade_share > c.max_single_trade_share:
            reasons.append(
                f"Lucky Punch: größter Trade = {m.max_trade_share:.0%} des Gewinns "
                f"(> {c.max_single_trade_share:.0%})"
            )
        if m.profitable_week_share < c.min_profitable_week_share:
            reasons.append(
                f"inkonsistent: nur {m.profitable_week_share:.0%} der Wochen profitabel "
                f"(< {c.min_profitable_week_share:.0%})"
            )
        if m.max_drawdown > c.max_drawdown:
            reasons.append(
                f"überhebelt: {m.max_drawdown:.0%} Drawdown (> {c.max_drawdown:.0%})"
            )
        if 0 < m.median_holding_minutes < c.min_median_holding_minutes:
            reasons.append(
                f"Scalper: mediane Haltedauer {m.median_holding_minutes:.0f}min "
                f"(< {c.min_median_holding_minutes:.0f}min) - Copy-Lag frisst den Edge"
            )

        return LarpVerdict(passed=not reasons, reasons=reasons)
