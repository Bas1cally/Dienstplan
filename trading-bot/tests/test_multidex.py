"""Unit-Tests: Multi-DEX-Unterstützung (Aktien/Gold/Öl auf Builder-DEXs)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import ConvergenceConfig
from bot.convergence import ConvergenceEngine
from bot.copytrade.copier import exempt_inventory
from bot.copytrade.tracker import LeaderTracker


class FakeMultiDexInfo:
    """user_state mit DEX-Parameter: Haupt-DEX Krypto, Builder-DEX Aktien/Gold."""

    def __init__(self):
        self.calls = []

    def user_state(self, addr, dex=""):
        self.calls.append(dex)
        if dex == "":
            return {
                "marginSummary": {"accountValue": "100000"},
                "assetPositions": [{"position": {
                    "coin": "BTC", "szi": "1.0", "entryPx": "100000",
                    "positionValue": "100000", "leverage": {"value": 3}}}],
            }
        if dex == "km":
            return {
                "marginSummary": {"accountValue": "50000"},
                "assetPositions": [
                    {"position": {"coin": "km:TSLA", "szi": "200", "entryPx": "400",
                                  "positionValue": "80000", "leverage": {"value": 5}}},
                    {"position": {"coin": "km:XAU", "szi": "-10", "entryPx": "2600",
                                  "positionValue": "26000", "leverage": {"value": 2}}},
                ],
            }
        raise ConnectionError(f"DEX {dex} down")


def test_tracker_merges_dexs():
    info = FakeMultiDexInfo()
    tracker = LeaderTracker(info, ["0xleader"], dexs=["", "km"])
    snap = tracker.snapshot("0xleader")
    # Equity summiert über DEXs, Positionen vereint
    assert snap.equity == 150_000
    assert set(snap.positions) == {"BTC", "km:TSLA", "km:XAU"}
    # Exposure relativ zur GESAMT-Equity, Vorzeichen korrekt
    assert abs(snap.exposure("km:TSLA") - 80_000 / 150_000) < 1e-9
    assert abs(snap.exposure("km:XAU") + 26_000 / 150_000) < 1e-9


def test_tracker_skips_broken_dex():
    info = FakeMultiDexInfo()
    tracker = LeaderTracker(info, ["0xleader"], dexs=["", "km", "kaputt"])
    snap = tracker.snapshot("0xleader")
    assert snap.equity == 150_000, "kaputter DEX wird übersprungen, Rest bleibt"
    assert len(snap.positions) == 3


def test_tracker_default_main_dex_only():
    info = FakeMultiDexInfo()
    snap = LeaderTracker(info, ["0xleader"]).snapshot("0xleader")
    assert set(snap.positions) == {"BTC"}
    assert snap.equity == 100_000


def test_convergence_neutral_for_builder_assets():
    class Boom(ConvergenceEngine):
        def _external(self, coin):
            raise AssertionError("Builder-Assets dürfen keine externen Calls auslösen")

    eng = Boom(ConvergenceConfig())
    for coin in ("km:TSLA", "km:XAU", "uoil:CL"):
        v = eng.vote(coin, 1.0)
        assert v.factor == 1.0 and v.sources == 0


def test_exempt_inventory_with_builder_coins():
    book = exempt_inventory({"km:TSLA": 200.0, "BTC": 1.0}, {"km:TSLA": 50.0})
    assert abs(book["km:TSLA"] - 150.0) < 1e-12 and book["BTC"] == 1.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
