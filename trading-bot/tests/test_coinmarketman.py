"""Tests für die HyperTracker/CoinMarketMan-Tooling-Schicht (ohne Netzwerk).

Der echte API-Call wird per /cmm-Live-Probe vom VPS bestätigt (Sandbox blockt die
Domain). Hier prüfen wir nur die netzfreien Bausteine: .env-Schreiber, defensives
Zeilen-Extrahieren und die Roh-Struktur-Beschreibung."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.autopilot import set_env_var
from bot.sources.coinmarketman import describe_payload, rows_of


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
