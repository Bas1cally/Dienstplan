"""Unit-Tests: Report-Engine, Empfehlungs-Regeln, Watchdog-Diagnose."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.autopilot import diagnose_inactivity
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


# ---------- recommendations ----------

def test_recommends_more_leaders_when_zero_orders():
    s = summarize([veto(i) for i in range(5)], history(3), None)
    recs = recommendations(s)
    assert any("top_percent" in r for r in recs)


def test_recommends_loosening_validator_when_vetoes_profitable():
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(5), None)
    veto_stats = {"evaluated": 20, "avg_return_pct": 0.8, "win_share": 0.7, "horizon_hours": 24}
    recs = recommendations(s, veto_stats)
    assert any("min_score" in r for r in recs)


def test_keeps_validator_when_vetoes_lossy():
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(5), None)
    veto_stats = {"evaluated": 20, "avg_return_pct": -0.9, "win_share": 0.3, "horizon_hours": 24}
    recs = recommendations(s, veto_stats)
    assert any("rettet" in r for r in recs)
    assert not any("min_score 2 -> 1" in r for r in recs)


def test_validator_conflict_surfaced_not_two_recs():
    """Der reale 6-Tage-Fall: Veto-Outcome sagt 'rettet', Shadow sagt 'schadet'.
    Statt zweier gegensätzlicher Ratschläge muss EIN Widerspruchs-Hinweis kommen."""
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(6), None)
    veto_stats = {"evaluated": 55, "avg_return_pct": -1.44, "win_share": 0.33, "horizon_hours": 24}
    shadow_stats = {"baseline": 10_140, "variants": {"ohne_validator": {"equity": 10_256, "trades": 60}}}
    recs = recommendations(s, veto_stats, shadow_stats)
    assert any("WIDERSPRECHEN" in r for r in recs)
    assert not any("enabled:false" in r.replace(" ", "") for r in recs), "kein Abschalt-Rat bei Widerspruch"
    assert sum("Validator" in r or "Filter" in r for r in recs) == 1, "genau EIN Validator-Urteil"


def test_validator_both_agree_hurts_short_sample_is_cautious():
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(6), None)
    veto_stats = {"evaluated": 55, "avg_return_pct": 0.8, "win_share": 0.7, "horizon_hours": 24}
    shadow_stats = {"baseline": 10_000, "variants": {"ohne_validator": {"equity": 10_300, "trades": 60}}}
    recs = recommendations(s, veto_stats, shadow_stats)
    assert any("min_score 2 -> 1" in r for r in recs)
    assert any("enabled:false" in r.replace(" ", "") for r in recs), "muss VOR enabled:false warnen"
    assert any("Stressphase" in r or "Abverkauf" in r for r in recs)


def test_validator_both_agree_helps():
    journal = [order(1)] + [veto(i + 10) for i in range(20)]
    s = summarize(journal, history(6), None)
    veto_stats = {"evaluated": 55, "avg_return_pct": -1.0, "win_share": 0.3, "horizon_hours": 24}
    shadow_stats = {"baseline": 10_000, "variants": {"ohne_validator": {"equity": 9_800, "trades": 60}}}
    recs = recommendations(s, veto_stats, shadow_stats)
    assert any("rettet" in r and "behalten" in r for r in recs)
    assert not any("WIDERSPRECHEN" in r for r in recs)


def test_recommends_fee_reduction():
    journal = [order(i) for i in range(20)]
    s = summarize(journal, history(7, 10_000, 10_010), {"fees_paid": 50.0, "realized_pnl": 10.0})
    assert any("rebalance_threshold" in r for r in recommendations(s))


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
