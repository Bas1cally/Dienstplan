"""Quantitative Analyse einzelner Trader anhand ihrer öffentlichen Fill-Historie.

Warum nicht einfach das Leaderboard-Ranking übernehmen? Weil dort ein einziger
Glückstreffer mit 40x Leverage ganz oben stehen kann. Wir wollen Trader, deren
Edge REPRODUZIERBAR ist. Dafür zerlegen wir die Fill-Historie in Kennzahlen:

  Profitabilität   - Netto-PnL (nach Fees!) relativ zum Kontowert
  Treffsicherheit  - Win-Rate und Profit Factor über viele Trades
  Konsistenz       - Anteil profitabler Tage (ein guter Monat != ein guter Trader)
  Risiko           - Max Drawdown der realisierten PnL-Kurve
  Stichprobe       - genug Trades, um Zufall auszuschließen

Der Score gewichtet diese Dimensionen und bestraft hartes Risiko überproportional.
"""

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class TraderMetrics:
    address: str
    account_value: float
    days: int
    # Profitabilität
    realized_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    roi: float = 0.0                # net_pnl / account_value
    # Treffsicherheit
    closed_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    # Konsistenz
    active_days: int = 0
    profitable_day_share: float = 0.0
    # Risiko
    max_drawdown: float = 0.0       # relativ zum Kontowert, >= 0
    # Aktivität
    coins: list[str] = field(default_factory=list)
    trades_per_day: float = 0.0
    score: float = 0.0


def analyze_fills(address: str, fills: list[dict], account_value: float, days: int) -> TraderMetrics:
    """Berechnet Metriken aus rohen userFillsByTime-Fills (zeitlich sortiert)."""
    m = TraderMetrics(address=address, account_value=account_value, days=days)
    if not fills or account_value <= 0:
        return m

    fills = sorted(fills, key=lambda f: f["time"])
    daily_pnl: dict[str, float] = defaultdict(float)
    coins: set[str] = set()
    gross_win = gross_loss = 0.0
    wins = 0
    cum = peak = 0.0

    for f in fills:
        coins.add(f["coin"])
        closed = float(f.get("closedPnl", 0))
        fee = float(f.get("fee", 0))
        m.fees += fee
        net = closed - fee
        day = _day_key(int(f["time"]))
        daily_pnl[day] += net

        if closed != 0:  # schließender Fill = abgeschlossener (Teil-)Trade
            m.realized_pnl += closed
            m.closed_trades += 1
            if closed > 0:
                wins += 1
                gross_win += closed
            else:
                gross_loss += -closed

        # Drawdown auf der realisierten Netto-Kurve
        cum += net
        peak = max(peak, cum)
        m.max_drawdown = max(m.max_drawdown, (peak - cum) / account_value)

    m.net_pnl = m.realized_pnl - m.fees
    m.roi = m.net_pnl / account_value
    m.coins = sorted(coins)
    m.active_days = len(daily_pnl)
    m.trades_per_day = len(fills) / max(days, 1)
    if m.closed_trades:
        m.win_rate = wins / m.closed_trades
    m.profit_factor = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    if daily_pnl:
        m.profitable_day_share = sum(1 for v in daily_pnl.values() if v > 0) / len(daily_pnl)

    m.score = _score(m)
    return m


def _day_key(ts_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _score(m: TraderMetrics) -> float:
    """Composite-Score 0..100. Konservativ: Risiko und dünne Datenlage drücken hart."""
    if m.closed_trades < 5 or m.net_pnl <= 0:
        return 0.0

    # ROI: bei 10% im Fenster voll ausgereizt (alles darüber ist meist Leverage-Glück)
    roi_part = min(m.roi / 0.10, 1.0)
    # Profit Factor: 2.0 gilt als exzellent
    pf = min(m.profit_factor, 5.0)
    pf_part = min(pf / 2.0, 1.0)
    consistency_part = m.profitable_day_share
    # Drawdown: 15% relativ zum Konto = Score-Anteil 0
    risk_part = max(0.0, 1.0 - m.max_drawdown / 0.15)
    # Stichprobengröße: ab 50 abgeschlossenen Trades volle Punktzahl
    sample = min(m.closed_trades / 50.0, 1.0)

    raw = (0.30 * roi_part + 0.25 * pf_part + 0.20 * consistency_part + 0.25 * risk_part)
    return round(100 * raw * (0.5 + 0.5 * sample), 1)


class TraderAnalyzer:
    """Holt Fill-Historien über die Info-API und bewertet Trader."""

    def __init__(self, info, days: int):
        self.info = info
        self.days = days

    def analyze(self, address: str) -> TraderMetrics:
        import time as _time

        end = int(_time.time() * 1000)
        start = end - self.days * 86_400_000
        fills = self.info.user_fills_by_time(address, start, end)
        state = self.info.user_state(address)
        account_value = float(state["marginSummary"]["accountValue"])
        return analyze_fills(address, fills, account_value, self.days)

    def rank(self, addresses: list[str], min_score: float) -> list[TraderMetrics]:
        results = []
        for addr in addresses:
            try:
                m = self.analyze(addr)
                log.info("Analysiert %s: score=%.1f roi=%.1f%% pf=%.2f trades=%d dd=%.1f%%",
                         addr[:10], m.score, m.roi * 100, m.profit_factor,
                         m.closed_trades, m.max_drawdown * 100)
                if m.score >= min_score:
                    results.append(m)
            except Exception:
                log.exception("Analyse fehlgeschlagen für %s", addr)
        results.sort(key=lambda m: m.score, reverse=True)
        return results
