"""Unit-Tests: Trade-Validator (Bot 2) - Technik-Checks und Veto-Semantik."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from bot.config import ValidationConfig
from bot.copytrade.copier import RebalanceOrder
from bot.validator import TradeValidator, Verdict

CFG = ValidationConfig(cache_seconds=0)  # Cache aus, damit jeder Test frisch prüft


def make_candles(trend: str, n=150, start=100.0):
    """Synthetische OHLCV-Serie: up / down / range (realistisches Rauschen,
    damit der RSI nicht ins Extrem läuft und das harte Veto triggert)."""
    rng = np.random.default_rng(7)
    drift = {"up": 0.0012, "down": -0.0012, "range": 0.0}[trend]
    close = start * np.exp(np.cumsum(drift + rng.normal(0, 0.004, n)))
    o = np.roll(close, 1); o[0] = close[0]
    spread = np.abs(rng.normal(0, 0.002, n)) * close
    return pd.DataFrame({
        "time": np.arange(n) * 60_000, "open": o,
        "high": np.maximum(o, close) + spread, "low": np.minimum(o, close) - spread,
        "close": close, "volume": rng.uniform(10, 100, n),
    })


class FakeClient:
    """Liefert pro Intervall eine vorgegebene Candle-Serie."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    def candles(self, coin, interval, lookback):
        return self.frames[interval]


def validator(t15: str, t1h: str) -> TradeValidator:
    return TradeValidator(CFG, FakeClient({"15m": make_candles(t15), "1h": make_candles(t1h)}))


# ---------- Technik-Checks ----------

def test_long_approved_in_uptrend():
    v = validator("up", "up").check("BTC", is_long=True)
    assert v.ok and v.score == 3, v.summary()


def test_long_vetoed_in_downtrend():
    v = validator("down", "down").check("BTC", is_long=True)
    assert not v.ok and v.score == 0, v.summary()


def test_short_approved_in_downtrend():
    v = validator("down", "down").check("BTC", is_long=False)
    assert v.ok, v.summary()


def test_mixed_timeframes_need_majority():
    # 15m aufwärts, 1h abwärts: Long hat Trend(15m)+Momentum = 2/3 -> knapp OK
    v = validator("up", "down").check("BTC", is_long=True)
    assert v.score == 2 and v.ok, v.summary()
    # Short dagegen nur Trend(1h) = 1/3 -> Veto
    v = validator("up", "down").check("BTC", is_long=False)
    assert not v.ok, v.summary()


def test_rsi_extreme_hard_veto():
    # Steiler, pausenloser Anstieg treibt den RSI über 75 -> Long-Veto trotz Trend
    n = 150
    close = 100 * np.exp(np.cumsum(np.full(n, 0.01)))
    df = pd.DataFrame({"time": np.arange(n) * 60_000, "open": close, "high": close * 1.001,
                       "low": close * 0.999, "close": close, "volume": np.ones(n)})
    v = TradeValidator(CFG, FakeClient({"15m": df, "1h": df})).check("BTC", is_long=True)
    assert not v.ok
    assert any("VETO: RSI" in r for r in v.reasons), v.summary()


def test_validator_failure_rejects_conservatively():
    class BrokenClient:
        def candles(self, *a):
            raise ConnectionError("API down")

    v = TradeValidator(CFG, BrokenClient()).check("BTC", is_long=True)
    assert not v.ok
    assert any("konservativ" in r for r in v.reasons)


def test_disabled_validator_approves():
    cfg = ValidationConfig(enabled=False)
    v = TradeValidator(cfg, FakeClient({})).check("BTC", is_long=True)
    assert v.ok


# ---------- Veto-Semantik im Copier ----------

class StubValidator:
    def __init__(self, ok: bool):
        self._ok = ok
        self.checked: list = []

    def check(self, coin, is_long):
        self.checked.append((coin, is_long))
        return Verdict(ok=self._ok, reasons=["stub"])


def order(coin, current, target, price=100.0):
    return RebalanceOrder(coin=coin, delta_size=(target - current) / price,
                          target_notional=target, current_notional=current, price=price)


def make_copier(validator):
    """Minimaler CopyTrader nur für _validate_orders (ohne Netz/Config)."""
    from bot.copytrade.copier import CopyTrader

    ct = CopyTrader.__new__(CopyTrader)
    ct.validator = validator
    ct.journal = None
    return ct


def test_veto_blocks_only_increases():
    val = StubValidator(ok=False)
    ct = make_copier(val)
    orders = [
        order("BTC", current=0, target=2000),       # Neueröffnung -> geprüft, geblockt
        order("ETH", current=2000, target=3000),    # Aufstockung  -> geprüft, geblockt
        order("SOL", current=2000, target=500),     # Reduzierung  -> läuft IMMER durch
        order("DOGE", current=1500, target=0),      # Schließung   -> läuft IMMER durch
    ]
    out = ct._validate_orders(orders)
    assert [o.coin for o in out] == ["SOL", "DOGE"]
    assert {c for c, _ in val.checked} == {"BTC", "ETH"}, "Reduzierungen dürfen nie geprüft werden"


def test_approval_lets_increases_through():
    ct = make_copier(StubValidator(ok=True))
    out = ct._validate_orders([order("BTC", 0, 2000), order("ETH", 0, -1500)])
    assert len(out) == 2


def test_short_entry_validated_with_correct_direction():
    val = StubValidator(ok=True)
    make_copier(val)._validate_orders([order("BTC", 0, -2000)])
    assert val.checked == [("BTC", False)], "Short-Einstieg muss als Short geprüft werden"


def test_no_validator_passthrough():
    ct = make_copier(None)
    out = ct._validate_orders([order("BTC", 0, 2000)])
    assert len(out) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
