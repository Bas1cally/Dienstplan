"""Event-basierter Backtester für die Trendfolge-Strategie.

Konservative Annahmen:
- Einstieg zum Open der Candle NACH dem Signal (kein Look-Ahead)
- Innerhalb einer Candle wird der Stop-Loss VOR dem Take-Profit geprüft
- Taker-Fees und Slippage auf jedem Fill
"""

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Config
from .risk import RiskManager
from .strategy import Signal, TrendStrategy

log = logging.getLogger(__name__)


@dataclass
class Trade:
    direction: str
    entry_time: int
    entry: float
    size: float
    stop_loss: float
    take_profit: float
    exit_time: int = 0
    exit: float = 0.0
    pnl: float = 0.0
    reason: str = ""


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series
    initial_equity: float

    @property
    def final_equity(self) -> float:
        return float(self.equity_curve.iloc[-1])

    def summary(self) -> dict:
        pnls = np.array([t.pnl for t in self.trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        eq = self.equity_curve
        running_max = eq.cummax()
        max_dd = float(((eq - running_max) / running_max).min())
        returns = eq.pct_change().dropna()
        sharpe = float(returns.mean() / returns.std() * np.sqrt(365 * 24)) if len(returns) > 1 and returns.std() > 0 else 0.0
        return {
            "trades": len(self.trades),
            "win_rate": round(len(wins) / len(pnls), 3) if len(pnls) else 0.0,
            "total_pnl": round(float(pnls.sum()), 2),
            "return_pct": round((self.final_equity / self.initial_equity - 1) * 100, 2),
            "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else float("inf"),
            "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
            "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe_per_bar_annualized": round(sharpe, 2),
            "final_equity": round(self.final_equity, 2),
        }


class Backtester:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.strategy = TrendStrategy(cfg.strategy)
        self.risk = RiskManager(cfg.risk)

    def run(self, df: pd.DataFrame) -> BacktestResult:
        bt = self.cfg.backtest
        df = self.strategy.enrich(df).reset_index(drop=True)
        equity = bt.initial_equity
        equity_curve: list[float] = []
        trades: list[Trade] = []
        open_trade: Trade | None = None
        pending: Signal | None = None
        cost = bt.fee_rate + bt.slippage  # pro Fill, anteilig am Notional

        warmup = self.strategy.min_candles
        for i in range(len(df)):
            row = df.iloc[i]
            ts = int(row["time"])

            # 1) Offene Position gegen High/Low der aktuellen Candle prüfen
            if open_trade:
                is_long = open_trade.direction == "long"
                touch = row["low"] if is_long else row["high"]
                reason = RiskManager.stop_hit(is_long, float(touch), open_trade.stop_loss, open_trade.take_profit)
                if reason == "stop_loss":
                    equity += self._close(open_trade, open_trade.stop_loss, ts, reason, cost)
                    trades.append(open_trade)
                    open_trade = None
                else:
                    other = row["high"] if is_long else row["low"]
                    reason = RiskManager.stop_hit(is_long, float(other), open_trade.stop_loss, open_trade.take_profit)
                    if reason == "take_profit":
                        equity += self._close(open_trade, open_trade.take_profit, ts, reason, cost)
                        trades.append(open_trade)
                        open_trade = None

            # 2) Signal der Vorcandle zum Open dieser Candle ausführen
            if pending is not None:
                entry = float(row["open"])
                if open_trade and (
                    pending in (Signal.FLAT,)
                    or (pending == Signal.LONG and open_trade.direction == "short")
                    or (pending == Signal.SHORT and open_trade.direction == "long")
                ):
                    equity += self._close(open_trade, entry, ts, "signal_exit", cost)
                    trades.append(open_trade)
                    open_trade = None
                if open_trade is None and pending in (Signal.LONG, Signal.SHORT):
                    atr_val = float(df.iloc[i - 1]["atr"])
                    plan = self.risk.plan_position(equity, entry, atr_val, pending == Signal.LONG)
                    if plan:
                        equity -= plan.notional * cost  # Einstiegs-Fee
                        open_trade = Trade(
                            direction="long" if pending == Signal.LONG else "short",
                            entry_time=ts,
                            entry=entry,
                            size=plan.size,
                            stop_loss=plan.stop_loss,
                            take_profit=plan.take_profit,
                        )
                pending = None

            # 3) Signal auf der (abgeschlossenen) aktuellen Candle berechnen
            if i >= warmup:
                sig = self._signal_at(df, i)
                if sig not in (Signal.HOLD,):
                    pending = sig

            # Mark-to-Market-Equity
            mtm = equity
            if open_trade:
                d = 1 if open_trade.direction == "long" else -1
                mtm += d * open_trade.size * (float(row["close"]) - open_trade.entry)
            equity_curve.append(mtm)

        if open_trade:
            last = df.iloc[-1]
            equity += self._close(open_trade, float(last["close"]), int(last["time"]), "end_of_data", cost)
            trades.append(open_trade)
            equity_curve[-1] = equity

        return BacktestResult(trades, pd.Series(equity_curve), bt.initial_equity)

    def _signal_at(self, df: pd.DataFrame, i: int) -> Signal:
        cur, prev = df.iloc[i], df.iloc[i - 1]
        s = self.cfg.strategy
        crossed_up = prev["ema_fast"] <= prev["ema_slow"] and cur["ema_fast"] > cur["ema_slow"]
        crossed_down = prev["ema_fast"] >= prev["ema_slow"] and cur["ema_fast"] < cur["ema_slow"]
        if crossed_up:
            return Signal.LONG if cur["rsi"] < s.rsi_overbought else Signal.FLAT
        if crossed_down:
            if s.allow_shorts and cur["rsi"] > s.rsi_oversold:
                return Signal.SHORT
            return Signal.FLAT
        return Signal.HOLD

    @staticmethod
    def _close(trade: Trade, price: float, ts: int, reason: str, cost: float) -> float:
        d = 1 if trade.direction == "long" else -1
        gross = d * trade.size * (price - trade.entry)
        fees = trade.size * price * cost
        trade.exit = price
        trade.exit_time = ts
        trade.reason = reason
        trade.pnl = gross - fees
        return trade.pnl
