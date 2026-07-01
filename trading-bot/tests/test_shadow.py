"""Unit-Tests: Shadow-Varianten (A/B-Tuning auf denselben Daten)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import AnalysisConfig, CopytradeConfig
from bot.shadow import ShadowFleet, shadow_recommendations
from bot.validator import Verdict

CT = CopytradeConfig(analysis=AnalysisConfig(), copy_ratio=0.6, rebalance_threshold=0.02,
                     min_notional=10)
PRICES = {"BTC": 100_000.0, "ETH": 3_500.0}


class StubValidator:
    """Liefert feste Scores je Coin (BTC stark, ETH schwach). ETH zusätzlich
    RSI-überkauft (harter Veto), damit crash_only ETH blockt, BTC durchlässt."""

    def check(self, coin, is_long):
        score = {"BTC": 3, "ETH": 0}.get(coin, 0)
        reasons = ["VETO: RSI 80 überkauft (15m)"] if coin == "ETH" else []
        return Verdict(ok=score >= 2, score=score, max_score=3, reasons=reasons)

    def crash_only(self, coin, is_long):
        return not any(r.startswith("VETO: RSI") for r in self.check(coin, is_long).reasons)


def fleet(tmp, validator=None):
    return ShadowFleet(CT, 10_000, 0.00045, validator=validator, runtime=Path(tmp))


def test_no_validator_variant_follows_targets_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        f = fleet(tmp)
        f.tick({"BTC": 2_000.0, "ETH": -1_500.0}, PRICES)
        broker = f.variants[0].broker
        sizes = broker.sizes()
        assert abs(sizes["BTC"] * PRICES["BTC"] - 2_000) < 1e-6
        assert abs(sizes["ETH"] * PRICES["ETH"] + 1_500) < 1e-6


def test_loose_variant_filters_weak_scores():
    with tempfile.TemporaryDirectory() as tmp:
        f = fleet(tmp, validator=StubValidator())
        assert len(f.variants) == 3  # ohne_validator, validator_locker, validator_crash_only
        f.tick({"BTC": 2_000.0, "ETH": -1_500.0}, PRICES)
        loose = next(v for v in f.variants if v.name == "validator_locker").broker
        assert "BTC" in loose.sizes(), "Score 3 >= 1 muss durch"
        assert "ETH" not in loose.sizes(), "Score 0 < 1 muss geblockt bleiben"


def test_crash_only_variant_keeps_rsi_veto_drops_trend_block():
    with tempfile.TemporaryDirectory() as tmp:
        f = fleet(tmp, validator=StubValidator())
        f.tick({"BTC": 2_000.0, "ETH": -1_500.0}, PRICES)
        crash = next(v for v in f.variants if v.name == "validator_crash_only").broker
        # BTC: Score-Block würde beim vollen Validator NICHT feuern (Score 3), aber
        # selbst schwacher Score wäre bei crash_only egal - nur RSI zählt. BTC durch.
        assert "BTC" in crash.sizes(), "kein RSI-Veto -> crash_only lässt durch (Trend-Block ignoriert)"
        # ETH: harter RSI-Veto -> auch crash_only blockt (Versicherung bleibt)
        assert "ETH" not in crash.sizes(), "RSI-Veto muss auch bei crash_only blocken"


def test_variants_reconcile_their_own_book():
    with tempfile.TemporaryDirectory() as tmp:
        f = fleet(tmp)
        f.tick({"BTC": 2_000.0}, PRICES)
        f.tick({"BTC": 500.0}, PRICES)    # Leader reduziert -> Shadow folgt
        sizes = f.variants[0].broker.sizes()
        assert abs(sizes["BTC"] * PRICES["BTC"] - 500) < 1e-6
        f.tick({}, PRICES)                # Leader raus -> Shadow schließt
        assert f.variants[0].broker.sizes() == {}


def test_reductions_never_filtered():
    """Auch Varianten mit Filter dürfen Reduzierungen nie blocken."""
    with tempfile.TemporaryDirectory() as tmp:
        class BlockAll:
            def check(self, coin, is_long):
                return Verdict(ok=False, score=0, max_score=3)

        f = fleet(tmp, validator=BlockAll())
        loose = next(v for v in f.variants if v.name == "validator_locker")
        # Bestand künstlich aufbauen, dann Ziel auf 0 -> muss schließen dürfen
        loose.broker.execute("BTC", 0.02, 100_000)
        f.tick({}, PRICES)
        assert loose.broker.sizes() == {}


def test_flatten_hits_all_variants():
    with tempfile.TemporaryDirectory() as tmp:
        f = fleet(tmp)
        f.tick({"BTC": 2_000.0}, PRICES)
        f.flatten(PRICES)
        assert all(v.broker.sizes() == {} for v in f.variants)


def test_stats_shape_and_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        f = fleet(tmp)
        f.tick({"BTC": 2_000.0}, PRICES)
        stats = f.stats(PRICES)
        v = stats["ohne_validator"]
        assert set(v) == {"equity", "trades", "realized_pnl"} and v["trades"] == 1
        # Neustart: gleicher State
        f2 = fleet(tmp)
        assert f2.variants[0].broker.trades == 1


def test_shadow_recommendations():
    shadows = {"ohne_validator": {"equity": 10_300, "trades": 25, "realized_pnl": 300}}
    recs = shadow_recommendations(10_000, shadows)
    assert any("validation.enabled" in r for r in recs)
    # zu wenige Trades -> keine Empfehlung
    assert shadow_recommendations(10_000, {"ohne_validator": {"equity": 11_000, "trades": 3,
                                                              "realized_pnl": 0}}) == []
    # Variante hinten -> Filter behalten
    recs = shadow_recommendations(10_000, {"ohne_validator": {"equity": 9_700, "trades": 25,
                                                              "realized_pnl": -300}})
    assert any("behalten" in r for r in recs)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
