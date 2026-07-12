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


MARKETS = [{"symbol": "BTC", "market_id": 1}, {"symbol": "ETH", "market_id": 2},
           {"symbol": "SOL", "market_id": 3}]


def http_markets_and_trades(trades_by_market, extra=None):
    """Fake-HTTP: orderBooks liefert MARKETS, recentTrades(market_id=X) liefert
    trades_by_market[X]. account-Requests gehen an `extra` (dict ref->response)."""
    def http(url, params):
        if "orderBooks" in url:
            return {"order_books": MARKETS}
        if "recentTrades" in url:
            return {"trades": trades_by_market.get(params["market_id"], [])}
        if extra is not None:
            return extra[params["value"]]
        raise AssertionError(f"unerwarteter Call: {url} {params}")
    return http


def test_resolve_market_ids_filters_by_coins():
    cfg = LighterConfig(coins=["BTC", "ETH"])
    src = LighterSource(cfg, LighterClient(http=http_markets_and_trades({})))
    ids = src._resolve_market_ids()
    assert set(ids) == {1, 2}, "nur konfigurierte Coins, SOL raus"
    assert src._resolve_market_ids() is ids, "gecacht, kein zweiter Call nötig"


def test_discover_queries_each_market_and_dedupes():
    trades = {1: [{"maker_account_id": 7}, {"taker_account_id": 8}],
             2: [{"maker_account_id": 8}]}   # 8 doppelt -> dedup
    cfg = LighterConfig(accounts=[999], auto_discover=True, coins=["BTC", "ETH"], throttle_s=0)
    src = LighterSource(cfg, LighterClient(http=http_markets_and_trades(trades)))
    cands = src.discover_active()
    assert cands[0] == "999", "Seeds zuerst"
    assert set(cands) == {"999", "7", "8"}
    assert len(cands) == len(set(cands)), "dedupliziert"
    assert src.last_scan_note == "", "Trades gefunden -> keine Fehlermeldung"


def test_discover_no_markets_resolved_sets_note():
    """Regression: der reale Bug - orderBooks liefert nichts -> vorher stille
    Endlosschleife ('sucht…'), jetzt eine sichtbare Diagnose."""
    cfg = LighterConfig(auto_discover=True, coins=["BTC"], throttle_s=0)
    src = LighterSource(cfg, LighterClient(http=lambda url, params: {}))
    cands = src.discover_active()
    assert cands == []
    assert "Märkte" in src.last_scan_note


def test_discover_markets_ok_but_no_trades_sets_note():
    cfg = LighterConfig(auto_discover=True, coins=["BTC", "ETH"], throttle_s=0)
    src = LighterSource(cfg, LighterClient(http=http_markets_and_trades({})))
    src.discover_active()
    assert "keine Trades" in src.last_scan_note


def test_rank_filters_and_tops():
    # Konten: 5 mit Positionen/Equity, eins zu klein, eins flach
    accounts = {
        "1": acct(20_000, pos("BTC", 1, 1, 60_000), pos("ETH", -1, 5, 15_000)),
        "2": acct(9_000, pos("SOL", 1, 10, 1_400)),
        "3": acct(1_000, pos("BTC", 1, 1, 60_000)),   # < min_equity -> raus
        "4": acct(50_000),                             # flach -> raus
    }
    trades = {1: [{"maker_account_id": int(k)} for k in accounts], 2: [], 3: []}

    cfg = LighterConfig(auto_discover=True, min_equity=5000, min_positions=1,
                        max_leaders=2, coins=["BTC", "ETH", "SOL"], throttle_s=0)
    src = LighterSource(cfg, LighterClient(http=http_markets_and_trades(trades, extra=accounts)))
    top = src.rank()
    addrs = [s.address for s in top]
    assert "1" in addrs and "2" in addrs, "qualifizierte drin"
    assert "3" not in addrs and "4" not in addrs, "zu klein/flach raus"
    assert addrs[0] == "1", "aktivstes Konto zuerst (Equity x Positionen)"


def test_lighter_shadow_reconciles_and_persists():
    import tempfile
    accounts = {"1": acct(20_000, pos("BTC", 1, 1, 60_000))}
    trades = {1: [{"maker_account_id": 1}]}

    from bot.sources.lighter import LighterShadow
    cfg = LighterConfig(auto_discover=True, min_equity=5000, coins=["BTC"],
                        initial_equity=10_000, throttle_s=0)
    src = LighterSource(cfg, LighterClient(http=http_markets_and_trades(trades, extra=accounts)))
    with tempfile.TemporaryDirectory() as tmp:
        sh = LighterShadow(cfg, 0.00045, source=src, runtime_dir=Path(tmp))
        sh.tick({"BTC": 60_000.0})
        assert sh.paper.sizes(), "kopiert die Lighter-Position auf HL-Preisen"
        assert "BTC" in sh.paper.sizes()
        st = sh.stats({"BTC": 60_000.0})
        assert st["trades"] >= 1 and "1" in st["leaders"]
        assert st["note"] == ""


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
