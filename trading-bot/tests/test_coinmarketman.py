"""Tests für die HyperTracker/CoinMarketMan-Tooling-Schicht (ohne Netzwerk).

Der echte API-Call wird per /cmm-Live-Probe vom VPS bestätigt (Sandbox blockt die
Domain). Hier prüfen wir nur die netzfreien Bausteine: .env-Schreiber, defensives
Zeilen-Extrahieren und die Roh-Struktur-Beschreibung."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.autopilot import set_env_var
from bot.sources.coinmarketman import CMMClient, describe_payload, rows_of


class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_leaderboard_sends_required_params(monkeypatch=None):
    """Die API verlangt rankBy+orderBy+order (400 sonst, live bewiesen) -
    der Client MUSS alle drei setzen."""
    import bot.sources.coinmarketman as cmm

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update({"url": url, "params": params, "headers": headers})
        return _FakeResp(payload={"traders": []})

    orig_get, orig_env = cmm.requests.get, dict(cmm.os.environ)
    cmm.requests.get = fake_get
    cmm.os.environ[cmm.TOKEN_ENV] = "x.y.z"
    try:
        CMMClient("https://api.example/api/external").leaderboard(
            board="perp-pnl", rank_by="pnlMonth", limit=25)
    finally:
        cmm.requests.get = orig_get
        cmm.os.environ.clear()
        cmm.os.environ.update(orig_env)
    p = captured["params"]
    assert captured["url"].endswith("/leaderboards/perp-pnl")
    assert p["rankBy"] == "pnlMonth" and p["orderBy"] == "pnlMonth"
    assert p["order"] == "desc" and p["limit"] == 25
    assert captured["headers"]["Authorization"] == "Bearer x.y.z"


def test_leaderboard_rejects_bad_rankby_and_snaps_limit():
    import bot.sources.coinmarketman as cmm

    c = CMMClient("https://api.example/api/external")
    try:
        c.leaderboard(rank_by="pnlYear")
        assert False, "ungültiges rank_by muss ValueError werfen"
    except ValueError:
        pass
    # limit wird auf erlaubte Werte (25|50|100) gerundet statt 400 zu riskieren
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params)
        return _FakeResp(payload=[])

    orig_get, orig_env = cmm.requests.get, dict(cmm.os.environ)
    cmm.requests.get = fake_get
    cmm.os.environ[cmm.TOKEN_ENV] = "x.y.z"
    try:
        c.leaderboard(limit=30)
    finally:
        cmm.requests.get = orig_get
        cmm.os.environ.clear()
        cmm.os.environ.update(orig_env)
    assert captured["limit"] == 25


def test_client_error_includes_response_body():
    """400er müssen den API-Fehlertext zeigen - '400 Client Error' ohne Body
    war live nicht debugbar."""
    import bot.sources.coinmarketman as cmm

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResp(status=400, text='{"error":"rankBy is required"}')

    orig_get, orig_env = cmm.requests.get, dict(cmm.os.environ)
    cmm.requests.get = fake_get
    cmm.os.environ[cmm.TOKEN_ENV] = "x.y.z"
    try:
        CMMClient("https://api.example/api/external").leaderboard()
        assert False, "400 muss RuntimeError werfen"
    except RuntimeError as e:
        assert "rankBy is required" in str(e), "API-Fehlertext muss durchkommen"
    finally:
        cmm.requests.get = orig_get
        cmm.os.environ.clear()
        cmm.os.environ.update(orig_env)


def test_set_env_var_replaces_and_preserves():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / ".env"
        p.write_text("A=1\nCOINMARKETMAN_TOKEN=old\nB=2\n")
        set_env_var("COINMARKETMAN_TOKEN", "new", p)
        txt = p.read_text()
        assert "COINMARKETMAN_TOKEN=new" in txt
        assert "old" not in txt, "alter Token ersetzt"
        assert "A=1" in txt and "B=2" in txt, "andere Zeilen bleiben"
        assert (p.stat().st_mode & 0o777) == 0o600, "Secret-Datei chmod 600"


def test_set_env_var_appends_when_absent():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / ".env"
        p.write_text("HL_PRIVATE_KEY=0xabc\n")
        set_env_var("COINMARKETMAN_TOKEN", "x", p)
        txt = p.read_text()
        assert "COINMARKETMAN_TOKEN=x" in txt and "HL_PRIVATE_KEY=0xabc" in txt


def test_set_env_var_creates_file():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / ".env"
        set_env_var("K", "v", p)
        assert p.read_text().strip() == "K=v"


def test_rows_of_tolerates_shapes():
    assert rows_of([{"a": 1}]) == [{"a": 1}]              # nackte Liste
    assert rows_of({"data": [{"a": 1}]}) == [{"a": 1}]    # {data: [...]}
    assert rows_of({"rows": [1, 2]}) == [1, 2]            # {rows: [...]}
    assert rows_of({"meta": {}, "leaderboard": [{"x": 1}]}) == [{"x": 1}]
    assert rows_of({"data": {"rows": [{"n": 1}]}}) == [{"n": 1}]  # verschachtelt
    assert rows_of({"nope": 5}) == []                     # keine Liste
    assert rows_of("garbage") == []


def test_describe_payload_summarizes_row():
    payload = {"data": [{"address": "0xabc", "perpEquity": 50000,
                         "pnlMonth": 1234.5, "exposureRatio": 0.8,
                         "directionalBias": "long"}]}
    s = describe_payload(payload, "pnlMonth", 25)
    assert "Zeilen erkannt: 1" in s
    assert "address" in s and "pnlMonth" in s
    assert "0xabc" in s


def test_describe_payload_empty_shows_raw():
    s = describe_payload({"unexpected": "shape"}, "pnlMonth", 25)
    assert "keine Zeilen erkannt" in s


def test_cmd_setcmm_rejects_non_jwt():
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())
    assert "JWT" in ap._cmd_setcmm("not-a-token"), "kein Punkt -> abgelehnt"
    assert "Nutzung" in ap._cmd_setcmm(""), "leeres Arg -> Hilfe"
    assert "JWT" in ap._cmd_setcmm("a.b.c"), "zu kurz -> abgelehnt"


# Exakt das live bestätigte Contract (Probe 13.07.2026): Zahlen als STRINGS.
# _ROW ist der echte Rang-1-Whale: 408x Monats-Umsatz zur Equity, 5.2x Hebel -
# ein Market-Maker, den der Kopierbarkeits-Filter aussortieren MUSS.
_ROW = {"address": "0x4e23288cee4960f9f962195c22948e4bc7ae2001",
        "age": "2026-04-08T22:40:00.026Z", "perpEquity": "4522388.484516",
        "openValue": "23471677.91024", "openValueLong": "23346067.31737",
        "exposureRatio": "5.190106509116501", "bias": "5.134555955111655",
        "pnlDay": "-736553.612319", "pnlWeek": "4542601.498803",
        "pnlMonth": "11658199.786792", "pnlAllTime": "16931007.91796",
        "rank": "1", "volumeMonth": "1844272759.2"}
# Kopierbarer Day-Trader: 20x Monats-Umsatz, 1.8x Hebel, 150k Konto.
_COPYABLE = {**_ROW, "address": "0x9e23288cee4960f9f962195c22948e4bc7ae2002",
             "perpEquity": "150000", "openValue": "270000",
             "exposureRatio": "1.8", "pnlMonth": "42000",
             "volumeMonth": "3000000", "rank": "17"}


def _cmm_cfg(**over):
    from bot.config import CoinMarketManConfig
    return CoinMarketManConfig(**{"enabled": True, "min_equity": 10_000,
                                  "min_pnl": 0, "pages": 1, **over})


def _with_fake_board(rows, fn, pages=None):
    """fetch_cmm_candidates gegen ein gefaktes Board laufen lassen.
    `pages`: Liste je Seite; sonst dieselben rows für jede Seite. Gibt
    zusätzlich die abgefragten Offsets über das Attribut .offsets preis."""
    import bot.sources.coinmarketman as cmm

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        if pages is not None:
            idx = len(calls) - 1
            page_rows = pages[idx] if idx < len(pages) else []
        else:
            page_rows = rows if len(calls) == 1 else []
        return _FakeResp(payload={"totalCount": str(len(page_rows)), "data": page_rows})

    orig_get, orig_env = cmm.requests.get, dict(cmm.os.environ)
    cmm.requests.get = fake_get
    cmm.os.environ[cmm.TOKEN_ENV] = "x.y.z"
    try:
        result = fn()
        return result, calls
    finally:
        cmm.requests.get = orig_get
        cmm.os.environ.clear()
        cmm.os.environ.update(orig_env)


def test_fetch_candidates_parses_string_numbers():
    from bot.sources.coinmarketman import fetch_cmm_candidates

    cands, _ = _with_fake_board([_COPYABLE],
                                lambda: fetch_cmm_candidates(_cmm_cfg()))
    assert len(cands) == 1
    c = cands[0]
    assert c.address == _COPYABLE["address"]
    assert abs(c.equity - 150_000) < 0.01, "String-Equity -> float"
    assert abs(c.pnl_month - 42_000) < 0.01
    assert c.pnl_day < 0, "negative Strings korrekt"
    assert c.rank == 17 and abs(c.exposure_ratio - 1.8) < 0.01


def test_fetch_candidates_filters_out_mm_whales():
    """Der echte Rang-1 (408x Umsatz, 5.2x Hebel) und andere Unkopierbare
    fliegen raus, der solide Day-Trader bleibt."""
    from bot.sources.coinmarketman import fetch_cmm_candidates

    rows = [
        _ROW,                                                         # MM: Turnover 408x
        {**_COPYABLE, "address": "0xaa" + "1" * 38,
         "exposureRatio": "7.5"},                                     # Hebel > 6
        {**_COPYABLE, "address": "0xbb" + "1" * 38,
         "perpEquity": "9000000", "volumeMonth": "9000000"},          # > max_equity
        _COPYABLE,                                                    # bleibt
    ]
    cands, _ = _with_fake_board(rows, lambda: fetch_cmm_candidates(_cmm_cfg()))
    assert [c.address for c in cands] == [_COPYABLE["address"]]


def test_fetch_candidates_filters_and_dedupes():
    from bot.sources.coinmarketman import fetch_cmm_candidates

    rows = [
        _COPYABLE,                                                        # gut
        {**_COPYABLE, "address": _COPYABLE["address"].upper()},           # Duplikat
        {**_COPYABLE, "address": "0xaa" + "1" * 38, "perpEquity": "500"}, # zu klein
        {**_COPYABLE, "address": "0xbb" + "1" * 38, "pnlMonth": "-5"},    # Verlierer
        {**_COPYABLE, "address": "kein-hex"},                             # kaputt
        "garbage",                                                        # kein dict
        {**_COPYABLE, "address": "0xcc" + "1" * 38, "perpEquity": None},  # None
    ]
    cands, _ = _with_fake_board(rows, lambda: fetch_cmm_candidates(_cmm_cfg()))
    assert [c.address for c in cands] == [_COPYABLE["address"]], \
        "nur die eine saubere Zeile überlebt Filter+Dedupe"


def test_fetch_candidates_paginates_with_offsets():
    from bot.sources.coinmarketman import fetch_cmm_candidates

    page1 = [{**_COPYABLE, "address": f"0x{i:040x}"} for i in range(1, 4)]
    page2 = [{**_COPYABLE, "address": f"0x{i:040x}"} for i in range(100, 103)]
    cands, calls = _with_fake_board(None,
                                    lambda: fetch_cmm_candidates(
                                        _cmm_cfg(pages=3, limit=100)),
                                    pages=[page1, page2, []])
    assert [c["offset"] for c in calls] == [0, 100, 200], "Seiten via offset"
    assert len(cands) == 6, "beide Seiten eingesammelt, leere Seite stoppt"


def test_discover_candidates_union_and_fallback():
    """CMM + HL werden VEREINT (CMM zuerst, dedupliziert, top_n-Deckel).
    CMM tot (kein Token) -> HL alleine. HL tot -> CMM alleine."""
    import bot.autopilot as ap_mod
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())
    ap.cfg.coinmarketman.enabled = True
    ap.cfg.coinmarketman.pages = 1
    an = ap.cfg.copytrade.analysis

    class _C:
        def __init__(self, a): self.address = a

    orig_hl = ap_mod.fetch_candidates
    import bot.sources.coinmarketman as cmm
    orig_env = dict(cmm.os.environ)
    try:
        # 1) CMM tot (kein Token) -> nur HL
        ap_mod.fetch_candidates = lambda **kw: [_C("0xHL1"), _C("0xHL2")]
        cmm.os.environ.pop(cmm.TOKEN_ENV, None)
        addrs, note = ap._discover_candidates(an)
        assert addrs == ["0xHL1", "0xHL2"] and "CMM 0" in note

        # 2) beide liefern -> Union, CMM zuerst, HL-Duplikat verschwindet
        dup = _COPYABLE["address"]
        ap_mod.fetch_candidates = lambda **kw: [_C(dup.upper()), _C("0xHL9")]
        rows = [_COPYABLE,
                {**_COPYABLE, "address": "0xdd" + "1" * 38, "rank": "18"}]
        (addrs, note), _ = _with_fake_board(rows,
                                            lambda: ap._discover_candidates(an))
        assert addrs[0] == dup and "0xdd" + "1" * 38 in addrs
        assert "0xHL9" in addrs and dup.upper() not in addrs
        assert len(addrs) <= an.top_n

        # 3) HL tot -> CMM alleine (kein Abbruch)
        def hl_boom(**kw): raise RuntimeError("leaderboard down")
        ap_mod.fetch_candidates = hl_boom
        (addrs, note), _ = _with_fake_board(rows,
                                            lambda: ap._discover_candidates(an))
        assert addrs and addrs[0] == dup
    finally:
        ap_mod.fetch_candidates = orig_hl
        cmm.os.environ.clear()
        cmm.os.environ.update(orig_env)


def test_discover_hl_not_crowded_out_and_current_leaders_kept():
    """Audit-Befund (Live: '50 Kandidaten (CMM 96 + HL 38)' -> alle 50 Plätze an
    CMM): CMM-Masse verdrängte die HL-Day-Trader (= die Häufig-Öffner, Sprints
    Signalquelle) komplett aus dem top_n-Deckel, und der Bestands-Leader wurde
    gar nicht mehr analysiert (rotate_leaders wirft Verschwundene raus).
    Jetzt: Bestands-Leader zuerst, dann Interleave beider Quellen."""
    import bot.autopilot as ap_mod
    from bot.autopilot import Autopilot
    from bot.config import load_config

    ap = Autopilot(load_config())
    ap.cfg.coinmarketman.enabled = True
    ap.cfg.coinmarketman.pages = 1
    ap.leaders = [{"address": "0xcurrent", "weight": 1.0, "score": 50}]
    an = ap.cfg.copytrade.analysis

    class _C:
        def __init__(self, a): self.address = a

    rows = [{**_COPYABLE, "address": f"0x{i:040x}"} for i in range(1, 101)]
    orig_hl = ap_mod.fetch_candidates
    ap_mod.fetch_candidates = lambda **kw: [_C("0xHL1"), _C("0xHL2"), _C("0xHL3")]
    try:
        (addrs, note), _ = _with_fake_board(rows, lambda: ap._discover_candidates(an))
    finally:
        ap_mod.fetch_candidates = orig_hl
    assert addrs[0] == "0xcurrent", "Bestands-Leader wird IMMER mit-analysiert"
    assert all(h in addrs for h in ("0xHL1", "0xHL2", "0xHL3")), \
        "HL-Kandidaten überleben den Deckel trotz CMM-Masse"
    assert len(addrs) <= an.top_n
    assert sum(a.startswith("0x00") for a in addrs) < an.top_n, "CMM füllt nur den Rest"


def test_rank_report_counts_truncated_and_reasons():
    """rank() zählt, WARUM Kandidaten sterben (zu aktiv/LARP/Fehler) und
    sammelt die Scores - Basis der /status-Trichter-Diagnose."""
    from bot.copytrade.analyzer import TraderAnalyzer, TraderMetrics

    a = TraderAnalyzer(info=None, days=30, throttle_s=0)

    def fake_analyze(addr):
        m = TraderMetrics(address=addr, account_value=50_000, days=30)
        if addr == "0xtrunc":
            m.fills_truncated = True
        elif addr == "0xboom":
            raise RuntimeError("kaputt")
        else:
            m.score = 30.0
        return m

    a.analyze = fake_analyze
    report = {}
    out = a.rank(["0xtrunc", "0xboom", "0xok"], min_score=25, report=report)
    assert [m.address for m in out] == ["0xok"]
    assert report["truncated"] == 1 and report["errors"] == 1
    assert report["analyzed"] == 2, "kaputte Wallet zählt nicht als analysiert"
    assert report["scores"] == [("0xok", 30.0)], "nur bewertete Wallets gelistet"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
