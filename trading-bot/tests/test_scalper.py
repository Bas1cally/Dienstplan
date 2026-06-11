"""Unit-Tests: VolScalper - Stabilisierung, Einstieg, Exits, Copier-Ausnahme."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from bot.config import ScalpConfig
from bot.copytrade.copier import exempt_inventory
from bot.paper import PaperBroker
from bot.scalper import VolScalper

CFG = ScalpConfig(enabled=True, stabilize_minutes=3, confirm_candles=3,
                  entry_window_minutes=25, max_holding_minutes=30)


class FakeClient:
    """1m-Preisserie, minutenweise vorspulbar."""

    def __init__(self, closes):
        self.closes = np.array(closes, dtype=float)
        self.minute = len(closes) - 1
        self.info = self

    def price(self):
        return float(self.closes[self.minute])

    def all_mids(self):
        return {"BTC": str(self.price())}

    def candles(self, coin, interval, lookback):
        c = self.closes[: self.minute + 1][-lookback:]
        o = np.roll(c, 1); o[0] = c[0]
        return pd.DataFrame({"time": np.arange(len(c)) * 60_000, "open": o,
                             "high": np.maximum(o, c), "low": np.minimum(o, c),
                             "close": c, "volume": np.ones(len(c))})


class FakeShock:
    def __init__(self, t, move):
        self.last_event = {"time": t, "move": move}


def crash_series(stabilized=True):
    """60min flat @100k, Crash auf 96k, danach Stabilisierung (oder Weiterfall)."""
    flat = [100_000.0] * 60
    crash = [99_200, 98_400, 97_500, 96_500, 96_000]
    after = [96_300, 96_400, 96_500, 96_600] if stabilized else [95_700, 95_400, 95_000, 94_600]
    return flat + crash + after


def make_scalper(client, shock, clock_minute):
    broker = PaperBroker(10_000, 0.00045, path=Path(tempfile.mkdtemp()) / "p.json")
    s = VolScalper(CFG, client, shock, paper=broker, journal=None,
                   equity_fn=lambda: 10_000, clock=lambda: clock_minute[0] * 60.0)
    return s, broker


def test_enters_long_after_crash_stabilizes():
    clock = [69]                                  # 4 Min nach Schock (Minute 65)
    client = FakeClient(crash_series(stabilized=True))
    s, broker = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    assert s.position is not None, "stabilisierter Crash muss Long-Scalp öffnen"
    assert s.position.size > 0
    assert s.position.stop < 96_000, "Stop unter dem Crash-Tief"
    assert s.position.take_profit > s.position.entry
    assert broker.sizes()["BTC"] == s.position.size
    assert s.inventory() == {"BTC": s.position.size}


def test_no_entry_while_still_falling():
    clock = [69]
    client = FakeClient(crash_series(stabilized=False))   # Messer fällt weiter
    s, _ = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    assert s.position is None


def test_no_entry_before_stabilize_window():
    clock = [66]                                  # erst 1 Min nach Schock
    client = FakeClient(crash_series(stabilized=True))
    s, _ = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    assert s.position is None


def test_event_expires_after_window():
    clock = [65 + 30]                             # 30 Min > entry_window 25
    client = FakeClient(crash_series(stabilized=True))
    s, _ = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    assert s.position is None
    assert s._consumed_event == 65 * 60.0, "verfallenes Event muss abgehakt sein"


def test_one_scalp_per_event():
    clock = [69]
    client = FakeClient(crash_series(stabilized=True))
    s, _ = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    pos = s.position
    # Position manuell schließen und erneut ticken: gleiches Event darf nicht erneut feuern
    s.position = None
    s.tick()
    assert s.position is None and pos is not None


def test_take_profit_exit():
    clock = [69]
    series = crash_series(stabilized=True)
    client = FakeClient(series)
    s, broker = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    tp = s.position.take_profit
    client.closes = np.append(client.closes, tp * 1.001)   # Preis erreicht TP
    client.minute += 1
    clock[0] += 1
    s.tick()
    assert s.position is None
    assert broker.sizes() == {}
    assert broker.realized_pnl > 0, "TP-Exit muss Gewinn realisieren"


def test_stop_loss_exit():
    clock = [69]
    client = FakeClient(crash_series(stabilized=True))
    s, broker = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    stop = s.position.stop
    client.closes = np.append(client.closes, stop * 0.999)
    client.minute += 1
    clock[0] += 1
    s.tick()
    assert s.position is None
    assert broker.realized_pnl < 0, "Stop-Exit realisiert den begrenzten Verlust"


def test_time_stop_exit():
    clock = [69]
    client = FakeClient(crash_series(stabilized=True))
    s, broker = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    client.closes = np.append(client.closes, client.price())   # Preis bewegt sich nicht
    client.minute += 1
    clock[0] += CFG.max_holding_minutes + 1
    s.tick()
    assert s.position is None and broker.sizes() == {}


def test_risk_sizing_respects_caps():
    clock = [69]
    client = FakeClient(crash_series(stabilized=True))
    s, _ = make_scalper(client, FakeShock(t=65 * 60.0, move=-0.04), clock)
    s.tick()
    notional = abs(s.position.size) * s.position.entry
    assert notional <= 10_000 * CFG.max_notional_frac + 1e-6
    risk = abs(s.position.size) * abs(s.position.entry - s.position.stop)
    assert risk <= 10_000 * CFG.risk_per_scalp + 1e-6


# ---------- Copier-Ausnahme ----------

def test_exempt_inventory_math():
    current = {"BTC": 0.5, "ETH": -2.0}
    scalp = {"BTC": 0.2}
    book = exempt_inventory(current, scalp)
    assert abs(book["BTC"] - 0.3) < 1e-12, "Copy-Buch sieht nur seinen eigenen Anteil"
    assert book["ETH"] == -2.0
    # Scalp ist die einzige BTC-Position -> Copy-Buch hat dort nichts
    assert "BTC" not in exempt_inventory({"BTC": 0.2}, {"BTC": 0.2})
    # ohne Scalp unverändert
    assert exempt_inventory(current, {}) == current


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
