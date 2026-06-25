"""Unit-Tests: Konvergenz-Engine, Day-Trader-LARP-Gate, Equity-Historie (ohne Netzwerk)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import AnalysisConfig, ConvergenceConfig, CopytradeConfig, RiskConfig
from bot.convergence import ConvergenceEngine, _conviction_from_ratio
from bot.copytrade.analyzer import TraderMetrics
from bot.copytrade.copier import compute_targets
from bot.copytrade.larp import LarpConfig, LarpFilter
from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot

RISK = RiskConfig(risk_per_trade=0.01, atr_stop_mult=2.0, take_profit_r=2.0,
                  max_leverage=4, max_daily_loss=0.05, slippage=0.005)
CT = CopytradeConfig(analysis=AnalysisConfig(), copy_ratio=0.6)


class StubEngine(ConvergenceEngine):
    """Konvergenz-Engine mit fest verdrahteter externer Stimme statt API-Calls."""

    def __init__(self, cfg, external: dict[str, tuple[float, int]]):
        super().__init__(cfg)
        self._fixed = external

    def _external(self, coin):
        return self._fixed.get(coin, (0.0, 0))


def snap(addr, equity, **positions):
    pos = {c: LeaderPosition(coin=c, size=s, entry=100.0, position_value=abs(s) * 100.0, leverage=2)
           for c, s in positions.items()}
    return LeaderSnapshot(address=addr, equity=equity, positions=pos)


# ---------- Konvergenz-Mathematik ----------

def test_conviction_from_ratio():
    assert _conviction_from_ratio(1.0) == 0.0           # ausgeglichen
    assert _conviction_from_ratio(2.0) > 0.5            # klar long
    assert _conviction_from_ratio(0.5) < -0.5           # klar short
    assert -1.0 <= _conviction_from_ratio(100) <= 1.0   # geclippt


def test_vote_agreement_boosts():
    eng = StubEngine(ConvergenceConfig(), {"BTC": (0.6, 3)})  # extern stark long
    v = eng.vote("BTC", our_sign=1.0)
    assert v.factor > 1.0 and v.factor <= 1.25
    assert v.sources == 3


def test_vote_disagreement_dampens():
    eng = StubEngine(ConvergenceConfig(), {"BTC": (-0.5, 2)})  # extern short, wir long
    v = eng.vote("BTC", our_sign=1.0)
    assert v.factor == 0.4


def test_vote_neutral_and_no_data():
    eng = StubEngine(ConvergenceConfig(), {"BTC": (0.05, 3), "ETH": (0.0, 0)})
    assert eng.vote("BTC", 1.0).factor == 1.0   # im Neutral-Band
    assert eng.vote("ETH", 1.0).factor == 1.0   # keine Daten
    assert eng.vote("ETH", 1.0).external is None


def test_vote_disabled_engine():
    cfg = ConvergenceConfig(enabled=False)
    eng = StubEngine(cfg, {"BTC": (0.9, 3)})
    assert eng.vote("BTC", 1.0).factor == 1.0


def test_total_source_failure_not_cached():
    """Bug-Fix: Fällt JEDE Quelle aus, darf das nicht als 'neutral' gecacht werden -
    sonst bleibt die Konvergenz für cache_seconds blind statt es neu zu versuchen."""
    class FlakySource:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        def conviction(self, coin, period, timeout=10):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Netz weg")
            return 0.8  # zweiter Versuch klappt

        def reset(self):
            pass

    eng = ConvergenceEngine(ConvergenceConfig(cache_seconds=300))
    src = FlakySource()
    eng.sources = [src]
    avg, n = eng._external("BTC")
    assert n == 0, "Total-Ausfall -> keine Daten"
    avg2, n2 = eng._external("BTC")  # sofort erneut: darf NICHT aus dem Cache kommen
    assert n2 == 1 and abs(avg2 - 0.8) < 1e-9, "muss neu versuchen, nicht 'neutral' cachen"
    assert src.calls == 2


# ---------- Konvergenz im Copier ----------

def test_targets_boosted_on_agreement_but_capped():
    # Leader 40% long BTC -> Basis 0.4*0.6*10000 = 2400; Boost 1.25 -> 3000 > Cap 2500
    eng = StubEngine(ConvergenceConfig(), {"BTC": (0.9, 3)})
    targets = compute_targets([snap("0xa", 1000, BTC=4)], {"0xa": 1.0}, 10000, CT, RISK, convergence=eng)
    assert abs(targets["BTC"] - 2500) < 1e-9, "Coin-Cap muss auch den Boost deckeln"


def test_targets_dampened_on_disagreement():
    eng = StubEngine(ConvergenceConfig(), {"BTC": (-0.6, 3)})
    targets = compute_targets([snap("0xa", 1000, BTC=4)], {"0xa": 1.0}, 10000, CT, RISK, convergence=eng)
    assert abs(targets["BTC"] - 2400 * 0.4) < 1e-9


def test_targets_short_agreement():
    # Leader short, extern auch short -> Boost in negativer Richtung
    eng = StubEngine(ConvergenceConfig(), {"ETH": (-0.6, 2)})
    targets = compute_targets([snap("0xa", 1000, ETH=-3)], {"0xa": 1.0}, 10000, CT, RISK, convergence=eng)
    assert targets["ETH"] < -1800  # Basis -1800, geboostet


def test_convergence_never_creates_position():
    # Kein Leader-Signal -> auch bei extrem bullishen Externen kein Target
    eng = StubEngine(ConvergenceConfig(), {"SOL": (0.95, 3)})
    targets = compute_targets([snap("0xa", 1000, BTC=2)], {"0xa": 1.0}, 10000, CT, RISK, convergence=eng)
    assert "SOL" not in targets


# ---------- Day-Trader-LARP-Gate ----------

def metrics_with_holding(minutes):
    m = TraderMetrics(address="0x1", account_value=50000, days=21)
    m.net_pnl = 5000
    m.round_trips = 60
    m.active_days = 15
    m.max_trade_share = 0.2
    m.profitable_week_share = 0.8
    m.max_drawdown = 0.1
    m.median_holding_minutes = minutes
    return m


def test_daytrader_gate_rejects_swing_trader():
    f = LarpFilter(LarpConfig(max_median_holding_minutes=720))
    v = f.check(metrics_with_holding(2880))  # mediane Haltedauer 2 Tage
    assert not v.passed
    assert any("Swing-Trader" in r for r in v.reasons)


def test_daytrader_gate_accepts_intraday():
    f = LarpFilter(LarpConfig(max_median_holding_minutes=720))
    assert f.check(metrics_with_holding(180)).passed  # 3h Hold = Day-Trader


def test_daytrader_gate_off_by_default():
    f = LarpFilter(LarpConfig())  # max = 0 -> Gate inaktiv
    assert f.check(metrics_with_holding(2880)).passed


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
