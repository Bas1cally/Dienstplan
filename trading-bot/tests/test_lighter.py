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


def test_discover_extracts_account_ids_from_trades():
    from bot.sources.lighter import _account_ids
    t = {"market_id": 1, "maker_account_id": 7, "taker_account_index": 12, "price": "100"}
    ids = _account_ids(t)
    assert set(ids) == {"7", "12"}, "nur account-Felder, market_id nicht"


def test_discover_includes_seeds_and_budget():
    trades = [{"maker_account_id": i} for i in range(100)]
    cfg = LighterConfig(accounts=[999], auto_discover=True, max_candidates=10)
    client = LighterClient(http=lambda url, params: {"trades": trades})
    src = LighterSource(cfg, client)
    cands = src.discover_active()
    assert cands[0] == "999", "Seeds zuerst"
    assert len(set(cands)) == len(cands), "dedupliziert"


def test_rank_filters_and_tops():
    # Konten: 5 mit Positionen/Equity, eins zu klein, eins flach
    accounts = {
        "1": acct(20_000, pos("BTC", 1, 1, 60_000), pos("ETH", -1, 5, 15_000)),
        "2": acct(9_000, pos("SOL", 1, 10, 1_400)),
        "3": acct(1_000, pos("BTC", 1, 1, 60_000)),   # < min_equity -> raus
        "4": acct(50_000),                             # flach -> raus
    }

    def http(url, params):
        if "recentTrades" in url or "trades" in url:
            return {"trades": [{"maker_account_id": int(k)} for k in accounts]}
        return accounts[params["value"]]

    cfg = LighterConfig(auto_discover=True, min_equity=5000, min_positions=1,
                        max_leaders=2, coins=["BTC", "ETH", "SOL"])
    src = LighterSource(cfg, LighterClient(http=http))
    top = src.rank()
    addrs = [s.address for s in top]
    assert "1" in addrs and "2" in addrs, "qualifizierte drin"
    assert "3" not in addrs and "4" not in addrs, "zu klein/flach raus"
    assert addrs[0] == "1", "aktivstes Konto zuerst (Equity x Positionen)"


def test_lighter_shadow_reconciles_and_persists():
    import tempfile
    accounts = {"1": acct(20_000, pos("BTC", 1, 1, 60_000))}

    def http(url, params):
        if "account" in url:
            return accounts[params["value"]]
        return {"trades": [{"maker_account_id": 1}]}

    from bot.sources.lighter import LighterShadow
    cfg = LighterConfig(auto_discover=True, min_equity=5000, coins=["BTC"], initial_equity=10_000)
    src = LighterSource(cfg, LighterClient(http=http))
    with tempfile.TemporaryDirectory() as tmp:
        sh = LighterShadow(cfg, 0.00045, source=src, runtime_dir=Path(tmp))
        sh.tick({"BTC": 60_000.0})
        assert sh.paper.sizes(), "kopiert die Lighter-Position auf HL-Preisen"
        assert "BTC" in sh.paper.sizes()
        st = sh.stats({"BTC": 60_000.0})
        assert st["trades"] >= 1 and "1" in st["leaders"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
