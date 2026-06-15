"""Unit-Tests für den Polymarket-Scout (read-only, ohne Netzwerk).

Muster: große Wette + Wallet mit profitabler Historie -> Meldung. Kleine
Wetten oder Wallets ohne Track-Record -> still. Der Scout handelt nie.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import PolymarketConfig
from bot.polymarket import PolymarketScout

NOW = 1_750_000_000.0


class FakeApi:
    """Stellt /trades und /positions aus Testdaten bereit."""

    def __init__(self, trades=None, positions=None, errors=None):
        self.trades = trades or []
        self.positions = positions or {}   # wallet -> Positions-Liste
        self.errors = errors or set()      # Wallets, deren /positions knallt
        self.calls = []

    def fetch(self, url, params):
        if "trades" in url:
            self.calls.append(("trades", None))
            return self.trades
        addr = params.get("user", "")
        self.calls.append(("positions", addr))
        if addr in self.errors:
            raise RuntimeError("API kaputt")
        return self.positions.get(addr, [])


def trade(wallet, usdc, market="Wird BTC 100k erreichen?", outcome="YES", side="BUY"):
    return {"proxyWallet": wallet, "usdcSize": usdc, "title": market,
            "outcome": outcome, "side": side}


def position(realized, value=0.0):
    return {"realizedPnl": realized, "currentValue": value}


def scout(api, **overrides):
    cfg = PolymarketConfig(enabled=True, throttle_s=0, **overrides)
    s = PolymarketScout(cfg, fetch=api.fetch, clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "polymarket.jsonl"
    return s


def test_big_bet_by_profitable_wallet_flagged():
    api = FakeApi(
        trades=[trade("0xpro", 20_000)],
        positions={"0xpro": [position(50_000, 30_000), position(10_000)]},
    )
    out = scout(api).scan()
    assert len(out) == 1
    f = out[0]
    assert f["address"] == "0xpro" and f["bet_usdc"] == 20_000
    assert f["realized_pnl"] == 60_000 and f["outcome"] == "YES"


def test_small_bet_not_checked():
    api = FakeApi(trades=[trade("0xsmall", 500)])
    assert scout(api).scan() == []
    assert not any(k == "positions" for k, _ in api.calls), "kleine Wette: keine Wallet-Prüfung"


def test_unprofitable_wallet_not_flagged():
    api = FakeApi(
        trades=[trade("0xnoob", 20_000)],
        positions={"0xnoob": [position(500)]},  # Track-Record unter Schwelle
    )
    assert scout(api).scan() == []


def test_losing_wallet_not_flagged():
    api = FakeApi(
        trades=[trade("0xloser", 20_000)],
        positions={"0xloser": [position(-40_000)]},
    )
    assert scout(api).scan() == []


def test_notional_from_size_times_price_fallback():
    # ohne usdcSize: notional = size * price
    t = {"proxyWallet": "0xpro", "size": 10_000, "price": 0.8,
         "title": "M", "outcome": "NO", "side": "SELL"}
    api = FakeApi(trades=[t], positions={"0xpro": [position(50_000)]})
    out = scout(api).scan()
    assert out and out[0]["bet_usdc"] == 8_000


def test_recheck_cooldown():
    api = FakeApi(trades=[trade("0xpro", 20_000)], positions={"0xpro": [position(50_000)]})
    s = scout(api)
    assert len(s.scan()) == 1
    assert s.scan() == [], "innerhalb recheck_hours nicht erneut prüfen"
    assert sum(1 for k, a in api.calls if k == "positions" and a == "0xpro") == 1


def test_check_budget_limits_api_calls():
    trades = [trade(f"0x{i:040x}", 20_000) for i in range(10)]
    api = FakeApi(trades=trades, positions={f"0x{i:040x}": [position(50_000)] for i in range(10)})
    scout(api, max_checks_per_scan=3).scan()
    assert sum(1 for k, _ in api.calls if k == "positions") == 3


def test_api_error_skips_wallet_without_crash():
    api = FakeApi(
        trades=[trade("0xbroken", 20_000), trade("0xpro", 20_000)],
        positions={"0xpro": [position(50_000)]},
        errors={"0xbroken"},
    )
    out = scout(api).scan()
    assert [f["address"] for f in out] == ["0xpro"]


def test_trades_endpoint_failure_is_silent():
    class DeadApi:
        def fetch(self, url, params):
            raise RuntimeError("Netz weg")

    s = PolymarketScout(PolymarketConfig(enabled=True), fetch=DeadApi().fetch,
                        clock=lambda: NOW)
    assert s.scan() == []  # kein Crash


def test_notifier_and_journal_receive_finding():
    sent, recorded = [], []

    class N:
        def send(self, m):
            sent.append(m)

    class J:
        def record(self, kind, **d):
            recorded.append((kind, d))

    api = FakeApi(trades=[trade("0xpro", 20_000)], positions={"0xpro": [position(50_000)]})
    cfg = PolymarketConfig(enabled=True, throttle_s=0)
    s = PolymarketScout(cfg, notifier=N(), journal=J(), fetch=api.fetch,
                        clock=lambda: NOW, sleep=lambda _: None)
    s.path = Path(tempfile.mkdtemp()) / "polymarket.jsonl"
    s.scan()
    assert sent and "Polymarket" in sent[0]
    assert recorded and recorded[0][0] == "polymarket"


def test_tick_respects_poll_interval():
    api = FakeApi(trades=[])
    cfg = PolymarketConfig(enabled=True, poll_seconds=600)
    t = {"now": NOW}
    s = PolymarketScout(cfg, fetch=api.fetch, clock=lambda: t["now"])
    s.tick()
    s.tick()
    assert sum(1 for k, _ in api.calls if k == "trades") == 1
    t["now"] += 601
    s.tick()
    assert sum(1 for k, _ in api.calls if k == "trades") == 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
