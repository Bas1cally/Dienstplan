"""Technische Indikatoren auf Basis von pandas/numpy."""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder-Glättung
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    # Sonderfall avg_loss == 0: reine Gewinnserie ist RSI 100 (nicht neutral 50),
    # sonst greift das Überkauft-Veto des Validators bei Parabel-Moves nicht.
    out = out.where(avg_loss > 0, np.where(avg_gain > 0, 100.0, 50.0))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range. Erwartet Spalten high, low, close."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()
