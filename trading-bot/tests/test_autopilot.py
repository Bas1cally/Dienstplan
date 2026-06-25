"""Unit-Tests für Leader-Rotation und KI-News-Merge (ohne Netzwerk)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.autopilot import rotate_leaders
from bot.copytrade.analyzer import TraderMetrics


def metrics(addr, score, roi=0.05):
    m = TraderMetrics(address=addr, account_value=50000, days=30)
    m.score = score
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


def test_cmd_positions():
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())
    assert "keine offenen" in ap._cmd_positions().lower(), "leer: keine Positionen"

    ap._status = {"positions": [
        {"coin": "BTC", "size": 0.5, "entry": 60_000, "unrealized_pnl": 120.0},
        {"coin": "ETH", "size": -2.0, "entry": 3_000, "unrealized_pnl": -15.0},
    ]}

    class C:
        last_prices = {"BTC": 61_000, "ETH": 3_000}

    ap.copier = C()
    ap.labs = None
    msg = ap._cmd_positions()
    assert "LONG BTC" in msg and "SHORT ETH" in msg
    assert "Σ unrealisiert" in msg and "+105" in msg.replace(",", "")  # 120 - 15


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
