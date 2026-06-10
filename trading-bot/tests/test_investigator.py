"""Unit-Tests: Investigator - Watchlist-Diff und Markt-Puls (ohne Netzwerk)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.investigator import MarketPulse, WalletWatcher, market_pulse


class FakeInfo:
    """user_state-Antworten pro Adresse, pro poll() umschaltbar."""

    def __init__(self):
        self.states: dict[str, dict[str, float]] = {}

    def set(self, addr, **positions):
        self.states[addr.lower()] = positions

    def user_state(self, addr):
        positions = self.states.get(addr.lower(), {})
        return {
            "marginSummary": {"accountValue": "1000000"},
            "assetPositions": [{
                "position": {"coin": c, "szi": str(1 if v > 0 else -1),
                             "positionValue": str(abs(v))}
            } for c, v in positions.items()],
        }

    def meta_and_asset_ctxs(self):
        meta = {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "SOL"}]}
        ctxs = [
            {"funding": "0.0001", "openInterest": "10000", "markPx": "100000"},   # 87% p.a. long
            {"funding": "-0.00008", "openInterest": "50000", "markPx": "3500"},   # -70% p.a. short
            {"funding": "0.00001", "openInterest": "1000", "markPx": "150"},      # 8.8% neutral
        ]
        return meta, ctxs


def watcher(info, *addrs, min_change=25_000):
    return WalletWatcher(info, list(addrs), min_notional_change=min_change)


def test_first_snapshot_never_alerts():
    info = FakeInfo()
    info.set("0xMM", BTC=5_000_000)
    w = watcher(info, "0xMM")
    assert w.poll() == [], "Bestehende Positionen beim ersten Poll sind kein Event"


def test_open_close_flip_detected():
    info = FakeInfo()
    info.set("0xMM")
    w = watcher(info, "0xMM")
    w.poll()

    info.set("0xMM", BTC=2_000_000)             # neue Long-Position
    events = w.poll()
    assert len(events) == 1 and events[0].kind == "open"

    info.set("0xMM", BTC=-3_000_000)            # dreht auf short
    events = w.poll()
    assert events[0].kind == "flip"

    info.set("0xMM")                            # alles zu
    events = w.poll()
    assert events[0].kind == "close" and events[0].new_notional == 0


def test_small_changes_filtered():
    info = FakeInfo()
    info.set("0xMM", BTC=1_000_000)
    w = watcher(info, "0xMM")
    w.poll()
    info.set("0xMM", BTC=1_010_000)             # +10k < 25k Schwelle
    assert w.poll() == []
    info.set("0xMM", BTC=1_500_000)             # +490k
    events = w.poll()
    assert events[0].kind == "increase"


def test_unreachable_wallet_skipped():
    class Broken:
        def user_state(self, addr):
            raise ConnectionError("down")

    w = watcher(Broken(), "0xMM")
    assert w.poll() == []                        # kein Crash, einfach übersprungen


def test_market_pulse_crowding():
    pulses = market_pulse(FakeInfo())
    by_coin = {p.coin: p for p in pulses}
    assert by_coin["BTC"].crowded == "long"      # 87% p.a. Funding
    assert by_coin["ETH"].crowded == "short"
    assert by_coin["SOL"].crowded == ""
    assert abs(by_coin["BTC"].oi_usd - 10_000 * 100_000) < 1e-6
    assert abs(by_coin["BTC"].funding_apr - 0.0001 * 24 * 365) < 1e-9


def test_event_text_readable():
    info = FakeInfo()
    info.set("0xabcdef1234", BTC=0)
    w = watcher(info, "0xabcdef1234")
    w.poll()
    info.set("0xabcdef1234", BTC=2_000_000)
    text = w.poll()[0].text()
    assert "OPEN" in text and "BTC" in text and "LONG" in text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
