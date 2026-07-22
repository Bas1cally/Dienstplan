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
from bot.sprint import CONFIDENCE_PER_WIN, STAR_THRESHOLD, SprintBook

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
    # Bestätigungsfenster standardmäßig AUS (altes Sofort-Verhalten) - sonst
    # bräche praktisch jeder bestehende Mechanik-Test, der einen Einstieg
    # innerhalb EINES tick()-Aufrufs erwartet. Eigene Tests unten überschreiben
    # confirm_delay_s explizit, um genau dieses Feature zu prüfen.
    overrides.setdefault("confirm_delay_s", 0)
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


def test_flat_leverage_ignores_leader_allocation_fraction():
    """Nutzer-Fund: die Zahlen bewegten sich schleppend, weil das Notional die
    ANTEILIGE Leader-Allokation spiegelte (Leader 10% in ETH -> nur 1x statt
    10x). Jetzt: nur die RICHTUNG des Leaders zählt, immer volle 10x."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)   # equity 1000, leverage 10
        # Leader hält nur 10% seines Buchs in ETH (exposure 0.1): 50*100/50_000
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, ETH=50)], P)
        notional = abs(b.paper.sizes()["ETH"]) * 100.0
        assert 9_900 < notional <= 10_000, \
            f"volle 10x (10.000$) trotz nur 10% Leader-Allokation, war {notional:.0f}"


def test_flat_leverage_follows_leader_short_direction():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, ETH=-50)], P)   # Leader SHORT, klein allokiert
        assert b.paper.sizes()["ETH"] < 0, "Richtung short übernommen"
        assert abs(b.paper.sizes()["ETH"]) * 100.0 > 9_900, "volle 10x"


def test_leverage_override_scales_notional_for_that_coin():
    """Nutzer (17.07.): BTC/ETH liquider, vertragen mehr Hebel als der Rest -
    coin-abhängiger Override statt fixem Basis-Hebel für alle Coins."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, leverage=10, leverage_overrides={"BTC": 20})
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        notional = abs(b.paper.sizes()["BTC"]) * 100.0
        assert 19_800 < notional <= 20_000, \
            f"BTC-Override x20 (20.000$) statt Basis x10, war {notional:.0f}"


def test_leverage_without_override_uses_base():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, leverage=10, leverage_overrides={"BTC": 20})
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, ETH=50)], P)   # ETH hat hier KEINEN Override
        notional = abs(b.paper.sizes()["ETH"]) * 100.0
        assert 9_900 < notional <= 10_000, \
            f"ETH ohne Override bleibt bei Basis x10, war {notional:.0f}"


def test_max_leverage_fn_caps_notional_below_configured_leverage():
    """Live-Fund (Nutzer 17.07.): Hyperliquid bietet auf manchen Coins (z.B.
    Meme-Perps wie PENGU) gar nicht den konfigurierten Hebel an - der Bot
    eröffnete den Trade trotzdem mit dem vollen konfigurierten Hebel. Paper-
    Zahlen müssen das ECHTE Exchange-Limit respektieren, sonst wären sie beim
    Live-Gang gar nicht 1:1 übernehmbar."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0, leverage=10)
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp),
                       max_leverage_fn=lambda coin: {"BTC": 3}.get(coin))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        notional = abs(b.paper.sizes()["BTC"]) * 100.0
        assert 2_900 < notional <= 3_000, \
            f"HL erlaubt hier nur x3 auf BTC, Bot sollte NICHT x10 fahren, war {notional:.0f}"


def test_max_leverage_fn_none_for_coin_leaves_configured_leverage():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0, leverage=10)
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp),
                       max_leverage_fn=lambda coin: None)  # kein Live-Datenpunkt
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        notional = abs(b.paper.sizes()["BTC"]) * 100.0
        assert 9_900 < notional <= 10_000, \
            "ohne Live-Datenpunkt bleibt der konfigurierte Hebel maßgeblich"


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


def test_manual_close_negative_now_strikes():
    """Nutzer-Entscheidung (17.07.): ein Ritt, den man vorzeitig per /quest
    close abbricht, ist meist genau DESHALB manuell, weil er schon erkennbar
    schlecht läuft ('das ist ganz klar ein Gambler') - soll wie jeder andere
    Verlust-Ritt einen Strike geben, nicht mehr exempt sein."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        n = b.close({"BTC": 99.0, "ETH": 100.0})   # im Minus, manuell
        assert n == 1 and b.paper.sizes() == {}
        assert b.strikes.get("0xbest") == 1, "manueller Verlust-Ausstieg striked jetzt"
        # v3: Zyklus = Ritt - manueller Close verbucht SOFORT (Nutzer-Anforderung:
        # "ich hab den btc Trade geschlossen heißt auf der Bank liegen X$").
        assert b.busted == 1 and b.won == 0, "Verlust wird trotzdem sofort verbucht"
        assert b.banked < 0
        assert abs(b.paper.equity({}) - 1000.0) < 1e-6, "Konto sofort auf 1000 zurück"


def test_manual_close_profitable_still_heals_no_strike():
    """Symmetrisch zu oben: ein GEWINN-Manual-Close bleibt wie jeder andere
    Gewinn-Ritt strike-heilend, striked also weiterhin nicht."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        b.strikes["0xbest"] = 1
        n = b.close({"BTC": 101.5, "ETH": 100.0})   # im Plus, manuell
        assert n == 1
        assert b.strikes.get("0xbest") == 0, "profitabler manueller Exit heilt weiter"
        assert b.won == 1 and b.busted == 0


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


def test_stats_hides_stale_leader_when_flat():
    """Nutzer-Befund: /sprint zeigte 'wartet auf frisches Signal' UND trotzdem
    einen Leader an - self.ride_leader wird separat von der positions-
    Momentaufnahme gelesen, kann also (Hintergrund-Loop, anderer Thread) kurz
    hinter 'schon geschlossen' zurückbleiben. leader/leader_is_star müssen wie
    held/state aus DERSELBEN positions-Abfrage abgeleitet werden."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.ride_leader = "0xghost"   # simuliert: schon geschlossen, aber noch nicht geräumt
        b.confidence["0xghost"] = STAR_THRESHOLD
        s = b.stats(P)
        assert s["state"] == "wartet auf frisches Signal"
        assert s["leader"] == "", "kein Leader, solange wir nicht wirklich reiten"
        assert s["leader_is_star"] is False


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


def test_stats_watch_shows_holding_vs_flat_pool_wallets():
    """Nutzer-Fund 18.07. ('seit gestern Abend keine Signale, egal wie der
    Pool aussieht'): ohne Sichtbarkeit, ob Pool-Wallets gerade Positionen
    HALTEN (Baseline != leer - nur Close+Reopen/Flip/Aufstocken triggert
    noch) oder FLACH sind (nur ein frisches 0->Position-Signal triggert),
    lässt sich 'Bug oder erwartete Ruhe' nicht unterscheiden."""
    with tempfile.TemporaryDirectory() as tmp:
        two = leaders(("0xholder", 80), ("0xflat", 60))
        b = book(tmp)
        b.tick(two, [snap("0xholder", 50_000, BTC=500), snap("0xflat", 50_000)], P)
        s = b.stats(P)["watch"]
        assert s["tracked"] == 2
        assert s["holding_now"] == 1 and s["flat_now"] == 1
        assert s["sample"] == [{"addr": "0xholder"[:10], "coins": ["BTC"]}]


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


# ---------- MESS-MODUS: parallele Ritte (1 Signal = 1 Ritt = 1k) ----------

def test_parallel_two_leaders_two_rides_same_tick():
    """Mess-Woche: zwei Leader feuern gleichzeitig -> BEIDE Ritte laufen
    parallel (vorher blockte die eine Position alles andere)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=True)
        two = leaders(("0xbest", 90), ("0xsecond", 70))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)], P)
        sizes = b.paper.sizes()
        assert "BTC" in sizes and "ETH" in sizes, "beide Signale werden geritten"
        assert b.ride_leaders == {"BTC": "0xbest", "ETH": "0xsecond"}


def test_parallel_ride_settles_independently_with_own_strike():
    """Ein Ritt endet (Leader-Exit, Verlust) -> NUR dieser wird als Zyklus
    verbucht und NUR sein Leader gestriked; der andere Ritt läuft weiter."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=True)
        two = leaders(("0xbest", 90), ("0xsecond", 70))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)], P)
        # 0xsecond steigt aus (Preis unverändert -> Fee-Verlust), 0xbest hält
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000)], P)
        assert "ETH" not in b.paper.sizes() and "BTC" in b.paper.sizes()
        assert b.busted == 1 and b.won == 0, "nur der beendete Ritt zählt"
        assert b.strikes.get("0xsecond") == 1, "Strike für DEN Ritt-Leader"
        assert "0xbest" not in b.strikes, "der laufende Ritt bleibt unberührt"
        assert "ETH" not in b.ride_leaders and b.ride_leaders.get("BTC") == "0xbest"


def test_parallel_tp_per_ride_own_base():
    """Ziel je Ritt auf EIGENER 1000$-Basis: +1% Kursbewegung bei 10x = +100$
    -> dieser Ritt bankt, der andere läuft weiter."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=True)
        two = leaders(("0xbest", 90), ("0xsecond", 70))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)], P)
        # BTC +1.5% -> Ritt-PnL ~ +150$ > +100$-Ziel; ETH unverändert
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)],
               {"BTC": 101.5, "ETH": 100.0})
        assert "BTC" not in b.paper.sizes(), "TP-Ritt ist gebankt"
        assert "ETH" in b.paper.sizes(), "anderer Ritt läuft weiter"
        assert b.won == 1 and b.banked > 90


def test_parallel_coin_belegt_and_max_rides():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=True, max_rides=1)
        two = leaders(("0xbest", 90), ("0xsecond", 70))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, ETH=500),
                     snap("0xsecond", 50_000, ETH=300, BTC=200)], P)
        assert list(b.paper.sizes()) == ["ETH"], "max_rides deckelt"
        rej = b.stats(P)["scan"]["rejected"]
        assert rej.get("max_ritte", 0) >= 1, "Deckel sichtbar"
    with tempfile.TemporaryDirectory() as tmp2:
        b2 = book(tmp2, parallel_rides=True)
        two = leaders(("0xbest", 90), ("0xsecond", 70))
        b2.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b2.tick(two, [snap("0xbest", 50_000, ETH=500),
                      snap("0xsecond", 50_000, ETH=300)], P)
        assert b2.ride_leaders.get("ETH") == "0xbest", "höherer Score gewinnt den Coin"
        assert b2.stats(P)["scan"]["rejected"].get("coin_belegt") == 1


def test_parallel_one_ride_per_leader_across_ticks():
    """Nutzer-Befund: '7 Ritte laufen - ist das ein Leader oder 8?' Ohne Deckel
    füllt EIN aktiver Leader über mehrere Ticks alle Slots - 7 korrelierte
    Wetten sähen aus wie 7 unabhängige Messpunkte. Jetzt: 1 Position pro Trader;
    erst wenn sein Ritt beendet ist, darf er den nächsten eröffnen."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=True)
        b.tick(LED, [snap("0xbest", 50_000)], P)                    # Baseline
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)           # Ritt 1
        assert list(b.paper.sizes()) == ["BTC"]
        # Nächster Tick: DERSELBE Leader eröffnet frisch ETH -> abgelehnt
        b.tick(LED, [snap("0xbest", 50_000, BTC=500, ETH=300)], P)
        assert "ETH" not in b.paper.sizes(), "1 Position pro Trader"
        assert b.stats(P)["scan"]["rejected"].get("leader_belegt") == 1
        # Leader steigt aus BTC aus -> Ritt 1 verbucht, Slot frei
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], P)
        assert "BTC" not in b.paper.sizes()
        # ETH ist jetzt sein LAUFENDER Bestand (Baseline kennt ihn) - erst ein
        # NEUES Signal öffnet den nächsten Ritt
        b.tick(LED, [snap("0xbest", 50_000, ETH=300, SOL=200)],
               {"BTC": 100.0, "ETH": 100.0, "SOL": 100.0})
        assert "SOL" in b.paper.sizes(), "Slot nach Ritt-Ende wieder frei"
        assert b.ride_leaders == {"SOL": "0xbest"}


def test_parallel_stats_show_leader_per_ride():
    """Jede Positions-Zeile trägt ihren Leader - sonst ist die Herkunft der
    Ritte vom Handy aus nicht zuordenbar."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=True)
        two = leaders(("0xbest", 90), ("0xsecond", 70))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)], P)
        rows = {p["coin"]: p for p in b.stats(P)["positions"]}
        assert rows["BTC"]["leader"] == "0xbest"
        assert rows["ETH"]["leader"] == "0xsecond"


def test_parallel_manual_close_settles_each_ride():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=True)
        two = leaders(("0xbest", 90), ("0xsecond", 70))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)], P)
        n = b.close(P)
        assert n == 2 and b.paper.sizes() == {}
        assert b.won + b.busted == 2, "jeder Ritt = eigener Zyklus"
        # Nutzer-Entscheidung 17.07.: manuelle Closes striken jetzt bei Verlust
        # wie jeder andere Ritt - hier ist der Preis unverändert (P->P), aber
        # die Roundtrip-Fee macht das Netto-PnL leicht negativ -> beide striked.
        assert b.strikes == {"0xbest": 1, "0xsecond": 1}, \
            "manueller Verlust-Ausstieg (auch nur durch Fees) striked jetzt"


def test_crypto_only_rejects_stock_coins():
    """Aktien-Perps (Builder-DEX, 'xyz:INTC') stören die Messlatte außerhalb
    der Börsenzeiten -> raus, sichtbar verworfen. Gilt in BEIDEN Modi."""
    from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot as LS

    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=True)
        flat = LS(address="0xbest", equity=50_000, positions={})
        stock = LS(address="0xbest", equity=50_000, positions={
            "xyz:INTC": LeaderPosition(coin="xyz:INTC", size=100.0, entry=100.0,
                                       position_value=10_000.0, leverage=2)})
        prices = {"xyz:INTC": 100.0}
        b.tick(LED, [flat], prices)
        b.tick(LED, [stock], prices)
        assert b.paper.sizes() == {}, "Aktien-Perp wird nicht geritten"
        assert b.stats(prices)["scan"]["rejected"].get("kein_krypto") == 1


def test_basket_open_takes_only_strongest_signal():
    """Live-Vorfall: Leader eröffnet 7 Coins auf einmal (Aktien-Korb), Bot fraß
    alle 7. Jetzt: EIN Ritt = EINE Position - das Signal mit der größten
    relativen Überzeugung (|exposure|) gewinnt, der Rest wird sichtbar verworfen."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        prices = {"BTC": 100.0, "ETH": 100.0, "SOL": 100.0}
        b.tick(LED, [snap("0xbest", 50_000)], prices)                   # Baseline
        # Korb: ETH ist mit Abstand die größte Position (stärkste Überzeugung)
        b.tick(LED, [snap("0xbest", 50_000, BTC=150, ETH=600, SOL=90)], prices)
        sizes = b.paper.sizes()
        assert list(sizes) == ["ETH"], f"nur das stärkste Signal wird geritten: {sizes}"
        assert b.paper.trades == 1
        sc = b.stats(prices)["scan"]
        assert sc["fresh_seen"] == 3, "alle 3 Korb-Signale wurden GESEHEN"
        assert sc["rejected"].get("korb_begrenzt") == 2, "Rest sichtbar verworfen"


def test_no_extra_coin_added_mid_ride():
    """Auch mitten im Ritt gilt: eine Position. Frische Zusatz-Coins desselben
    Leaders werden verworfen statt aufgesattelt."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)   # reitet BTC
        b.tick(LED, [snap("0xbest", 50_000, BTC=500, ETH=400)], P)   # ETH frisch
        assert "ETH" not in b.paper.sizes(), "kein Zusatz-Coin im Ritt"
        assert b.stats(P)["scan"]["rejected"].get("korb_begrenzt") == 1


def test_excluded_coin_does_not_block_the_slot():
    """Der stärkste Korb-Coin ist ausgeschlossen (BTC) -> der nächststärkste
    rückt nach, der Slot verfällt nicht."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = SprintConfig()   # BTC default ausgeschlossen
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=800, ETH=300)], P)   # BTC stärker
        assert list(b.paper.sizes()) == ["ETH"], "ETH rückt nach, BTC blockt nicht"


def test_add_signal_on_significant_increase():
    """Positions-Trader eröffnen selten neu - Aufstockung >= add_signal_frac
    ist ihr Überzeugungs-Moment und zählt als frisches Signal (nur FLACH-Scan)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)   # add_signal_frac default 0.5
        b.tick(LED, [snap("0xbest", 50_000, BTC=400)], P)   # Baseline: hält 400
        assert b.paper.sizes() == {}, "Bestand allein ist kein Signal"
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # +25%: zu wenig
        assert b.paper.sizes() == {}, "kleine Aufstockung ist Rauschen"
        b.tick(LED, [snap("0xbest", 50_000, BTC=800)], P)   # +60% vs Baseline 500
        assert b.paper.sizes().get("BTC", 0) > 0, "deutliche Aufstockung = Einstieg"


def test_add_signal_on_direction_flip_in_holdings():
    """Leader dreht eine BESTEHENDE Position (Long -> Short): stärkstes
    Richtungs-Signal - wir steigen in die neue Richtung ein."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], P)    # Baseline: long 300
        b.tick(LED, [snap("0xbest", 50_000, ETH=-250)], P)   # Flip auf short
        assert b.paper.sizes().get("ETH", 0) < 0, "Flip im Bestand = Short-Einstieg"


def test_add_signal_disabled_keeps_strict_fresh_only():
    """add_signal_frac 0 = altes Verhalten: NUR 0->Position zählt."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, add_signal_frac=0)
        b.tick(LED, [snap("0xbest", 50_000, BTC=400)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=900)], P)    # +125% Aufstockung
        b.tick(LED, [snap("0xbest", 50_000, BTC=-900)], P)   # sogar Flip
        assert b.paper.sizes() == {}, "aus = nur klassische 0->Position-Signale"


def test_scan_telemetry_makes_rejections_visible():
    """Audit-Befund: verworfene Signale verschwanden spurlos - 'kein Signal kam'
    und 'Signal kam, wurde verworfen' waren von außen identisch. Jetzt zählt
    und zeigt stats() jede Verwerfung mit Grund."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, exclude_coins=["BTC"])
        b.tick(LED, [snap("0xbest", 50_000)], P)                 # Baseline
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)        # frisch, aber BTC
        assert b.paper.sizes() == {}, "BTC bleibt ausgeschlossen"
        sc = b.stats(P)["scan"]
        assert sc["fresh_seen"] == 1, "das Signal wurde GESEHEN"
        assert sc["rejected"].get("coin_ausgeschlossen") == 1
        assert sc["last_rejected"][-1]["coin"] == "BTC"
        assert sc["last_fresh_min"] is not None


def test_banned_leader_rejection_visible():
    """Gebannte Leader werden weiter übersprungen - aber sichtbar (Telemetrie),
    nicht still."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.banned.add("0xbad")
        led = leaders(("0xbad", 90))
        b.tick(led, [snap("0xbad", 50_000)], P)                  # Baseline
        b.tick(led, [snap("0xbad", 50_000, ETH=300)], P)         # frisch, gebannt
        assert b.paper.sizes() == {}, "gebannter Leader löst keinen Ritt aus"
        sc = b.stats(P)["scan"]
        assert sc["rejected"].get("leader_gesperrt") == 1
        assert sc["last_rejected"][-1]["grund"] == "leader_gesperrt"


def test_baselines_survive_short_restart_no_blind_window():
    """Audit-Befund: Baselines nur im RAM -> jeder Deploy riss ein Blindfenster
    (während der Downtime eröffnete Positionen galten für immer als 'alt').
    Jetzt überleben Baselines kurze Neustarts - das Downtime-Signal wird geritten."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        cfg = SprintConfig(exclude_coins=[])
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        b.tick(LED, [snap("0xbest", 50_000)], P)     # Baseline flach, persistiert
        t["now"] += 60                                # ~1 min Deploy-Downtime
        b2 = SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        assert "warm übernommen (60s alt beim Start)" == b2._baseline_status
        b2.tick(LED, [snap("0xbest", 50_000, ETH=200)], P)   # während Downtime eröffnet
        assert "ETH" in b2.paper.sizes(), \
            "kurz nach Neustart ist das Downtime-Signal noch frisch -> reiten"


def test_baselines_stale_file_discarded():
    """Nach LANGER Downtime wäre der Einstieg längst verpasst - alte Baseline-
    Datei wird verworfen, es gilt wieder: erst re-baselinen, nichts reiten."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        cfg = SprintConfig(exclude_coins=[])
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        b.tick(LED, [snap("0xbest", 50_000)], P)
        t["now"] += 3600                              # 1h down: viel zu alt
        b2 = SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        assert "kalt neu gesetzt (Datei war 3600s alt)" == b2._baseline_status
        b2.tick(LED, [snap("0xbest", 50_000, ETH=200)], P)
        assert b2.paper.sizes() == {}, "alte Baselines verworfen -> ETH gilt als laufend"
        b2.tick(LED, [snap("0xbest", 50_000, ETH=200)], P)
        assert b2.paper.sizes() == {}, "ETH bleibt 'laufender Trade', kein Späteinstieg"


def test_baseline_status_no_file_at_all():
    """Allererster Start (noch keine sprint_baselines.json) - eigener Fall, kein
    Lesefehler einer kaputten Datei, sondern schlicht 'noch nie gespeichert'."""
    with tempfile.TemporaryDirectory() as tmp:
        b = SprintBook(SprintConfig(exclude_coins=[]), FEE, runtime_dir=Path(tmp))
        assert b._baseline_status == "kalt neu gesetzt (kein Vorstand)"
        assert b.stats(P)["baseline_status"] == "kalt neu gesetzt (kein Vorstand)"


# ---------- Idle-Rotation: stumme Wallets nach hinten (Quest) ----------

def _idle_book(tmp, t):
    cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0)
    return SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])


def test_idle_addr_after_silence_active_resets_clock():
    """Eine Wallet, die seit Pool-Eintritt kein frisches Signal gab, gilt nach
    max_idle_s als idle. Ein frisches Signal setzt die Uhr zurück."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _idle_book(tmp, t)
        # Tick 1: Leader tritt in den Pool ein (Baseline), Idle-Uhr startet jetzt
        b.tick(LED, [snap("0xbest", 50_000)], P)
        assert b.idle_addrs(3600) == set(), "gerade erst aufgenommen -> Karenz"

        t["now"] += 4000   # > 1h stumm
        b.tick(LED, [snap("0xbest", 50_000)], P)   # immer noch keine neue Position
        assert "0xbest" in b.idle_addrs(3600), "über die Schwelle stumm -> idle"

        # frisches Signal (0 -> Position) setzt die Uhr zurück
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], P)
        assert b.idle_addrs(3600) == set(), "aktiv -> nicht mehr idle"


def test_idle_rotation_disabled_when_zero():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _idle_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        t["now"] += 100_000
        b.tick(LED, [snap("0xbest", 50_000)], P)
        assert b.idle_addrs(0) == set(), "max_idle_s<=0 = Rotation aus"


def test_star_never_counts_as_idle():
    """Ein Star (bewiesener Verdiener) wird NICHT wegen Stille rausrotiert -
    das Confidence-System soll ihn gerade halten."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _idle_book(tmp, t)
        b.confidence["0xbest"] = STAR_THRESHOLD
        b.tick(LED, [snap("0xbest", 50_000)], P)
        t["now"] += 100_000   # lange stumm
        b.tick(LED, [snap("0xbest", 50_000)], P)
        assert b.idle_addrs(3600) == set(), "Star ist idle-immun"


def test_last_active_survives_restart():
    """Die Idle-Uhr überlebt einen Neustart (persistiert neben den Baselines) -
    sonst bekäme jede Stumme nach jedem Deploy die volle Karenz und würde nie
    rausrotiert."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _idle_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)     # Idle-Uhr startet bei now
        t["now"] += 4000
        b.tick(LED, [snap("0xbest", 50_000)], P)     # persistiert (Zeitstempel-Refresh)
        fresh = SprintBook(SprintConfig(exclude_coins=[]), FEE,
                           runtime_dir=Path(tmp), clock=lambda: t["now"])
        assert "0xbest" in fresh.idle_addrs(3600), "Idle-Stand überlebt Neustart"


def test_ride_leader_never_idle_rotated():
    """Latenter Bug-Schutz: der Leader, den wir GERADE reiten, hält eine
    Position, gibt aber definitionsgemäß kein frisches Signal - er darf NICHT
    als idle rausrotiert werden (sonst blinder Close des laufenden Ritts)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _idle_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # jetzt reiten wir 0xbest
        assert b.ride_leader == "0xbest"
        t["now"] += 100_000   # lange kein NEUES Signal (er hält ja)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert "0xbest" not in b.idle_addrs(3600), "Ride-Leader ist idle-immun"


# ---------- Börsen-Schluss: profitable Aktien sichern (Worldclock) ----------

def _stock_entered(tmp, coin="xyz:TSLA", price=100.0):
    """Buch mit frisch eingestiegener Aktien-Long-Position (crypto_only aus)."""
    cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0, crypto_only=False)
    b = SprintBook(cfg, FEE, runtime_dir=Path(tmp))
    px = {coin: price}
    b.tick(LED, [snap("0xbest", 50_000)], px)
    b.tick(LED, [snap("0xbest", 50_000, **{coin: 300})], px)
    assert coin in b.paper.sizes()
    return b


def test_close_stock_winners_locks_profitable_position():
    with tempfile.TemporaryDirectory() as tmp:
        b = _stock_entered(tmp)
        n = b.close_stock_winners({"xyz:TSLA": 101.0})   # +PnL (long 100 -> 101)
        assert n == 1 and b.paper.sizes() == {}, "profitable Aktie am Gong gesichert"
        assert b.won == 1, "zählt als Gewinn-Zyklus"
        assert b.strikes.get("0xbest", 0) == 0, "markt_zu ist strike-exempt"


def test_close_stock_winners_leaves_losing_position_for_2h_rule():
    with tempfile.TemporaryDirectory() as tmp:
        b = _stock_entered(tmp)
        n = b.close_stock_winners({"xyz:TSLA": 99.0})    # -PnL
        assert n == 0 and "xyz:TSLA" in b.paper.sizes(), \
            "Verlust-Aktie bleibt offen, die 2h-Regel fängt sie"


def test_close_stock_winners_leaves_crypto_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)   # crypto_only default egal, BTC ist Krypto
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert "BTC" in b.paper.sizes()
        n = b.close_stock_winners({"BTC": 101.0})        # Krypto, +PnL - trotzdem NICHT
        assert n == 0 and "BTC" in b.paper.sizes(), "Krypto (24/7) bleibt am Aktien-Gong unberührt"


# ---------- Zeit+negativ-Cut: blutenden Ritt früh schließen (Quest) ----------

def _timecut_book(tmp, t, hours=2.0):
    cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0, max_ride_hours=hours)
    return SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])


def _enter_timecut(tmp, t, hours=2.0):
    b = _timecut_book(tmp, t, hours)
    b.tick(LED, [snap("0xbest", 50_000)], P)
    b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # BTC long @100
    assert "BTC" in b.paper.sizes()
    return b


def test_time_cut_closes_long_running_negative_ride():
    """Nutzer: 4h im Minus, dann -230$ beim Leader-Exit. Ein Ritt, der >2h
    offen UND im Minus ist, wird jetzt gecuttet - Slot frei, Leader kriegt
    seinen Strike."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _enter_timecut(tmp, t, hours=2.0)
        t["now"] += 2 * 3600 + 1   # >2h später
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})  # im Minus
        assert b.paper.sizes() == {}, "Zeit-Cut hat den blutenden Ritt geschlossen"
        assert b.busted == 1 and b.won == 0
        assert b.strikes.get("0xbest") == 1, "Verlust-Cut -> Strike für den Leader"


def test_time_cut_does_not_fire_while_positive():
    """Im Plus wird NICHT gecuttet - der Ritt läuft weiter Richtung Ziel,
    auch wenn er schon lange offen ist."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _enter_timecut(tmp, t, hours=2.0)
        t["now"] += 5 * 3600   # 5h offen, aber ...
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 100.5, "ETH": 100.0})  # ... im Plus
        assert "BTC" in b.paper.sizes(), "positiver Ritt läuft trotz langer Dauer weiter"


def test_time_cut_waits_until_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _enter_timecut(tmp, t, hours=2.0)
        t["now"] += 3600   # erst 1h - noch nicht über der Schwelle
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), "unter der Schwelle: Geduld, kein Cut"


def test_time_cut_disabled_when_zero():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _enter_timecut(tmp, t, hours=0.0)   # aus
        t["now"] += 10 * 3600
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), "max_ride_hours=0 -> nie zeit-cutten"


def test_time_cut_clock_survives_restart():
    """Die Ritt-Startzeit überlebt einen Neustart - sonst würde ein Deploy die
    Cut-Uhr zurücksetzen und ein Dauer-Bluter nie geschlossen."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _enter_timecut(tmp, t, hours=2.0)
        t["now"] += 2 * 3600 + 1
        # Neustart: neues Buch lädt offene Position + ride_leader + ride_start_t
        fresh = _timecut_book(tmp, t, hours=2.0)
        assert "BTC" in fresh.paper.sizes() and fresh.ride_leader == "0xbest"
        fresh.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})
        assert fresh.paper.sizes() == {}, "Cut greift auch nach Neustart (Startzeit geladen)"


def test_ride_entry_sizes_survive_restart_scaleout_still_works():
    """Deep-Dive-Fund: ohne Persistenz von _ride_entry_sizes ist der Scale-out-
    Exit nach einem Neustart tot (entry_sz fällt jeden Tick auf die aktuelle
    Leader-Größe -> Bedingung nie erfüllt). Der Leader kann fast ganz abbauen,
    ohne dass wir mitgehen."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)   # BTC-Ritt, Leader-Größe 500 -> entry_sz 500
        assert b._ride_entry_sizes.get("BTC") == 500
        fresh = book(tmp)   # Neustart
        assert fresh._ride_entry_sizes.get("BTC") == 500, "Einstiegsgröße überlebt Neustart"
        # Leader baut >75% ab (500 -> 100): Scale-out muss auch nach Neustart greifen
        fresh.tick(LED, [snap("0xbest", 50_000, BTC=100)], P)
        assert fresh.paper.sizes() == {}, "Scale-out-Exit greift auch nach Neustart"


def test_bilanz_reset_survives_restart_mid_drain():
    """Deep-Dive-Fund (HIGH): faellt ein Neustart mitten in den Mode-Switch-Drain
    (ein Alt-Ritt-Coin bepreist -> gesettlet und parallel_rides=false auf Platte,
    ein anderer ohne Preis -> Drain in der Karenz), ging der ausstehende Bilanz-
    Reset verloren: das Gate 'was_parallel and not cfg.parallel_rides' triggert
    nach dem Neustart nicht mehr (Platte sagt schon false). Alt-Ritt-PnL
    verseuchte die frische Schatztruhe dauerhaft. Fix: Flag persistieren."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        two = leaders(("0xbest", 80), ("0xsecond", 70))   # 1 Ritt pro Leader
        px = {"ETH": 100.0, "xyz:TSLA": 100.0}
        bp = SprintBook(SprintConfig(exclude_coins=[], parallel_rides=True,
                                     crypto_only=False, confirm_delay_s=0),
                        FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        bp.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], px)
        bp.tick(two, [snap("0xbest", 50_000, ETH=300),
                      snap("0xsecond", 50_000, **{"xyz:TSLA": 300})], px)
        bp.won, bp.banked = 5, 500.0   # Mess-Woche-Bilanz, darf nicht ueberleben
        bp._save_state()
        assert set(bp.ride_leaders) == {"ETH", "xyz:TSLA"}

        cfg_single = dict(exclude_coins=[], parallel_rides=False, confirm_delay_s=0)
        flat = [snap("0xbest", 50_000), snap("0xsecond", 50_000)]
        b1 = SprintBook(SprintConfig(**cfg_single), FEE,
                        runtime_dir=Path(tmp), clock=lambda: t["now"])
        assert b1._bilanz_reset_pending is True
        # ETH bepreist (gesettlet, schreibt parallel_rides=false), TSLA ohne Preis
        b1.tick(two, flat, {"ETH": 100.0})
        assert "xyz:TSLA" in b1.ride_leaders and b1._bilanz_reset_pending is True

        # Neustart MITTEN im Drain
        b2 = SprintBook(SprintConfig(**cfg_single), FEE,
                        runtime_dir=Path(tmp), clock=lambda: t["now"])
        assert b2._bilanz_reset_pending is True, "Flag hat den Neustart ueberlebt (der Fix)"
        b2.tick(two, flat, {"ETH": 100.0, "xyz:TSLA": 100.0})   # TSLA bepreist -> Drain fertig
        assert b2.ride_leaders == {}
        assert b2.won == 0 and b2.banked == 0.0, \
            "Bilanz-Reset angewendet - keine Alt-Ritt-PnL-Verseuchung"


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
        assert any("Quest-Einstieg" in m for m in sent)
        assert recorded and recorded[0][0] == "sprint_entry"   # Journal-Kind bleibt intern
        st = b.stats(P)
        assert st["state"] == "hält" and st["held"] == ["BTC LONG"]
        b.tick(LED, [snap("0xbest", 50_000)], P)
        assert any("Quest-Exit" in m for m in sent)
        assert any(k == "sprint_exit" for k, _ in recorded)


def test_sprint_entry_journal_shows_leverage_and_hl_lookup():
    """Live-Fund (17.07., PENGU): ohne diese Felder ist aus dem Journal/Status-
    Spiegel nicht unterscheidbar, ob 'voller Hebel gefahren' heißt 'HL erlaubt
    hier wirklich mehr' oder 'der HL-Lookup lieferte None'."""
    recorded = []

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        b = SprintBook(SprintConfig(exclude_coins=[], leverage=10), FEE, journal=J(),
                       runtime_dir=Path(tmp),
                       max_leverage_fn=lambda coin: {"BTC": 3}.get(coin))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        entry = next(d for k, d in recorded if k == "sprint_entry")
        assert entry["leverage"] == 3.0, "gekappt auf HL-Limit, im Journal sichtbar"
        assert entry["hl_max_leverage"] == 3


def test_sprint_entry_journal_shows_none_when_hl_lookup_unknown():
    recorded = []

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        b = SprintBook(SprintConfig(exclude_coins=[], leverage=10), FEE, journal=J(),
                       runtime_dir=Path(tmp), max_leverage_fn=lambda coin: None)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        entry = next(d for k, d in recorded if k == "sprint_entry")
        assert entry["leverage"] == 10.0
        assert entry["hl_max_leverage"] is None, \
            "kein Datenpunkt muss sichtbar None bleiben, nicht stillschweigend wie 'passt'"


# ---------- Bestätigungsfenster gegen Flip-Flopper (QOL-Runde) ----------

def _confirm_book(tmp, t, delay=10.0):
    cfg = SprintConfig(exclude_coins=[], confirm_delay_s=delay)
    return SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])


def test_pending_candidate_promoted_after_delay_if_never_negative():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)                 # Baseline
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)        # frisches Signal
        assert b.paper.sizes() == {}, "kein Sofort-Einstieg mehr"
        assert any(p["coin"] == "BTC" for p in b.stats(P)["pending"])

        t["now"] += 5   # noch nicht lange genug
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert b.paper.sizes() == {}, "Fenster noch nicht um"

        t["now"] += 6   # jetzt > 10s seit Entdeckung
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert "BTC" in b.paper.sizes(), "bestätigt -> jetzt eingestiegen"
        assert b.stats(P)["pending"] == []


def test_pending_candidate_rejected_immediately_on_negative_tick():
    """'Hält sich mindestens 10s positiv' heißt DURCHGEHEND - ein Ausreißer
    nach unten disqualifiziert sofort, nicht erst am Fensterende (genau der
    Flip-Flop-Fall: Leader schießt ins Minus, wir sollen NICHT einsteigen)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)        # Entry-Preis 100
        assert b.stats(P)["pending"]

        t["now"] += 3
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})
        assert b.stats(P)["pending"] == [], "sofort verworfen, nicht bis Fensterende gewartet"
        assert b.stats(P)["scan"]["rejected"].get("unbestaetigt_negativ") == 1

        t["now"] += 20   # selbst weit über dem Fenster: kein nachträglicher Einstieg
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 105.0, "ETH": 100.0})
        assert b.paper.sizes() == {}, "verworfener Kandidat lebt nicht wieder auf"


def test_pending_candidate_rejected_when_leader_exits_during_wait():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert b.stats(P)["pending"]

        t["now"] += 3
        b.tick(LED, [snap("0xbest", 50_000)], P)   # Leader selbst schon wieder raus
        assert b.stats(P)["pending"] == []
        assert b.stats(P)["scan"]["rejected"].get("unbestaetigt_leader_weg") == 1

        t["now"] += 20
        b.tick(LED, [snap("0xbest", 50_000)], P)
        assert b.paper.sizes() == {}


def test_pending_promotion_uses_current_price_not_signal_price():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 100.0, "ETH": 100.0})
        t["now"] += 11
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 103.0, "ETH": 100.0})
        rows = {r["coin"]: r for r in b.paper.position_rows({"BTC": 103.0})}
        assert abs(rows["BTC"]["entry"] - 103.0) < 1e-9, \
            "Einstieg zum AKTUELLEN Preis bei Bestätigung, nicht dem Signal-Preis"


def test_no_price_neither_promotes_nor_rejects_pending():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        t["now"] += 20
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"ETH": 100.0})  # kein BTC-Preis
        assert b.paper.sizes() == {} and b.stats(P)["pending"], \
            "weder promoted noch verworfen - bleibt stehen"
        assert b.stats({"ETH": 100.0})["pending"][0]["hat_preis"] is False, \
            "sichtbar machen, DASS gerade kein Preis da ist"
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 100.0, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), "sobald der Preis wieder da ist, geht's normal weiter"


def test_pending_survives_spread_noise_with_confirm_tolerance():
    """Live-Fund 18.07.: die ersten 2 frischen Signale nach dem Frequenz-Fix
    wurden BEIDE als 'unbestaetigt_negativ' verworfen (xyz:BRENTOIL). Der
    Leader füllt am Ask, wir vergleichen gegen den Mid - direkt nach jeder
    Eröffnung steht die Position ~einen halben Spread 'im Minus', ohne echte
    Marktbewegung. Mit confirm_tolerance übersteht Spread-Rauschen das
    Fenster; eine ECHTE Bewegung ins Minus verwirft weiterhin sofort."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        cfg = SprintConfig(exclude_coins=[], confirm_delay_s=10.0,
                           confirm_tolerance=0.002)
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # Leader-Entry 100.0
        assert b.stats(P)["pending"]

        t["now"] += 5   # Mid einen halben Spread unter dem Ask-Entry: -0.1%
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.9, "ETH": 100.0})
        assert b.stats(P)["pending"], "Spread-Rauschen (-0.1%) verwirft NICHT mehr"

        t["now"] += 6   # Fenster um, Preis weiter im Toleranzband
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.9, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), "trotz Mini-Minus im Spread-Band promotet"


def test_pending_real_negative_move_still_rejects_despite_tolerance():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        cfg = SprintConfig(exclude_coins=[], confirm_delay_s=10.0,
                           confirm_tolerance=0.002)
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        t["now"] += 3   # -1% ist ECHTE Bewegung, kein Spread
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})
        assert b.stats(P)["pending"] == []
        assert b.stats(P)["scan"]["rejected"].get("unbestaetigt_negativ") == 1


def test_pending_candidate_times_out_if_price_never_arrives():
    """Live-Befund: ein Kandidat (TAO) blieb minutenlang 'wird bestätigt',
    weit über dem Fenster hinaus, weil nie ein Preis für den Coin ankam -
    weder Promotion noch Reject war möglich. Sicherheitsnetz: nach
    _PENDING_MAX_AGE_S OHNE JEMALS einen Preis gesehen zu haben, wird
    verworfen statt für immer zu hängen."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert b.stats(P)["pending"]

        t["now"] += 119   # knapp unter dem Sicherheitsnetz
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"ETH": 100.0})
        assert b.stats(P)["pending"], "noch nicht so lange - bleibt stehen"

        t["now"] += 2   # jetzt über 120s ohne jemals einen Preis gesehen zu haben
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"ETH": 100.0})
        assert b.stats(P)["pending"] == [], "Sicherheitsnetz greift - hängt nicht ewig"
        assert b.stats(P)["scan"]["rejected"].get("unbestaetigt_kein_preis") == 1


def test_two_candidates_confirm_same_tick_highest_score_wins():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        two = leaders(("0xbest", 90), ("0xsecond", 50))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)], P)
        assert len(b.stats(P)["pending"]) == 2

        t["now"] += 11
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)], P)
        assert list(b.paper.sizes()) == ["BTC"], "höherer Score (0xbest) gewinnt"
        assert b.stats(P)["scan"]["rejected"].get("andere_bestaetigt") == 1


def test_second_registration_same_coin_while_pending_ignored():
    """Egal ob derselbe oder ein anderer Leader: der Coin-Slot gehört dem
    ERSTEN Kandidaten, bis er aufgelöst ist - kein Timer-Reset, kein Ersatz."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        s1 = snap("0xbest", 50_000, BTC=500)
        s2 = snap("0xsecond", 50_000, BTC=300)
        b._register_pending("BTC", s1)
        first_since = b._pending["BTC"]["since"]
        t["now"] += 5
        assert b._register_pending("BTC", s2) is False, "Kollision -> False"
        assert b._pending["BTC"]["leader"] == "0xbest"
        assert b._pending["BTC"]["since"] == first_since, "kein Timer-Reset"


def test_coin_collision_lets_second_leader_take_next_coin():
    """Audit-Fund (lautloser Signal-Verlust): meldet Leader B einen Korb, dessen
    stärkster Coin schon von Leader A pending ist, darf B NICHT leer ausgehen
    und den Rest fälschlich als korb_begrenzt verlieren - B rückt auf seinen
    NÄCHSTEN Coin nach. Genau das '5 frisch / 4 korb_begrenzt / 0 Trades'."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        two = leaders(("0xA", 90), ("0xB", 50))
        prices = {"ETH": 100.0, "SOL": 100.0, "ADA": 100.0}
        b.tick(two, [snap("0xA", 50_000), snap("0xB", 50_000)], prices)
        # A eröffnet ETH; B eröffnet ETH+SOL+ADA (Kollision auf ETH)
        b.tick(two, [snap("0xA", 50_000, ETH=300),
                     snap("0xB", 50_000, ETH=300, SOL=300, ADA=300)], prices)
        pend = {(p["coin"], p["leader"]) for p in b.stats(prices)["pending"]}
        assert ("ETH", "0xA") in pend, "A hält den ETH-Slot"
        assert any(c in ("SOL", "ADA") and ldr == "0xB" for c, ldr in pend), \
            "B rückt auf SOL/ADA nach, geht NICHT leer aus"
        # nur EIN korb_begrenzt (B's dritter Coin), nicht zwei
        assert b.stats(prices)["scan"]["rejected"].get("korb_begrenzt") == 1


def test_flip_reentry_stays_instant_even_with_confirm_delay():
    """Kritischer Fund des Architektur-Gegenchecks: eine flip-getriebene Re-
    Entry MUSS sofort bleiben, sonst bricht die 'Flip = derselbe Zyklus läuft
    weiter'-Semantik (sizes() wäre zwischen Close und Re-Entry leer ->
    _settle_ride würde jedes Mal zwischenfeuern)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t, delay=10.0)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # Long: erst pending
        t["now"] += 11   # Bestätigungsfenster abwarten -> jetzt eingestiegen
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert "BTC" in b.paper.sizes()
        t["now"] += 1   # WEIT unter dem Bestätigungsfenster
        b.tick(LED, [snap("0xbest", 50_000, BTC=-500)], P)  # Leader flippt auf Short
        assert b.paper.sizes().get("BTC", 0) < 0, \
            "Flip-Re-Entry bleibt sofort, auch mit confirm_delay_s=10"
        assert b.won == 0 and b.busted == 0, \
            "derselbe Zyklus läuft weiter (kein Zwischen-Settle beim Flip)"


# ---------- Edge-Runde 19.07.: Trailing-TP, Hot-Hand, Trust=Speed ----------

def _edge_book(tmp, t, **overrides):
    overrides.setdefault("exclude_coins", [])
    overrides.setdefault("confirm_delay_s", 0)
    overrides.setdefault("parallel_rides", True)
    cfg = SprintConfig(**overrides)
    return SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])


def test_trailing_tp_lets_winner_run_past_target():
    """Edge-Kern: fixes +10%-Ziel kappte die Überschießer (+225 kam nur durch
    eine Tick-Lücke durch). Mit trail_frac läuft der Ritt über das Ziel hinaus
    und schließt erst beim Rückfall vom Peak - als tp, mit dem HÖHEREN PnL."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, trail_frac=0.3)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # LONG Entry 100
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 101.5, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), \
            "+~150$ ist ÜBER dem 100$-Ziel - früher sofort zu, jetzt läuft er"
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 102.5, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), "Peak steigt weiter (~+240), kein Exit"
        # Rückfall auf ~+140 = unter 70% vom ~240er-Peak -> Trail zieht
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 101.45, "ETH": 100.0})
        assert b.paper.sizes() == {}, "Trail-Exit beim Rückfall vom Peak"
        assert b.won == 1
        assert b.banked > 120, f"mehr als das fixe Ziel eingesammelt: {b.banked:.2f}"


def test_trailing_tp_disabled_keeps_instant_tp():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, trail_frac=0.0)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 101.5, "ETH": 100.0})
        assert b.paper.sizes() == {} and b.won == 1, "trail aus -> altes Sofort-TP"


def test_leader_record_counts_wins_and_exempt_losses():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t)
        b._book_cycle("0xA", 50.0, "tp")
        b._book_cycle("0xA", -20.0, "leader_exit")
        b._book_cycle("0xA", -30.0, "risk_off")   # erzwungen -> zählt nicht
        assert b.leader_record["0xa"] == {"won": 1, "lost": 1}
        assert b.wins_of("0xA") == 1
        # Persistenz über Neustart
        b2 = _edge_book(tmp, t)
        assert b2.wins_of("0xa") == 1


def test_hot_hand_gets_extra_slots_rookie_stays_capped():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, max_rides_per_leader=1, hot_hand_extra_rides=2)
        b.leader_record["0xbest"] = {"won": 1, "lost": 0}
        prices = {"BTC": 100.0, "ETH": 100.0, "SOL": 100.0}
        b.tick(LED, [snap("0xbest", 50_000)], prices)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], prices)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500, ETH=300)], prices)
        assert set(b.paper.sizes()) == {"BTC", "ETH"}, \
            "Hot Hand darf über das Basis-Limit (1) hinaus"
        rook = leaders(("0xrookie", 60))
        b.tick(rook, [snap("0xrookie", 50_000)], prices)
        b.tick(rook, [snap("0xrookie", 50_000, SOL=300)], prices)
        assert "SOL" in b.paper.sizes()
        b.tick(rook, [snap("0xrookie", 50_000, SOL=300, BTC=100)], prices)
        assert b.stats(prices)["scan"]["rejected"].get("leader_belegt") == 1, \
            "Rookie bleibt beim Basis-Limit gedeckelt"


def test_hot_hand_size_mult_after_two_wins():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, hot_hand_size_mult=1.5)
        b.leader_record["0xbest"] = {"won": 2, "lost": 0}
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        notional = abs(b.paper.sizes()["BTC"]) * 100.0
        assert 14_800 < notional <= 15_000, \
            f"1.5x auf die 10k-Basis ab 2 Gewinn-Zyklen, war {notional:.0f}"


def test_trusted_leader_skips_confirm_window_rookie_waits():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, confirm_delay_s=10.0, trusted_skip_confirm=True)
        b.leader_record["0xbest"] = {"won": 1, "lost": 0}
        two = leaders(("0xbest", 90), ("0xrookie", 50))
        prices = {"BTC": 100.0, "ETH": 100.0}
        b.tick(two, [snap("0xbest", 50_000), snap("0xrookie", 50_000)], prices)
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xrookie", 50_000, ETH=300)], prices)
        assert "BTC" in b.paper.sizes(), "bewiesener Leader: SOFORT drin, kein 10s-Fenster"
        assert "ETH" not in b.paper.sizes(), "Rookie wartet weiter im Fenster"
        assert any(p["coin"] == "ETH" for p in b.stats(prices)["pending"])


# ---------- Toxic Flow (Nutzer 20.07.): gebannte Leader counter-traden ----------

def test_toxic_counter_enters_opposite_direction():
    """Gebannter Leader eröffnet LONG -> wir gehen SHORT, gebucht unter der
    Counter-Identität (eigenes Strike-/Record-Konto)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=True)
        b.banned.add("0xbad")
        two = leaders(("0xbest", 80))
        snaps = [snap("0xbest", 50_000), snap("0xbad", 50_000)]
        b.tick(two, snaps, P)                                   # Baselines
        b.tick(two, [snap("0xbest", 50_000),
                     snap("0xbad", 50_000, BTC=500)], P)        # Toxic geht LONG
        assert b.paper.sizes().get("BTC", 0) < 0, "Gegenwette: wir SHORT"
        assert b.ride_leaders["BTC"] == "counter:0xbad"


def test_toxic_counter_disabled_keeps_reject():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=False)
        b.banned.add("0xbad")
        two = leaders(("0xbad", 80))
        b.tick(two, [snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbad", 50_000, BTC=500)], P)
        assert b.paper.sizes() == {}
        assert b.stats(P)["scan"]["rejected"].get("leader_gesperrt") == 1


def test_toxic_counter_ignores_leader_exit_and_flip():
    """v2 (Nutzer 20.07.: 'wenn wir counter traden macht es keinen Sinn wenn
    die Position zu macht weil der Leader beendet'): eine Gegenwette ist
    UNSER Trade - weder Exit noch Flip des Toxic-Leaders beenden sie.
    Journal-Beweis v1: alle 9 Counter-Zyklen endeten durch Leader-Aktionen
    (inkl. -204$ Zwangsschluss mitten im Drawdown), keiner über eigene Ziele."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=True)
        b.banned.add("0xbad")
        two = leaders(("0xbest", 80))
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)], P)
        assert b.paper.sizes().get("BTC", 0) < 0
        cycles_before = b.won + b.busted
        # Toxic-Leader steigt aus -> Gegenwette läuft WEITER
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000)], P)
        assert b.paper.sizes().get("BTC", 0) < 0, "Leader-Exit ist KEIN Signal mehr"
        # Toxic-Leader flippt auf Short -> Gegenwette bleibt unverändert
        # (sein Flip ist ein frisches Signal, aber der Coin ist belegt)
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=-500)], P)
        assert b.paper.sizes().get("BTC", 0) < 0, "Flip dreht die Gegenwette NICHT mehr"
        assert b.won + b.busted == cycles_before, "kein Leader-getriebenes Settle"
        assert b.ride_leaders["BTC"] == "counter:0xbad"


def test_toxic_counter_exits_via_own_take_profit():
    """Die Gegenwette endet über UNSERE Mechanik: erreicht sie das Ziel,
    schließt der TP - egal was der Toxic-Leader gerade hält."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=True)
        b.banned.add("0xbad")
        two = leaders(("0xbest", 80))
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)], P)
        assert b.paper.sizes().get("BTC", 0) < 0   # Gegenwette SHORT
        # Kurs fällt ~1.5% -> Short-PnL über dem +100$-Ziel -> eigener TP
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)],
               {"BTC": 98.5, "ETH": 100.0})
        assert b.paper.sizes() == {}, "eigenes Ziel schließt, Leader hält noch"
        assert b.won == 1 and b.banked > 100


def test_toxic_counter_identity_self_bans_via_own_exit():
    """Verliert die Gegenwette wiederholt über die EIGENE Mechanik (hier:
    Zeit-Cut), bannt die Strike-Maschine die Counter-Identität - danach
    keine neuen Gegenwetten, sichtbar als counter_gesperrt."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=True, strike_ban=1, max_ride_hours=2.0)
        b.banned.add("0xbad")
        two = leaders(("0xbest", 80))
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)], P)
        assert b.paper.sizes().get("BTC", 0) < 0
        t["now"] += 2.5 * 3600   # Gegenwette hängt >2h im Minus -> Zeit-Cut
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)],
               {"BTC": 100.5, "ETH": 100.0})
        assert b.paper.sizes() == {}, "Zeit-Cut (eigene Mechanik) beendet die Gegenwette"
        assert "counter:0xbad" in b.banned, "Gegenwette enttarnt sich selbst"
        # Nächstes Toxic-Signal: KEINE neue Gegenwette mehr
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500, ETH=300)], P)
        assert b.paper.sizes() == {}
        assert b.stats(P)["scan"]["rejected"].get("counter_gesperrt") == 1


def test_toxic_by_record_counters_without_formal_ban():
    """Spiegel-Fund 20.07.: ein Leader mit miesem LANGZEIT-Record, aber
    (noch) nicht formell gebannt, ist trotzdem toxisch - toxic_record_deficit
    macht das Gegenhandeln unabhängig vom aktuellen Strike-Stand."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=True, toxic_record_deficit=3)
        b.leader_record["0xbad"] = {"won": 1, "lost": 4}   # Defizit 3
        assert "0xbad" not in b.banned, "Vorbedingung: nicht formell gebannt"
        two = leaders(("0xbest", 80))
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)], P)
        assert b.paper.sizes().get("BTC", 0) < 0, "Record-toxisch wird gekontert"
        assert b.ride_leaders["BTC"] == "counter:0xbad"


def test_toxic_by_record_survives_amnesty():
    """Kernfund: /quest amnestie leert `banned` - der Record-Defizit bleibt
    (leader_record überlebt Amnestien bewusst), also bleibt der Leader
    toxisch und Toxic Flow hat weiter Futter."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=True, toxic_record_deficit=3)
        b.banned.add("0xbad")
        b.leader_record["0xbad"] = {"won": 0, "lost": 5}
        assert b._is_toxic("0xbad") is True
        b.amnesty()
        assert "0xbad" not in b.banned, "Amnestie hat den Bann gelöscht"
        assert b._is_toxic("0xbad") is True, "Record-Defizit macht ihn weiter toxisch"
        assert "0xbad" in b.toxic_addrs()


def test_toxic_by_record_below_threshold_stays_regular():
    """Defizit unter der Schwelle -> ganz normaler Leader, wird gefolgt."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=True, toxic_record_deficit=3)
        b.leader_record["0xbad"] = {"won": 3, "lost": 4}   # Defizit nur 1
        two = leaders(("0xbest", 80), ("0xbad", 50))
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)], P)
        assert b.paper.sizes().get("BTC", 0) > 0, "unter der Schwelle -> ganz normal gefolgt"
        assert b.ride_leaders["BTC"] == "0xbad"


def test_toxic_by_record_rejects_when_counter_flag_off():
    """toxic_record_deficit ohne counter_toxic: kein Gegenwette-Mechanismus
    verfügbar, ein bekannt schlechter Leader wird trotzdem NICHT gefolgt -
    sichtbar verworfen statt stillschweigend geritten."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, counter_toxic=False, toxic_record_deficit=3)
        b.leader_record["0xbad"] = {"won": 0, "lost": 5}
        two = leaders(("0xbad", 80))
        b.tick(two, [snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbad", 50_000, BTC=500)], P)
        assert b.paper.sizes() == {}
        assert b.stats(P)["scan"]["rejected"].get("leader_toxisch") == 1


def test_flip_flopper_confirmed_when_counter_also_bans():
    """Nutzer 21.07.: 'umgekehrte Strikes bei einer gebannten Wallet machen
    keinen Sinn - 2 Strikes, er ist raus, 2 weitere trotz Counter heißt:
    Wallet ist Flip-Flopper.' Bannt sich die GEGENWETTE gegen eine bereits
    gebannte Wallet ebenfalls, ist das die Bestätigung: weder Folgen noch
    Kontern funktioniert - journalisiert für die Toxic-Watch-Bereinigung."""
    recorded = []

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0, parallel_rides=True,
                            counter_toxic=True, strike_ban=1, max_ride_hours=2.0)
        b = SprintBook(cfg, FEE, journal=J(), runtime_dir=Path(tmp), clock=lambda: t["now"])
        b.banned.add("0xbad")
        two = leaders(("0xbest", 80))
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)], P)
        t["now"] += 2.5 * 3600   # Gegenwette hängt >2h im Minus -> Zeit-Cut -> eigener Bann
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)],
               {"BTC": 100.5, "ETH": 100.0})
        assert "counter:0xbad" in b.banned
        assert any(k == "sprint_flip_flopper_confirmed" and d.get("leader") == "0xbad"
                   for k, d in recorded), recorded


def test_flip_flopper_not_confirmed_for_plain_ban():
    """Ein ganz normaler (Nicht-Counter-)Bann ist KEIN Flip-Flopper-Fund -
    das Event darf nur bei bestätigter Gegenwette gegen eine bereits
    gebannte Wallet feuern, nicht bei jedem gewöhnlichen Strike-Bann."""
    recorded = []

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0)
        b = SprintBook(cfg, FEE, journal=J(), runtime_dir=Path(tmp))
        led = leaders(("0xbad", 80))
        b.tick(led, [snap("0xbad", 50_000)], P)
        b.tick(led, [snap("0xbad", 50_000, BTC=500)], P)
        b.tick(led, [snap("0xbad", 50_000)], {"BTC": 99.0, "ETH": 100.0})
        b.tick(led, [snap("0xbad", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})
        b.tick(led, [snap("0xbad", 50_000)], {"BTC": 98.0, "ETH": 100.0})
        assert "0xbad" in b.banned
        assert not any(k == "sprint_flip_flopper_confirmed" for k, _ in recorded), recorded


def test_flip_flopper_not_confirmed_when_original_not_yet_banned():
    """Bannt sich die Gegenwette, während die ORIGINAL-Wallet (noch) nicht
    gebannt ist, ist das kein Flip-Flopper-Beweis - counter_toxic kann auch
    rein über den Record-Defizit (toxic_addrs) ohne formellen Bann greifen."""
    recorded = []

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0, parallel_rides=True,
                            counter_toxic=True, strike_ban=1, max_ride_hours=2.0,
                            toxic_record_deficit=3)
        b = SprintBook(cfg, FEE, journal=J(), runtime_dir=Path(tmp), clock=lambda: t["now"])
        b.leader_record["0xbad"] = {"won": 0, "lost": 4}   # record-toxisch, NICHT formell gebannt
        assert "0xbad" not in b.banned
        two = leaders(("0xbest", 80))
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)], P)
        t["now"] += 2.5 * 3600
        b.tick(two, [snap("0xbest", 50_000), snap("0xbad", 50_000, BTC=500)],
               {"BTC": 100.5, "ETH": 100.0})
        assert "counter:0xbad" in b.banned
        assert not any(k == "sprint_flip_flopper_confirmed" for k, _ in recorded), recorded


def test_banned_leader_flip_does_not_reenter():
    """Spiegel-Fund 20.07. (Ban-Bypass): lighter:726722 wurde bei 2 Strikes
    gebannt und machte über den Flip-Re-Entry-Pfad weitere 3 Verlust-Ritte
    (~-220$ NACH dem Bann) - das Settle des Flips kann den Bann gerade
    ausgelöst haben, der Wiedereinstieg lief daran vorbei."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, strike_ban=1)   # 1 Strike = sofortiger Bann
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)      # LONG Entry 100
        assert "BTC" in b.paper.sizes()
        # Leader flippt auf Short, unser Long steht leicht im Minus -> Settle
        # mit Verlust -> Strike 1 -> BANN. Der Re-Entry darf NICHT feuern.
        b.tick(LED, [snap("0xbest", 50_000, BTC=-500)], {"BTC": 99.5, "ETH": 100.0})
        assert "0xbest" in b.banned, "Vorbedingung: Flip-Verlust löste den Bann aus"
        assert b.paper.sizes() == {}, \
            "gebannter Leader darf über den Flip-Pfad NICHT wieder einsteigen"


def test_hot_hand_denied_for_net_loser_despite_wins():
    """Spiegel-Fund 20.07.: lighter:366058 bekam mit 2 Siegen bei 5 Pleiten
    die 1.5x-Size (-164$ in einem Ritt). Heiße Hand braucht jetzt Siege UND
    positive Bilanz - ein Netto-Verlierer bleibt bei Basis-Slots/-Size und
    wartet im Bestätigungsfenster wie jeder No-Name."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, max_rides_per_leader=1, hot_hand_extra_rides=2,
                       hot_hand_size_mult=1.5, trusted_skip_confirm=True)
        b.leader_record["0xbest"] = {"won": 2, "lost": 5}
        assert b._leader_ride_limit("0xbest") == 1, "keine Extra-Slots"
        assert b._size_mult("0xbest") == 1.0, "keine Extra-Size"
        assert b._skip_confirm("0xbest") is False, "kein Fenster-Skip"
        b.leader_record["0xbest"] = {"won": 2, "lost": 1}   # positive Bilanz
        assert b._leader_ride_limit("0xbest") == 3
        assert b._size_mult("0xbest") == 1.5
        assert b._skip_confirm("0xbest") is True


def test_plus_lock_closes_green_before_it_turns_red():
    """Nutzer 19.07.: 'sobald wir im Plus sind sollten wir nie mit Minus
    rausgehen' - Ritte standen im Plus und endeten Stunden später per
    Zeit-Cut im Minus. Peak >= arm -> Rückfall auf floor schließt grün."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, plus_lock_arm=30.0, plus_lock_floor=5.0)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)          # Entry 100
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 100.5, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), "~+41$ Peak - läuft weiter Richtung Ziel"
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 100.1, "ETH": 100.0})
        assert b.paper.sizes() == {}, "Rückfall auf ~+1$ (<= floor 5) -> gesichert raus"
        assert b.won == 1 and b.banked > 0, "klein-grün verbucht statt später rot"
        assert b.strikes == {}, "plus_lock ist strike-exempt (unsere Regel)"


def test_plus_lock_not_armed_below_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, plus_lock_arm=30.0, plus_lock_floor=5.0)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 100.2, "ETH": 100.0})
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.8, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), \
            "Peak (~+11$) blieb unter arm (30$) - Sicherung nie scharf, Ritt läuft"


def test_fast_exit_check_catches_plus_lock_between_full_ticks():
    """Spiegel-Fund 22.07.: Plus-Lock (Floor 15$) schoss trotzdem bis zu
    -33.57$ ins Minus durch - der volle Tick lief nur alle poll_seconds
    (20s), ein 10x-Ritt kann sich in dieser Zeit weiter bewegen als der
    Floor-Puffer gibt. fast_exit_check() muss denselben Plus-Lock-Exit
    auch OHNE Leader-Scan (nur Preise) auslösen können."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, plus_lock_arm=30.0, plus_lock_floor=5.0)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)          # Entry 100
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 100.5, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), "~+41$ Peak - armed, läuft weiter"
        b.fast_exit_check({"BTC": 100.1, "ETH": 100.0})   # kein voller Tick, nur Preise
        assert b.paper.sizes() == {}, "Plus-Lock feuert auch über den Fast-Path"
        assert b.won == 1 and b.banked > 0
        assert b.strikes == {}


def test_fast_exit_check_noop_in_single_ride_mode():
    """Einzel-Ritt hat eigenes Timing/eigene Baseline-Logik - fast_exit_check
    darf dort NICHT eingreifen (parallel_rides=False -> No-Op)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp, parallel_rides=False)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert "BTC" in b.paper.sizes()
        b.fast_exit_check({"BTC": 1.0, "ETH": 1.0})   # absurder Preis, würde sonst busten
        assert "BTC" in b.paper.sizes(), "No-Op im Einzel-Ritt-Modus"


def test_fast_exit_check_noop_without_prices():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t, plus_lock_arm=30.0, plus_lock_floor=5.0)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        b.fast_exit_check({})
        assert "BTC" in b.paper.sizes(), "leere Preise -> No-Op, kein falscher Exit"


def test_amnesty_clears_strikes_and_bans_keeps_positive_proof():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _edge_book(tmp, t)
        b.strikes = {"0xa": 2, "0xb": 1}
        b.banned = {"0xa"}
        b.confidence = {"0xc": 10}
        b.leader_record = {"0xc": {"won": 2, "lost": 0}}
        n_strikes, n_bans = b.amnesty()
        assert (n_strikes, n_bans) == (2, 1)
        assert b.strikes == {} and b.banned == set()
        assert b.confidence == {"0xc": 10}, "Confidence bleibt"
        assert b.wins_of("0xc") == 2, "Gewinn-Historie bleibt"
        b2 = _edge_book(tmp, t)
        assert b2.strikes == {} and b2.banned == set(), "Amnestie überlebt Neustart"


# ---------- Zeit+negativ-Cut auch im MESS-MODUS (Live-Fund 18.07.) ----------
# Ein BTC-Mess-Ritt hing 3.3h im Minus, obwohl max_ride_hours: 2 scharf war -
# der Cut lief bisher NUR im Einzel-Ritt-Zweig, _tick_parallel erfasste nicht
# einmal Startzeiten je Ritt.

def _parallel_cut_book(tmp, t, hours=2.0):
    cfg = SprintConfig(exclude_coins=[], confirm_delay_s=0, parallel_rides=True,
                       max_ride_hours=hours)
    return SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])


def test_parallel_time_cut_closes_long_negative_ride():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _parallel_cut_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # LONG, Entry 100
        assert "BTC" in b.paper.sizes()
        t["now"] += 2.5 * 3600   # über der 2h-Schwelle, leicht im Minus
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.5, "ETH": 100.0})
        assert b.paper.sizes() == {}, "Dauer-Bluter wird auch im Mess-Modus gecuttet"
        assert b.busted == 1
        assert b.strikes.get("0xbest") == 1, "zeit_negativ ist NICHT strike-exempt"


def test_parallel_time_cut_spares_positive_ride():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _parallel_cut_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        t["now"] += 3 * 3600   # lange offen, aber im PLUS (unter TP-Schwelle)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 100.5, "ETH": 100.0})
        assert "BTC" in b.paper.sizes(), "im Plus läuft der Ritt weiter Richtung Ziel"


def test_parallel_time_cut_clock_survives_restart():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _parallel_cut_book(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        t["now"] += 1.5 * 3600   # Deploy mitten im Ritt
        b2 = _parallel_cut_book(tmp, t)
        assert "BTC" in b2._ride_start_ts, "Uhr überlebt den Neustart"
        t["now"] += 1 * 3600     # gesamt 2.5h seit dem ECHTEN Start
        b2.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.5, "ETH": 100.0})
        assert b2.paper.sizes() == {}, \
            "Cut rechnet ab dem echten Ritt-Beginn, nicht ab dem Deploy"


# ---------- Bestätigungsfenster auch im MESS-MODUS (Wave-3-Fund 18.07.) ----------
# Audit-Befund: tick() kehrte im parallel_rides-Zweig bisher IMMER vor dem
# einzigen _process_pending()-Aufruf zurück - config.yaml hatte confirm_delay_s
# trotzdem scharf ("Flip-Flopper-Schutz"), er griff im Mess-Modus aber nie,
# jedes Signal wurde sofort geritten.

def _confirm_book_parallel(tmp, t, delay=10.0, **overrides):
    cfg = SprintConfig(exclude_coins=[], confirm_delay_s=delay,
                       parallel_rides=True, **overrides)
    return SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])


def test_parallel_fresh_signal_registers_pending_not_instant():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book_parallel(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)                 # Baseline
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)        # frisches Signal
        assert b.paper.sizes() == {}, "kein Sofort-Ritt mehr, auch im Mess-Modus"
        assert any(p["coin"] == "BTC" for p in b.stats(P)["pending"])

        t["now"] += 11   # Fenster um
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert "BTC" in b.paper.sizes(), "bestätigt -> jetzt als eigener Mess-Ritt eröffnet"
        assert b.ride_leaders.get("BTC") == "0xbest"


def test_parallel_pending_rejected_immediately_on_negative_tick():
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book_parallel(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert b.stats(P)["pending"]
        t["now"] += 3
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 99.0, "ETH": 100.0})
        assert b.stats(P)["pending"] == [], "sofort verworfen, wie im Einzel-Ritt"
        assert b.paper.sizes() == {}


def test_parallel_multiple_confirmed_candidates_each_get_own_ride():
    """Kernunterschied zum Einzel-Ritt: KEIN 'nur einer gewinnt' - im Mess-
    Modus ist Platz für mehrere parallele Ritte, jeder bestätigte Kandidat
    bekommt seinen eigenen Slot."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book_parallel(tmp, t)
        two = leaders(("0xA", 90), ("0xB", 50))
        prices = {"BTC": 100.0, "ETH": 100.0}
        b.tick(two, [snap("0xA", 50_000), snap("0xB", 50_000)], prices)
        b.tick(two, [snap("0xA", 50_000, BTC=500),
                     snap("0xB", 50_000, ETH=300)], prices)
        assert len(b.stats(prices)["pending"]) == 2

        t["now"] += 11
        b.tick(two, [snap("0xA", 50_000, BTC=500),
                     snap("0xB", 50_000, ETH=300)], prices)
        assert set(b.paper.sizes()) == {"BTC", "ETH"}, \
            "BEIDE bestätigten Kandidaten eröffnen je einen eigenen Mess-Ritt"


def test_parallel_flip_reentry_within_running_ride_stays_instant():
    """Design-Entscheidung bleibt auch im Mess-Modus gültig: eine flip-
    getriebene Re-Entry INNERHALB eines laufenden Ritts (Schritt 3 in
    _tick_parallel) bleibt sofort - nur FRISCHE Signale (Schritt 4) laufen
    über das Bestätigungsfenster."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book_parallel(tmp, t)
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # pending
        t["now"] += 11
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # bestätigt -> Ritt läuft
        assert "BTC" in b.paper.sizes()
        t["now"] += 1   # weit unter dem Bestätigungsfenster
        b.tick(LED, [snap("0xbest", 50_000, BTC=-500)], P)  # Leader flippt
        assert b.paper.sizes().get("BTC", 0) < 0, \
            "Flip-Re-Entry bleibt sofort, auch im Mess-Modus mit confirm_delay_s"


def test_parallel_pending_promotion_rechecks_capacity():
    """Kapazitäts-Checks gelten erneut bei der Promotion, nicht nur bei der
    Registrierung - der Zustand kann sich während der Wartezeit geändert
    haben (hier: max_rides inzwischen ausgeschöpft)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book_parallel(tmp, t, max_rides=1)
        two = leaders(("0xA", 90), ("0xB", 50))
        prices = {"BTC": 100.0, "ETH": 100.0}
        b.tick(two, [snap("0xA", 50_000), snap("0xB", 50_000)], prices)
        b.tick(two, [snap("0xA", 50_000, BTC=500),
                     snap("0xB", 50_000, ETH=300)], prices)
        assert len(b.stats(prices)["pending"]) == 2

        t["now"] += 11
        b.tick(two, [snap("0xA", 50_000, BTC=500),
                     snap("0xB", 50_000, ETH=300)], prices)
        assert len(b.paper.sizes()) == 1, "max_rides=1 lässt nur EINEN der beiden zu"
        assert b.stats(prices)["scan"]["rejected"].get("max_ritte") == 1


# ---------- Confidence-Points/Star-Kern (QOL-Runde) ----------

def test_tp_earns_confidence_leader_exit_win_does_not():
    """Nur das eigene, durchgehaltene +10%-Ziel (tp) verdient einen Confidence-
    Punkt. Ein leader-getriebener Exit im Plus bleibt Bilanz-Gewinn UND heilt
    weiter einen Strike - ist aber confidence-neutral (kein bewiesener
    Richtungs-Skill, nur zufälliges Grün beim Leader-Exit)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)   # BTC-Long, Einstieg bei Preis 100
        b.strikes["0xbest"] = 1   # simuliert einen vorherigen Verlust-Ritt
        # Leader steigt aus, Preis ist gestiegen -> Gewinn-Ritt, aber leader_exit
        b.tick(LED, [snap("0xbest", 50_000)], {"BTC": 101.0, "ETH": 100.0})
        assert b.won == 1, "zählt als Gewinn-Zyklus"
        assert b.strikes.get("0xbest", 0) == 0, "Strike heilt trotzdem"
        assert b.confidence.get("0xbest", 0) == 0, "aber KEIN Confidence-Punkt"


def test_tp_reason_grants_five_confidence():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        # Preis weit genug rauf für +100$ TP bei 10x auf 1000$ Basis
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 101.5, "ETH": 100.0})
        assert b.won == 1
        assert b.confidence.get("0xbest", 0) == CONFIDENCE_PER_WIN


def test_confidence_of_exposes_raw_points():
    """Vorher nur is_star() (binär ab 100) abrufbar - der Fortschritt davor
    war für /quest pool nicht auslesbar (Nutzer-Fund 17.07.)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        assert b.confidence_of("0xBest") == 0
        b.confidence["0xbest"] = 15
        assert b.confidence_of("0xBEST") == 15, "case-insensitiv wie is_star"


def test_tp_confidence_visible_in_telegram_message():
    """Nutzer-Fund (17.07.): '2 erfolgreiche Trader gestern, aber ich hab
    nichts von Confidence-Punkten gesehen' - die Vergabe selbst lief korrekt
    (Punkte landeten im confidence-dict), war aber komplett STUMM: kein Push,
    kein Journal-Eintrag für den normalen +5-Fall, nur beim seltenen Star-
    Sprung (>=100) gab's überhaupt ein Signal. Jetzt hängt die Info an der
    ohnehin gesendeten Zyklus-Ende-Meldung."""
    sent = []

    class N:
        def send(self, m):
            sent.append(m)

    with tempfile.TemporaryDirectory() as tmp:
        b = SprintBook(SprintConfig(exclude_coins=[], confirm_delay_s=0), FEE,
                       notifier=N(), runtime_dir=Path(tmp))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        sent.clear()
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 101.5, "ETH": 100.0})
        assert b.won == 1
        msg = next(m for m in sent if "beendet" in m)
        assert f"+{CONFIDENCE_PER_WIN} Confidence" in msg
        assert f"({CONFIDENCE_PER_WIN}/{STAR_THRESHOLD})" in msg


def test_star_marked_at_threshold_and_stays_strike_prone():
    """Star ab genau STAR_THRESHOLD Punkten (= 20 TP-Ritte). Ein Star ist
    NICHT strike-immun - ein Verlust-Ritt striked ihn wie jeden anderen."""
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        b.confidence["0xbest"] = STAR_THRESHOLD - CONFIDENCE_PER_WIN  # kurz davor
        assert not b.is_star("0xbest")
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 101.5, "ETH": 100.0})
        assert b.confidence["0xbest"] == STAR_THRESHOLD
        assert b.is_star("0xbest")

        # Star bleibt strike-anfällig: nächster Ritt verliert -> normaler Strike
        b.tick(LED, [snap("0xbest", 50_000)], P)                    # Baseline
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], P)           # frischer Ritt
        b.tick(LED, [snap("0xbest", 50_000)], P)                    # Leader raus, Fee-Verlust
        assert b.strikes.get("0xbest") == 1, "Star ist NICHT strike-immun"
        assert b.confidence["0xbest"] == STAR_THRESHOLD, "Verlust senkt Confidence nicht"
        assert b.is_star("0xbest"), "bleibt Star trotz Strike"


def test_star_push_fires_once_at_threshold_crossing():
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
        b.confidence["0xbest"] = STAR_THRESHOLD - CONFIDENCE_PER_WIN
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 101.5, "ETH": 100.0})
    assert any("Star enttarnt" in m for m in sent)
    assert any(k == "sprint_star" for k, _ in recorded)


def test_confidence_survives_restart():
    with tempfile.TemporaryDirectory() as tmp:
        b = _entered(tmp)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], {"BTC": 101.5, "ETH": 100.0})
        assert b.confidence.get("0xbest", 0) == CONFIDENCE_PER_WIN
        fresh = book(tmp)
        assert fresh.confidence.get("0xbest", 0) == CONFIDENCE_PER_WIN


def test_star_priority_over_higher_score_on_simultaneous_signals():
    """BACKLOG-Entscheidung: bei gleichzeitigen Signalen gewinnt der Star vor
    dem reinen Score-Tie-Break."""
    with tempfile.TemporaryDirectory() as tmp:
        b = book(tmp)
        b.confidence["0xsecond"] = STAR_THRESHOLD   # 0xsecond ist Star, aber schwächerer Score
        two = leaders(("0xbest", 90), ("0xsecond", 50))
        b.tick(two, [snap("0xbest", 50_000, BTC=100), snap("0xsecond", 50_000, ETH=100)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=200), snap("0xsecond", 50_000, ETH=200)], P)
        assert b.ride_leader == "0xsecond", "Star gewinnt trotz niedrigerem Score"


# ---------- Star-Preemption (QOL-Runde) ----------

def _riding_with_star(tmp, t):
    """Helfer: 0xbest reitet BTC (direkt via _enter, ohne Bestätigungsfenster
    für den Ritt-Aufbau selbst), 0xstar ist bereits ein Star. Gibt (b, two,
    s_best, s_star) zurück - der Aufrufer triggert das frische ETH-Signal
    per tick() selbst, um den genauen Preis/Zeitpunkt zu kontrollieren."""
    b = _confirm_book(tmp, t)
    two = leaders(("0xbest", 80), ("0xstar", 95))
    b.confidence["0xstar"] = STAR_THRESHOLD
    s_best = snap("0xbest", 50_000, BTC=500)
    b._enter("BTC", s_best, P)
    assert "BTC" in b.paper.sizes()
    b._baselines["0xbest"] = b._book_of(s_best)
    b._baselines["0xstar"] = {}
    return b, two, s_best


def test_star_preemption_full_flow_settles_old_ride_as_star_preempt():
    """Kompletter Preemption-Fluss: profitabler BTC-Ritt läuft, ein Star
    meldet ein frisches Signal, das Fenster wird abgewartet, dann wird der
    alte Ritt mit reason='star_preempt' abgeschlossen (+5 Confidence für den
    ALTEN Leader, nicht den Star) und der Star-Ritt eröffnet."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b, two, s_best = _riding_with_star(tmp, t)
        s_star = snap("0xstar", 50_000, ETH=300)
        hold = {"BTC": 100.5, "ETH": 100.0}   # BTC profitabel, aber unter TP

        b.tick(two, [s_best, s_star], hold)
        assert any(p["coin"] == "ETH" for p in b.stats(hold)["pending"])
        assert "BTC" in b.paper.sizes(), "alter Ritt läuft während der Bestätigung weiter"

        t["now"] += 11
        b.tick(two, [s_best, s_star], hold)
        assert "ETH" in b.paper.sizes() and "BTC" not in b.paper.sizes()
        assert b.ride_leader == "0xstar"
        assert b.won == 1, "der preemptete Ritt zählt als Bilanz-Gewinn"
        assert b.confidence.get("0xbest", 0) == CONFIDENCE_PER_WIN, \
            "star_preempt gibt dem ALTEN Leader +5 Confidence (BACKLOG-Versprechen)"
        assert b.confidence.get("0xstar", 0) == STAR_THRESHOLD, \
            "der Star selbst bekommt hier keine zusätzlichen Punkte"
        assert b.stats(hold)["pending"] == []


def test_star_preemption_rechecks_profitability_at_confirmation_time():
    """Zweiter Fund des Gegenchecks: Profitabilität wird zum Bestätigungs-
    zeitpunkt neu geprüft, nicht zur Entdeckungszeit - kippt der Ritt
    zwischendurch ins Minus/Breakeven, bleibt die Preemption gegenstandslos
    und der Kandidat wartet einfach weiter."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b, two, s_best = _riding_with_star(tmp, t)
        s_star = snap("0xstar", 50_000, ETH=300)

        b.tick(two, [s_best, s_star], {"BTC": 100.5, "ETH": 100.0})   # profitabel
        assert any(p["coin"] == "ETH" for p in b.stats(P)["pending"])

        t["now"] += 11
        # Preis zurück auf Einstand (netto durch die Entry-Fee sogar leicht
        # unter 1000 Basis) - zum Bestätigungszeitpunkt NICHT mehr profitabel
        b.tick(two, [s_best, s_star], {"BTC": 100.0, "ETH": 100.0})
        assert "ETH" not in b.paper.sizes(), "Preemption gegenstandslos ohne Profit JETZT"
        assert "BTC" in b.paper.sizes(), "alter Ritt läuft unverändert weiter"
        assert any(p["coin"] == "ETH" for p in b.stats(P)["pending"]), \
            "Kandidat wird nicht verworfen, bleibt einfach stehen"


def test_star_preemption_moot_when_ride_ends_naturally_during_wait():
    """Endet der laufende Ritt während der Wartezeit von selbst (Leader steigt
    aus), ist die Preemption gegenstandslos: der Ritt settled ganz normal
    (reason='leader_exit', KEINE Confidence fürs BACKLOG-'nur tp'-Ergebnis),
    der Star-Kandidat wird zum normalen Fresh-Entry, sobald wieder flach."""
    recorded = []

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b, two, s_best = _riding_with_star(tmp, t)
        b.journal = J()
        s_star = snap("0xstar", 50_000, ETH=300)
        hold = {"BTC": 100.5, "ETH": 100.0}

        b.tick(two, [s_best, s_star], hold)
        assert any(p["coin"] == "ETH" for p in b.stats(hold)["pending"])

        t["now"] += 3
        s_best_flat = snap("0xbest", 50_000)   # Leader selbst schon wieder raus
        b.tick(two, [s_best_flat, s_star], hold)
        assert b.paper.sizes() == {}, "Ritt hat sich ganz normal beendet"
        assert b.won == 1
        cycle_end = next(d for k, d in recorded if k == "sprint_cycle_end")
        assert cycle_end["reason"] == "leader_exit"
        assert b.confidence.get("0xbest", 0) == 0, \
            "leader_exit-Gewinn bleibt confidence-neutral, auch als Preemption-Auslöser"

        t["now"] += 10   # insgesamt 13s seit Entdeckung des Star-Signals
        b.tick(two, [s_best_flat, s_star], hold)
        assert "ETH" in b.paper.sizes(), "Star-Kandidat promotet normal, sobald flach"
        assert b.ride_leader == "0xstar"


def test_non_star_candidate_does_not_preempt_while_riding():
    """Nur Stars dürfen einen laufenden, profitablen Ritt preempten - ein
    normaler (nicht-Star) Kandidat bleibt einfach liegen, bis wir wieder
    flach sind (unabhängig vom Score)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        b = _confirm_book(tmp, t)
        s_best = snap("0xbest", 50_000, BTC=500)
        b._enter("BTC", s_best, P)
        s2 = snap("0xsecond", 50_000, ETH=300)   # kein Star
        b._register_pending("ETH", s2)

        t["now"] += 11
        two = leaders(("0xbest", 80), ("0xsecond", 90))
        b.tick(two, [s_best, s2], {"BTC": 100.5, "ETH": 100.0})
        assert "ETH" not in b.paper.sizes()
        assert "BTC" in b.paper.sizes()
        assert any(p["coin"] == "ETH" for p in b.stats(P)["pending"]), \
            "bleibt liegen, bis wir wieder flach sind"


# ---------- coin-Feld im Einzel-Ritt-Zyklusende (QOL-Runde) ----------

def test_single_ride_settle_carries_coin_field():
    """_settle_ride() ließ das coin-Feld bisher aus (anders als _settle_one im
    Mess-Modus) - ein Auswertungs-Tool konnte Einzel-Ritt-Zyklen nicht nach
    Coin/Asset-Klasse aufschlüsseln. Jetzt trägt jedes Journal-Kind (auch
    sprint_tp/sprint_bust/sprint_cycle_end) den Coin des Ritts."""
    recorded = []

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    with tempfile.TemporaryDirectory() as tmp:
        b = SprintBook(SprintConfig(exclude_coins=[]), FEE, journal=J(), runtime_dir=Path(tmp))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # Einstieg BTC
        b.tick(LED, [snap("0xbest", 50_000)], P)             # Leader raus -> Zyklusende
    kinds = {k: d for k, d in recorded}
    assert kinds["sprint_cycle_end"]["coin"] == "BTC", \
        f"coin fehlt im Einzel-Ritt-Zyklusende: {kinds['sprint_cycle_end']}"


# ---------- Mode-Switch-Sicherung: Mess-Modus -> Einzel-Ritt (QOL-Runde) ----------

def test_mode_switch_settles_leftover_parallel_rides_individually():
    """Schaltet man mitten im Mess-Modus (mehrere offene Ritte) auf Einzel-Ritt
    um, würde die alte Einzel-Ritt-Logik das als 'Leader aus der Rotation
    gefallen' fehldeuten und ALLE Coins auf einmal zu einem falschen Zyklus
    verbuchen, ohne Strike/Heilung. Die Sicherung muss jeden Alt-Ritt EINZELN
    über die Mess-Modus-Logik sauber abschließen, bevor irgendetwas anderes
    passiert - korrekte eigene 1000$-Basis, korrekte Strikes, ride_leaders
    geleert."""
    with tempfile.TemporaryDirectory() as tmp:
        # Zwei parallele Ritte simulieren: Mess-Modus an, zwei Leader eröffnen
        b = SprintBook(SprintConfig(exclude_coins=[], parallel_rides=True), FEE,
                       runtime_dir=Path(tmp))
        two = leaders(("0xbest", 90), ("0xsecond", 70))
        b.tick(two, [snap("0xbest", 50_000), snap("0xsecond", 50_000)], P)
        b.tick(two, [snap("0xbest", 50_000, BTC=500),
                     snap("0xsecond", 50_000, ETH=300)], P)
        assert set(b.paper.sizes()) == {"BTC", "ETH"}
        assert b.ride_leaders == {"BTC": "0xbest", "ETH": "0xsecond"}

        # Jetzt der Moduswechsel: parallel_rides aus, nächster Tick im
        # Einzel-Ritt-Modus muss die zwei Alt-Ritte sauber abwickeln
        b.cfg.parallel_rides = False
        b.tick(LED, [snap("0xbest", 50_000)], P)   # beliebiger Einzel-Ritt-Tick

        assert b.paper.sizes() == {}, "beide Alt-Ritte wurden geschlossen"
        assert b.ride_leaders == {}, "kein State-Leak"
        assert b.won + b.busted == 2, "JEDER Alt-Ritt als eigener Zyklus verbucht"
        # Preis unverändert -> beide Ritte enden im (Fee-)Minus -> je 1 Strike
        assert b.strikes.get("0xbest") == 1
        assert b.strikes.get("0xsecond") == 1
        assert abs(b.paper.equity(P) - 1000.0) < 1e-6, \
            "Konto danach sauber auf 1000 (kein falsch verbuchter Kombi-Zyklus)"


def test_mode_switch_defers_coin_without_price_to_next_tick():
    """Fehlt beim Drain der Preis für einen Alt-Ritt-Coin, wird er NICHT mit
    einem falschen Preis zwangsverbucht, sondern bleibt bis zum nächsten Tick
    stehen (kein Datenverlust durch eine kurze Preis-Lücke direkt nach einem
    Neustart)."""
    with tempfile.TemporaryDirectory() as tmp:
        b = SprintBook(SprintConfig(exclude_coins=[], parallel_rides=True), FEE,
                       runtime_dir=Path(tmp))
        b.tick(LED, [snap("0xbest", 50_000)], P)
        b.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)
        assert "BTC" in b.paper.sizes()

        b.cfg.parallel_rides = False
        b.tick(LED, [snap("0xbest", 50_000)], {"ETH": 100.0})  # kein BTC-Preis!
        assert "BTC" in b.paper.sizes(), "ohne Preis nicht zwangsverbucht"
        assert b.ride_leaders == {"BTC": "0xbest"}, "Alt-Ritt bleibt bis zum nächsten Tick"

        b.tick(LED, [snap("0xbest", 50_000)], P)   # jetzt mit BTC-Preis
        assert b.paper.sizes() == {} and b.ride_leaders == {}


def test_mode_switch_never_freezes_forever_on_unpriceable_coin():
    """Audit-Fund (Freeze-Fallstrick): bleibt ein Alt-Ritt-Coin DAUERHAFT ohne
    Preis (Aktien-Perp am Wochenende, fehlt in all_mids()), würde die Sicherung
    sonst jeden Tick für immer zurückkehren -> der Bot handelt nie wieder. Nach
    _DRAIN_MAX_AGE_S wird zum Einstand zwangsabgerechnet, damit die Pipeline
    weiterläuft."""
    from bot.sprint import _DRAIN_MAX_AGE_S

    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        bp = SprintBook(SprintConfig(exclude_coins=[], parallel_rides=True,
                                     crypto_only=False, confirm_delay_s=0),
                        FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        bp.tick(LED, [snap("0xbest", 50_000)], {"xyz:TSLA": 100.0})
        bp.tick(LED, [snap("0xbest", 50_000, **{"xyz:TSLA": 300})], {"xyz:TSLA": 100.0})
        assert bp.ride_leaders == {"xyz:TSLA": "0xbest"}

        # Neustart im Einzel-Modus, xyz:TSLA hat KEINEN Preis mehr (Wochenende)
        b = SprintBook(SprintConfig(exclude_coins=[], parallel_rides=False, confirm_delay_s=0),
                       FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])
        assert b.ride_leaders == {"xyz:TSLA": "0xbest"}
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], {"ETH": 100.0})   # kein TSLA-Preis
        assert b.ride_leaders, "in der Karenz: noch nicht zwangsabgerechnet"

        t["now"] += _DRAIN_MAX_AGE_S + 1
        b.tick(LED, [snap("0xbest", 50_000, ETH=300)], {"ETH": 100.0})
        assert b.ride_leaders == {}, "nach der Karenz zwangsabgerechnet - kein ewiger Freeze"


# ---------- Bilanz-Reset beim ersten Laden nach Mess-Modus (QOL-Runde) ----------

def test_bilanz_resets_when_loading_after_mess_modus_switch():
    """Erster Load unter parallel_rides=false, nachdem der State zuletzt unter
    parallel_rides=true gespeichert wurde: Zyklus-Zähler und Schatztruhe (Mess-
    Woche-Bilanz, andere Regeln - parallele Ritte, kein Bestätigungsfenster)
    starten frisch bei 0. Strikes/Bans/Confidence (echtes LARP-Wissen, keine
    Mess-Modus-spezifische Zahl) bleiben unangetastet."""
    with tempfile.TemporaryDirectory() as tmp:
        old = book(tmp, parallel_rides=True)
        old.won, old.busted, old.banked, old.total_trades = 5, 3, 842.17, 40
        old.strikes["0xbad"] = 1
        old.banned.add("0xzzz")
        old.confidence["0xbest"] = 55
        old._save_state()

        fresh = book(tmp)   # Default parallel_rides=False
        assert fresh.won == 0 and fresh.busted == 0
        assert fresh.banked == 0.0 and fresh.total_trades == 0
        assert fresh.strikes.get("0xbad") == 1, "Strikes bleiben"
        assert "0xzzz" in fresh.banned, "Bans bleiben"
        assert fresh.confidence.get("0xbest") == 55, "Confidence bleibt"


def test_bilanz_reset_waits_for_leftover_ride_drain():
    """Live-Bug (Nutzer-Befund): der Reset lief bisher schon beim Laden - vor
    dem ersten Tick, der noch offene Alt-Mess-Ritte über die Mode-Switch-
    Sicherung abrechnet. Der Forced-Close des Alt-Ritts sickerte dadurch als
    allererster Eintrag in die eigentlich frische Bilanz. Reset muss WARTEN,
    bis der Drain fertig ist."""
    with tempfile.TemporaryDirectory() as tmp:
        old = book(tmp, parallel_rides=True)
        old.tick(LED, [snap("0xbest", 50_000)], P)
        old.tick(LED, [snap("0xbest", 50_000, BTC=500)], P)   # offener Mess-Ritt
        assert "BTC" in old.paper.sizes() and "BTC" in old.ride_leaders
        old.won, old.banked = 5, 842.17   # simuliert echte Mess-Woche-Historie
        old._save_state()

        fresh = book(tmp)   # parallel_rides=False (Default)
        assert fresh.won == 5 and fresh.banked == 842.17, \
            "Reset noch NICHT angewendet - wartet auf den Drain des Alt-Ritts"
        assert "BTC" in fresh.ride_leaders

        fresh.tick(LED, [snap("0xbest", 50_000)], P)   # erster Tick: drained + resettet
        assert fresh.ride_leaders == {} and fresh.paper.sizes() == {}
        assert fresh.won == 0 and fresh.busted == 0 and fresh.banked == 0.0, \
            "jetzt wirklich sauber - der Forced-Close zählt nicht in die frische Bilanz"


def test_bilanz_reset_does_not_repeat_on_next_restart():
    with tempfile.TemporaryDirectory() as tmp:
        old = book(tmp, parallel_rides=True)
        old.won, old.banked = 5, 500.0
        old._save_state()

        first = book(tmp)
        assert first.won == 0 and first.banked == 0.0
        first.won, first.banked = 2, 150.0   # neue Einzel-Ritt-Bilanz sammelt sich an
        first._save_state()

        second = book(tmp)
        assert second.won == 2 and second.banked == 150.0, \
            "kein wiederholter Reset - der Umstieg ist ein einmaliges Ereignis"


def test_missing_parallel_rides_field_treated_as_was_parallel():
    """Alte State-Dateien (vor diesem Feature gespeichert) haben kein
    'parallel_rides'-Feld - genau der Fall auf dem echten Server gerade jetzt.
    Muss wie 'war Mess-Modus' behandelt werden, sonst würde der Umstieg beim
    allerersten Deploy dieses Features gar keinen Reset auslösen."""
    with tempfile.TemporaryDirectory() as tmp:
        rt = Path(tmp)
        (rt / "sprint_cycles.json").write_text(json.dumps({
            "banked": 1370.63, "won": 26, "busted": 23, "total_trades": 90,
            "ride_leader": "", "strikes": {"0xbad": 1}, "banned": ["0xzzz"],
        }))
        fresh = SprintBook(SprintConfig(exclude_coins=[]), FEE, runtime_dir=rt)
        assert fresh.won == 0 and fresh.busted == 0 and fresh.banked == 0.0
        assert fresh.strikes.get("0xbad") == 1
        assert "0xzzz" in fresh.banned


def test_no_bilanz_reset_when_staying_in_parallel_mode():
    with tempfile.TemporaryDirectory() as tmp:
        old = book(tmp, parallel_rides=True)
        old.won, old.banked = 5, 500.0
        old._save_state()

        still_parallel = book(tmp, parallel_rides=True)
        assert still_parallel.won == 5 and still_parallel.banked == 500.0, \
            "kein Reset, solange der Mess-Modus weiterläuft"


# ---------- Produktions-Settings-Smoke-Test (Nutzer-Sorge: "keine Signale
# trotz vollem Pool" nach der QOL-Runde) ----------

def test_fresh_signal_reaches_entry_under_production_settings_full_pool():
    """Deckt genau die Sorge ab, die live nach dem Deploy aufkam: mit ALLEN
    QOL-Runde-Einstellungen gleichzeitig scharf (confirm_delay_s=10,
    crypto_only=false, exclude_coins=[BTC], parallel_rides=false) und einem
    vollen 21er-Pool (wie live gemeldet) muss ein frisches Signal eines
    NICHT-Top-Score-Leaders trotzdem zuverlässig durch die komplette Kette
    (Entdeckung -> Bestätigungsfenster -> Promotion) bis zum echten Einstieg
    kommen - keine der neuen Zustandsmaschinen darf das Signal verschlucken."""
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        cfg = SprintConfig(
            enabled=True, equity=1000.0, leverage=10.0, target_profit=100.0,
            max_positions=1, parallel_rides=False, max_rides=8,
            max_rides_per_leader=1, crypto_only=False, bust_frac=0.05,
            pool_size=20, pool_min_score=10.0,
            min_notional=10.0, partial_exit_frac=0.75, add_signal_frac=0.5,
            confirm_delay_s=10.0, strike_ban=2, exclude_coins=["BTC"],
        )
        b = SprintBook(cfg, FEE, runtime_dir=Path(tmp), clock=lambda: t["now"])

        addrs = [f"0x{i:040x}" for i in range(21)]
        pool = [{"address": a, "score": 10 + i, "weight": 1.0} for i, a in enumerate(addrs)]
        flat = [snap(a, 50_000) for a in addrs]
        prices = {"BTC": 100.0, "ETH": 100.0}

        b.tick(pool, flat, prices)   # Baselines für alle 21
        assert b.stats(prices)["pending"] == [] and b.paper.sizes() == {}

        # Mittiger (nicht bester) Leader eröffnet ein frisches ETH-Signal
        signalling = list(flat)
        signalling[10] = snap(addrs[10], 50_000, ETH=300)
        b.tick(pool, signalling, prices)
        assert b.stats(prices)["pending"], "Signal wurde nicht als Kandidat registriert"

        t["now"] += 20   # typischer Poll-Abstand (copytrade.poll_seconds=20) > 10s-Fenster
        b.tick(pool, signalling, prices)
        assert "ETH" in b.paper.sizes(), \
            "Signal kam trotz voller Produktions-Kette nicht bis zum Einstieg durch"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
