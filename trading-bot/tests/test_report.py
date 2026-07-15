"""Unit-Tests: Report-Engine, Empfehlungs-Regeln, Watchdog-Diagnose."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.autopilot import chunk_for_telegram, diagnose_inactivity
from bot.report import recommendations, summarize, veto_outcomes


def order(t, coin="BTC"):
    return {"t": t, "kind": "order", "coin": coin, "side": "BUY", "size": 0.01, "price": 100_000}


def veto(t, coin="BTC", side="LONG", price=100_000, reasons=None):
    return {"t": t, "kind": "veto", "coin": coin, "side": side, "price": price,
            "reasons": reasons or ["15m-Trend gegen Long"]}


def history(days, start=10_000, end=10_000):
    n = max(days * 24, 2)
    return [{"t": i * 3600, "equity": start + (end - start) * i / (n - 1)} for i in range(n)]


# ---------- summarize ----------

def test_summarize_basic():
    journal = [order(100), order(200), veto(300), veto(400, reasons=["VETO: RSI 82 überkauft"]),
               {"t": 500, "kind": "scalp_close", "pnl": 12.5}]
    s = summarize(journal, history(7, 10_000, 10_300), {"fees_paid": 5.0, "realized_pnl": 295.0})
    assert s["orders"] == 2 and s["vetoes"] == 2
    assert s["veto_per_order"] == 1.0
    assert s["scalp_pnl"] == 12.5
    assert s["return_pct"] == 3.0
    assert s["veto_reasons"] == {"trend_gegen_richtung": 1, "rsi_extrem": 1}
    assert 0 < s["fee_share_pct"] < 5


def test_summarize_empty_safe():
    s = summarize([], [], None)
    assert s["orders"] == 0 and s["veto_per_order"] == 0.0


# ---------- veto_outcomes ----------

def test_veto_outcomes_directional():
    # Long-Veto, Preis steigt 2% -> Veto war falsch; Short-Veto, Preis steigt -> Veto war richtig
    vetoes = [veto(1000, side="LONG", price=100.0), veto(2000, side="SHORT", price=100.0)]
    price_fn = lambda coin, t: 102.0  # nach Horizont immer 102
    stats = veto_outcomes(vetoes, price_fn, horizon_hours=24)
    assert stats["evaluated"] == 2
    assert abs(stats["avg_return_pct"] - 0.0) < 1e-9  # +2% und -2% mitteln sich
    assert stats["win_share"] == 0.5


def test_veto_outcomes_missing_prices_skipped():
    stats = veto_outcomes([veto(1000)], lambda c, t: None, 24)
    assert stats["evaluated"] == 0 and stats["avg_return_pct"] == 0.0


def test_veto_outcomes_evaluates_matured_not_just_recent():
    """Regression: bei vielen frischen Signalen müssen die AUSGEREIFTEN bewertet
    werden, nicht die jüngsten (deren Zukunftspreis noch nicht existiert)."""
    H = 24 * 3600
    now = 1_000_000
    # 2H auseinander -> eigenständige Episoden (Dedup lässt sie stehen), alle reif
    matured = [veto(now - (20 - 2 * i) * H, side="LONG", price=100.0) for i in range(5)]
    fresh = [veto(now + i, side="LONG", price=100.0) for i in range(200)]

    def price_fn(coin, t):
        return 102.0 if t <= now else None  # Zukunft (frische Einträge) nicht abrufbar

    stats = veto_outcomes(matured + fresh, price_fn, horizon_hours=24, max_samples=60)
    assert stats["evaluated"] == 5, "ausgereifte Einträge trotz 200 frischer bewerten"


# ---------- recommendations ----------

def test_recommends_more_leaders_when_zero_orders():
    s = summarize([veto(i) for i in range(5)], history(3), None)
    recs = recommendations(s)
    assert any("top_percent" in r for r in recs)


def test_recommends_loosening_validator_when_vetoes_profitable():
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(5), None)
    veto_stats = {"evaluated": 20, "avg_return_pct": 0.8, "win_share": 0.7,
                  "significant": True, "horizon_hours": 24}
    recs = recommendations(s, veto_stats)
    assert any("min_score" in r for r in recs)


def test_keeps_validator_when_vetoes_lossy():
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(5), None)
    veto_stats = {"evaluated": 20, "avg_return_pct": -0.9, "win_share": 0.3,
                  "significant": True, "horizon_hours": 24}
    recs = recommendations(s, veto_stats)
    assert any("rettet" in r for r in recs)
    assert not any("min_score 2 -> 1" in r for r in recs)


def test_validator_conflict_surfaced_not_two_recs():
    """Der reale 6-Tage-Fall: Veto-Outcome sagt 'rettet', Shadow sagt 'schadet'.
    Statt zweier gegensätzlicher Ratschläge muss EIN Widerspruchs-Hinweis kommen."""
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(6), None)
    veto_stats = {"evaluated": 55, "avg_return_pct": -1.44, "win_share": 0.33,
                  "significant": True, "horizon_hours": 24}
    shadow_stats = {"baseline": 10_140, "variants": {"ohne_validator": {"equity": 10_256, "trades": 60}}}
    recs = recommendations(s, veto_stats, shadow_stats)
    assert any("WIDERSPRECHEN" in r for r in recs)
    assert not any("enabled:false" in r.replace(" ", "") for r in recs), "kein Abschalt-Rat bei Widerspruch"
    assert sum("Validator" in r or "Filter" in r for r in recs) == 1, "genau EIN Validator-Urteil"


def test_validator_both_agree_hurts_short_sample_is_cautious():
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(6), None)
    veto_stats = {"evaluated": 55, "avg_return_pct": 0.8, "win_share": 0.7,
                  "significant": True, "horizon_hours": 24}
    shadow_stats = {"baseline": 10_000, "variants": {"ohne_validator": {"equity": 10_300, "trades": 60}}}
    recs = recommendations(s, veto_stats, shadow_stats)
    assert any("min_score 2 -> 1" in r for r in recs)
    assert any("enabled:false" in r.replace(" ", "") for r in recs), "muss VOR enabled:false warnen"
    assert any("Stressphase" in r or "Abverkauf" in r for r in recs)


def test_validator_both_agree_helps():
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(6), None)
    veto_stats = {"evaluated": 55, "avg_return_pct": -1.0, "win_share": 0.3,
                  "significant": True, "horizon_hours": 24}
    shadow_stats = {"baseline": 10_000, "variants": {"ohne_validator": {"equity": 9_800, "trades": 60}}}
    recs = recommendations(s, veto_stats, shadow_stats)
    assert any("rettet" in r and "behalten" in r for r in recs)
    assert not any("WIDERSPRECHEN" in r for r in recs)


def test_insignificant_veto_outcome_gives_no_verdict():
    """Kernfix: ein nicht-signifikantes Veto-Outcome darf KEIN Urteil treiben."""
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(6), None)
    veto_stats = {"evaluated": 55, "avg_return_pct": 0.06, "win_share": 0.5,
                  "significant": False, "horizon_hours": 24}
    recs = recommendations(s, veto_stats)  # ohne Shadow -> kein Signal -> kein Validator-Satz
    assert not any("Validator" in r or "Filter" in r for r in recs)


def test_return_stats_significance():
    from bot.report import _return_stats
    # enges, klar positives Sample -> signifikant
    tight = _return_stats([0.012, 0.011, 0.013, 0.012, 0.011, 0.012, 0.013], 24)
    assert tight["significant"] and tight["ci_low_pct"] > 0
    # verrauschtes Sample um 0 -> nicht signifikant
    noisy = _return_stats([0.05, -0.04, 0.03, -0.05, 0.04, -0.03], 24)
    assert not noisy["significant"]


def test_veto_window_rolls_to_newest():
    """Regression gegen eingefrorene Zahlen: das Fenster nimmt die NEUESTEN
    gereiften Episoden, nicht für immer die ältesten vom Journal-Anfang."""
    H = 24 * 3600
    now = 10_000_000
    old = [veto(now - (60 - 3 * i) * H, side="LONG", price=90.0) for i in range(8)]   # alt, Einstieg 90
    new = [veto(now - (24 - 3 * i) * H, side="LONG", price=110.0) for i in range(8)]  # neu, Einstieg 110
    price_fn = lambda coin, t: 100.0 if t <= now else None
    stats = veto_outcomes(old + new, price_fn, horizon_hours=24, max_samples=5)
    assert stats["avg_return_pct"] < 0, "rollendes Fenster -> neueste (Einstieg 110, LONG) dominieren"
    assert stats["window_to"] > stats["window_from"]


def test_dedup_veto_collapses_despite_price_jitter():
    """Dieselbe Blockade wird je Tick zu leicht anderem Mid geloggt - sie darf
    trotzdem als EINE Episode zählen (Preis nicht mehr im Dedup-Key)."""
    from bot.report import _dedup_episodes
    base = 1_000_000
    jitter = [{"coin": "BTC", "side": "LONG", "price": 100 + i * 0.01, "t": base + i * 120}
              for i in range(20)]
    assert len(_dedup_episodes(jitter, 24)) == 1


def test_dedup_collapses_repeated_episodes():
    from bot.report import _dedup_episodes
    H = 24
    base = 1_000_000
    # dieselbe Wallet/Coin/Seite 5x im Fenster -> 1 Episode
    rep = [{"coin": "BTC", "side": "LONG", "address": "0xa", "t": base + i * 3600} for i in range(5)]
    assert len(_dedup_episodes(rep, H)) == 1
    # nach >Horizont kommt eine neue Episode
    rep.append({"coin": "BTC", "side": "LONG", "address": "0xa", "t": base + 30 * 3600})
    assert len(_dedup_episodes(rep, H)) == 2


def test_recommends_fee_reduction():
    journal = [order(i) for i in range(20)]
    s = summarize(journal, history(7, 10_000, 10_010), {"fees_paid": 50.0, "realized_pnl": 10.0})
    assert any("rebalance_threshold" in r for r in recommendations(s))


def test_fee_rec_config_aware_when_throttle_active():
    """Drossel schon aktiv (threshold >= 0.03): keine 'erhöhen'-Empfehlung mehr,
    sondern Hinweis auf die verzerrte Quote + Fees/Tag als ehrliche Zahl."""
    journal = [order(i) for i in range(20)]
    s = summarize(journal, history(7, 10_000, 10_010), {"fees_paid": 50.0, "realized_pnl": 10.0})
    recs = recommendations(s, cfg_hint={"rebalance_threshold": 0.035, "poll_seconds": 20})
    assert not any("rebalance_threshold erhöhen" in r for r in recs)
    assert any("verzerrt" in r and "Fees/Tag" in r for r in recs)
    assert s["fees_per_day"] == round(50.0 / s["days"], 2)


def test_recommends_disabling_losing_scalper():
    journal = [order(1)] + [{"t": i, "kind": "scalp_close", "pnl": -5.0} for i in range(6)]
    s = summarize(journal, history(3), None)
    assert any("scalp.enabled" in r for r in recommendations(s))


def test_flat_run_warns_against_leverage():
    journal = [order(i) for i in range(30)]
    s = summarize(journal, history(10, 10_000, 10_010), None)
    recs = recommendations(s)
    assert any("NICHT einfach Leverage" in r for r in recs)


def test_healthy_run_no_noise():
    journal = [order(i * 3600) for i in range(50)] + [veto(99)]
    s = summarize(journal, history(7, 10_000, 10_600), {"fees_paid": 20.0, "realized_pnl": 600.0})
    recs = recommendations(s)
    assert recs == ["Keine Auffälligkeiten - weiterlaufen lassen und Stichprobe wachsen lassen."]


# ---------- build_report (voller Report, /fullreport per Telegram) ----------

def test_build_report_no_data():
    import report as report_mod

    with tempfile.TemporaryDirectory() as tmp:
        text = report_mod.build_report(offline=True, runtime_dir=Path(tmp))
    assert "Noch keine Daten" in text


def test_build_report_offline_includes_all_active_tracks():
    """Volle Fixture (Haupt-Buch + Sprint + Vetos) -> alle Abschnitte im
    Text vorhanden, identisch zur bisherigen CLI-Ausgabe (Zeilen/Labels)."""
    import report as report_mod

    with tempfile.TemporaryDirectory() as tmp:
        rt = Path(tmp)
        trades = [order(i * 3600) for i in range(5)]
        trades.append(veto(999, coin="ETH"))
        (rt / "trades.jsonl").write_text("\n".join(json.dumps(t) for t in trades))
        (rt / "history.jsonl").write_text(
            "\n".join(json.dumps(h) for h in history(2, 10_000, 10_235)))
        (rt / "paper_state.json").write_text(json.dumps(
            {"trades": 5, "realized_pnl": 120.5, "fees_paid": 3.2, "initial_equity": 10_000}))
        (rt / "sprint_cycles.json").write_text(json.dumps(
            {"won": 3, "busted": 2, "banked": 252.89,
             "strikes": {"0xfd688aed": 2}, "banned": ["0xfd688aed"]}))
        (rt / "sprint_book.json").write_text(json.dumps(
            {"initial_equity": 1000, "realized_pnl": 0, "trades": 0}))
        text = report_mod.build_report(offline=True, runtime_dir=rt)

    assert "=== Paper-Lauf-Report ===" in text
    assert "Realisierter PnL" in text and "+120.50" in text
    assert "Sprint-Buch" in text and "banked +252.89" in text
    assert "Strikes {'0xfd688aed': 2}" in text and "gesperrt ['0xfd688aed']" in text
    assert "=== Empfehlungen ===" in text
    assert "Bewerte geblockte Trades" not in text, "offline darf keine Netz-Analyse anstoßen"


def test_build_report_offline_skips_network_veto_analysis():
    """offline=True darf NIE versuchen, HyperliquidClient/Preise zu holen -
    sonst würde /fullreport offline auf dem Server unnötig Netz-Last erzeugen."""
    import report as report_mod

    with tempfile.TemporaryDirectory() as tmp:
        rt = Path(tmp)
        trades = [order(1)] + [veto(i + 10) for i in range(5)]
        (rt / "trades.jsonl").write_text("\n".join(json.dumps(t) for t in trades))
        (rt / "history.jsonl").write_text(
            "\n".join(json.dumps(h) for h in history(3, 10_000, 10_100)))
        # Kein Netz-Client importierbar/aufrufbar in diesem Testlauf - würde
        # build_report ihn dennoch bauen, flöge hier eine Exception
        text = report_mod.build_report(offline=True, runtime_dir=rt)
    assert "=== Empfehlungen ===" in text  # kein Crash, kein Netz-Versuch


def test_build_report_cli_main_prints_same_content():
    """main() (python report.py) muss weiterhin exakt das drucken, was
    build_report() zurückgibt - reiner Verhaltens-Erhalt nach dem Refactor."""
    import contextlib
    import io
    import sys as _sys

    import report as report_mod

    with tempfile.TemporaryDirectory() as tmp:
        rt = Path(tmp)
        orig_argv, orig_runtime = _sys.argv, report_mod.RUNTIME
        _sys.argv = ["report.py", "--offline"]
        report_mod.RUNTIME = rt
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                report_mod.main()
        finally:
            _sys.argv, report_mod.RUNTIME = orig_argv, orig_runtime
    assert "Noch keine Daten in runtime/" in buf.getvalue()


# ---------- chunk_for_telegram (Telegram-4096-Zeichen-Limit) ----------

def test_chunk_short_text_stays_one_piece():
    assert chunk_for_telegram("hallo\nwelt") == ["hallo\nwelt"]


def test_chunk_respects_limit_and_is_lossless():
    text = "\n".join(f"Zeile {i}: " + "x" * 100 for i in range(200))
    chunks = chunk_for_telegram(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) > 1
    assert "\n".join(chunks) == text, "Zeilengrenzen-Split darf nichts verlieren"


def test_chunk_breaks_only_on_line_boundaries():
    text = "\n".join(["kurz"] * 3 + ["y" * 90] + ["kurz"] * 3)
    chunks = chunk_for_telegram(text, limit=100)
    for c in chunks:
        for line in c.split("\n"):
            assert line in text.split("\n"), "keine mitten im Wort abgeschnittene Zeile"


def test_chunk_hard_splits_single_oversized_line():
    """Eine Einzelzeile länger als das Limit (sollte im Report nicht vorkommen,
    aber die Funktion darf nicht endlos/leer zurückgeben)."""
    text = "x" * 9000
    chunks = chunk_for_telegram(text, limit=4000)
    assert len(chunks) == 3
    assert "".join(chunks) == text
    assert all(len(c) <= 4000 for c in chunks)


def test_chunk_empty_text():
    assert chunk_for_telegram("") == [""]


def test_chunk_never_emits_a_spurious_empty_chunk():
    """Verifikations-Fund: eine leere Zeile gefolgt von einer fast limit-langen
    Folgezeile flushte die leere Zeile als EIGENEN, leeren Chunk - eine
    Telegram-Nachricht nur mit '📊 Report i/N'-Header, ohne Inhalt, plus ein
    aufgeblähtes N."""
    text = "\n" + "x" * 3800
    chunks = chunk_for_telegram(text, limit=3800)
    assert "" not in chunks, f"kein Chunk darf leer sein: {chunks}"
    assert chunks == ["x" * 3800]


# ---------- Watchdog-Diagnose ----------

class FakeCopier:
    def __init__(self, positions=0, halted=False):
        self.halted = halted
        snap = type("S", (), {"positions": dict.fromkeys(range(positions))})()
        self.last_snapshots = [snap] if positions else []


def test_diagnose_vetoes_dominant():
    entries = [veto(100, reasons=["VETO: RSI 85 überkauft"]), veto(200, reasons=["VETO: RSI 80 überkauft"])]
    text = diagnose_inactivity(entries, since_t=0, copier=FakeCopier(positions=3))
    assert "2 Vetos" in text and "RSI" in text


def test_diagnose_flat_leaders():
    text = diagnose_inactivity([], since_t=0, copier=FakeCopier(positions=0))
    assert "keine Positionen" in text


def test_diagnose_book_on_target():
    text = diagnose_inactivity([], since_t=0, copier=FakeCopier(positions=4))
    assert "kein Fehler" in text


def test_diagnose_halted_flagged():
    text = diagnose_inactivity([], since_t=0, copier=FakeCopier(positions=0, halted=True))
    assert "HALTED" in text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
