"""Unit-Tests: Copy-Quellen-Interface + Multi-DEX-Probe (ohne Netzwerk)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import probe_dexs
from bot.sources.base import (CopySource, ExtendedSource, HyperliquidSource,
                              LighterSource, VariationalSource)

OK, WARN, FAIL, SKIP = probe_dexs.OK, probe_dexs.WARN, probe_dexs.FAIL, probe_dexs.SKIP


# ---------- Adapter-Interface ----------

class FakeTracker:
    def __init__(self):
        self.addresses = ["0xa", "0xb"]

    def snapshot(self, ref):
        from bot.copytrade.tracker import LeaderSnapshot
        return LeaderSnapshot(address=ref, equity=1000.0, positions={})


def test_hyperliquid_source_satisfies_protocol():
    src = HyperliquidSource(FakeTracker())
    assert isinstance(src, CopySource)
    assert src.name == "hyperliquid"


def test_hyperliquid_source_discover_and_snapshot():
    src = HyperliquidSource(FakeTracker())
    assert src.discover(min_score=0) == ["0xa", "0xb"]
    assert src.snapshot("0xa").address == "0xa"


def test_map_coin_identity_and_whitelist():
    assert HyperliquidSource(FakeTracker()).map_coin("BTC") == "BTC"
    src = HyperliquidSource(FakeTracker(), hl_coins={"BTC", "ETH"})
    assert src.map_coin("BTC") == "BTC"
    assert src.map_coin("TSLA:xyz") is None, "nicht handelbares Symbol -> None"


def test_foreign_stubs_refuse_loudly():
    for cls in (ExtendedSource, LighterSource, VariationalSource):
        src = cls()
        try:
            src.discover(0)
            assert False, f"{cls.__name__} müsste NotImplementedError werfen"
        except NotImplementedError as e:
            assert "probe_dexs" in str(e)
        assert src.map_coin("BTC") is None  # nichts kopierbar, bis Probe bestätigt


# ---------- Probe-Klassifikation ----------

def test_classify_auth_open_missing():
    assert probe_dexs._classify(403, {"x": 1})[0] == WARN
    assert probe_dexs._classify(401, "")[0] == WARN
    assert probe_dexs._classify(404, "")[0] == FAIL
    assert probe_dexs._classify(200, {"a": 1})[0] == OK
    assert probe_dexs._classify(200, [])[0] == WARN, "leer ist kein offener Zugriff"


def test_has_positions_detects_fields():
    assert probe_dexs._has_positions({"assetPositions": [1]}, ["assetPositions"])
    assert not probe_dexs._has_positions({"foo": 1}, ["assetPositions", "positions"])


# ---------- Probe je Venue mit injiziertem HTTP ----------

def _fetch_map(mapping):
    """mapping: url-substring -> (status, payload). fetch(method,url,body)."""
    def fetch(method, url, body):
        for frag, resp in mapping.items():
            if frag in url:
                return resp
        return 404, ""
    return fetch


def test_probe_open_positions_marks_usable():
    cfg = probe_dexs.VENUES["hyperliquid"]
    fetch = _fetch_map({
        "info": (200, {"assetPositions": [{"position": {}}], "marginSummary": {}}),
        "leaderboard": (200, [{"a": 1}]),
    })
    r = probe_dexs.probe_venue("hyperliquid", cfg, fetch=fetch)
    assert r["foreign_positions"][0] == OK
    assert "NUTZBAR" in probe_dexs.verdict(r)


def test_probe_auth_required_marks_key_only():
    cfg = probe_dexs.VENUES["hyperliquid"]  # hat sample-Adresse gesetzt
    fetch = _fetch_map({"info": (403, "forbidden"), "leaderboard": (200, [{"a": 1}])})
    r = probe_dexs.probe_venue("hyperliquid", cfg, fetch=fetch)
    assert r["foreign_positions"][0] == WARN
    assert "API-Key" in probe_dexs.verdict(r)


def test_probe_skips_when_no_sample():
    cfg = dict(probe_dexs.VENUES["extended"])  # sample = "" -> Positions-Probe skippt
    fetch = _fetch_map({"markets": (200, [{"m": 1}]), "leaderboard": (404, "")})
    r = probe_dexs.probe_venue("extended", cfg, fetch=fetch)
    assert r["reachable"][0] == OK
    assert r["foreign_positions"][0] == SKIP, "ohne Sample-ID kein Positions-Test"


def test_probe_open_but_no_position_fields_is_warn():
    cfg = probe_dexs.VENUES["hyperliquid"]
    fetch = _fetch_map({"info": (200, {"unrelated": True}), "leaderboard": (200, [1])})
    r = probe_dexs.probe_venue("hyperliquid", cfg, fetch=fetch)
    assert r["foreign_positions"][0] == WARN
    assert "keine pos-Felder" in r["foreign_positions"][1]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
