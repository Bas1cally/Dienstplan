"""Unit-Tests: Lighter-Quelle (Positionen lesen -> LeaderSnapshot), ohne Netz."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import LighterConfig
from bot.sources.base import CopySource
from bot.sources.lighter import LighterClient, LighterSource


def acct(collateral, *positions):
    return {"accounts": [{"index": 7, "collateral": str(collateral),
                          "positions": list(positions)}]}


def pos(symbol, sign, position, value, entry=100.0):
    return {"symbol": symbol, "sign": sign, "position": str(position),
            "position_value": str(value), "avg_entry_price": str(entry)}


def client_for(response):
    return LighterClient(http=lambda url, params: response)


def test_snapshot_parses_long_and_short():
    resp = acct(10_000, pos("BTC", 1, 0.5, 30_000), pos("ETH", -1, 4, 12_000))
    snap = client_for(resp).snapshot("7")
    assert snap.equity == 10_000
    assert snap.positions["BTC"].size > 0, "sign 1 -> long"
    assert snap.positions["ETH"].size < 0, "sign -1 -> short"
    assert snap.positions["BTC"].position_value == 30_000
    # Exposure signiert relativ zur Equity
    assert abs(snap.exposure("BTC") - 3.0) < 1e-9
    assert abs(snap.exposure("ETH") + 1.2) < 1e-9


def test_snapshot_skips_flat_and_unmappable():
    resp = acct(5_000, pos("BTC", 1, 0, 0), pos("TSLA", 1, 10, 2_000))
    # coins-Whitelist: nur BTC/ETH handelbar -> TSLA fällt raus, BTC ist flat
    src = LighterSource(LighterConfig(coins=["BTC", "ETH"]), client_for(resp))
    snap = src.snapshot("7")
    assert snap.positions == {}, "flache + nicht handelbare Positionen raus"


def test_map_coin_whitelist_and_normalize():
    src = LighterSource(LighterConfig(coins=["BTC", "ETH"]))
    assert src.map_coin("BTC") == "BTC"
    assert src.map_coin("BTC-USD") == "BTC", "Suffix normalisieren"
    assert src.map_coin("DOGE") is None, "nicht in Whitelist"
    # ohne Whitelist alles durch
    assert LighterSource(LighterConfig()).map_coin("DOGE") == "DOGE"


def test_source_is_copysource_and_discover_watchlist():
    src = LighterSource(LighterConfig(accounts=[7, "0xabc"]))
    assert isinstance(src, CopySource)
    assert src.discover() == ["7", "0xabc"]


def test_l1_address_uses_address_param():
    seen = {}

    def http(url, params):
        seen.update(params)
        return acct(1_000, pos("SOL", 1, 10, 1_400))

    LighterClient(http=http).snapshot("0xDEADBEEF")
    assert seen["by"] == "l1_address" and seen["value"] == "0xDEADBEEF"
    LighterClient(http=http).snapshot("42")
    assert seen["by"] == "index" and seen["value"] == "42"


def test_tolerates_response_shapes():
    p = pos("BTC", 1, 1, 60_000)
    direct = {"collateral": "9000", "positions": [p]}   # Konto direkt
    wrapped = {"account": {"collateral": "9000", "positions": [p]}}
    for resp in (direct, wrapped):
        snap = client_for(resp).snapshot("7")
        assert snap.equity == 9000 and "BTC" in snap.positions


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
