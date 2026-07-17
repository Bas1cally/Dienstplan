"""Unit-Tests: die drei Edges - Maker-Execution, Echtzeit-Feed, Funding-Tilt."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import AnalysisConfig, CopytradeConfig, FundingTiltConfig, RiskConfig
from bot.copytrade.copier import compute_targets
from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot
from bot.exchange import HyperliquidClient
from bot.funding import FundingCache, apply_funding_tilt, tilt_factor

TILT = FundingTiltConfig()
RISK = RiskConfig(risk_per_trade=0.01, atr_stop_mult=2.0, take_profit_r=2.0,
                  max_leverage=4, max_daily_loss=0.05, slippage=0.005)
CT = CopytradeConfig(analysis=AnalysisConfig(), copy_ratio=0.6)


# ---------- Funding-Tilt ----------

def test_tilt_long_pays_scaled_down():
    # long + stark positives Funding (Longs zahlen) -> verkleinern
    assert tilt_factor(2000, 0.50, TILT) == TILT.pay_scale
    # halber Weg zwischen min und max -> halber Tilt
    mid = tilt_factor(2000, 0.30, TILT)
    assert TILT.pay_scale < mid < 1.0


def test_tilt_long_earns_boosted():
    # long + negatives Funding (Shorts zahlen an Longs) -> vergrößern
    assert tilt_factor(2000, -0.50, TILT) == TILT.earn_boost


def test_tilt_short_symmetric():
    assert tilt_factor(-2000, -0.50, TILT) == TILT.pay_scale   # short zahlt bei apr<0
    assert tilt_factor(-2000, 0.50, TILT) == TILT.earn_boost   # short kassiert bei apr>0


def test_tilt_neutral_band_and_missing_data():
    assert tilt_factor(2000, 0.05, TILT) == 1.0    # unter min_apr
    assert tilt_factor(2000, None, TILT) == 1.0    # keine Daten (Builder-DEX)
    assert tilt_factor(0, 0.50, TILT) == 1.0       # keine Position
    assert tilt_factor(2000, 0.50, FundingTiltConfig(enabled=False)) == 1.0


def test_apply_tilt_keeps_direction():
    targets = {"BTC": 2000.0, "ETH": -1500.0, "km:TSLA": 1000.0}
    out = apply_funding_tilt(targets, {"BTC": 0.6, "ETH": 0.6}, TILT)
    assert out["BTC"] == 2000 * TILT.pay_scale and out["BTC"] > 0
    assert out["ETH"] == -1500 * TILT.earn_boost and out["ETH"] < 0
    assert out["km:TSLA"] == 1000.0, "ohne Funding-Daten neutral"


def test_tilt_boost_still_capped():
    """earn_boost darf den Coin-Cap nicht durchbrechen (Tilt läuft VOR den Caps)."""
    snap = LeaderSnapshot("0xa", 1000, {
        "BTC": LeaderPosition("BTC", -4, 100.0, 400.0, 2)})  # 40% short
    # Basis: -0.4*0.6*10000 = -2400; Boost 1.10 -> -2640; Cap 25% = 2500
    targets = compute_targets([snap], {"0xa": 1.0}, 10_000, CT, RISK,
                              funding=({"BTC": 0.50}, TILT))
    assert abs(targets["BTC"] + 2500) < 1e-9


def test_funding_cache_resilient():
    class BrokenInfo:
        def meta_and_asset_ctxs(self):
            raise ConnectionError("down")

    cache = FundingCache(BrokenInfo(), TILT, clock=lambda: 1000.0)
    assert cache.apr_by_coin() == {}  # Ausfall = neutral, kein Crash


# ---------- Maker-Execution (smart_order) ----------

class FakeExchange:
    def __init__(self, first_status, fill_after_polls=None):
        self.first_status = first_status
        self.fill_after_polls = fill_after_polls
        self.cancelled = []
        self.market_orders = []

    def order(self, coin, is_buy, sz, px, order_type):
        assert order_type == {"limit": {"tif": "Alo"}}, "muss Post-Only sein"
        self.placed_px = px
        return {"status": "ok", "response": {"data": {"statuses": [self.first_status]}}}

    def cancel(self, coin, oid):
        self.cancelled.append(oid)


def make_client(exchange, order_status_seq=None, default_status=None):
    """HyperliquidClient ohne Netz: nur die für smart_order nötigen Teile."""
    c = HyperliquidClient.__new__(HyperliquidClient)
    c.exchange = exchange
    c.account_address = "0xme"
    c.market_orders = []
    c.all_mids = lambda: {"BTC": "104536.789"}
    c.sz_decimals = lambda coin: 5
    c.market_open = lambda coin, is_buy, sz, slip: exchange.market_orders.append((coin, is_buy, sz))
    seq = list(order_status_seq or [])
    fallback = default_status or {"order": {"status": "open"}}
    c.info = type("I", (), {"query_order_by_oid":
                            staticmethod(lambda user, oid: seq.pop(0) if seq else fallback)})()
    return c


def test_round_px_five_significant_digits():
    assert HyperliquidClient.round_px(104536.789) == 104540.0
    assert HyperliquidClient.round_px(3.4567891) == 3.4568
    assert HyperliquidClient.round_px(0.000123456) == 0.000123


def test_smart_order_immediate_maker_fill():
    ex = FakeExchange({"filled": {"totalSz": "0.01"}})
    c = make_client(ex)
    assert c.smart_order("BTC", True, 0.01, 0.005) == "maker"
    assert ex.market_orders == []
    assert ex.placed_px == 104540.0  # gerundeter Mid


def test_smart_order_alo_reject_falls_back():
    ex = FakeExchange({"error": "Post only order would have immediately matched"})
    c = make_client(ex)
    assert c.smart_order("BTC", True, 0.01, 0.005) == "taker_fallback"
    assert len(ex.market_orders) == 1


def test_smart_order_resting_then_filled():
    ex = FakeExchange({"resting": {"oid": 7}})
    c = make_client(ex, order_status_seq=[{"order": {"status": "filled"}}])
    assert c.smart_order("BTC", True, 0.01, 0.005, timeout_s=2, poll_s=0.01) == "maker"
    assert ex.cancelled == [] and ex.market_orders == []


def test_smart_order_timeout_cancels_and_falls_back():
    ex = FakeExchange({"resting": {"oid": 7}})
    # Order bleibt dauerhaft offen mit Rest 0.004 -> Timeout, Cancel, Market-Rest
    c = make_client(ex, default_status={"order": {"status": "open", "order": {"sz": "0.004"}}})
    kind = c.smart_order("BTC", True, 0.01, 0.005, timeout_s=0.03, poll_s=0.01)
    assert kind == "taker_fallback"
    assert ex.cancelled == [7]
    assert len(ex.market_orders) == 1
    assert abs(ex.market_orders[0][2] - 0.004) < 1e-12, "nur der offene Rest als Market"


# ---------- Echtzeit-Feed ----------

def make_feed():
    """RealtimeFeed ohne echte WS-Verbindung (Callbacks direkt füttern)."""
    from bot.realtime import RealtimeFeed

    feed = RealtimeFeed.__new__(RealtimeFeed)
    import threading

    feed.wake = threading.Event()
    feed.connected = True
    feed.fills_seen = 0
    feed.last_fill_t = 0.0
    feed._mids = {}
    feed._mids_t = 0.0
    feed._lock = threading.Lock()
    feed._info = None
    feed._subscribed = []
    return feed


def test_fill_event_wakes_loop():
    feed = make_feed()
    assert not feed.wait(0.01), "ohne Event Timeout"
    feed._on_fill({"data": {"fills": [{"coin": "BTC"}], "user": "0xa"}})
    assert feed.wait(0.01) is True, "Fill muss sofort wecken"
    assert feed.fills_seen == 1
    assert not feed.wait(0.01), "Event muss nach dem Wecken zurückgesetzt sein"


def test_fill_snapshot_ignored():
    feed = make_feed()
    feed._on_fill({"data": {"isSnapshot": True, "fills": [{}, {}]}})
    assert feed.fills_seen == 0 and not feed.wait(0.01)


def test_mids_freshness():
    feed = make_feed()
    assert feed.mids() is None
    feed._on_mids({"data": {"mids": {"BTC": "104000.5", "ETH": "3500"}}})
    mids = feed.mids()
    assert mids["BTC"] == 104000.5
    feed._mids_t = time.time() - 10  # veraltet
    assert feed.mids() is None, "alte WS-Preise dürfen nicht genutzt werden"


def test_broken_callbacks_never_crash():
    feed = make_feed()
    feed._on_fill({"kaputt": True})
    feed._on_mids({"data": {"mids": {"BTC": "nicht-numerisch"}}})
    assert feed.fills_seen == 0


# ---------- HyperliquidClient: Timeout-Weitergabe ----------

class _FakeInfo:
    """Ersetzt hyperliquid.info.Info: keine echten Netz-Calls, zeichnet nur
    den übergebenen timeout je Konstruktion auf."""
    calls: list = []

    def __init__(self, base_url, skip_ws=False, perp_dexs=None, timeout=None):
        _FakeInfo.calls.append(timeout)

    def perp_dexs(self):
        return [{"name": ""}]


def test_hyperliquid_client_threads_timeout_to_every_info_construction():
    """Verifikations-Fund: ohne expliziten Timeout wartet requests bei einem
    degradierten HL-API UNBEGRENZT auf eine Antwort (kein Fehler, kein Crash -
    der aufrufende Thread stirbt einfach nie). timeout=X muss bis zu JEDER
    Info(...)-Konstruktion durchgereicht werden (Haupt-Client, DEX-Probe,
    Markt-Client) - hier verifiziert für den /fullreport-Aufrufpfad."""
    import bot.exchange as ex_mod

    _FakeInfo.calls = []
    orig_info = ex_mod.Info
    ex_mod.Info = _FakeInfo
    try:
        ex_mod.HyperliquidClient(testnet=False, dexs="auto", timeout=20.0)
    finally:
        ex_mod.Info = orig_info
    assert _FakeInfo.calls, "keine Info()-Konstruktion aufgezeichnet"
    assert all(t == 20.0 for t in _FakeInfo.calls), \
        f"jede Info()-Konstruktion braucht den Timeout: {_FakeInfo.calls}"


def test_hyperliquid_client_default_timeout_stays_none():
    """Ohne explizites timeout darf sich das Verhalten für den Rest des Bots
    (24/7-Loop, Autopilot-Setup) NICHT ändern - Default bleibt None."""
    import bot.exchange as ex_mod

    _FakeInfo.calls = []
    orig_info = ex_mod.Info
    ex_mod.Info = _FakeInfo
    try:
        ex_mod.HyperliquidClient(testnet=False, dexs="auto")
    finally:
        ex_mod.Info = orig_info
    assert all(t is None for t in _FakeInfo.calls)


# ---------- HyperliquidClient: max_leverage (Live-Fund PENGU) ----------

class _FakeMarket:
    """Ersetzt HyperliquidClient.market: zeichnet auf, wie oft meta() wirklich
    einen Netz-Call macht (soll EINMAL je Prozess-Lauf gecacht werden)."""

    def __init__(self, universe):
        self.universe = universe
        self.calls = 0

    def meta(self, dex=""):
        self.calls += 1
        return {"universe": self.universe}


def test_max_leverage_reads_hl_per_asset_limit_and_caches():
    """Live-Fund (Nutzer 17.07.): PENGU wurde vom Bot mit dem konfigurierten
    Hebel eröffnet, den Hyperliquid für diesen Coin gar nicht anbietet (Meme-
    Perps haben oft ein niedrigeres Limit als Majors). max_leverage() muss das
    ECHTE Limit liefern - und darf es nur EINMAL je Prozess-Lauf abfragen."""
    c = HyperliquidClient.__new__(HyperliquidClient)
    c.dexs = [""]
    c.market = _FakeMarket([
        {"name": "PENGU", "szDecimals": 0, "maxLeverage": 5},
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
    ])
    c._max_leverage_cache = None

    assert c.max_leverage("PENGU") == 5
    assert c.max_leverage("BTC") == 40
    assert c.max_leverage("UNKNOWN") is None, "kein Datenpunkt -> None, kein Raten"
    assert c.market.calls == 1, "gecacht - drei Abfragen, nur ein echter meta()-Call"


def test_max_leverage_merges_across_dexs():
    """Aktien/Gold laufen auf Builder-DEXs mit eigener meta() - das Limit muss
    über ALLE konfigurierten DEXs zusammengeführt werden, nicht nur den Haupt-DEX."""
    c = HyperliquidClient.__new__(HyperliquidClient)
    c.dexs = ["", "xyz"]
    markets = {
        "": _FakeMarket([{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]),
        "xyz": _FakeMarket([{"name": "xyz:TSLA", "szDecimals": 2, "maxLeverage": 5}]),
    }

    class _Multi:
        def meta(self, dex=""):
            return markets[dex].meta(dex)

    c.market = _Multi()
    c._max_leverage_cache = None
    assert c.max_leverage("BTC") == 40
    assert c.max_leverage("xyz:TSLA") == 5


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
