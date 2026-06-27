"""Unit-Tests fürs Strategie-Labor (TrendLab + FundingLab), ohne Netzwerk.

Geprüft wird die Handelslogik gegen synthetische Daten: TA-Signale lösen
Paper-Trades aus, Stop/Take-Profit greifen, Funding-Capture nimmt die
KASSIERENDE Seite und steigt bei Normalisierung/Stop/Zeit wieder aus.
"""

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import FundingLabConfig, TrendLabConfig
from bot.labs import FundingLab, TrendLab
from bot.paper import PaperBroker
from bot.strategy import Analysis, Signal


def broker():
    return PaperBroker(10_000, fee_rate=0.00045, path=Path(tempfile.mkdtemp()) / "lab.json")


class StubStrategy:
    """Liefert ein vorgegebenes Signal - testet TrendLabs Handelslogik isoliert
    (die EMA/RSI-Mechanik ist in test_validator/strategy schon abgedeckt)."""

    min_candles = 2

    def __init__(self, signal, close=100.0, atr=2.0):
        self._a = Analysis(signal=signal, close=close, atr=atr,
                           ema_fast=close, ema_slow=close, rsi=50.0)

    def set(self, signal, close=None):
        if close is not None:
            self._a.close = close
        self._a.signal = signal

    def analyze(self, df):
        return self._a


def candle(t):
    return pd.DataFrame([{"time": t, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
                         {"time": t + 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}])


@dataclass
class Pulse:
    coin: str
    funding_apr: float
    mark: float


# ---------- TrendLab (Handelslogik mit Signal-Stub) ----------

def test_trend_lab_goes_long_on_signal():
    strat = StubStrategy(Signal.LONG, close=100, atr=2.0)
    lab = TrendLab(TrendLabConfig(coin="BTC", strategy=None), broker(), strat)
    lab.on_candle("BTC", candle(1000), equity=10_000)
    size = lab.paper.sizes().get("BTC", 0)
    assert size > 0, "LONG-Signal muss Long öffnen"
    # Sizing: 1% Risiko / (2*ATR Stop-Distanz) = 100 / 4 = 25 Coins (unter Notional-Cap)
    assert abs(size - 25.0) < 0.01
    assert lab._stop["BTC"] == 96.0 and lab._tp["BTC"] == 108.0  # Stop -2*ATR, TP +2R


def test_trend_lab_shorts_on_signal():
    lab = TrendLab(TrendLabConfig(coin="BTC", strategy=None), broker(), StubStrategy(Signal.SHORT))
    lab.on_candle("BTC", candle(1000), equity=10_000)
    assert lab.paper.sizes().get("BTC", 0) < 0


def test_trend_lab_stop_closes_position():
    lab = TrendLab(TrendLabConfig(coin="BTC", strategy=None), broker(), StubStrategy(Signal.LONG))
    lab.on_candle("BTC", candle(1000), equity=10_000)
    assert lab.paper.sizes().get("BTC", 0) > 0
    lab.on_price("BTC", lab._stop["BTC"] - 1)
    assert lab.paper.sizes().get("BTC", 0) == 0
    assert "BTC" not in lab._stop


def test_trend_lab_take_profit_closes_position():
    lab = TrendLab(TrendLabConfig(coin="BTC", strategy=None), broker(), StubStrategy(Signal.LONG))
    lab.on_candle("BTC", candle(1000), equity=10_000)
    lab.on_price("BTC", lab._tp["BTC"] + 1)
    assert lab.paper.sizes().get("BTC", 0) == 0


def test_trend_lab_opposite_signal_flips_flat():
    strat = StubStrategy(Signal.LONG)
    lab = TrendLab(TrendLabConfig(coin="BTC", strategy=None), broker(), strat)
    lab.on_candle("BTC", candle(1000), equity=10_000)
    assert lab.paper.sizes().get("BTC", 0) > 0
    strat.set(Signal.FLAT)
    lab.on_candle("BTC", candle(2000), equity=10_000)  # neue Candle -> Flat schließt
    assert lab.paper.sizes().get("BTC", 0) == 0


def test_trend_lab_one_signal_per_candle():
    lab = TrendLab(TrendLabConfig(coin="BTC", strategy=None), broker(), StubStrategy(Signal.LONG))
    df = candle(1000)
    lab.on_candle("BTC", df, equity=10_000)
    trades_after_first = lab.paper.trades
    lab.on_candle("BTC", df, equity=10_000)  # gleiche Candle -> nichts Neues
    assert lab.paper.trades == trades_after_first


# ---------- FundingLab ----------

def test_funding_lab_shorts_when_longs_pay():
    lab = FundingLab(FundingLabConfig(coins=["BTC"], entry_apr=0.3), broker())
    # +50% p.a. Funding = Longs zahlen -> kassierende Seite ist SHORT
    lab.on_pulse([Pulse("BTC", 0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=1000)
    assert lab.paper.sizes().get("BTC", 0) < 0, "positives Funding -> Short kassiert"


def test_funding_lab_longs_when_shorts_pay():
    lab = FundingLab(FundingLabConfig(coins=["BTC"], entry_apr=0.3), broker())
    lab.on_pulse([Pulse("BTC", -0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=1000)
    assert lab.paper.sizes().get("BTC", 0) > 0, "negatives Funding -> Long kassiert"


def test_funding_lab_ignores_mild_funding():
    lab = FundingLab(FundingLabConfig(coins=["BTC"], entry_apr=0.3), broker())
    lab.on_pulse([Pulse("BTC", 0.10, 30_000)], {"BTC": 30_000}, equity=10_000, now=1000)
    assert lab.paper.sizes().get("BTC", 0) == 0, "10% < entry_apr -> kein Einstieg"


def test_funding_lab_exits_when_funding_normalizes():
    lab = FundingLab(FundingLabConfig(coins=["BTC"], entry_apr=0.3, exit_apr=0.1), broker())
    lab.on_pulse([Pulse("BTC", 0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=1000)
    assert lab.paper.sizes().get("BTC", 0) < 0
    # Funding fällt unter exit_apr -> raus
    lab.on_pulse([Pulse("BTC", 0.05, 30_000)], {"BTC": 30_000}, equity=10_000, now=2000)
    assert lab.paper.sizes().get("BTC", 0) == 0


def test_funding_lab_time_stop():
    cfg = FundingLabConfig(coins=["BTC"], entry_apr=0.3, max_hold_hours=1)
    lab = FundingLab(cfg, broker())
    lab.on_pulse([Pulse("BTC", 0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=0)
    assert lab.paper.sizes().get("BTC", 0) < 0
    # 2h später, Funding noch extrem, aber Zeitlimit erreicht -> raus
    lab.on_pulse([Pulse("BTC", 0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=7200)
    assert lab.paper.sizes().get("BTC", 0) == 0


def test_funding_lab_hard_stop():
    cfg = FundingLabConfig(coins=["BTC"], entry_apr=0.3, stop_frac=0.03)
    lab = FundingLab(cfg, broker())
    lab.on_pulse([Pulse("BTC", 0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=0)
    # Short bei 30k, Stop bei +3% = 30900; Preis schießt auf 31000 -> Stop
    lab.on_pulse([Pulse("BTC", 0.50, 31_000)], {"BTC": 31_000}, equity=10_000, now=100)
    assert lab.paper.sizes().get("BTC", 0) == 0


def test_funding_lab_credits_carry():
    """Bug-Fix: das eingesammelte Funding muss gutgeschrieben werden, sonst misst
    der Lab nur Kursrisiko. Short bei +50% APR, Preis konstant -> positiver PnL."""
    b = broker()
    lab = FundingLab(FundingLabConfig(coins=["BTC"], entry_apr=0.3, exit_apr=0.1), b)
    # Einstieg: +50% APR -> Short kassiert
    lab.on_pulse([Pulse("BTC", 0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=0)
    pnl_after_entry = b.realized_pnl
    # 10h später, Funding noch extrem, Preis unverändert -> nur Funding fließt
    lab.on_pulse([Pulse("BTC", 0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=10 * 3600)
    assert b.realized_pnl > pnl_after_entry, "Carry muss gutgeschrieben werden"
    # grobe Größenordnung: |size|*price*apr/(24*365)*10h, size~200/30000*... klein aber >0
    assert b.realized_pnl - pnl_after_entry > 0


def test_funding_lab_records_per_trade_returns():
    """Per-Trade-Returns für den Signifikanztest werden beim Exit erfasst."""
    b = broker()
    lab = FundingLab(FundingLabConfig(coins=["BTC"], entry_apr=0.3, exit_apr=0.1), b)
    lab.on_pulse([Pulse("BTC", 0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=0)
    assert lab.closed_returns == [], "noch offen -> kein Return"
    # Funding normalisiert -> Exit -> ein Return-Datensatz
    lab.on_pulse([Pulse("BTC", 0.05, 30_000)], {"BTC": 30_000}, equity=10_000, now=5 * 3600)
    assert len(lab.closed_returns) == 1
    assert isinstance(lab.closed_returns[0], float)


def test_funding_lab_carry_sign_correct_for_long():
    """Negatives Funding -> Long kassiert -> Gutschrift positiv."""
    b = broker()
    lab = FundingLab(FundingLabConfig(coins=["BTC"], entry_apr=0.3, exit_apr=0.1), b)
    lab.on_pulse([Pulse("BTC", -0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=0)
    before = b.realized_pnl
    lab.on_pulse([Pulse("BTC", -0.50, 30_000)], {"BTC": 30_000}, equity=10_000, now=10 * 3600)
    assert b.realized_pnl > before, "Long bei negativem Funding kassiert auch"


def test_funding_lab_respects_max_positions():
    cfg = FundingLabConfig(coins=["BTC", "ETH", "SOL"], entry_apr=0.3, max_positions=2)
    lab = FundingLab(cfg, broker())
    pulses = [Pulse("BTC", 0.50, 30_000), Pulse("ETH", 0.60, 2_000), Pulse("SOL", 0.70, 100)]
    prices = {"BTC": 30_000, "ETH": 2_000, "SOL": 100}
    lab.on_pulse(pulses, prices, equity=10_000, now=0)
    assert len(lab._entry) == 2, "Deckel auf max_positions"
    # die stärksten Funding-Coins zuerst (SOL 70%, ETH 60%)
    assert set(lab._entry) == {"SOL", "ETH"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
