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


def test_sprint_snapshots_prefixes_addresses_and_keeps_full_book():
    """Nutzer-Entscheidung (17.07., Option B): Lighter-Leader fürs Sprint-Buch
    bekommen einen eigenen Adress-Namensraum (lighter:-Präfix), damit sie nie
    mit echten HL-Adressen in strikes/banned/confidence kollidieren. Score
    bewusst fix und niedrig (kein Ranking-API bei Lighter).

    Kontrakt-Änderung 19.07.: KEIN Preis-Filter mehr im Buch - ein transient
    fehlender HL-Preis ließ Coins aus dem Buch flackern (Baseline sah 0, Coin
    kam zurück -> falsches frisches Signal bzw. falscher leader_exit). Coins
    sind bereits per map_coin whitelist-gemappt; unpreisbare weist _enter()
    mit 'kein_hl_preis' ab - dort gehört der Check hin."""
    import tempfile

    from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot
    from bot.sources.lighter import LighterShadow

    cfg = LighterConfig(max_leaders=5)
    with tempfile.TemporaryDirectory() as tmp:
        sh = LighterShadow(cfg, 0.00045, source=LighterSource(cfg), runtime_dir=Path(tmp))
        sh._leaders = [LeaderSnapshot("42", 10_000.0, {
            "BTC": LeaderPosition(coin="BTC", size=1.0, entry=100.0,
                                  position_value=1000.0, leverage=1),
            "MOMENTAN_OHNE_PREIS": LeaderPosition(coin="MOMENTAN_OHNE_PREIS", size=1.0,
                                                  entry=1.0, position_value=1.0, leverage=1),
        })]
        sh._last_scan_ok = sh.clock()
        leaders, snaps = sh.sprint_snapshots({"BTC": 65_000.0})

    assert leaders == [{"address": "lighter:42", "score": 15.0, "weight": 1.0}]
    assert len(snaps) == 1 and snaps[0].address == "lighter:42"
    assert sorted(snaps[0].positions) == ["BTC", "MOMENTAN_OHNE_PREIS"], \
        "Buch bleibt VOLLSTÄNDIG stabil - kein Preis-Flackern in der Baseline"


def test_sprint_snapshots_respects_max_leaders_cap():
    import tempfile

    from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot
    from bot.sources.lighter import LighterShadow

    cfg = LighterConfig(max_leaders=2)
    with tempfile.TemporaryDirectory() as tmp:
        sh = LighterShadow(cfg, 0.00045, source=LighterSource(cfg), runtime_dir=Path(tmp))
        sh._leaders = [
            LeaderSnapshot(str(i), 10_000.0, {
                "BTC": LeaderPosition(coin="BTC", size=1.0, entry=100.0,
                                      position_value=1000.0, leverage=1),
            }) for i in range(5)
        ]
        sh._last_scan_ok = sh.clock()
        leaders, snaps = sh.sprint_snapshots({"BTC": 65_000.0})
    assert len(leaders) == 2 and len(snaps) == 2


def test_sprint_snapshots_drops_leader_with_empty_book():
    """Nur Leader ganz OHNE Positionen fliegen raus (nichts zu beobachten) -
    seit 19.07. NICHT mehr Leader, deren Coins nur gerade keinen Preis haben
    (das war die Flacker-Quelle für falsche frische Signale)."""
    import tempfile

    from bot.copytrade.tracker import LeaderSnapshot
    from bot.sources.lighter import LighterShadow

    cfg = LighterConfig()
    with tempfile.TemporaryDirectory() as tmp:
        sh = LighterShadow(cfg, 0.00045, source=LighterSource(cfg), runtime_dir=Path(tmp))
        sh._leaders = [LeaderSnapshot("9", 10_000.0, {})]
        sh._last_scan_ok = sh.clock()
        leaders, snaps = sh.sprint_snapshots({"BTC": 65_000.0})
    assert leaders == [] and snaps == []


def test_sprint_snapshots_keep_survives_topn_churn():
    """Live-Fund 19.07.: rank() wählt die Top-max_leaders je Scan NEU - fiel
    ein Konto aus der Liste, während Sprint seinen Ritt ritt, wurde der Ritt
    als 'leader_rotated' zwangsgeschlossen (BTC -14.30, lighter:30323).
    Adressen in `keep` müssen aus dem _known-Cache im Feed bleiben."""
    import tempfile

    from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot
    from bot.sources.lighter import LighterShadow

    def _snap(addr):
        return LeaderSnapshot(addr, 10_000.0, {
            "BTC": LeaderPosition(coin="BTC", size=1.0, entry=100.0,
                                  position_value=1000.0, leverage=1)})

    cfg = LighterConfig(max_leaders=2, scan_seconds=300)
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        sh = LighterShadow(cfg, 0.00045, source=LighterSource(cfg),
                           runtime_dir=Path(tmp), clock=lambda: t["now"])
        sh._known["30323"] = _snap("30323")          # frueher gescannt
        sh._leaders = [_snap("111"), _snap("222")]   # 30323 aus Top-2 gefallen
        sh._last_scan_ok = t["now"]
        leaders, _ = sh.sprint_snapshots({"BTC": 65_000.0}, keep={"30323"})
        addrs = [l["address"] for l in leaders]
        assert "lighter:30323" in addrs, "geritten werdender Leader bleibt im Feed"
        assert "lighter:111" in addrs and "lighter:222" in addrs

        # Auch bei STALE Scans: keep wird weiter bedient, Top-Liste nicht
        t["now"] += 3 * cfg.scan_seconds + 1
        leaders, _ = sh.sprint_snapshots({"BTC": 65_000.0}, keep={"30323"})
        addrs = [l["address"] for l in leaders]
        assert addrs == ["lighter:30323"], \
            "stale: keine NEUEN Signale mehr, aber der laufende Ritt bleibt versorgt"


def test_sprint_snapshots_uses_cached_leaders_no_extra_network_call():
    """KEIN eigener rank()-Aufruf - nutzt exakt den scan_seconds-gedrosselten
    Cache aus tick(), kein zusätzliches Lighter-API-Budget."""
    import tempfile

    from bot.copytrade.tracker import LeaderSnapshot
    from bot.sources.lighter import LighterShadow

    cfg = LighterConfig()

    class _BoomSource(LighterSource):
        def rank(self):
            raise AssertionError("sprint_snapshots darf rank() nicht selbst aufrufen")

    with tempfile.TemporaryDirectory() as tmp:
        sh = LighterShadow(cfg, 0.00045, source=_BoomSource(cfg), runtime_dir=Path(tmp))
        sh._leaders = [LeaderSnapshot("1", 1000.0, {})]
        sh.sprint_snapshots({"BTC": 1.0})   # darf nicht raisen


def test_sprint_snapshots_empty_when_scan_stale():
    """Wave-3-Audit-Fund (18.07.): tick() aktualisiert _leaders nur bei
    ERFOLGREICHEM rank() - schlägt der Scan wiederholt fehl, blieb der Cache
    bisher unbegrenzt lange gültig. Ab dem 3-fachen Scan-Intervall ohne
    erfolgreichen Scan müssen die (potenziell längst überholten) Snapshots
    NICHT mehr als frische Sprint-Signale durchgehen."""
    import tempfile

    from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot
    from bot.sources.lighter import LighterShadow

    cfg = LighterConfig(scan_seconds=300)
    with tempfile.TemporaryDirectory() as tmp:
        t = {"now": 1_000_000.0}
        sh = LighterShadow(cfg, 0.00045, source=LighterSource(cfg),
                           runtime_dir=Path(tmp), clock=lambda: t["now"])
        sh._leaders = [LeaderSnapshot("42", 10_000.0, {
            "BTC": LeaderPosition(coin="BTC", size=1.0, entry=100.0,
                                  position_value=1000.0, leverage=1),
        })]
        sh._last_scan_ok = t["now"]
        leaders, snaps = sh.sprint_snapshots({"BTC": 65_000.0})
        assert leaders and snaps, "Vorbedingung: frisch gescannt liefert normal"

        t["now"] += 3 * cfg.scan_seconds + 1   # letzter erfolgreicher Scan zu alt
        leaders, snaps = sh.sprint_snapshots({"BTC": 65_000.0})
        assert leaders == [] and snaps == [], \
            "eingefrorener Cache darf nicht mehr als frisches Signal durchgehen"


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
