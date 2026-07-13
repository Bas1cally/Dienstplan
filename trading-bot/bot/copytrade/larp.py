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
    min_median_holding_minutes: float = 30.0   # Scalper aussortieren
    max_median_holding_minutes: float = 0.0    # >0: Day-Trading-Profil - Swing-Trader raus


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
        if c.max_median_holding_minutes > 0 and m.median_holding_minutes > c.max_median_holding_minutes:
            reasons.append(
                f"Swing-Trader: mediane Haltedauer {m.median_holding_minutes / 60:.1f}h "
                f"(> {c.max_median_holding_minutes / 60:.0f}h) - passt nicht zum Day-Trading-Profil"
            )

        return LarpVerdict(passed=not reasons, reasons=reasons)


# Sprint-Buch: Münzwurf wäre 50% - darunter erkennt niemand "die Richtung".
SPRINT_MIN_WIN_RATE = 0.52


def check_sprint(m: TraderMetrics, cfg: LarpConfig | None = None) -> LarpVerdict:
    """Richtungs-orientiertes LARP-Gate fürs SPRINT-Buch (Nutzer-Vorgabe: es
    zählt, dass der Leader die Richtung erkennt - nicht, wie viel Profit er
    selbst aus dem Trade holt; Sprint nimmt +10% und ist raus).

    ZWEI Qualifikations-Pfade - Richtung kann man auf zwei Arten beweisen:

    A) Aktiv-Pfad (genug geschlossene Trades): Trefferquote >= 52%, halbierte
       Stichproben-Hürde, Aktivität, Scalper-Boden (unter ~30min Haltedauer
       erreicht kein Ritt die nötige ~1%-Bewegung).
    B) Positions-Pfad (Live-Befund: 25 von 34 LARP-Toten waren 'netto
       unprofitabel', weil Positions-Trader ihren Gewinn UNREALISIERT in
       offenen Positionen halten - Fills-Metriken bestrafen 'Gewinner laufen
       lassen'): offenes Buch mehrheitlich im Plus (>=60% wertgewichtet) UND
       Gesamt-PnL des Fensters inkl. unrealisiert positiv.

    GESTRICHEN bleiben die Profit-Größen-Gates des Hauptbuchs (Lucky-Punch,
    Wochen-Konsistenz, Drawdown, Swing-Cap): für +10%-und-raus irrelevant.
    Strikes/Bans räumen schwache Leader ohnehin nach 2 Verlust-Ritten ab."""
    c = cfg or LarpConfig()

    # --- Pfad A: aktiver Trader ---
    a: list[str] = []
    min_trips = max(10, c.min_round_trips // 2)
    if m.round_trips < min_trips:
        a.append(f"nur {m.round_trips} Round-Trips (< {min_trips})")
    # Bei verkürztem Messfenster (7d-Retry sehr aktiver Trader) anteilig fordern
    min_days = min(c.min_active_days, max(1, int(m.days * 0.6)))
    if m.active_days < min_days:
        a.append(f"nur {m.active_days} aktive Tage (< {min_days})")
    if 0 < m.median_holding_minutes < c.min_median_holding_minutes:
        a.append(
            f"Scalper: mediane Haltedauer {m.median_holding_minutes:.0f}min "
            f"(< {c.min_median_holding_minutes:.0f}min) - zu kurz für ~1% Bewegung"
        )
    if m.win_rate < SPRINT_MIN_WIN_RATE:
        a.append(
            f"Trefferquote {m.win_rate:.0%} (< {SPRINT_MIN_WIN_RATE:.0%}) - "
            f"erkennt die Richtung nicht besser als der Münzwurf"
        )
    if not a:
        return LarpVerdict(passed=True)

    # --- Pfad B: Positions-Trader mit grünem offenem Buch ---
    b: list[str] = []
    if m.open_positions < 1:
        b.append("keine offenen Positionen")
    if m.open_green_share < 0.60:
        b.append(f"offenes Buch nur {m.open_green_share:.0%} im Plus (< 60%)")
    if m.net_pnl + m.open_unrealized <= 0:
        b.append("Fenster-PnL inkl. unrealisiert <= 0")
    if not b:
        return LarpVerdict(passed=True)

    return LarpVerdict(passed=False, reasons=[
        "Aktiv: " + "; ".join(a), "Positions: " + "; ".join(b)])
