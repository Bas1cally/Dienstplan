"""Unit-Tests: Paper-Broker (Fills, PnL, Persistenz) und Trade-Journal."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.journal import Journal
from bot.paper import PaperBroker

FEE = 0.00045


def broker(tmp) -> PaperBroker:
    return PaperBroker(10000, FEE, path=Path(tmp) / "paper.json")


def test_open_and_close_long_with_profit():
    with tempfile.TemporaryDirectory() as tmp:
        b = broker(tmp)
        b.execute("BTC", 0.1, 100_000)          # long 0.1 @ 100k
        assert b.sizes() == {"BTC": 0.1}
        assert abs(b.equity({"BTC": 100_000}) - (10000 - 0.1 * 100_000 * FEE)) < 1e-6
        b.execute("BTC", -0.1, 110_000)         # close @ 110k -> +1000 brutto
        fees = (0.1 * 100_000 + 0.1 * 110_000) * FEE
        assert b.sizes() == {}
        assert abs(b.realized_pnl - (1000 - fees)) < 1e-6
        assert abs(b.equity({}) - (11000 - fees)) < 1e-6


def test_short_profit_and_unrealized():
    with tempfile.TemporaryDirectory() as tmp:
        b = broker(tmp)
        b.execute("ETH", -2.0, 3000)            # short 2 ETH
        assert abs(b.unrealized({"ETH": 2800}) - 400) < 1e-6   # Kurs fällt -> Gewinn
        assert abs(b.unrealized({"ETH": 3200}) + 400) < 1e-6


def test_average_entry_on_increase():
    with tempfile.TemporaryDirectory() as tmp:
        b = broker(tmp)
        b.execute("BTC", 0.1, 100_000)
        b.execute("BTC", 0.1, 110_000)
        assert abs(b.positions["BTC"]["entry"] - 105_000) < 1e-6


def test_partial_reduce_books_partial_pnl():
    with tempfile.TemporaryDirectory() as tmp:
        b = broker(tmp)
        b.execute("BTC", 0.2, 100_000)
        b.execute("BTC", -0.1, 120_000)          # halbe Position mit +2000 zu
        gross = 0.1 * 20_000
        assert abs(b.sizes()["BTC"] - 0.1) < 1e-12
        assert b.realized_pnl > gross - 50       # brutto minus Fees
        assert abs(b.positions["BTC"]["entry"] - 100_000) < 1e-6  # Entry bleibt


def test_flip_long_to_short():
    with tempfile.TemporaryDirectory() as tmp:
        b = broker(tmp)
        b.execute("BTC", 0.1, 100_000)
        b.execute("BTC", -0.3, 90_000)           # dreht auf short 0.2
        assert abs(b.sizes()["BTC"] + 0.2) < 1e-12
        assert abs(b.positions["BTC"]["entry"] - 90_000) < 1e-6  # Rest zum Fill-Preis
        # realisiert: 0.1 * (90k - 100k) = -1000 (minus Fees)
        assert b.realized_pnl < -1000


def test_flatten_closes_everything():
    with tempfile.TemporaryDirectory() as tmp:
        b = broker(tmp)
        b.execute("BTC", 0.1, 100_000)
        b.execute("ETH", -1.0, 3000)
        b.flatten({"BTC": 100_000, "ETH": 3000})
        assert b.sizes() == {}


def test_state_survives_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "paper.json"
        b1 = PaperBroker(10000, FEE, path=path)
        b1.execute("BTC", 0.1, 100_000)
        b1.execute("ETH", -1.0, 3000)
        # "Neustart": neue Instanz lädt denselben State
        b2 = PaperBroker(10000, FEE, path=path)
        assert b2.sizes() == b1.sizes()
        assert abs(b2.realized_pnl - b1.realized_pnl) < 1e-12
        assert b2.trades == 2


def test_reset_clears_state():
    with tempfile.TemporaryDirectory() as tmp:
        b = broker(tmp)
        b.execute("BTC", 0.1, 100_000)
        b.reset()
        assert b.sizes() == {} and b.trades == 0 and b.equity({}) == 10000
        assert not b.path.exists()


def test_journal_roundtrip_and_order():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(path=Path(tmp) / "trades.jsonl")
        assert j.tail() == []
        j.record("order", coin="BTC", side="BUY", size=0.1)
        j.record("veto", coin="ETH", reasons=["Trend gegen Long"])
        entries = j.tail(10)
        assert len(entries) == 2
        assert entries[0]["kind"] == "veto", "neueste zuerst"
        assert entries[1]["coin"] == "BTC"
        assert all("t" in e for e in entries)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
