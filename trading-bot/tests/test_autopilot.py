"""Unit-Tests für Leader-Rotation und KI-News-Merge (ohne Netzwerk)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.autopilot import rotate_leaders, cap_path_b_admission
from bot.copytrade.analyzer import TraderMetrics


def metrics(addr, score, roi=0.05, sprint=None):
    m = TraderMetrics(address=addr, account_value=50000, days=30)
    m.score = score
    m.sprint_score = score if sprint is None else sprint
    m.roi = roi
    m.profit_factor = 2.0
    m.closed_trades = 50
    return m


def leader(addr, weight=0.33):
    return {"address": addr, "weight": weight, "score": 50}


def test_rotation_drops_weak_leader():
    current = [leader("0xa"), leader("0xb")]
    ranked = [metrics("0xa", 20), metrics("0xb", 60), metrics("0xc", 55)]
    out = rotate_leaders(current, ranked, max_leaders=2, min_keep_score=35)
    addrs = [l["address"] for l in out]
    assert "0xa" not in addrs, "Score 20 < 35 muss rausfliegen"
    assert addrs == ["0xb", "0xc"]


def test_rotation_keep_bonus_prevents_churn():
    # Bestehender Leader (58) vs. neuer Kandidat (60): Bonus hält den Bestand
    current = [leader("0xa")]
    ranked = [metrics("0xa", 58), metrics("0xb", 60)]
    out = rotate_leaders(current, ranked, max_leaders=1, min_keep_score=35, keep_bonus=5)
    assert out[0]["address"] == "0xa"


def test_rotation_clearly_better_candidate_wins():
    current = [leader("0xa")]
    ranked = [metrics("0xa", 50), metrics("0xb", 70)]
    out = rotate_leaders(current, ranked, max_leaders=1, min_keep_score=35)
    assert out[0]["address"] == "0xb"


def test_rotation_vanished_leader_not_kept():
    # 0xa taucht in der neuen Analyse gar nicht mehr auf -> wird nicht blind behalten
    current = [leader("0xa")]
    ranked = [metrics("0xb", 45)]
    out = rotate_leaders(current, ranked, max_leaders=2, min_keep_score=35)
    assert [l["address"] for l in out] == ["0xb"]


def test_rotation_weights_sum_to_one():
    ranked = [metrics("0xa", 60), metrics("0xb", 40), metrics("0xc", 20)]
    out = rotate_leaders([], ranked, max_leaders=3, min_keep_score=0)
    assert abs(sum(l["weight"] for l in out) - 1.0) < 0.01


def _autopilot():
    from bot.autopilot import Autopilot
    from bot.config import load_config
    return Autopilot(load_config())


def _gong_autopilot(tmp):
    from datetime import datetime, timezone

    from bot.sprint import SprintBook
    ap = _autopilot()
    ap.cfg.sprint.stock_market_hours = True
    ap.cfg.sprint.confirm_delay_s = 0   # sofortiger Einstieg im Test (kein 10s-Fenster)
    ap.cfg.sprint.exclude_coins = []
    ap.sprint = SprintBook(ap.cfg.sprint, ap.cfg.backtest.fee_rate, runtime_dir=Path(tmp))
    sent = []
    ap.notifier = type("N", (), {"send": lambda self, m, **k: sent.append(m)})()
    ap.copier = type("C", (), {"last_prices": {}})()
    ap._sent = sent
    return ap


def _utc(y, mo, d, h, mi):
    from datetime import datetime, timezone
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_leverage_desc_shows_base_only_without_overrides():
    ap = _autopilot()
    ap.cfg.sprint.leverage = 10
    ap.cfg.sprint.leverage_overrides = {}
    assert ap._leverage_desc() == "x10"


def test_leverage_desc_lists_overrides_sorted():
    """Nutzer (17.07.): BTC x20/ETH x15 statt Basis x10 - Log/Telegram-Meldungen
    sollen das sichtbar machen statt weiter pauschal 'x10' zu behaupten."""
    ap = _autopilot()
    ap.cfg.sprint.leverage = 10
    ap.cfg.sprint.leverage_overrides = {"ETH": 15, "BTC": 20}
    assert ap._leverage_desc() == "x10 (BTC x20, ETH x15)"


def _join_status_push(ap, timeout=2.0):
    """Wave-2-Fund (17.07.): push_snapshot läuft jetzt im Hintergrund-Thread
    (löst das Blockier-Problem) - Tests müssen deterministisch auf ihn warten,
    statt sich auf Zufalls-Timing zu verlassen."""
    t = ap._status_push_thread
    if t is not None:
        t.join(timeout=timeout)


def test_maybe_push_status_respects_enabled_flag():
    ap = _autopilot()
    ap.cfg.status_push.enabled = False
    import bot.status_push as sp_mod

    calls = []
    orig = sp_mod.push_snapshot
    sp_mod.push_snapshot = lambda *a, **k: calls.append(1) or True
    try:
        ap._maybe_push_status()
        _join_status_push(ap)
    finally:
        sp_mod.push_snapshot = orig
    assert calls == [], "enabled=false -> nie pushen"


def test_maybe_push_status_respects_interval_gate():
    """Nutzer (17.07.): periodischer Status-Spiegel, nicht jeder Tick - sonst
    Rauschen im dedizierten Branch und unnötige API-Calls."""
    ap = _autopilot()
    ap.cfg.status_push.enabled = True
    ap.cfg.status_push.interval_minutes = 5
    import bot.status_push as sp_mod

    calls = []
    orig = sp_mod.push_snapshot
    sp_mod.push_snapshot = lambda *a, **k: calls.append(1) or True
    try:
        ap._maybe_push_status()
        _join_status_push(ap)
        ap._maybe_push_status()   # sofort erneut - Intervall noch nicht um
        _join_status_push(ap)
    finally:
        sp_mod.push_snapshot = orig
    assert len(calls) == 1


def test_maybe_push_status_fires_again_after_interval():
    ap = _autopilot()
    ap.cfg.status_push.enabled = True
    ap.cfg.status_push.interval_minutes = 5
    import bot.status_push as sp_mod

    calls = []
    orig = sp_mod.push_snapshot
    sp_mod.push_snapshot = lambda *a, **k: calls.append(1) or True
    try:
        ap._maybe_push_status()
        _join_status_push(ap)
        ap._last_status_push -= 301   # Intervall künstlich verstreichen lassen
        ap._maybe_push_status()
        _join_status_push(ap)
    finally:
        sp_mod.push_snapshot = orig
    assert len(calls) == 2


def test_maybe_push_status_skips_while_previous_push_still_running():
    """Neu (Wave-2-Fund 17.07.): der Hintergrund-Thread darf nicht doppelt
    laufen, wenn ein vorheriger Push (Netzwerk) noch nicht fertig ist."""
    import threading

    ap = _autopilot()
    ap.cfg.status_push.enabled = True
    ap.cfg.status_push.interval_minutes = 5
    import bot.status_push as sp_mod

    calls = []
    release = threading.Event()

    def slow_push(*a, **k):
        calls.append(1)
        release.wait(timeout=2)
        return True

    orig = sp_mod.push_snapshot
    sp_mod.push_snapshot = slow_push
    try:
        ap._maybe_push_status()          # startet den (blockierten) Hintergrund-Push
        ap._last_status_push -= 301      # Intervall künstlich verstreichen lassen
        ap._maybe_push_status()          # sollte NICHT parallel pushen (noch "running")
        release.set()
        _join_status_push(ap)
    finally:
        sp_mod.push_snapshot = orig
        release.set()
    assert len(calls) == 1, "kein Parallel-Push, solange der vorherige noch läuft"


def test_funnel_cache_survives_restart():
    """Nutzer (17.07.): 'kein blinder Fleck mehr' - analysis_funnel/pool_funnel
    waren bisher nach JEDEM Neustart bis zu reanalyze_hours lang blank, weil
    _load_or_analyze_leaders() den Pool nur aus der Datei lädt statt neu zu
    bauen. Jetzt persistiert (runtime/funnel_cache.json) und beim nächsten
    Konstruktor-Aufruf wieder geladen - Datei danach wieder entfernen, sonst
    leckt das in andere Tests im selben Lauf (echtes globales RUNTIME-Dir,
    kein Tempdir-Override möglich)."""
    from bot.autopilot import RUNTIME

    cache_path = RUNTIME / "funnel_cache.json"
    orig = cache_path.read_text() if cache_path.exists() else None
    try:
        ap1 = _autopilot()
        ap1._analysis_funnel = {"candidates": 42}
        ap1._pool_funnel = {"final_pool": 7}
        ap1._save_funnel_cache()

        ap2 = _autopilot()
        assert ap2._analysis_funnel == {"candidates": 42}
        assert ap2._pool_funnel == {"final_pool": 7}
    finally:
        if orig is None:
            cache_path.unlink(missing_ok=True)
        else:
            cache_path.write_text(orig)


def test_cmd_set_status_push_rejects_invalid_token():
    """Nutzer (17.07.): /setstatuspush vom Handy, Muster wie /setcmm - hier NUR
    die Ablehnungspfade testen, damit nie versehentlich in die echte .env
    geschrieben wird (wie test_cmd_setcmm_rejects_non_jwt es für CMM hält)."""
    ap = _autopilot()
    assert "Nutzung" in ap._cmd_set_status_push("")
    assert "PAT" in ap._cmd_set_status_push("not-a-token")
    assert "PAT" in ap._cmd_set_status_push("ghp_zukurz")


def test_cmd_status_push_reports_disabled():
    ap = _autopilot()
    ap.cfg.status_push.enabled = False
    assert "aus" in ap._cmd_status_push()


def test_cmd_status_push_reports_missing_token():
    import bot.status_push as sp_mod

    ap = _autopilot()
    ap.cfg.status_push.enabled = True
    orig = sp_mod.os.environ.pop(sp_mod.TOKEN_ENV, None)
    try:
        assert "Token" in ap._cmd_status_push()
    finally:
        if orig is not None:
            sp_mod.os.environ[sp_mod.TOKEN_ENV] = orig


def test_cmd_status_push_forces_immediate_push_bypassing_interval():
    """Test-Knopf fürs Handy: NICHT auf die 5min-Bremse warten müssen."""
    import bot.status_push as sp_mod

    ap = _autopilot()
    ap.cfg.status_push.enabled = True
    orig_env = sp_mod.os.environ.get(sp_mod.TOKEN_ENV)
    sp_mod.os.environ[sp_mod.TOKEN_ENV] = "ghp_test"
    orig_push = sp_mod.push_snapshot
    sp_mod.push_snapshot = lambda *a, **k: True
    try:
        out = ap._cmd_status_push()
    finally:
        sp_mod.push_snapshot = orig_push
        if orig_env is None:
            sp_mod.os.environ.pop(sp_mod.TOKEN_ENV, None)
        else:
            sp_mod.os.environ[sp_mod.TOKEN_ENV] = orig_env
    assert "✅" in out and "status-feed" in out


def test_sprint_tick_inputs_without_lighter_promote_returns_hl_only():
    ap = _autopilot()
    ap.sprint_leaders = [{"address": "0xabc", "score": 80}]
    ap.copier = type("C", (), {"last_snapshots": ["snap1"], "last_prices": {"BTC": 1.0}})()
    ap.lighter = None
    leaders, snaps = ap._sprint_tick_inputs()
    assert leaders == [{"address": "0xabc", "score": 80}]
    assert snaps == ["snap1"]


def test_sprint_tick_inputs_merges_lighter_when_promote_enabled():
    """Nutzer-Entscheidung (17.07., Option B): Lighter-Signale zusätzlich ins
    Sprint-Buch, eigener Adress-Namensraum, kein Duplizieren der HL-Liste."""
    ap = _autopilot()
    ap.cfg.lighter.sprint_promote = True
    ap.sprint_leaders = [{"address": "0xabc", "score": 80}]
    ap.copier = type("C", (), {"last_snapshots": ["snap1"], "last_prices": {"BTC": 1.0}})()

    class _FakeLighter:
        def sprint_snapshots(self, prices, keep=None):
            assert prices == {"BTC": 1.0}
            return ([{"address": "lighter:1", "score": 15.0, "weight": 1.0}],
                    ["lighter_snap"])

    ap.lighter = _FakeLighter()
    leaders, snaps = ap._sprint_tick_inputs()
    assert leaders == [{"address": "0xabc", "score": 80},
                       {"address": "lighter:1", "score": 15.0, "weight": 1.0}]
    assert snaps == ["snap1", "lighter_snap"]


def test_sprint_tick_inputs_ignores_lighter_when_promote_disabled():
    ap = _autopilot()
    ap.cfg.lighter.sprint_promote = False
    ap.sprint_leaders = [{"address": "0xabc", "score": 80}]
    ap.copier = type("C", (), {"last_snapshots": ["snap1"], "last_prices": {}})()

    class _BoomLighter:
        def sprint_snapshots(self, prices, keep=None):
            raise AssertionError("darf nicht aufgerufen werden, wenn sprint_promote aus ist")

    ap.lighter = _BoomLighter()
    leaders, snaps = ap._sprint_tick_inputs()
    assert leaders == [{"address": "0xabc", "score": 80}]
    assert snaps == ["snap1"]


def test_market_gong_first_call_syncs_crypto_only_no_action():
    """Erststart synchronisiert crypto_only mit dem Marktzustand, feuert aber
    KEINEN Gong (kein /analyze, keine Nachricht)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _gong_autopilot(tmp)
        ap._force_analysis = False
        ap._maybe_market_gong(now=_utc(2026, 7, 15, 17, 0))   # Mi 13:00 ET = offen
        assert ap.cfg.sprint.crypto_only is False, "offen -> Aktien erlaubt"
        assert ap._force_analysis is False and ap._sent == [], "kein Gong beim Erststart"


def test_market_gong_opening_activates_basket_and_analyze():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _gong_autopilot(tmp)
        ap._maybe_market_gong(now=_utc(2026, 7, 15, 12, 0))   # 8:00 ET = zu (Erststart)
        assert ap.cfg.sprint.crypto_only is True
        ap._force_analysis = False
        ap._maybe_market_gong(now=_utc(2026, 7, 15, 14, 0))   # 10:00 ET = offen -> Gong
        assert ap.cfg.sprint.crypto_only is False, "Aktien-Basket aktiv"
        assert ap._force_analysis is True, "/analyze angefordert"
        assert any("Börse offen" in m for m in ap._sent)


def test_market_gong_close_deactivates_and_secures_winner():
    import tempfile

    from bot.sprint import SprintBook
    with tempfile.TemporaryDirectory() as tmp:
        ap = _gong_autopilot(tmp)
        ap._maybe_market_gong(now=_utc(2026, 7, 15, 14, 0))   # offen (Erststart)
        assert ap.cfg.sprint.crypto_only is False
        # profitable Aktien-Position aufbauen
        from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot
        px = {"xyz:TSLA": 100.0}
        led = [{"address": "0xbest", "score": 80, "weight": 1.0}]
        s0 = LeaderSnapshot("0xbest", 50_000, {})
        s1 = LeaderSnapshot("0xbest", 50_000,
                            {"xyz:TSLA": LeaderPosition("xyz:TSLA", 300, 100.0, 30_000, 2)})
        ap.sprint.tick(led, [s0], px)
        ap.sprint.tick(led, [s1], px)
        assert "xyz:TSLA" in ap.sprint.paper.sizes()
        ap.copier.last_prices = {"xyz:TSLA": 101.0}           # +PnL
        ap._maybe_market_gong(now=_utc(2026, 7, 15, 20, 0))   # 16:00 ET = Schluss -> Gong
        assert ap.cfg.sprint.crypto_only is True, "Aktien-Basket aus"
        assert ap.sprint.paper.sizes() == {}, "profitable Aktie gesichert"
        assert any("Börse zu" in m for m in ap._sent)


def test_market_gong_no_transition_is_noop():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _gong_autopilot(tmp)
        ap._maybe_market_gong(now=_utc(2026, 7, 15, 14, 0))   # offen (Erststart)
        ap._force_analysis = False
        ap._sent.clear()
        ap._maybe_market_gong(now=_utc(2026, 7, 15, 15, 0))   # immer noch offen
        assert ap._force_analysis is False and ap._sent == [], "kein Zustandswechsel -> nichts"


def test_is_sleeper_flat_and_stale_is_excluded():
    """Nutzer-Kernbefund: 20 flache Schläfer-Wallets, kein Signal = sus. Eine
    Wallet ohne offene Position UND seit >max_idle_days ohne Trade ist ein
    toter Slot und wird vor dem Pool aussortiert - egal wie gut die Alt-
    Historie."""
    ap = _autopilot()
    ap.cfg.sprint.max_idle_days = 3

    def m(open_pos, idle_days):
        x = TraderMetrics(address="0xa", account_value=50_000, days=21)
        x.open_positions = open_pos
        x.days_since_last_trade = idle_days
        return x

    assert ap._is_sleeper(m(0, 10)) is True, "flach + 10 Tage still = Schläfer"
    assert ap._is_sleeper(m(0, 1)) is False, "flach aber gestern getradet = wach"
    assert ap._is_sleeper(m(2, 30)) is False, "hält Positionen = wach (auch bei alten Fills)"
    ap.cfg.sprint.max_idle_days = 0
    assert ap._is_sleeper(m(0, 999)) is False, "Filter aus -> nie Schläfer"


def test_is_stock_dominated():
    """Live-Fund: die alte Alles-oder-nichts-Regel (nur 100% reine Aktien-
    Historie fliegt raus) ließ 'fast nur Aktien, einmal Krypto probiert'-
    Wallets komplett durch - 50 Signale in Folge alle verworfen, 0 Krypto-
    Einstiege. Jetzt ein Anteil-Schwellwert."""
    ap = _autopilot()
    ap.cfg.sprint.regime_min_crypto_share = 0.34

    def m(coins):
        x = TraderMetrics(address="0xa", account_value=50_000, days=21)
        x.coins = coins
        return x

    assert ap._is_stock_dominated(m(["xyz:TSLA", "xyz:NVDA"])) is True, "100% Aktien"
    assert ap._is_stock_dominated(m(["ETH", "xyz:TSLA", "xyz:NVDA"])) is True, \
        "33% Krypto < 34%-Schwelle -> immer noch überwiegend Aktien (der Live-Fund)"
    assert ap._is_stock_dominated(m(["ETH", "SOL", "xyz:TSLA"])) is False, "67% Krypto, klar drüber"
    assert ap._is_stock_dominated(m(["ETH", "SOL"])) is False, "100% Krypto"
    assert ap._is_stock_dominated(m([])) is False, "unbekannt -> sicher nicht aussortieren"

    ap.cfg.sprint.regime_min_crypto_share = 0.0
    assert ap._is_stock_dominated(m(["ETH", "xyz:TSLA", "xyz:NVDA"])) is False, \
        "Schwelle 0 = altes Verhalten (nur 0% Krypto fliegt raus)"


def test_regime_pool_drops_stock_dominated_when_market_closed():
    """Nutzer: außerhalb der Börsenzeiten läuft der Pool leer, weil überwiegend
    Aktien-Trader nur gesperrte Signale liefern. Bei crypto_only (Börse zu)
    fliegen sie raus, Krypto-/Misch-Trader füllen die Slots."""
    ap = _autopilot()
    ap.cfg.sprint.pool_size = 5
    ap.cfg.sprint.regime_min_crypto_share = 0.34
    ap.leaders = []

    def m(addr, score, coins):
        x = metrics(addr, score, sprint=score)
        x.address = addr
        x.coins = coins
        return x

    ranked = [m("0xstock", 90, ["xyz:TSLA"]),               # 100% Aktien, Top-Score
              m("0xmostlystock", 80, ["ETH", "xyz:TSLA", "xyz:NVDA"]),  # 33% Krypto - der Live-Fund
              m("0xcrypto", 50, ["ETH"]),
              m("0xmixed", 40, ["BTC", "xyz:NVDA"])]         # 50% Krypto - bleibt drin

    ap.cfg.sprint.crypto_only = True                  # Börse zu
    pool = [p["address"] for p in ap._build_sprint_pool(ranked)]
    assert "0xstock" not in pool, "reiner Aktien-Trader raus, wenn Börse zu"
    assert "0xmostlystock" not in pool, "überwiegend Aktien-Trader jetzt AUCH raus (der Fix)"
    assert "0xcrypto" in pool and "0xmixed" in pool

    ap.cfg.sprint.crypto_only = False                 # Börse offen
    pool = [p["address"] for p in ap._build_sprint_pool(ranked)]
    assert "0xstock" in pool and "0xmostlystock" in pool, \
        "zu Börsenzeiten sind die Aktien-Trader wieder dabei"


# ---------- /quest funnel: Trichter-Diagnose (Nutzer: 'proper Analyse-Tools') ----------

def test_build_sprint_pool_records_funnel_stats():
    """_build_sprint_pool füllt _pool_funnel mit den Rohzahlen jeder Filterstufe -
    Grundlage für /quest funnel. Deckt genau den Live-Fund ab: 14 Kandidaten
    < 20 Slots -> Idle-Rotation kann nicht abschneiden."""
    ap = _autopilot()
    ap.cfg.sprint.pool_size = 20
    ap.leaders = []
    ranked = [metrics(f"0x{i}", 50, sprint=50) for i in range(14)]
    for m in ranked:
        m.coins = ["ETH"]
    ap._build_sprint_pool(ranked)
    pf = ap._pool_funnel
    assert pf["sprint_ok_in"] == 14
    assert pf["cands_after_filters"] == 14
    assert pf["pool_size_target"] == 20
    assert pf["final_pool"] == 14
    assert pf["truncated_by_size"] == 0, "unter pool_size -> niemand fällt wegen Platzmangel raus"


def test_build_sprint_pool_funnel_truncation_when_oversubscribed():
    """Gegenprobe: sind es MEHR Kandidaten als Slots, schneidet [:n] tatsächlich
    ab - truncated_by_size > 0."""
    ap = _autopilot()
    ap.cfg.sprint.pool_size = 3
    ap.leaders = []
    ranked = [metrics(f"0x{i}", 50 - i, sprint=50 - i) for i in range(6)]
    for m in ranked:
        m.coins = ["ETH"]
    ap._build_sprint_pool(ranked)
    pf = ap._pool_funnel
    assert pf["cands_after_filters"] == 6 and pf["pool_size_target"] == 3
    assert pf["truncated_by_size"] == 3


def test_cmd_quest_funnel_renders_without_analysis():
    ap = _autopilot()
    # Explizit leer, nicht auf den Konstruktor-Default verlassen: seit die
    # Funnel-Zahlen einen Neustart überleben (Nutzer 17.07., persistiert in
    # runtime/funnel_cache.json), ist "frisch konstruiert" NICHT mehr
    # automatisch "keine Analyse" - das hängt jetzt vom Cache-File ab, das
    # andere Tests im selben Lauf (dieselbe globale RUNTIME) füllen können.
    ap._analysis_funnel = {}
    ap._pool_funnel = {}
    out = ap._cmd_quest_funnel()
    assert "keine analyse" in out.lower()


def test_cmd_quest_funnel_renders_full_report():
    ap = _autopilot()
    ap._analysis_funnel = {
        "candidates": 50, "source": "CMM 30 + HL 20", "main_ranked": 6, "min_score": 35,
        "sprint_gate_ko": 20, "sprint_score_ko": 8, "sprint_idle_ko": 5, "sprint_ok": 17,
        "pool_min_score": 10, "truncated": 3, "larp_ko": 2, "larp_reasons": {"lucky_punch": 2},
        "errors": 1,
    }
    ap._pool_funnel = {
        "sprint_ok_in": 17, "banned_out": 0, "regime_out": 3, "crypto_only": True,
        "cands_after_filters": 14, "idle_total": 6, "idle_in_pool": 6,
        "truncated_by_size": 0, "pool_size_target": 20, "forced_main": 0, "final_pool": 14,
    }
    out = ap._cmd_quest_funnel()
    assert "50 Kandidaten" in out
    assert "17 Sprint-tauglich" in out
    assert "Pool NICHT voll" in out and "14/20" in out
    assert "6 von 6 idle" in out
    assert "finaler Pool: 14" in out


def test_sprint_pool_wider_than_main_and_sorted_by_direction_score():
    """Sprint bekommt einen breiteren Pool (pool_size) als das Hauptbuch,
    sortiert nach RICHTUNGS-Score (sprint_score, nicht Haupt-Score) - das
    Hauptbuch (max_leaders) bleibt davon unberührt."""
    ap = _autopilot()
    ap.cfg.sprint.pool_size = 5
    ap.leaders = [leader("0xa"), leader("0xb")]   # Hauptbuch: nur 2
    # Haupt-Score absichtlich GEGENLÄUFIG zum Sprint-Score: 0xf ist der
    # schwächste fürs Hauptbuch, aber der beste Richtungs-Trader
    ranked = [metrics("0xa", 60, sprint=40), metrics("0xb", 55, sprint=45),
              metrics("0xc", 50, sprint=50), metrics("0xd", 45, sprint=55),
              metrics("0xe", 40, sprint=60), metrics("0xf", 38, sprint=65)]
    pool = ap._build_sprint_pool(ranked)
    assert [p["address"] for p in pool[:5]] == ["0xf", "0xe", "0xd", "0xc", "0xb"], \
        "Richtungs-Score entscheidet, nicht der Haupt-Score"
    assert pool[0]["score"] == 65, "angezeigt wird der Richtungs-Score"
    # 0xa (bester Haupt-Score!) fiel aus den Top-5 - als Haupt-Leader kommt er
    # trotzdem rein
    assert "0xa" in [p["address"] for p in pool]
    assert len(pool) == 6 and len(ap.leaders) == 2, "Hauptbuch bleibt schmal"


def test_sprint_pool_always_contains_main_leaders():
    """Ein Bestands-Leader, den der Keep-Bonus trotz niedrigem Score hält, kann
    aus den rohen Top-N fallen - er MUSS trotzdem im Sprint-Pool sein, sonst
    scannt Sprint einen aktiven Copy-Leader nicht."""
    ap = _autopilot()
    ap.cfg.sprint.pool_size = 5
    ap.leaders = [leader("0xz")]   # Richtungs-Score nur 36 -> außerhalb Top-5
    ranked = [metrics("0xa", 60), metrics("0xb", 55), metrics("0xc", 50),
              metrics("0xd", 48), metrics("0xe", 46), metrics("0xz", 36)]
    pool = ap._build_sprint_pool(ranked)
    assert "0xz" in [p["address"] for p in pool], "Haupt-Leader immer im Pool"
    assert len(pool) == 6, "0xz zusätzlich zu den Top-5 angehängt"


def _autopilot_with_sprint(tmp):
    from bot.sprint import SprintBook
    ap = _autopilot()
    ap.sprint = SprintBook(ap.cfg.sprint, ap.cfg.backtest.fee_rate, runtime_dir=Path(tmp))
    return ap


def test_cmd_quest_pool_shows_confidence_progress_and_star_badge():
    """Nutzer-Fund (17.07.): '2 erfolgreiche Trader, aber ich hab nichts von
    Confidence-Punkten gesehen' - vorher zeigte /quest pool NUR das binäre
    ⭐ ab 100 Punkten, der Fortschritt davor war unsichtbar."""
    import tempfile

    from bot.sprint import STAR_THRESHOLD

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        progressing = "0x" + "1" * 40
        star = "0x" + "2" * 40
        plain = "0x" + "3" * 40
        ap.sprint.confidence[progressing] = 15
        ap.sprint.confidence[star] = STAR_THRESHOLD
        ap.sprint_leaders = [
            {"address": progressing, "score": 70},
            {"address": star, "score": 80},
            {"address": plain, "score": 60},
        ]
        out = ap._cmd_sprint("pool")
        assert f"🔸15/{STAR_THRESHOLD}" in out
        assert "⭐" in out
        assert "Confidence-Fortschritt" in out   # Legende nur, wenn wirklich einer fortschreitet


def test_cmd_quest_pool_shows_lighter_signals_when_promoted():
    """Nutzer-Entscheidung (17.07., Option B): Lighter-Leader stehen nie in
    self.sprint_leaders (nur ad-hoc pro Tick beigemischt, siehe
    _sprint_tick_inputs) - ohne eigene Anzeige wären sie in /quest pool
    unsichtbar, obwohl sie tatsächlich mitscannen."""
    import tempfile

    from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.cfg.lighter.sprint_promote = True
        ap.sprint_leaders = [{"address": "0x" + "1" * 40, "score": 70}]

        class _FakeLighter:
            def sprint_snapshots(self, prices, keep=None):
                return ([{"address": "lighter:42", "score": 15.0, "weight": 1.0}],
                        [LeaderSnapshot("lighter:42", 1000.0, {
                            "BTC": LeaderPosition(coin="BTC", size=1.0, entry=100.0,
                                                  position_value=1000.0, leverage=1)})])

        ap.lighter = _FakeLighter()
        out = ap._cmd_sprint("pool")
        assert "Lighter-Signale" in out
        assert "lighter:42" in out
        assert "📈" in out   # hält gerade eine Position


def test_cmd_quest_pool_omits_lighter_section_when_not_promoted():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.cfg.lighter.sprint_promote = False
        ap.sprint_leaders = [{"address": "0x" + "1" * 40, "score": 70}]

        class _BoomLighter:
            def sprint_snapshots(self, prices, keep=None):
                raise AssertionError("darf nicht aufgerufen werden, wenn sprint_promote aus ist")

        ap.lighter = _BoomLighter()
        out = ap._cmd_sprint("pool")
        assert "Lighter-Signale" not in out


def test_build_sprint_pool_evicts_banned_larp_next_candidate_rises():
    """Nutzer-Kernbefund: der Sinn der Strikes ist 'LARPs raus, neue Wallets
    rein'. Ein gebannter Leader darf keinen Pool-Slot mehr blockieren - der
    nächstbeste frische Kandidat rückt auf den frei gewordenen Platz nach."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.cfg.sprint.pool_size = 3
        ap.leaders = []
        ap.sprint.banned.add("0xb")   # wäre nach Score der Zweitbeste, aber LARP
        ranked = [metrics("0xa", 60), metrics("0xb", 55),
                  metrics("0xc", 50), metrics("0xd", 45)]
        pool = ap._build_sprint_pool(ranked)
    addrs = [p["address"] for p in pool]
    assert "0xb" not in addrs, "gebannter LARP fliegt aus dem Pool"
    assert addrs == ["0xa", "0xc", "0xd"], \
        "nächstbester (0xd) rückt auf den frei gewordenen Slot statt leer zu bleiben"


def test_build_sprint_pool_rotates_idle_wallets_to_the_back():
    """Nutzer: 'scannen scannen Daten'. Eine Wallet, die über Stunden kein
    Signal gab (idle), rutscht beim Rebuild nach hinten - frische Kandidaten
    kriegen Vorrang, auch wenn die Stumme einen höheren Score hätte."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.cfg.sprint.pool_size = 2
        ap.cfg.sprint.rotate_idle_hours = 6
        ap.leaders = []
        # 0xhigh hat den besten Score, ist aber seit Ewigkeiten stumm -> idle
        ap.sprint._last_active = {"0xhigh": 0.0}   # uralt = idle
        ranked = [metrics("0xhigh", 90), metrics("0xb", 50), metrics("0xc", 40)]
        pool = ap._build_sprint_pool(ranked)
    addrs = [p["address"] for p in pool]
    assert addrs == ["0xb", "0xc"], \
        "idle 0xhigh trotz Top-Score nach hinten, frische 0xb/0xc kriegen die Slots"


def test_build_sprint_pool_keeps_idle_when_no_fresh_candidates():
    """Self-balancing: gibt es nicht genug aktive/neue Kandidaten, rutscht die
    Stumme doch wieder rein - der Pool verhungert nie."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.cfg.sprint.pool_size = 3
        ap.cfg.sprint.rotate_idle_hours = 6
        ap.leaders = []
        ap.sprint._last_active = {"0xhigh": 0.0}
        pool = ap._build_sprint_pool([metrics("0xhigh", 90), metrics("0xb", 50)])
    assert "0xhigh" in [p["address"] for p in pool], \
        "nur 2 Kandidaten für 3 Slots -> die idle Wallet bleibt drin"


def test_build_sprint_pool_does_not_force_in_banned_main_leader():
    """Selbst ein Haupt-Leader wird NICHT in den Sprint-Pool gezwungen, wenn
    Sprint ihn gebannt hat - der Ban ist das speziellere, stärkere Urteil."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.cfg.sprint.pool_size = 5
        ap.leaders = [leader("0xz")]
        ap.sprint.banned.add("0xz")
        pool = ap._build_sprint_pool([metrics("0xa", 60), metrics("0xz", 36)])
    assert "0xz" not in [p["address"] for p in pool]


def test_cap_path_b_admission_caps_even_when_total_below_pool_size():
    """Live-Befund 18.07. (zweiter Fund): sprint_ok lag praktisch immer UNTER
    pool_size, deshalb griff die reine Nachrang-Sortierung im Pool-Ranking
    nie (kein Überangebot -> [:n] schneidet nichts ab). Der Deckel muss daher
    an der Admission selbst greifen, auch wenn insgesamt WENIGER Kandidaten
    da sind als pool_size Slots frei wären."""
    path_a = [metrics("0xa1", 50)]  # nur 1 Pfad-A-Wallet
    path_b = [metrics(f"0xb{i}", 90 - i) for i in range(10)]  # 10 Pfad-B-Sitzer
    # pool_size=20, max_share=0.35 -> b_cap = round(20*0.35) = 7, GREIFT trotzdem,
    # obwohl 1+10=11 < 20 (also ohne Deckel gäbe es gar keine Größenbeschränkung)
    admitted, ko = cap_path_b_admission(path_a, path_b, pool_size=20, max_share=0.35)
    assert ko == 3, "10 Pfad-B - 7 Deckel = 3 gedeckelt"
    admitted_addrs = {m.address for m in admitted}
    assert admitted_addrs == {"0xa1", "0xb0", "0xb1", "0xb2", "0xb3", "0xb4", "0xb5", "0xb6"}, \
        "Pfad-A komplett drin, von Pfad B nur die 7 bestbewerteten"


def test_cap_path_b_admission_keeps_all_path_b_when_under_cap():
    path_a = [metrics("0xa1", 50)]
    path_b = [metrics("0xb1", 80)]
    admitted, ko = cap_path_b_admission(path_a, path_b, pool_size=20, max_share=0.35)
    assert ko == 0
    assert {m.address for m in admitted} == {"0xa1", "0xb1"}


def test_build_sprint_pool_prefers_path_a_over_higher_scored_path_b():
    """Nutzer-Fund 18.07.: 11 von 18 Pool-Wallets qualifizierten nur über Pfad B
    (grünes offenes Buch, trips<=1) und können strukturell NIE ein frisches
    0->Position-Signal geben - sie fressen Pool-Slots ohne je Signale zu
    liefern. Pfad-A-Wallets (aktive Trader) müssen bei gleicher Slot-Anzahl
    vorgehen, auch wenn ein Pfad-B-Kandidat einen höheren Score hat."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.cfg.sprint.pool_size = 2
        ap.leaders = []
        m_a = metrics("0xactive", 50)
        m_a.sprint_qualify_path = "A"
        m_b_high = metrics("0xsitter", 90)
        m_b_high.sprint_qualify_path = "B"
        m_b_low = metrics("0xsitter2", 40)
        m_b_low.sprint_qualify_path = "B"
        pool = ap._build_sprint_pool([m_b_high, m_b_low, m_a])
    addrs = [p["address"] for p in pool]
    assert addrs == ["0xactive", "0xsitter"], \
        "Pfad-A (0xactive) geht trotz niedrigerem Score vor dem besser bewerteten Pfad-B (0xsitter) vor"


def test_build_sprint_pool_still_fills_with_path_b_when_no_path_a_left():
    """Self-balancing wie bei Idle-Rotation: gibt es nicht genug Pfad-A-Wallets,
    füllen Pfad-B-Kandidaten die restlichen Slots - kein harter Ausschluss."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.cfg.sprint.pool_size = 2
        ap.leaders = []
        m_b = metrics("0xsitter", 60)
        m_b.sprint_qualify_path = "B"
        pool = ap._build_sprint_pool([m_b])
    assert "0xsitter" in [p["address"] for p in pool]


def test_prune_banned_from_pool_evicts_and_forces_fresh_analysis():
    """Beim Start ist der Pool schon geladen, bevor das Sprint-Buch (mit Bans)
    existiert - _prune_banned_from_pool wirft die Gebannten nachträglich raus
    und fordert eine frische Analyse an, damit neue Wallets die freien Slots
    füllen statt den Pool bloß schrumpfen zu lassen."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap.leaders = []
        ap._force_analysis = False
        ap.sprint_leaders = [{"address": "0xa"}, {"address": "0xbad"}, {"address": "0xc"}]
        ap.sprint.banned.add("0xbad")
        ap._prune_banned_from_pool()
    assert [l["address"] for l in ap.sprint_leaders] == ["0xa", "0xc"], "0xbad geworfen"
    assert ap._force_analysis is True, "frische Analyse angefordert (neue Wallets rein)"


def test_prune_banned_from_pool_noop_without_bans():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ap = _autopilot_with_sprint(tmp)
        ap._force_analysis = False
        ap.sprint_leaders = [{"address": "0xa"}, {"address": "0xc"}]
        ap._prune_banned_from_pool()
    assert [l["address"] for l in ap.sprint_leaders] == ["0xa", "0xc"]
    assert ap._force_analysis is False, "ohne Bans keine unnötige Analyse-Anforderung"


def test_tracked_addresses_union_no_duplicates():
    ap = _autopilot()
    ap.cfg.sprint.enabled = True
    ap.leaders = [leader("0xa"), leader("0xb")]
    ap.sprint_leaders = [{"address": "0xa"}, {"address": "0xc"}, {"address": "0xd"}]
    assert ap._tracked_addresses() == ["0xa", "0xb", "0xc", "0xd"], \
        "Haupt-Leader zuerst, dann neue Sprint-Adressen, 0xa nicht doppelt"


def test_tracked_addresses_sprint_off_stays_narrow():
    """Sprint aus -> keine Extra-Adressen im Tracker (keine Extra-API-Last)."""
    ap = _autopilot()
    ap.cfg.sprint.enabled = False
    ap.leaders = [leader("0xa"), leader("0xb")]
    ap.sprint_leaders = [{"address": "0xc"}, {"address": "0xd"}]
    assert ap._tracked_addresses() == ["0xa", "0xb"]


def test_force_analysis_runs_in_background_once_and_pushes_result():
    """/analyze setzt nur ein Flag; der Loop startet die Analyse EINMAL im
    HINTERGRUND (sie blockierte vorher minutenlang den ganzen Loop - Copier
    und Sprint waren blind) und pusht danach das Ergebnis."""
    import time as _t

    ap = _autopilot()
    calls, sent = [], []

    def fake_reanalyze():
        calls.append(1)
        ap._analysis_note = "50 Kandidaten → 3/9"
        ap._analysis_running = False   # wie das finally des echten _reanalyze

    ap._reanalyze = fake_reanalyze
    ap.notifier.send = sent.append
    ap._last_analysis = _t.time()   # Zeit-Trigger aus - nur das Flag zählt
    ap._maybe_reanalyze()
    assert calls == [], "ohne Flag und ohne Fälligkeit keine Analyse"
    ap._force_analysis = True
    ap._maybe_reanalyze()
    assert ap._analysis_thread is not None, "läuft im eigenen Thread, nicht im Loop"
    ap._analysis_thread.join(timeout=5)
    assert calls == [1], "Flag löst genau eine Analyse aus"
    assert ap._force_analysis is False, "Flag wird konsumiert"
    assert sent and "Analyse fertig" in sent[0] and "50 Kandidaten" in sent[0], \
        "erzwungene Analyse pusht ihr Ergebnis"
    ap._maybe_reanalyze()
    if ap._analysis_thread:
        ap._analysis_thread.join(timeout=5)
    assert calls == [1] and len(sent) == 1, "kein Dauerfeuer, kein Push-Spam"


def test_no_parallel_analysis_while_running():
    """Loop-Tick während laufender Analyse darf keinen zweiten Lauf starten -
    zwei parallele Läufe würden sich die Leader-Listen zerschreiben."""
    import time as _t

    ap = _autopilot()
    calls = []
    ap._reanalyze = lambda: calls.append(1)   # setzt running NICHT zurück
    ap.notifier.send = lambda *a: None
    ap._last_analysis = 0.0                   # Zeit-Trigger AN (uralt)
    ap._analysis_running = True               # Analyse "läuft gerade"
    ap._maybe_reanalyze()
    assert calls == [], "kein Parallel-Start während laufender Analyse"


def test_cmd_analyze_sets_flag_only_when_running():
    ap = _autopilot()
    assert "läuft nicht" in ap._cmd_analyze(), "ohne laufenden Autopilot nur Hinweis"
    assert ap._force_analysis is False

    class _T:
        def is_alive(self): return True

    ap._thread = _T()   # running-Property simulieren
    out = ap._cmd_analyze()
    assert ap._force_analysis is True and "angestoßen" in out
    ap._force_analysis = False
    ap._analysis_running = True     # Doppel-Anstoß während laufender Analyse
    ap._analysis_started = __import__("time").time() - 120
    out = ap._cmd_analyze()
    assert "läuft bereits" in out and ap._force_analysis is False


def test_cmd_fullreport_without_notifier_shows_hint():
    ap = _autopilot()
    ap.notifier.token = ap.notifier.chat_id = ""   # enabled ist eine reine Property
    assert "Telegram-Push" in ap._cmd_fullreport()


def test_cmd_fullreport_chunks_and_pushes_with_headers():
    """Voller Report läuft im Hintergrund, wird gechunkt und OHNE HTML-Parsing
    gepusht (html=False) - Freitext mit '<'/'>' darf nie wieder Funkstille
    erzeugen. Jeder Chunk trägt einen 'Report i/N'-Header."""
    import report as report_mod

    ap = _autopilot()
    ap.notifier.token, ap.notifier.chat_id = "t", "42"   # enabled ist eine reine Property
    sent = []
    ap.notifier.send = lambda text, html=True: sent.append((text, html))

    long_text = "\n".join(f"Zeile {i}: " + "x" * 100 for i in range(120))
    orig_build = report_mod.build_report
    calls = []
    report_mod.build_report = lambda offline=False: (calls.append(offline), long_text)[1]
    try:
        out = ap._cmd_fullreport()
        assert "wird gebaut" in out
        ap._fullreport_thread.join(timeout=10)
    finally:
        report_mod.build_report = orig_build

    assert calls == [False], "ohne Arg -> online (nicht offline)"
    assert len(sent) > 1, "langer Text muss in mehreren Nachrichten ankommen"
    assert all(html is False for _, html in sent), "Report-Text ohne HTML-Parsing gesendet"
    assert sent[0][0].startswith("📊 Report 1/")
    assert sent[-1][0].startswith(f"📊 Report {len(sent)}/{len(sent)}")


def test_cmd_fullreport_no_double_start_while_running():
    """Verifikations-Fund: ohne Sperre startet ein Doppel-Tap (naheliegend in
    der Panik-Situation, für die das Feature gebaut ist) einen zweiten,
    parallelen Netz-Lauf. Jetzt: laufender Report -> zweiter Aufruf startet
    keinen neuen Thread, sondern meldet 'läuft bereits'."""
    ap = _autopilot()
    ap.notifier.token, ap.notifier.chat_id = "t", "42"
    ap.notifier.send = lambda text, html=True: None
    ap._fullreport_running = True   # simuliert: erster Lauf ist noch aktiv
    out = ap._cmd_fullreport()
    assert "läuft bereits" in out
    assert ap._fullreport_thread is None, "kein zweiter Thread wurde gestartet"


def test_cmd_fullreport_offline_arg_passed_through():
    import report as report_mod

    ap = _autopilot()
    ap.notifier.token, ap.notifier.chat_id = "t", "42"   # enabled ist eine reine Property
    ap.notifier.send = lambda text, html=True: None
    calls = []
    orig_build = report_mod.build_report
    report_mod.build_report = lambda offline=False: (calls.append(offline), "kurzer Text")[1]
    try:
        out = ap._cmd_fullreport("offline")
        assert "offline" in out
        ap._fullreport_thread.join(timeout=10)
    finally:
        report_mod.build_report = orig_build
    assert calls == [True]


def test_cmd_quest_cohorts_groups_source_reason_asset():
    """Masterplan Phase C (20.07.): Kohorten-Auswertung aus dem VOLLEN Journal -
    Quelle (HL vs Lighter), Exit-Grund, Asset-Klasse, Top/Flop-Leader. Damit
    fallen Steuerfragen (Lighter zulassen? Zeit-Cut richtig?) aus Daten."""
    import tempfile

    from bot.journal import Journal

    class FakeSprint:
        won, busted = 3, 2          # aktuelle Ära = 5 Zyklen
        strikes, banned, confidence = {}, set(), {}

    ap = _autopilot()
    ap.sprint = FakeSprint()
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(path=Path(tmp) / "trades.jsonl")
        j.record("sprint_cycle_end", coin="BTC", pnl=-999.0, leader="0xold")  # alte Ära
        j.record("sprint_tp", coin="ETH", pnl=150.0, leader="0xgood", reason="tp")
        j.record("sprint_cycle_end", coin="SOL", pnl=-60.0, leader="lighter:111",
                 reason="zeit_negativ")
        j.record("sprint_cycle_end", coin="HYPE", pnl=-40.0, leader="lighter:111",
                 reason="leader_flip")
        j.record("sprint_tp", coin="PUMP", pnl=80.0, leader="0xgood", reason="tp")
        j.record("sprint_cycle_end", coin="xyz:MU", pnl=20.0, leader="0xgood",
                 reason="plus_lock")
        ap.journal = j
        out = ap._cmd_quest_cohorts()

    assert "aktuelle Ära, 5 Zyklen" in out
    assert "Hyperliquid: 3x, 100% grün, +250 $" in out
    assert "Lighter: 2x, 0% grün, -100 $" in out
    assert "tp: 2x, 100% grün, +230 $" in out
    assert "zeit_negativ: 1x" in out
    assert "Aktien: 1x, 100% grün, +20 $" in out
    assert "0xgood" in out and "lighter:111" in out, "Top/Flop-Leader gelistet"
    assert "-999" not in out, "alte Ära bleibt draußen"


def test_sprint_asset_breakdown_scoped_to_current_era():
    """Krypto vs. Aktien NUR für die aktuelle Ära (letzte won+busted Zyklen).
    Alte Mess-Woche-Zyklen davor werden ausgeschlossen - genau das 'Krypto/
    Aktien muss reset werden' des Nutzers, ohne Journal zu löschen."""
    import tempfile

    from bot.journal import Journal

    class FakeSprint:
        won, busted = 2, 1          # aktuelle Ära = 3 Zyklen
        strikes, banned, confidence = {}, set(), {}

    ap = _autopilot()
    ap.sprint = FakeSprint()
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(path=Path(tmp) / "trades.jsonl")
        # 2 alte Ära-Zyklen (müssen RAUS aus der Auswertung)
        j.record("sprint_cycle_end", coin="BTC", pnl=-999.0, leader="0xold")
        j.record("sprint_cycle_end", coin="BTC", pnl=-999.0, leader="0xold")
        # aktuelle Ära: 3 Zyklen (Krypto x2, Aktie x1)
        j.record("sprint_tp", coin="ETH", pnl=100.0, leader="0xa")
        j.record("sprint_bust", coin="xyz:INTC", pnl=-50.0, leader="0xb")
        j.record("sprint_cycle_end", coin="SOL", pnl=30.0, leader="0xa")
        ap.journal = j
        out = ap._sprint_asset_breakdown()

    assert "aktuelle Ära, 3 Zyklen" in out
    assert "Krypto: 2 Zyklen, PnL +130.00 $ (Trefferquote 100%)" in out
    assert "Aktien: 1 Zyklen, PnL -50.00 $ (Trefferquote 0%)" in out
    assert "-999" not in out, "alte Ära-Zyklen dürfen NICHT mehr auftauchen"


def test_sprint_reset_zeroes_bilanz_keeps_strikes():
    """/sprint reset - Nutzer-Wunsch nach einem manuellen 'clean sheet' (z.B.
    wenn ein Forced-Close vor dem Reset-Zeitpunkt die frische Bilanz
    verunreinigt hat): Zyklus/Schatztruhe auf 0, Strikes/Bans/Confidence
    bleiben - das ist erprobtes LARP-Wissen, keine Bilanz-Zahl."""
    import tempfile

    from bot.sprint import SprintBook

    ap = _autopilot()
    with tempfile.TemporaryDirectory() as tmp:
        ap.sprint = SprintBook(ap.cfg.sprint, ap.cfg.backtest.fee_rate, runtime_dir=Path(tmp))
        ap.sprint.won, ap.sprint.busted, ap.sprint.banked = 1, 3, -200.57
        ap.sprint.strikes["0xbad"] = 1
        ap.sprint.banned.add("0xzzz")
        out = ap._cmd_sprint("reset")
    assert "zurückgesetzt" in out
    assert ap.sprint.won == 0 and ap.sprint.busted == 0 and ap.sprint.banked == 0.0
    assert ap.sprint.strikes.get("0xbad") == 1
    assert "0xzzz" in ap.sprint.banned


def test_sprint_asset_breakdown_empty_journal():
    import tempfile

    from bot.journal import Journal

    class FakeSprint:
        won = busted = 0
        strikes, banned, confidence = {}, set(), {}

    ap = _autopilot()
    ap.sprint = FakeSprint()
    with tempfile.TemporaryDirectory() as tmp:
        ap.journal = Journal(path=Path(tmp) / "trades.jsonl")
        out = ap._sprint_asset_breakdown()
    assert "keine abgeschlossenen" in out


def test_stale_feed_warns_once_and_recovers():
    """Audit-Befund: friert der Copier ein (halted/Störung), scannt Sprint
    Standbilder und 'wartet auf frisches Signal' sieht gesund aus. Jetzt: eine
    Warnung bei >5 min alten Snapshots, Entwarnung wenn wieder frisch."""
    import time as _t

    ap = _autopilot()

    class _Cop:
        last_snapshots_t = _t.time() - 400   # 6.7 min alt

    ap.copier = _Cop()
    sent = []
    ap.notifier.send = sent.append
    ap._maybe_warn_stale_feed()
    ap._maybe_warn_stale_feed()
    assert sum("eingefroren" in m for m in sent) == 1, "genau EINE Warnung, kein Spam"
    ap.copier.last_snapshots_t = _t.time()   # Feed wieder frisch
    ap._maybe_warn_stale_feed()
    assert any("wieder frisch" in m for m in sent), "Entwarnung kommt"
    ap.copier.last_snapshots_t = _t.time() - 400
    ap._maybe_warn_stale_feed()
    assert sum("eingefroren" in m for m in sent) == 2, "erneutes Einfrieren warnt wieder"


def test_status_shows_running_and_pending_analysis():
    """Während der minutenlangen Analyse muss /status 'läuft' zeigen - vorher
    stand dort stur 'vor 3.2h' und sah aus wie 'nichts passiert' (Live-Bug)."""
    import time as _t

    ap = _autopilot()
    ap._analysis_running = True
    ap._analysis_started = _t.time() - 180
    ap._analysis_note = "alte Notiz"
    s = ap._cmd_status()
    assert "LÄUFT seit 3 min" in s
    assert "alte Notiz" not in s, "alte Trichter-Zeile nicht als aktuell ausgeben"
    ap._analysis_running = False
    ap._force_analysis = True
    s = ap._cmd_status()
    assert "angefordert" in s


def test_status_shows_analysis_funnel_and_pool():
    ap = _autopilot()
    import time as _t

    ap._last_analysis = _t.time() - 3600   # vor 1h
    ap._analysis_note = "50 Kandidaten → 1 Haupt(≥35) / 9 Pool(≥25)"
    ap.leaders = [leader("0xa")]
    ap.sprint_leaders = [{"address": a} for a in ("0xa", "0xb", "0xc")]
    s = ap._cmd_status()
    assert "vor 1.0h" in s and "Kandidaten" in s
    assert "Pool: 3" in s, "Sprint-Pool-Größe sichtbar"


def test_setup_retries_until_success():
    """Setup-Fehler (z.B. 429 beim Hochfahren) dürfen den Autopilot nie endgültig
    töten: er probiert mit Backoff weiter und läuft beim nächsten Erfolg los."""
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())
    attempts = {"n": 0}

    def flaky_setup():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("(429, None, 'null', None, {...})")
        ap._stop.set()  # Erfolg: Loop direkt wieder beenden, kein echtes Setup nötig

    sent = []
    ap._setup = flaky_setup
    ap.notifier.send = sent.append
    ap._stop_wait_orig = ap._stop.wait
    ap._stop.wait = lambda t=None: ap._stop.is_set()  # Backoff im Test nicht schlafen
    ap._run()
    assert attempts["n"] == 3, "muss nach Fehlschlägen weiterprobieren"
    assert any("fehlgeschlagen" in s for s in sent), "erster Fehlschlag muss alarmieren"
    assert sum("fehlgeschlagen" in s for s in sent) == 1, "aber nur einmal (kein Spam)"
    assert any("geglückt" in s for s in sent)
    assert ap.status()["state"] == "stopped"


def test_setup_retry_stop_aborts():
    """Stop-Signal während des Retry-Backoffs beendet den Thread sauber."""
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())

    def failing_setup():
        raise RuntimeError("API down")

    ap._setup = failing_setup
    ap.notifier.send = lambda *a, **k: None
    ap._stop.wait = lambda t=None: True  # Nutzer stoppt während des Wartens
    ap._run()
    assert ap.status()["state"] == "stopped"
    assert "API down" in (ap.error or "")


def test_analyzer_retries_on_429():
    """429 vom Rate-Limit wird mit Backoff wiederholt statt die Wallet zu verwerfen."""
    from bot.copytrade.analyzer import TraderAnalyzer

    an = TraderAnalyzer(info=None, days=21, throttle_s=0)
    calls = {"n": 0}

    def rate_limited_then_ok(addr):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception((429, None, "null", None, {}))
        return {"ok": True}

    import time as _time
    naps = []
    orig_sleep = _time.sleep
    _time.sleep = naps.append
    try:
        out = an._call(rate_limited_then_ok, "0xabc")
    finally:
        _time.sleep = orig_sleep
    assert out == {"ok": True}
    assert calls["n"] == 3
    assert naps == [2.0, 4.0], "exponentieller Backoff"

    def always_500(addr):
        raise RuntimeError("HTTP 500 kaputt")

    try:
        an._call(always_500, "0xabc")
        assert False, "Nicht-429-Fehler müssen sofort durchschlagen"
    except RuntimeError:
        pass


def test_cmd_positions_shows_quest_only():
    """Quest-Bot-Umstellung: /positions zeigt NUR noch den Quest-Bot, kein
    Kopier-Buch/Labs mehr (die sind stillgelegt)."""
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())
    ap.sprint = None
    assert "nichts" in ap._cmd_positions().lower(), "leer: Quest-Bot hält nichts"

    class FakePaper:
        def position_rows(self, prices):
            return [{"coin": "BTC", "size": 0.5, "entry": 100.0, "unrealized_pnl": 12.0}]

    class FakeSprint:
        paper = FakePaper()

    class C:
        last_prices = {"BTC": 101.0}

    ap.sprint = FakeSprint()
    ap.copier = C()
    msg = ap._cmd_positions()
    assert "Quest-Bot" in msg and "LONG BTC" in msg
    assert "12" in msg


def test_cmd_update_already_current():
    import bot.autopilot as ap_mod
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())

    class R:
        returncode, stdout, stderr = 0, "Already up to date.\n", ""

    orig = ap_mod.subprocess.run
    ap_mod.subprocess.run = lambda *a, **k: R()
    try:
        msg = ap._cmd_update()
    finally:
        ap_mod.subprocess.run = orig
    assert "aktuell" in msg.lower()


def test_cmd_update_failure_reported():
    import bot.autopilot as ap_mod
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())

    class R:
        returncode, stdout, stderr = 1, "", "local changes would be overwritten"

    orig = ap_mod.subprocess.run
    ap_mod.subprocess.run = lambda *a, **k: R()
    try:
        msg = ap._cmd_update()
    finally:
        ap_mod.subprocess.run = orig
    assert "fehlgeschlagen" in msg.lower()


def test_cmd_update_success_schedules_restart():
    import bot.autopilot as ap_mod
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())

    class R:
        returncode, stdout, stderr = 0, "Updating a1b2..c3d4\n 3 files changed", ""

    captured = {}

    class FakeTimer:  # echten Exit-Timer NICHT starten
        def __init__(self, t, fn):
            captured["fn"] = fn

        def start(self):
            captured["started"] = True

    orig_run, orig_timer = ap_mod.subprocess.run, ap_mod.threading.Timer
    ap_mod.subprocess.run = lambda *a, **k: R()
    ap_mod.threading.Timer = FakeTimer
    try:
        msg = ap._cmd_update()
    finally:
        ap_mod.subprocess.run = orig_run
        ap_mod.threading.Timer = orig_timer
    assert "starte neu" in msg.lower() and captured.get("started")


def test_llm_merge_takes_maximum():
    """KI-Score überschreibt Keyword-Score nur, wenn er höher ist."""
    from bot.news.sentiment import score_item
    from bot.news.sources import NewsItem

    kw_hit = score_item(NewsItem("t", "Markets crash as exchange halts withdrawals", 0))
    kw_miss = score_item(NewsItem("t", "Federal Reserve schedules unscheduled sunday meeting", 0))
    assert kw_hit.score >= 8
    assert kw_miss.score < 4  # Keyword-Engine erkennt das nicht

    # Simulierter Claude-Merge wie in MarketGuard._poll_news
    scored = [kw_hit, kw_miss]
    llm_result = {0: (5.0, "exchange halt"), 1: (8.0, "emergency fed meeting implies crisis")}
    for idx, (risk, reason) in llm_result.items():
        if risk > scored[idx].score:
            scored[idx].score = risk
            scored[idx].matched.append(f"claude:{reason[:80]}")

    assert scored[0].score >= 8, "Keyword-Score (höher) darf nicht abgesenkt werden"
    assert scored[1].score == 8.0, "KI muss die Keyword-Lücke schließen"
    assert any(m.startswith("claude:") for m in scored[1].matched)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
