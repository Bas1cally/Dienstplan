"""Unit-Tests für den TWAP-Scout (laufende Whale-TWAPs, ohne Netzwerk)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import TwapConfig
from bot.twap import TwapScout, detect_active_twaps

NOW = 1_750_000_000.0
NOW_MS = int(NOW * 1000)


def sl(coin, sz, side, t_ms, px=100.0):
    return {"fill": {"coin": coin, "sz": str(sz), "side": side, "px": str(px), "time": t_ms}}


# ---------- reine Erkennung ----------

def test_detects_active_twap():
    slices = [sl("TRX", 1000, "B", NOW_MS - i * 60_000) for i in range(6)]
    out = detect_active_twaps(slices, NOW_MS, window_min=20, min_slices=4)
    assert len(out) == 1
    assert out[0]["coin"] == "TRX" and out[0]["side"] == "LONG" and out[0]["slices"] == 6


def test_too_few_slices_not_active():
    slices = [sl("TRX", 1000, "B", NOW_MS - i * 60_000) for i in range(3)]
    assert detect_active_twaps(slices, NOW_MS, window_min=20, min_slices=4) == []


def test_old_slices_excluded():
    # Slices liegen 2h zurück -> kein aktiver TWAP mehr
    slices = [sl("TRX", 1000, "B", NOW_MS - 7_200_000 - i * 60_000) for i in range(6)]
    assert detect_active_twaps(slices, NOW_MS, window_min=20, min_slices=4) == []


def test_sell_twap_direction():
    slices = [sl("BTC", 2, "A", NOW_MS - i * 60_000) for i in range(5)]
    out = detect_active_twaps(slices, NOW_MS, window_min=20, min_slices=4)
    assert out[0]["side"] == "SHORT"


def test_flat_slice_format_tolerated():
    # ohne {"fill": ..}-Verschachtelung
    slices = [{"coin": "ETH", "sz": "10", "side": "B", "px": "50", "time": NOW_MS - i * 30_000}
              for i in range(5)]
    out = detect_active_twaps(slices, NOW_MS, window_min=20, min_slices=4)
    assert out and out[0]["coin"] == "ETH"


# ---------- Scout-Verhalten ----------

class FakeMarket:
    def __init__(self, slices_by_addr, mids, errors=None):
        self.slices_by_addr = slices_by_addr
        self.mids = mids
        self.errors = errors or set()
        self.calls = []

    def all_mids(self):
        return {k: str(v) for k, v in self.mids.items()}

    def user_twap_slice_fills(self, user):
        self.calls.append(user)
        if user in self.errors:
            raise RuntimeError("kaputt")
        return self.slices_by_addr.get(user, [])


def scout(market, addresses, **ov):
    cfg = TwapConfig(throttle_s=0, **ov)
    s = TwapScout(market, cfg, address_source=lambda: addresses,
                  clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "twap.jsonl"
    return s


def test_scout_flags_running_twap_with_price():
    slices = [sl("TRX", 1000, "B", NOW_MS - i * 60_000, px=0.25) for i in range(6)]
    mkt = FakeMarket({"0xsun": slices}, {"TRX": 0.25})
    out = scout(mkt, ["0xsun"]).scan()
    assert len(out) == 1
    assert out[0]["coin"] == "TRX" and out[0]["address"] == "0xsun" and out[0]["price"] == 0.25


def test_scout_debounces_same_twap():
    slices = [sl("TRX", 1000, "B", NOW_MS - i * 60_000) for i in range(6)]
    mkt = FakeMarket({"0xsun": slices}, {"TRX": 100})
    s = scout(mkt, ["0xsun"])
    assert len(s.scan()) == 1
    assert s.scan() == [], "dieselbe laufende TWAP nicht erneut melden"


def test_scout_extra_addresses_always_checked():
    mkt = FakeMarket({"0xwhale": [sl("BTC", 1, "B", NOW_MS - i * 60_000) for i in range(5)]},
                     {"BTC": 60_000})
    s = scout(mkt, [], extra_addresses=["0xwhale"])
    out = s.scan()
    assert out and out[0]["address"] == "0xwhale"


def test_scout_check_budget():
    addrs = [f"0x{i:040x}" for i in range(20)]
    mkt = FakeMarket({}, {})
    scout(mkt, addrs, max_checks_per_scan=5).scan()
    assert len(mkt.calls) == 5


def test_scout_dedupes_candidate_addresses():
    mkt = FakeMarket({}, {})
    # gleiche Adresse aus extra + source -> nur einmal abfragen
    s = scout(mkt, ["0xabc"], extra_addresses=["0xabc"])
    s.scan()
    assert mkt.calls.count("0xabc") == 1


def test_scout_api_error_skips_without_crash():
    good = [sl("BTC", 1, "B", NOW_MS - i * 60_000) for i in range(5)]
    mkt = FakeMarket({"0xok": good}, {"BTC": 60_000}, errors={"0xbroken"})
    out = scout(mkt, ["0xbroken", "0xok"]).scan()
    assert [f["address"] for f in out] == ["0xok"]


def test_scout_notifier_journal():
    sent, recorded = [], []

    class N:
        def send(self, m):
            sent.append(m)

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    slices = [sl("TRX", 1000, "B", NOW_MS - i * 60_000) for i in range(6)]
    mkt = FakeMarket({"0xsun": slices}, {"TRX": 100})
    cfg = TwapConfig(throttle_s=0)
    s = TwapScout(mkt, cfg, address_source=lambda: ["0xsun"], notifier=N(), journal=J(),
                  clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "twap.jsonl"
    s.scan()
    assert sent and "TWAP" in sent[0]
    assert recorded and recorded[0][0] == "twap"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
