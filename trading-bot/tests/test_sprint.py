"""Unit-Tests: Sprint-Buch v2 - frisches Signal -> einsteigen -> halten -> +100$ TP.

Kernregression: KEIN Dauer-Rebalancing mehr (v1 produzierte 139 Trades).
"""

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
           for c, s in positions.items() if s != 0}
    return LeaderSnapshot(address=addr, equity=equity, positions=pos)


def leaders(*rows):
    return [{"address": a, "score": sc, "weight": 1.0} for a, sc in rows]


def book(tmp, **overrides):
    cfg = SprintConfig(**overrides)
    return SprintBook(cfg, FEE, runtime_dir=Path(tmp))


P = {"BTC": 100.0, "ETH": 100.0}
LED = leaders(("0xbest", 80))


def test_never_enters_running_trades():
    """Zyklusstart: Leader hält BEREITS Positionen -> nur Baseline, null Trades."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        for _ in range(5):
            b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert b.paper.sizes() == {}, "laufende Trades werden NIE kopiert"
        assert b.paper.trades == 0


def test_fresh_signal_enters_then_holds():
    """Frisches Signal (0 -> Position) -> Einstieg 10x; Wobbeln danach -> null Trades."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.tick(LED, [snap("0xbest", 50_000)], P)                 # Baseline: flach
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)        # frisch: BTC 0->500
        sizes = b.paper.sizes()
        assert "BTC" in sizes and sizes["BTC"] > 0
        assert 9_000 < sizes["BTC"] * 100.0 <= 10_000, "10x auf Zyklus-Equity"
        assert b.ride_leader == "0xbest"
        trades_after_entry = b.paper.trades
        # Kernregression: Leader wobbelt (±5%), Preis wobbelt -> KEINE weiteren Trades
        for s in (525, 475, 510, 490, 500, 530):
            b.tick(LED, [snap("0xbest", 50_000, BTC=s)], {"BTC": 100.5, "ETH": 100.0})
        assert b.paper.trades == trades_after_entry, "Halten heißt halten - kein Churn"


def _entered(tmp):
    """Helfer: Buch mit frisch eingestiegener BTC-Long-Position."""
    b = book(tmp)
    b.tick(LED, [snap("0xbest", 50_000)], P)
    b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
    assert b.paper.sizes()
    return b


def test_leader_full_exit_closes_partial_reduce_holds():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        t0 = b.paper.trades
        b.tick(LED, [snap("0xbest", 50_000, BTC=250)], P)   # halbiert -> halten
        assert b.paper.sizes() and b.paper.trades == t0
        b.tick(LED, [snap("0xbest", 50_000)], P)             # komplett raus -> mitgehen
        assert b.paper.sizes() == {}
        assert b.ride_leader == "", "nach Exit zurück auf FLACH"
        assert b.won == 0 and b.busted == 0, "kein Zyklus-Ende durch Leader-Exit"


def test_leader_flip_closes_and_reenters():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        b.tick(LED, [snap("0xbest", 50_000, BTC=-500)], P)   # Flip long -> short
        sizes = b.paper.sizes()
        # Flip schließt; Wiedereinstieg short via Frisch-Erkennung (gleicher oder Folge-Tick)
        if "BTC" not in sizes:
            b.tick(LED, [snap("0xbest", 50_000, BTC=-500)], P)
            sizes = b.paper.sizes()
        assert sizes.get("BTC", 0) < 0, "neue Richtung wird gefolgt"


def test_tp_accumulates_over_two_rides():
    """Ritt 1 endet +60 via Leader-Exit; Ritt 2 bringt den Zyklus über +100 -> TP."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)                                     # long ~10k Notional @100
        b.tick(LED, [snap("0xbest", 50_000)], {"BTC": 100.65, "ETH": 100.0})  # Exit ~+60
        assert b.paper.sizes() == {} and b.won == 0
        pnl_after_ride1 = b.paper.equity(P) - 1000.0
        assert 40 < pnl_after_ride1 < 100, f"Zwischen-PnL bleibt stehen ({pnl_after_ride1:.2f})"
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], P)     # frisches Signal Ritt 2
        assert "ETH" in b.paper.sizes()
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], {"BTC": 100.0, "ETH": 101.0})
        assert b.won == 1, "kumuliert >= +100 -> Take-Profit"
        assert b.banked > 100
        assert b.paper.sizes() == {} and abs(b.paper.equity({}) - 1000.0) < 1e-9


def test_bust_floor():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 90.0, "ETH": 100.0})
        assert b.busted == 1 and b.banked < -900
        assert b.paper.sizes() == {}


def test_risk_off_flattens_no_remirror():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P, risk_off=True)
        assert b.paper.sizes() == {} and b.won == 0 and b.busted == 0
        # Entwarnung: Leader hält BTC weiter -> das ist jetzt ein LAUFENDER Trade,
        # kein Wiedereinstieg. (Position lief während RISK_OFF weiter = nicht frisch
        # relativ zur letzten Baseline? Doch - Baseline hat BTC=500. Kein Einstieg.)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert b.paper.sizes() == {}, "kein Wiederspiegeln nach RISK_OFF"
        # Erst ein NEUES Signal (ETH 0->x) steigt wieder ein
        b.tick(LED, [snap("0xbest", 50_000, BTC=500, ETH=200)], P)
        assert "ETH" in b.paper.sizes() and "BTC" not in b.paper.sizes()


def test_no_mid_ride_leader_switch_but_rotation_while_flat():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)   # reitet 0xbest / BTC
        two = leaders(("0xbest", 80), ("0xnew", 95))
        snaps = [snap("0xbest", 50_000, BTC=500), snap("0xnew", 50_000, ETH=400)]
        b.tick(two, snaps, P)
        assert "BTC" in b.paper.sizes() and "ETH" not in b.paper.sizes(), \
            "im Ritt kein Leaderwechsel"
        # Leader-Exit -> flach; jetzt wird 0xnew beobachtet (Re-Baseline, sein
        # ETH-Bestand ist ein laufender Trade -> kein Einstieg)
        b.tick(two, [snap("0xbest", 50_000), snap("0xnew", 50_000, ETH=400)], P)
        assert b.paper.sizes() == {}
        b.tick(two, [snap("0xbest", 50_000), snap("0xnew", 50_000, ETH=400)], P)
        assert b.paper.sizes() == {}, "Bestand des neuen Beobachteten zählt nicht"
        # Frisches Signal des neuen Besten -> Einstieg
        b.tick(two, [snap("0xbest", 50_000), snap("0xnew", 50_000, ETH=400, BTC=300)], P)
        assert b.paper.sizes().get("BTC", 0) > 0


def test_ride_leader_rotated_out_closes():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        pnl_before = b.paper.equity(P) - 1000.0
        b.tick(leaders(("0xnew", 90)), [snap("0xnew", 50_000, ETH=400)], P)
        assert b.paper.sizes() == {}, "Ride-Leader weg -> blind -> schließen"
        assert b.won == 0 and b.busted == 0
        assert abs((b.paper.equity(P) - 1000.0) - pnl_before) < 10, "Zyklus-PnL bleibt"


def test_restart_keeps_ride_and_rebaselines():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        held = dict(b.paper.sizes())
        fresh = book(tmp)   # Neustart
        assert fresh.ride_leader == "0xbest", "ride_leader persistiert"
        assert fresh.paper.sizes() == held, "Positionen persistieren"
        t0 = fresh.paper.trades
        # Erster Tick nach Neustart: Baseline - Leader-Bestand erzeugt keine Fehl-Orders
        fresh.tick(LED, [snap("0xbest", 50_000, BTC=500, ETH=200)], P)
        assert fresh.paper.trades == t0, "Re-Baseline ohne Fehl-Einstieg (ETH lief schon)"
        # Exit-Folge funktioniert nach Neustart weiter
        fresh.tick(LED, [snap("0xbest", 50_000, ETH=200)], P)
        assert fresh.paper.sizes() == {}


def test_scans_all_leaders_not_just_best():
    """Der Beste pausiert, die Nr. 2 liefert das frische Signal -> Einstieg.
    (Vorher verharrte das Buch stur auf dem Besten - 'sonst passiert garnix'.)"""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        two = leaders(("0xbest", 90), ("0xsecond", 50))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)   # Baselines
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000, ETH=300)], P)
        assert b.paper.sizes().get("ETH", 0) > 0, "Signal der Nr. 2 wird genommen"
        assert b.ride_leader == "0xsecond"


def test_simultaneous_signals_highest_score_wins():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        two = leaders(("0xbest", 90), ("0xsecond", 50))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=400), snap("0xsecond", 50_000, ETH=300)], P)
        sizes = b.paper.sizes()
        assert "BTC" in sizes and "ETH" not in sizes, "höchster Score gewinnt"
        assert b.ride_leader == "0xbest"


def test_other_leader_ignored_while_riding_no_stale_entry_after():
    """Im Ritt zählen fremde Signale nicht - und nach dem Ritt gelten sie als
    LAUFENDE Trades (Baseline lief mit), nicht als frisch."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        two = leaders(("0xbest", 90), ("0xsecond", 50))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=400), snap("0xsecond", 50_000)], P)
        assert b.ride_leader == "0xbest"
        # Während des Ritts eröffnet die Nr. 2 ETH -> ignoriert, Baseline läuft mit
        b.tick(two, [snap("0xbest", 50_000, BTC=400), snap("0xsecond", 50_000, ETH=300)], P)
        assert "ETH" not in b.paper.sizes()
        # Ritt endet (Leader-Exit) -> flach; ETH der Nr. 2 läuft längst -> KEIN Einstieg
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000, ETH=300)], P)
        assert b.paper.sizes() == {}
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000, ETH=300)], P)
        assert b.paper.sizes() == {}, "laufender Trade der Nr. 2 bleibt tabu"


def test_v1_migration_resets_churned_book():
    with tempfile.TemporaryDirectory() as tmp:
        rt = Path(tmp)
        # v1-Hinterlassenschaft: verchurntes Buch + State ohne ride_leader
        b1 = book(tmp)
        b1.paper.execute("BTC", 1.0, 100.0)
        b1.paper.execute("BTC", -0.5, 100.0)
        (rt / "sprint_cycles.json").write_text(json.dumps({"banked": -50.0, "won": 1, "busted": 2}))
        b2 = SprintBook(SprintConfig(), FEE, runtime_dir=rt)
        assert b2.paper.trades == 0 and b2.paper.sizes() == {}, "Buch zurückgesetzt"
        assert b2.won == 1 and b2.busted == 2 and b2.banked == -50.0, "Bilanz bleibt"


def test_notifier_journal_and_stats():
    sent, recorded = [], []

    class N:
        def send(self, m):
            sent.append(m)

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        b = SprintBook(SprintConfig(), FEE, notifier=N(), journal=J(), runtime_dir=Path(tmp))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        assert b.stats(P)["state"] == "wartet auf frisches Signal"
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert any("Sprint-Einstieg" in m for m in sent)
        assert recorded and recorded[0][0] == "sprint_entry"
        st = b.stats(P)
        assert st["state"] == "hält" and st["held"] == ["BTC LONG"]
        b.tick(LED, [snap("0xbest", 50_000)], P)
        assert any("Sprint-Exit" in m for m in sent)
        assert any(k == "sprint_exit" for k, _ in recorded)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
