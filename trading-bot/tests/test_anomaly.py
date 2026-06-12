"""Unit-Tests für den Anomalie-Scout (ohne Netzwerk).

Muster: frische Wallet (kaum Historie) + große, konzentrierte Position
-> Meldung. Alles andere (alte Hasen, kleine Trades, breit gestreute Bücher)
-> still. Der Scout handelt nie - er produziert nur Meldungen.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.anomaly import AnomalyScout
from bot.config import AnomalyConfig

NOW = 1_750_000_000.0
NOW_MS = int(NOW * 1000)
DAY_MS = 86_400_000


class FakeInfo:
    """Stellt recentTrades / userFills / userState aus Testdaten bereit."""

    def __init__(self, trades=None, fills=None, states=None, errors=None):
        self.trades = trades or {}     # coin -> Trades
        self.fills = fills or {}       # addr -> Fills
        self.states = states or {}     # addr -> user_state
        self.errors = errors or set()  # Adressen, deren Abfragen knallen
        self.calls = []

    def post(self, path, payload):
        self.calls.append(("recentTrades", payload["coin"]))
        return self.trades.get(payload["coin"], [])

    def user_fills_by_time(self, addr, start, end):
        self.calls.append(("fills", addr))
        if addr in self.errors:
            raise RuntimeError("API kaputt")
        return self.fills.get(addr, [])

    def user_state(self, addr):
        self.calls.append(("state", addr))
        return self.states.get(addr, {"marginSummary": {"accountValue": "0"}, "assetPositions": []})


def trade(coin, px, sz, users):
    return {"coin": coin, "px": str(px), "sz": str(sz), "users": users}


def fill(t_ms):
    return {"time": t_ms, "coin": "SOL", "sz": "1", "side": "B"}


def state(account_value, positions):
    """positions: Liste (coin, szi, position_value)."""
    return {
        "marginSummary": {"accountValue": str(account_value)},
        "assetPositions": [
            {"position": {"coin": c, "szi": str(szi), "positionValue": str(v)}}
            for c, szi, v in positions
        ],
    }


def scout(info, **overrides) -> AnomalyScout:
    cfg = AnomalyConfig(coins=["SOL"], throttle_s=0, **overrides)
    s = AnomalyScout(info, cfg, clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "anomalies.jsonl"
    return s


def test_fresh_wallet_big_concentrated_bet_flagged():
    info = FakeInfo(
        trades={"SOL": [trade("SOL", 200, 1000, ["0xfresh"])]},  # $200k Trade
        fills={"0xfresh": [fill(NOW_MS - 3_600_000)]},           # nur heutiger Burst
        states={"0xfresh": state(300_000, [("SOL", 1200, 250_000), ("BTC", 0.1, 10_000)])},
    )
    s = scout(info)
    out = s.scan()
    assert len(out) == 1
    f = out[0]
    assert f["address"] == "0xfresh" and f["coin"] == "SOL" and f["side"] == "LONG"
    assert f["notional"] == 250_000 and f["prior_fills"] == 0
    assert s.flagged and s.path.exists(), "Fund muss persistiert werden"


def test_veteran_wallet_not_flagged():
    old_fills = [fill(NOW_MS - (2 + i) * DAY_MS) for i in range(30)]  # 30 alte Fills
    info = FakeInfo(
        trades={"SOL": [trade("SOL", 200, 1000, ["0xveteran"])]},
        fills={"0xveteran": old_fills},
        states={"0xveteran": state(300_000, [("SOL", 1200, 250_000)])},
    )
    assert scout(info).scan() == []


def test_small_trades_dont_trigger_checks():
    info = FakeInfo(trades={"SOL": [trade("SOL", 200, 10, ["0xsmall"])]})  # $2k
    assert scout(info).scan() == []
    assert not any(kind == "fills" for kind, _ in info.calls), "keine Wallet-Abfrage nötig"


def test_diversified_book_not_flagged():
    # Große Position, aber nur 40% des Buchs -> kein Insider-Muster
    info = FakeInfo(
        trades={"SOL": [trade("SOL", 200, 1000, ["0xdiv"])]},
        fills={"0xdiv": []},
        states={"0xdiv": state(1_000_000, [("SOL", 600, 120_000),
                                           ("BTC", 1, 100_000), ("ETH", 20, 80_000)])},
    )
    assert scout(info).scan() == []


def test_short_side_detected():
    info = FakeInfo(
        trades={"SOL": [trade("SOL", 200, 1000, ["0xshort"])]},
        fills={"0xshort": []},
        states={"0xshort": state(200_000, [("SOL", -900, 180_000)])},
    )
    out = scout(info).scan()
    assert out and out[0]["side"] == "SHORT"


def test_recheck_cooldown():
    info = FakeInfo(
        trades={"SOL": [trade("SOL", 200, 1000, ["0xfresh"])]},
        fills={"0xfresh": []},
        states={"0xfresh": state(300_000, [("SOL", 1200, 250_000)])},
    )
    s = scout(info)
    assert len(s.scan()) == 1
    assert s.scan() == [], "innerhalb recheck_hours nicht erneut prüfen"
    assert sum(1 for k, a in info.calls if k == "fills" and a == "0xfresh") == 1


def test_api_error_skips_wallet_without_crash():
    info = FakeInfo(
        trades={"SOL": [trade("SOL", 200, 1000, ["0xbroken"]),
                        trade("SOL", 200, 1000, ["0xfresh"])]},
        fills={"0xfresh": []},
        states={"0xfresh": state(300_000, [("SOL", 1200, 250_000)])},
        errors={"0xbroken"},
    )
    out = scout(info).scan()
    assert [f["address"] for f in out] == ["0xfresh"]


def test_check_budget_per_scan():
    users = [f"0x{i:040x}" for i in range(10)]
    info = FakeInfo(trades={"SOL": [trade("SOL", 200, 1000, [u]) for u in users]})
    s = scout(info, max_checks_per_scan=3)
    s.scan()
    assert sum(1 for k, _ in info.calls if k == "fills") == 3, "API-Budget muss greifen"


def test_tick_respects_poll_interval():
    info = FakeInfo()
    cfg = AnomalyConfig(coins=["SOL"], poll_seconds=300, throttle_s=0)
    t = {"now": NOW}
    s = AnomalyScout(info, cfg, clock=lambda: t["now"], sleep=lambda _: None)
    s.tick()
    s.tick()  # sofort danach: kein zweiter Scan
    assert info.calls.count(("recentTrades", "SOL")) == 1
    t["now"] += 301
    s.tick()
    assert info.calls.count(("recentTrades", "SOL")) == 2


def test_notifier_and_journal_receive_finding():
    sent, recorded = [], []

    class N:
        def send(self, msg):
            sent.append(msg)

    class J:
        def record(self, kind, **data):
            recorded.append((kind, data))

    info = FakeInfo(
        trades={"SOL": [trade("SOL", 200, 1000, ["0xfresh"])]},
        fills={"0xfresh": []},
        states={"0xfresh": state(300_000, [("SOL", 1200, 250_000)])},
    )
    cfg = AnomalyConfig(coins=["SOL"], throttle_s=0)
    s = AnomalyScout(info, cfg, notifier=N(), journal=J(),
                     clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "anomalies.jsonl"
    s.scan()
    assert sent and "Anomalie" in sent[0] and "0xfresh" in sent[0]
    assert recorded and recorded[0][0] == "anomaly"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
