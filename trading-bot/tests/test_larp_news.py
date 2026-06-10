"""Unit-Tests für LARP-Filter, Trade-Rekonstruktion, Sentiment und Schock-Detektor."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from bot.config import ShockConfig
from bot.copytrade.analyzer import analyze_fills, reconstruct_trades
from bot.copytrade.larp import LarpConfig, LarpFilter
from bot.news.sentiment import aggregate_score, score_item
from bot.news.shock import ShockDetector
from bot.news.sources import NewsItem

DAY = 86_400_000
MIN = 60_000


def fill(t, coin="BTC", side="B", sz=1.0, closed=0.0, fee=0.5, start=None):
    f = {"time": t, "coin": coin, "side": side, "sz": str(sz),
         "closedPnl": str(closed), "fee": str(fee)}
    if start is not None:
        f["startPosition"] = str(start)
    return f


# ---------- Trade-Rekonstruktion ----------

def test_reconstruct_simple_long():
    fills = [
        fill(0, side="B", sz=2.0, start=0),                 # open long 2
        fill(90 * MIN, side="A", sz=2.0, closed=500, start=2),  # close
    ]
    trips = reconstruct_trades(fills)
    assert len(trips) == 1
    assert abs(trips[0].holding_minutes - 90) < 1e-9
    assert abs(trips[0].net_pnl - 499) < 1e-9  # 500 - 2x 0.5 Fee


def test_reconstruct_partial_close_and_short():
    fills = [
        fill(0, side="A", sz=4.0, start=0),                   # open short 4
        fill(30 * MIN, side="B", sz=2.0, closed=100, start=-4),  # halb zu
        fill(60 * MIN, side="B", sz=2.0, closed=100, start=-2),  # ganz zu
    ]
    trips = reconstruct_trades(fills)
    assert len(trips) == 1
    assert abs(trips[0].holding_minutes - 60) < 1e-9
    assert abs(trips[0].net_pnl - 198.5) < 1e-9


def test_reconstruct_ignores_preexisting_position():
    # Fenster beginnt mitten in einer Position -> dieser Trade wird verworfen
    fills = [
        fill(0, side="A", sz=3.0, closed=900, start=3),    # schließt Alt-Position
        fill(DAY, side="B", sz=1.0, start=0),              # sauberer neuer Trade
        fill(DAY + 45 * MIN, side="A", sz=1.0, closed=50, start=1),
    ]
    trips = reconstruct_trades(fills)
    assert len(trips) == 1
    assert abs(trips[0].net_pnl - 49.0) < 1e-9  # 50 - 2x 0.5 Fee


# ---------- LARP-Filter ----------

def steady_fills(weeks=5, trades_per_week=10, win=80.0, loss=-40.0):
    """Konsistenter Trader: jede Woche profitabel, 2h-Haltedauer, kein Ausreißer."""
    fills = []
    t = 0
    for w in range(weeks):
        for i in range(trades_per_week):
            t = w * 7 * DAY + i * 12 * 3600 * 1000
            pnl = win if i % 3 else loss
            fills.append(fill(t, side="B", sz=1.0, start=0, fee=0.1))
            fills.append(fill(t + 120 * MIN, side="A", sz=1.0, closed=pnl, start=1, fee=0.1))
    return fills


def test_larp_passes_steady_trader():
    m = analyze_fills("0xsteady", steady_fills(), account_value=10000, days=35)
    verdict = LarpFilter().check(m)
    assert verdict.passed, f"Konsistenter Trader fälschlich aussortiert: {verdict.reasons}"


def test_larp_rejects_lucky_punch():
    fills = steady_fills(weeks=5, trades_per_week=6, win=10.0, loss=-5.0)
    # Ein Mega-Trade dominiert den Gesamt-PnL
    fills.append(fill(36 * DAY, side="B", sz=5.0, start=0))
    fills.append(fill(36 * DAY + 60 * MIN, side="A", sz=5.0, closed=5000, start=5))
    m = analyze_fills("0xluck", fills, account_value=10000, days=40)
    verdict = LarpFilter().check(m)
    assert not verdict.passed
    assert any("Lucky Punch" in r for r in verdict.reasons)


def test_larp_rejects_scalper():
    fills = []
    for i in range(50):
        t = i * 6 * 3600 * 1000
        fills.append(fill(t, side="B", sz=1.0, start=0, fee=0.1))
        fills.append(fill(t + 3 * MIN, side="A", sz=1.0, closed=30, start=1, fee=0.1))
    m = analyze_fills("0xscalp", fills, account_value=10000, days=15)
    verdict = LarpFilter().check(m)
    assert not verdict.passed
    assert any("Scalper" in r for r in verdict.reasons)


def test_larp_rejects_thin_history():
    fills = steady_fills(weeks=1, trades_per_week=5)
    m = analyze_fills("0xthin", fills, account_value=10000, days=30)
    verdict = LarpFilter().check(m)
    assert not verdict.passed
    assert any("Round-Trips" in r for r in verdict.reasons)


def test_larp_rejects_overleveraged():
    m = analyze_fills("0xdegen", steady_fills(), account_value=10000, days=35)
    m.max_drawdown = 0.40
    verdict = LarpFilter().check(m)
    assert not verdict.passed
    assert any("überhebelt" in r for r in verdict.reasons)


# ---------- Sentiment ----------

def item(title, age_min=0, now=1_000_000_000_000):
    return NewsItem(source="test", title=title, time_ms=now - age_min * MIN)


def test_sentiment_critical_news():
    s = score_item(item("Major exchange hacked, $400 million stolen from hot wallets"))
    assert s.score >= 9


def test_sentiment_trump_tariff_boosted():
    plain = score_item(item("New tariff package announced against chip imports"))
    trump = score_item(item("Trump announces new tariff package against chip imports"))
    assert trump.score > plain.score >= 5


def test_sentiment_benign_news_low():
    s = score_item(item("Bitcoin ETF inflows continue as institutions accumulate"))
    assert s.score < 2


def test_sentiment_decay():
    now = 1_000_000_000_000
    fresh = [score_item(item("Markets crash as war fears escalate", age_min=0, now=now))]
    old = [score_item(item("Markets crash as war fears escalate", age_min=120, now=now))]
    assert aggregate_score(fresh, half_life_minutes=30, now_ms=now) >= 8
    assert aggregate_score(old, half_life_minutes=30, now_ms=now) < 1


# ---------- Schock-Detektor ----------

def candles_1m(closes):
    return pd.DataFrame({"close": closes})


def test_shock_detects_crash():
    np.random.seed(1)
    calm = list(100000 * np.exp(np.cumsum(np.random.normal(0, 0.0003, 100))))
    crash = calm + [calm[-1] * (1 - 0.01 * i) for i in range(1, 6)]  # -5% in 5min
    det = ShockDetector(ShockConfig())
    state = det.check(candles_1m(crash), now=1000)
    assert state.triggered
    assert state.move_pct < -0.025


def test_shock_quiet_market_no_trigger():
    np.random.seed(2)
    calm = list(100000 * np.exp(np.cumsum(np.random.normal(0, 0.0003, 120))))
    det = ShockDetector(ShockConfig())
    assert not det.check(candles_1m(calm), now=1000).triggered


def test_shock_cooldown_persists():
    np.random.seed(3)
    calm = list(100000 * np.exp(np.cumsum(np.random.normal(0, 0.0003, 100))))
    crash = calm + [calm[-1] * (1 - 0.01 * i) for i in range(1, 6)]
    det = ShockDetector(ShockConfig(cooldown_minutes=30))
    assert det.check(candles_1m(crash), now=1000).triggered
    # Markt wieder ruhig, aber Cooldown hält RISK_OFF aufrecht
    assert det.check(candles_1m(calm), now=1000 + 60).triggered
    assert not det.check(candles_1m(calm), now=1000 + 31 * 60).triggered


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
