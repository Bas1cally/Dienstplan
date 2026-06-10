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
