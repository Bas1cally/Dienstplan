"""Unit-Tests: Sprint-Buch (1000$ x10 auf den besten Leader, Ziel +100$/Zyklus)."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import SprintConfig
from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot
from bot.sprint import SprintBook

FEE = 0.00045


def snap(addr, equity, **positions):
    pos = {c: LeaderPosition(coin=c, size=s, entry=100.0, position_value=abs(s) * 100.0, leverage=2)
           for c, s in positions.items()}
    return LeaderSnapshot(address=addr, equity=equity, positions=pos)


def leaders(*rows):
    return [{"address": a, "score": sc, "weight": 1.0} for a, sc in rows]


def book(tmp, **overrides):
    cfg = SprintConfig(**overrides)
    return SprintBook(cfg, FEE, runtime_dir=Path(tmp))


def test_targets_best_leader_at_10x():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        # Leader voll long BTC (Exposure 1.0) -> Sprint-Ziel = 10x Equity
        b.tick(leaders(("0xbest", 80), ("0xother", 50)),
               [snap("0xbest", 50_000, BTC=500), snap("0xother", 50_000, ETH=100)],
               {"BTC": 100.0, "ETH": 100.0})
        sizes = b.paper.sizes()
        assert "ETH" not in sizes, "nur der BESTE Leader wird gespiegelt"
        # ~10.000$ Notional BTC (10x auf 1000$), leicht unter wegen Fee-Abzug
        assert 9_000 < sizes["BTC"] * 100.0 <= 10_000


def test_take_profit_banks_and_resets():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        led = leaders(("0xbest", 80))
        snaps = [snap("0xbest", 50_000, BTC=500)]
        b.tick(led, snaps, {"BTC": 100.0})
        assert b.paper.sizes(), "Position offen"
        # +2% auf 10x = +200$ unrealisiert -> Ziel (+100$) erreicht
        b.tick(led, snaps, {"BTC": 102.0})
        assert b.won == 1 and b.busted == 0
        assert b.banked > 100, "Gewinn muss gebucht sein"
        assert b.paper.sizes() == {}, "Konto zurückgesetzt"
        assert abs(b.paper.equity({}) - 1000.0) < 1e-9, "frischer Zyklus startet bei 1000"


def test_bust_at_liquidation_floor():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        led = leaders(("0xbest", 80))
        snaps = [snap("0xbest", 50_000, BTC=500)]
        b.tick(led, snaps, {"BTC": 100.0})
        # -9.6% auf 10x = -960$ -> Equity ~35$ < 5% von 1000 -> geplatzt
        b.tick(led, snaps, {"BTC": 90.4})
        assert b.busted == 1 and b.won == 0
        assert b.banked < -900
        assert b.paper.sizes() == {}, "Konto zurückgesetzt"


def test_follows_rotation_to_new_best():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.tick(leaders(("0xa", 80), ("0xb", 50)),
               [snap("0xa", 50_000, BTC=500), snap("0xb", 50_000, ETH=100)],
               {"BTC": 100.0, "ETH": 100.0})
        assert "BTC" in b.paper.sizes()
        # Rotation: 0xb ist jetzt der Beste -> Buch baut auf dessen Positionen um
        b.tick(leaders(("0xa", 40), ("0xb", 70)),
               [snap("0xa", 50_000, BTC=500), snap("0xb", 50_000, ETH=100)],
               {"BTC": 100.0, "ETH": 100.0})
        sizes = b.paper.sizes()
        assert "BTC" not in sizes and "ETH" in sizes


def test_risk_off_flattens_without_ending_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        led = leaders(("0xbest", 80))
        snaps = [snap("0xbest", 50_000, BTC=500)]
        b.tick(led, snaps, {"BTC": 100.0})
        b.tick(led, snaps, {"BTC": 100.0}, risk_off=True)
        assert b.paper.sizes() == {}, "RISK_OFF stellt glatt"
        assert b.won == 0 and b.busted == 0, "kein Zyklus-Ende durch RISK_OFF"


def test_cycle_stats_survive_restart():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        led = leaders(("0xbest", 80))
        snaps = [snap("0xbest", 50_000, BTC=500)]
        b.tick(led, snaps, {"BTC": 100.0})
        b.tick(led, snaps, {"BTC": 102.0})   # TP -> won=1
        fresh = book(tmp)                     # Neustart
        assert fresh.won == 1
        assert fresh.banked > 100
        st = fresh.stats({})
        assert st["cycle"] == 2 and st["won"] == 1


def test_notifier_and_journal_on_cycle_end():
    sent, recorded = [], []

    class N:
        def send(self, m):
            sent.append(m)

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        cfg = SprintConfig()
        b = SprintBook(cfg, FEE, notifier=N(), journal=J(), runtime_dir=Path(tmp))
        led = leaders(("0xbest", 80))
        snaps = [snap("0xbest", 50_000, BTC=500)]
        b.tick(led, snaps, {"BTC": 100.0})
        b.tick(led, snaps, {"BTC": 102.0})
        assert sent and "Sprint-Zyklus 1" in sent[0] and "Ziel erreicht" in sent[0]
        assert recorded and recorded[0][0] == "sprint_tp"
        assert recorded[0][1]["pnl"] > 100


def test_missing_best_snapshot_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.tick(leaders(("0xbest", 80)), [snap("0xother", 50_000, BTC=500)], {"BTC": 100.0})
        assert b.paper.sizes() == {}, "ohne Snapshot des Besten passiert nichts"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
