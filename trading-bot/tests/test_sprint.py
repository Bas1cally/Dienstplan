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
    # Default für die generischen Mechanik-Tests: BTC NICHT ausschließen (das ist
    # ein eigenes Feature, unten separat getestet) - sonst bräche hier fast jeder
    # bestehende Test, weil er BTC als Test-Coin nutzt.
    overrides.setdefault("exclude_coins", [])
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


def test_partial_scaleout_triggers_exit():
    """Leader baut >=75% ab (500 -> 100) -> wir gehen mit (Scale-out = Ausstieg)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)   # Einstieg bei Leader-Größe 500
        b.tick(LED, [snap("0xbest", 50_000, BTC=120)], P)   # 76% abgebaut -> raus
        assert b.paper.sizes() == {}, "Scale-out >= partial_exit_frac schließt"


def test_small_reduction_still_holds():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        t0 = b.paper.trades
        b.tick(LED, [snap("0xbest", 50_000, BTC=300)], P)   # nur 40% ab -> halten
        assert b.paper.sizes() and b.paper.trades == t0


def test_losing_ride_strikes_leader_two_bans():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        led = leaders(("0xbad", 80))
        # Ritt 1: Einstieg long, Preis fällt, Leader exit -> Verlust -> Strike 1
        b.tick(led, [snap("0xbad", 50_000)], P)
        b.tick(led, [snap("0xbad", 50_000, BTC=500)], P)
        b.tick(led, [snap("0xbad", 50_000)], {"BTC": 99.0, "ETH": 100.0})  # exit im Minus
        assert b.strikes.get("0xbad") == 1 and "0xbad" not in b.banned
        # Ritt 2: erneut Verlust -> Strike 2 -> Ban
        b.tick(led, [snap("0xbad", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})
        b.tick(led, [snap("0xbad", 50_000)], {"BTC": 98.0, "ETH": 100.0})
        assert b.strikes.get("0xbad") == 2 and "0xbad" in b.banned


def test_banned_leader_skipped_in_scan():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.banned.add("0xbad")
        led = leaders(("0xbad", 99))
        b.tick(led, [snap("0xbad", 50_000)], P)
        b.tick(led, [snap("0xbad", 50_000, BTC=500)], P)   # frisches Signal, aber gesperrt
        assert b.paper.sizes() == {}, "gesperrter Leader wird nicht kopiert"


def test_winning_ride_heals_strike():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.strikes["0xok"] = 1
        led = leaders(("0xok", 80))
        b.tick(led, [snap("0xok", 50_000)], P)
        b.tick(led, [snap("0xok", 50_000, BTC=500)], P)
        b.tick(led, [snap("0xok", 50_000)], {"BTC": 101.0, "ETH": 100.0})  # Gewinn-Exit
        assert b.strikes.get("0xok") == 0, "profitabler Ritt heilt einen Strike"


def test_manual_close_no_strike():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        n = b.close({"BTC": 99.0, "ETH": 100.0})   # im Minus, aber manuell
        assert n == 1 and b.paper.sizes() == {}
        assert b.strikes == {}, "manueller Ausstieg strikt niemanden"
        # v3: Zyklus = Ritt - manueller Close verbucht SOFORT (Nutzer-Anforderung:
        # "ich hab den btc Trade geschlossen heißt auf der Bank liegen X$").
        assert b.busted == 1 and b.won == 0, "Verlust wird trotzdem sofort verbucht"
        assert b.banked < 0
        assert abs(b.paper.equity({}) - 1000.0) < 1e-6, "Konto sofort auf 1000 zurück"


def test_strikes_survive_restart():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.strikes["0xbad"] = 1
        b.banned.add("0xzzz")
        b._save_state()
        fresh = book(tmp)
        assert fresh.strikes.get("0xbad") == 1 and "0xzzz" in fresh.banned


def test_leader_full_exit_closes_partial_reduce_holds():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        t0 = b.paper.trades
        b.tick(LED, [snap("0xbest", 50_000, BTC=250)], P)   # halbiert -> halten
        assert b.paper.sizes() and b.paper.trades == t0
        b.tick(LED, [snap("0xbest", 50_000)], P)             # komplett raus -> mitgehen
        assert b.paper.sizes() == {}
        assert b.ride_leader == "", "nach Exit zurück auf FLACH"
        # v3: Zyklus = Ritt - Leader-Exit beendet und verbucht SOFORT (hier ein
        # Fee-only-Verlust, da Preis unverändert bei P blieb).
        assert b.busted == 1 and b.won == 0, "Leader-Exit beendet den Zyklus sofort"
        assert b.banked < 0 and abs(b.paper.equity(P) - 1000.0) < 1e-6, \
            "sofort verbucht und auf 1000 zurückgesetzt"


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


def test_each_ride_is_its_own_cycle_banked_immediately():
    """v3-Kernverhalten (Nutzer-Spec): 'Zyklus = 1 Trade, sei es geschlossen oder
    nicht'. Ritt 1 endet per Leader-Exit mit +60 -> SOFORT gebankt, Konto SOFORT
    zurück auf 1000. Ritt 2 (unabhängiger, neuer Zyklus) trifft das +100-Ziel ->
    wieder sofort gebankt. banked = Summe BEIDER separater Zyklen."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)                                     # Zyklus 1: long ~10k @100
        b.tick(LED, [snap("0xbest", 50_000)], {"BTC": 100.65, "ETH": 100.0})  # Exit ~+60
        assert b.paper.sizes() == {}
        assert b.won == 1 and b.busted == 0, "positiver Ritt zählt als gewonnen"
        banked_after_cycle1 = b.banked
        assert 40 < banked_after_cycle1 < 100, f"Ritt-1-PnL sofort gebankt ({banked_after_cycle1:.2f})"
        assert abs(b.paper.equity(P) - 1000.0) < 1e-6, "Konto sofort zurück auf 1000"
        assert b.stats(P)["cycle"] == 2, "Zyklus-Zähler ist schon beim zweiten"

        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], P)     # Zyklus 2: frisches Signal
        assert "ETH" in b.paper.sizes()
        s_fresh = b.stats(P)
        assert -20 < s_fresh["cycle_pnl"] < 0, "neuer Zyklus startet bei ~0, NICHT bei +60"
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], {"BTC": 100.0, "ETH": 110.0})
        assert b.won == 2, "Zyklus 2 trifft eigenständig das +100-Ziel"
        assert b.banked > banked_after_cycle1 + 90, "beide Zyklen zusammen gebankt"
        assert b.paper.sizes() == {} and abs(b.paper.equity({}) - 1000.0) < 1e-6


def test_new_cycle_pnl_resets_regardless_of_previous_outcome():
    """cycle_pnl startet nach JEDEM Ritt-Ende wieder nahe 0 - unabhängig davon,
    ob der vorherige Zyklus gewann oder verlor (kein Zwischenstand-Mitschleppen)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        b.tick(LED, [snap("0xbest", 50_000)], {"BTC": 100.65, "ETH": 100.0})  # Gewinn-Exit
        assert b.won == 1
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], P)     # Zyklus 2 startet
        s = b.stats(P)
        assert -20 < s["cycle_pnl"] < 20, "Zyklus 2 kennt Zyklus 1s Gewinn nicht mehr"


def test_stats_immune_to_concurrent_close_race():
    """Regression: der Nutzer sah state='hält' bei leerer Positionsliste, weil
    stats() den Bestand frueher ZWEIMAL abfragte (sizes() fuer state/held,
    position_rows() fuer positions) - der Hintergrund-Loop (anderer Thread)
    konnte den Ritt exakt dazwischen schliessen. Simuliert hier ohne echtes
    Threading: sizes() liefert bewusst einen VERALTETEN Bestand (noch offen),
    position_rows() den AKTUELLEN (schon zu) - stats() darf sich nur noch auf
    EINE Quelle stuetzen, nicht auf sizes()."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)  # BTC-Position offen
        b.close(P)         # wirklich geschlossen
        # sizes() luegt jetzt bewusst "noch offen" - genau das haette die Race
        # gezeigt, wenn stats() sich weiterhin darauf stuetzen wuerde.
        b.paper.sizes = lambda: {"BTC": 1.0}
        s = b.stats(P)
        assert s["state"] == "wartet auf frisches Signal" and s["held"] == [] \
            and s["positions"] == [], "stats() darf sich nicht mehr auf sizes() stützen"


def test_stats_survives_empty_prices_right_after_restart():
    """Regression: direkt nach einem Neustart (/update) sind self.copier.last_prices
    noch leer, bevor der erste Tick frische Preise holt - stats(prices={}) darf
    eine ECHTE offene Position dann nicht als 'wartet auf frisches Signal'/leer
    zeigen (equity()/position_rows() degradieren selbst schon sauber auf
    Entry-Preis bzw. 0 PnL - kein Grund, das in stats() nochmal zu erzwingen)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)  # BTC-Position offen, echte Preise vorhanden
        s = b.stats({})    # wie direkt nach Neustart: keine Preise verfügbar
        assert s["state"] == "hält", "echte offene Position darf nicht verschwinden"
        assert s["held"] == ["BTC LONG"]
        assert len(s["positions"]) == 1 and s["positions"][0]["coin"] == "BTC"
        assert s["equity"] == 1000.0 or s["equity"] > 0, "kein Crash, sinnvoller Fallback"


def test_stats_exposes_position_details():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)   # long BTC ~100 Einstieg
        s = b.stats({"BTC": 105.0, "ETH": 100.0})
        assert len(s["positions"]) == 1
        p = s["positions"][0]
        assert p["coin"] == "BTC" and p["entry"] == 100.0 and p["unrealized_pnl"] > 0


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
        assert b.paper.sizes() == {}
        # RISK_OFF beendet den Ritt sofort als eigenen Zyklus (v3), aber ohne Strike
        # fuers Leader-Konto (RISK_OFF ist nicht die Schuld des Leaders).
        assert b.busted == 1 and b.won == 0
        assert b.strikes == {}, "RISK_OFF ist strike-befreit"
        assert abs(b.paper.equity(P) - 1000.0) < 1e-6, "Zyklus-Equity resettet"
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
        b.tick(leaders(("0xnew", 90)), [snap("0xnew", 50_000, ETH=400)], P)
        assert b.paper.sizes() == {}, "Ride-Leader weg -> blind -> schließen"
        # v3: auch Leader-Rotation beendet den Ritt sofort als eigenen Zyklus
        assert b.won == 1 or b.busted == 1, "Ritt wurde als Zyklus verbucht"
        assert abs(b.paper.equity(P) - 1000.0) < 1e-6, "Zyklus-Equity resettet"


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


def test_btc_excluded_by_default():
    """BTC = Beta statt Leader-Alpha, plus Fee-Falle bei fixem 10%-Ziel -> raus."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = SprintConfig()  # KEIN exclude_coins-Override -> Default gilt
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp))
        b.tick(LED, [snap("0xbest", 50_000)], P)                  # Baseline
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)         # frisches BTC-Signal
        assert b.paper.sizes() == {}, "BTC-Signal wird ignoriert"
        assert b.ride_leader == "", "kein Ritt ausgelöst"


def test_non_excluded_coin_still_enters_when_btc_and_alt_open_together():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = SprintConfig()
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        # Leader eröffnet BEIDES gleichzeitig -> BTC raus, ETH bleibt
        b.tick(LED, [snap("0xbest", 50_000, BTC=500, ETH=300)], P)
        sizes = b.paper.sizes()
        assert "BTC" not in sizes and "ETH" in sizes


def test_exclude_coins_configurable():
    """Leere Liste erlaubt BTC wieder - keine hartkodierte Sonderregel."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = SprintConfig(exclude_coins=[])
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert "BTC" in b.paper.sizes()


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
        b = SprintBook(SprintConfig(exclude_coins=[]), FEE, notifier=N(), journal=J(),
                       runtime_dir=Path(tmp))
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
