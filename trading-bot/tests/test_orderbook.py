"""Unit-Tests für den Orderbuch-Scout (Mikrostruktur, ohne Netzwerk)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import OrderBookConfig
from bot.orderbook import OrderBookScout, analyze_book

NOW = 1_750_000_000.0


def lvl(px, sz):
    return {"px": str(px), "sz": str(sz)}


def book(bids, asks):
    return [[lvl(p, s) for p, s in bids], [lvl(p, s) for p, s in asks]]


# ---------- reine Analyse ----------

def test_imbalance_bid_heavy():
    m = analyze_book(book([(99.9, 100), (99.8, 100)], [(100.1, 10)]), mid=100, band_pct=0.01, wall_ratio=5)
    assert m["imbalance"] > 0.5, "viel mehr Bids -> positive Imbalance"


def test_imbalance_ask_heavy():
    m = analyze_book(book([(99.9, 10)], [(100.1, 100), (100.2, 100)]), mid=100, band_pct=0.01, wall_ratio=5)
    assert m["imbalance"] < -0.5


def test_band_excludes_far_levels():
    # weit entfernte Riesenorder zählt nicht (außerhalb 0.5%)
    m = analyze_book(book([(99.95, 10)], [(100.05, 10), (105.0, 10_000)]),
                     mid=100, band_pct=0.005, wall_ratio=5)
    assert abs(m["imbalance"]) < 0.2, "Order bei +5% außerhalb des Bands -> ignoriert"


def test_wall_detection():
    # realistisches Buch: viele kleine Levels + eine Ask-Wall (>=5x Mittel)
    bids = [(99.9 - 0.01 * i, 5) for i in range(9)]
    asks = [(100.1, 100), (100.2, 5)]
    m = analyze_book(book(bids, asks), mid=100, band_pct=0.02, wall_ratio=5)
    assert m["has_wall"] and m["wall_side"] == "ask", m


def test_empty_book_returns_none():
    assert analyze_book([[], []], mid=100, band_pct=0.01, wall_ratio=5) is None
    assert analyze_book([], mid=100, band_pct=0.01, wall_ratio=5) is None


# ---------- Scout-Verhalten ----------

class FakeMarket:
    def __init__(self, mids, books, errors=None):
        self.mids = mids
        self.books = books          # coin -> levels
        self.errors = errors or set()
        self.calls = []

    def all_mids(self):
        self.calls.append(("mids", None))
        return {k: str(v) for k, v in self.mids.items()}

    def l2_snapshot(self, coin):
        self.calls.append(("l2", coin))
        if coin in self.errors:
            raise RuntimeError("kaputt")
        return {"coin": coin, "levels": self.books.get(coin, [[], []])}


def scout(market, **ov):
    cfg = OrderBookConfig(coins=list(market.mids), throttle_s=0,
                          imbalance_threshold=0.35, wall_ratio=5.0, **ov)
    s = OrderBookScout(market, cfg, clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "ob.jsonl"
    return s


def test_strong_imbalance_flagged_with_price():
    mkt = FakeMarket({"BTC": 100}, {"BTC": book([(99.9, 100), (99.8, 100)], [(100.1, 5)])})
    out = scout(mkt).scan()
    assert len(out) == 1
    assert out[0]["coin"] == "BTC" and out[0]["side"] == "LONG" and out[0]["price"] == 100


def test_balanced_book_not_flagged():
    mkt = FakeMarket({"BTC": 100}, {"BTC": book([(99.9, 50)], [(100.1, 50)])})
    assert scout(mkt).scan() == []


def test_debounce_same_state():
    mkt = FakeMarket({"BTC": 100}, {"BTC": book([(99.9, 100), (99.8, 100)], [(100.1, 5)])})
    s = scout(mkt)
    assert len(s.scan()) == 1
    assert s.scan() == [], "gleiche Imbalance-Richtung -> kein neuer Eintrag"


def test_reflag_on_sign_flip():
    mkt = FakeMarket({"BTC": 100}, {"BTC": book([(99.9, 100)], [(100.1, 5)])})
    s = scout(mkt)
    assert len(s.scan()) == 1                       # Bid-Übergewicht
    mkt.books["BTC"] = book([(99.9, 5)], [(100.1, 100)])  # dreht auf Ask-Übergewicht
    assert len(s.scan()) == 1, "Vorzeichenwechsel -> neuer Eintrag"


def test_l2_error_skips_coin_without_crash():
    mkt = FakeMarket(
        {"BTC": 100, "ETH": 50},
        {"ETH": book([(49.95, 100)], [(50.05, 5)])},
        errors={"BTC"},
    )
    out = scout(mkt).scan()
    assert [f["coin"] for f in out] == ["ETH"]


def test_mids_failure_silent():
    class Dead:
        def all_mids(self):
            raise RuntimeError("Netz weg")

    s = OrderBookScout(Dead(), OrderBookConfig(coins=["BTC"]), clock=lambda: NOW)
    assert s.scan() == []


def test_tick_respects_interval():
    mkt = FakeMarket({"BTC": 100}, {"BTC": book([(99.9, 50)], [(100.1, 50)])})
    cfg = OrderBookConfig(coins=["BTC"], poll_seconds=60)
    t = {"now": NOW}
    s = OrderBookScout(mkt, cfg, clock=lambda: t["now"], sleep=lambda _: None)
    s.tick()
    s.tick()
    assert mkt.calls.count(("mids", None)) == 1
    t["now"] += 61
    s.tick()
    assert mkt.calls.count(("mids", None)) == 2


class _N:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


def test_no_telegram_by_default():
    # Default notify=False: auch starke Imbalance loggt nur, pusht NICHT (kein Spam)
    n = _N()
    mkt = FakeMarket({"BTC": 100}, {"BTC": book([(99.9, 100), (99.8, 100)], [(100.1, 5)])})
    s = OrderBookScout(mkt, OrderBookConfig(coins=["BTC"], throttle_s=0, imbalance_threshold=0.35),
                       notifier=n, clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "ob.jsonl"
    out = s.scan()
    assert out, "Signal wird trotzdem erfasst (fürs Report-Follow-through)"
    assert n.sent == [], "ohne notify=True kein Telegram"


def test_telegram_only_when_enabled_and_strong():
    n = _N()
    mkt = FakeMarket({"BTC": 100}, {"BTC": book([(99.9, 100), (99.8, 100)], [(100.1, 5)])})
    s = OrderBookScout(mkt, OrderBookConfig(coins=["BTC"], throttle_s=0,
                                            imbalance_threshold=0.35, notify=True),
                       notifier=n, clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "ob.jsonl"
    s.scan()
    assert n.sent and "Imbalance" in n.sent[0], "mit notify=True + starker Imbalance: Push"


def test_wall_only_never_pushes_even_with_notify():
    # nur Wall, schwache Imbalance: nie Telegram, auch mit notify=True
    n = _N()
    bids = [(99.9 - 0.01 * i, 10) for i in range(10)]
    asks = [(100.1, 100), (100.2, 5)]
    mkt = FakeMarket({"BTC": 100}, {"BTC": book(bids, asks)})
    s = OrderBookScout(mkt, OrderBookConfig(coins=["BTC"], throttle_s=0,
                                            imbalance_threshold=0.9, notify=True),
                       notifier=n, clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "ob.jsonl"
    out = s.scan()
    assert out and out[0]["wall"], "Wall muss erfasst werden"
    assert n.sent == [], "schwache Imbalance -> kein Push, auch bei notify=True"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
