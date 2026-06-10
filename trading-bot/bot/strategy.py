"""Trendfolge-Strategie: EMA-Crossover mit RSI-Filter und ATR für Stops.

Signal-Logik (nur auf abgeschlossenen Candles):
  LONG  - EMA(fast) kreuzt über EMA(slow), RSI nicht überkauft
  SHORT - EMA(fast) kreuzt unter EMA(slow), RSI nicht überverkauft
  EXIT  - Gegensignal (Stop-Loss/Take-Profit verwaltet das Risikomodul)
"""

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from .config import StrategyConfig
from .indicators import atr, ema, rsi


class Signal(Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    HOLD = "hold"


@dataclass
class Analysis:
    signal: Signal
    close: float
    atr: float
    ema_fast: float
    ema_slow: float
    rsi: float


class TrendStrategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    @property
    def min_candles(self) -> int:
        return max(self.cfg.ema_slow, self.cfg.rsi_period, self.cfg.atr_period) + 2

    def enrich(self, df: pd.DataFrame) -> pd.DataFrame:
        """Hängt Indikator-Spalten an einen OHLCV-DataFrame an."""
        df = df.copy()
        df["ema_fast"] = ema(df["close"], self.cfg.ema_fast)
        df["ema_slow"] = ema(df["close"], self.cfg.ema_slow)
        df["rsi"] = rsi(df["close"], self.cfg.rsi_period)
        df["atr"] = atr(df, self.cfg.atr_period)
        return df

    def analyze(self, df: pd.DataFrame) -> Analysis:
        """Bewertet die letzte abgeschlossene Candle in df."""
        if len(df) < self.min_candles:
            raise ValueError(f"Zu wenig Daten: {len(df)} < {self.min_candles} Candles")
        df = self.enrich(df)
        cur, prev = df.iloc[-1], df.iloc[-2]

        crossed_up = prev["ema_fast"] <= prev["ema_slow"] and cur["ema_fast"] > cur["ema_slow"]
        crossed_down = prev["ema_fast"] >= prev["ema_slow"] and cur["ema_fast"] < cur["ema_slow"]

        signal = Signal.HOLD
        if crossed_up:
            # Crossover nach oben beendet jede Short-Position; neue Longs nur
            # wenn der Markt nicht bereits überkauft ist.
            signal = Signal.LONG if cur["rsi"] < self.cfg.rsi_overbought else Signal.FLAT
        elif crossed_down:
            if self.cfg.allow_shorts and cur["rsi"] > self.cfg.rsi_oversold:
                signal = Signal.SHORT
            else:
                signal = Signal.FLAT

        return Analysis(
            signal=signal,
            close=float(cur["close"]),
            atr=float(cur["atr"]),
            ema_fast=float(cur["ema_fast"]),
            ema_slow=float(cur["ema_slow"]),
            rsi=float(cur["rsi"]),
        )
