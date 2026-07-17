"""Tests für den Status-Spiegel (bot/status_push.py): schreibt einen JSON-
Schnappschuss übers GitHub-Repo, damit Claude den Bot-Zustand direkt lesen
kann statt ihn manuell aus Telegram kopiert zu bekommen (Nutzer 17.07.).
Netzfrei - requests.get/put werden gefaked."""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.status_push import TOKEN_ENV, build_snapshot, push_snapshot


class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_push_without_token_is_a_noop():
    import bot.status_push as sp

    orig_env = dict(sp.os.environ)
    sp.os.environ.pop(TOKEN_ENV, None)
    try:
        assert push_snapshot({"a": 1}, repo="x/y", branch="status-feed",
                             path="p.json") is False
    finally:
        sp.os.environ.clear()
        sp.os.environ.update(orig_env)


def test_push_creates_new_file_without_sha_when_missing():
    """Datei existiert noch nicht (404 auf GET) -> PUT OHNE sha, sonst lehnt
    GitHub die Erstellung ab (sha ist nur für ÜBERSCHREIBEN Pflicht)."""
    import bot.status_push as sp

    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(("GET", url, params))
        return _FakeResp(status=404, text="Not Found")

    def fake_put(url, headers=None, json=None, timeout=None):
        calls.append(("PUT", url, json))
        return _FakeResp(status=201, payload={"content": {"sha": "new"}})

    orig_get, orig_put, orig_env = sp.requests.get, sp.requests.put, dict(sp.os.environ)
    sp.requests.get, sp.requests.put = fake_get, fake_put
    sp.os.environ[TOKEN_ENV] = "ghp_test"
    try:
        ok = push_snapshot({"quest": "ok"}, repo="x/y", branch="status-feed",
                           path="trading-bot/status/quest_status.json")
    finally:
        sp.requests.get, sp.requests.put = orig_get, orig_put
        sp.os.environ.clear()
        sp.os.environ.update(orig_env)

    assert ok is True
    assert calls[0][0] == "GET" and calls[0][2] == {"ref": "status-feed"}
    method, url, body = calls[1]
    assert method == "PUT" and "sha" not in body
    decoded = json.loads(base64.b64decode(body["content"]))
    assert decoded == {"quest": "ok"}


def test_push_reuses_sha_when_file_exists():
    """Datei existiert schon (200 auf GET mit sha) -> PUT MUSS den sha
    mitschicken, sonst lehnt GitHub die Überschreibung als Konflikt ab."""
    import bot.status_push as sp

    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResp(status=200, payload={"sha": "abc123"})

    def fake_put(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _FakeResp(status=200)

    orig_get, orig_put, orig_env = sp.requests.get, sp.requests.put, dict(sp.os.environ)
    sp.requests.get, sp.requests.put = fake_get, fake_put
    sp.os.environ[TOKEN_ENV] = "ghp_test"
    try:
        ok = push_snapshot({"a": 1}, repo="x/y", branch="status-feed", path="p.json")
    finally:
        sp.requests.get, sp.requests.put = orig_get, orig_put
        sp.os.environ.clear()
        sp.os.environ.update(orig_env)

    assert ok is True
    assert calls[0]["sha"] == "abc123"


def test_push_returns_false_on_put_failure_without_raising():
    import bot.status_push as sp

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResp(status=404)

    def fake_put(url, headers=None, json=None, timeout=None):
        return _FakeResp(status=403, text="rate limited")

    orig_get, orig_put, orig_env = sp.requests.get, sp.requests.put, dict(sp.os.environ)
    sp.requests.get, sp.requests.put = fake_get, fake_put
    sp.os.environ[TOKEN_ENV] = "ghp_test"
    try:
        ok = push_snapshot({"a": 1}, repo="x/y", branch="status-feed", path="p.json")
    finally:
        sp.requests.get, sp.requests.put = orig_get, orig_put
        sp.os.environ.clear()
        sp.os.environ.update(orig_env)
    assert ok is False


def test_push_survives_network_exception():
    import bot.status_push as sp

    def fake_get(url, headers=None, params=None, timeout=None):
        raise ConnectionError("kein Netz")

    orig_get, orig_env = sp.requests.get, dict(sp.os.environ)
    sp.requests.get = fake_get
    sp.os.environ[TOKEN_ENV] = "ghp_test"
    try:
        ok = push_snapshot({"a": 1}, repo="x/y", branch="status-feed", path="p.json")
    finally:
        sp.requests.get = orig_get
        sp.os.environ.clear()
        sp.os.environ.update(orig_env)
    assert ok is False


# ---------- build_snapshot ----------

class _FakeJournal:
    def tail(self, n=50):
        return [{"kind": "sprint_entry", "coin": "BTC"}]


class _FakeSprint:
    def stats(self, prices):
        return {"equity": 1000.0, "state": "wartet auf frisches Signal"}


class _FakeCopier:
    last_prices = {"BTC": 100.0}


def test_build_snapshot_bundles_autopilot_quest_and_journal():
    class _Ap:
        sprint = _FakeSprint()
        copier = _FakeCopier()
        journal = _FakeJournal()
        sprint_leaders = [{"address": "0xabc", "score": 42}]
        account_address = "0xwallet"
        _analysis_funnel = {"candidates": 10}
        _pool_funnel = {"final_pool": 5}

        def status(self):
            return {"state": "running"}

    snap = build_snapshot(_Ap())
    assert snap["autopilot"] == {"state": "running"}
    assert snap["wallet"] == "0xwallet"
    assert snap["quest"]["equity"] == 1000.0
    assert snap["quest"]["pool"] == [{"address": "0xabc", "score": 42}]
    assert snap["quest"]["analysis_funnel"] == {"candidates": 10}
    assert snap["quest"]["pool_funnel"] == {"final_pool": 5}
    assert snap["journal_tail"] == [{"kind": "sprint_entry", "coin": "BTC"}]


def test_build_snapshot_without_sprint_or_journal_omits_sections():
    class _Ap:
        sprint = None
        copier = None
        journal = None
        sprint_leaders = []
        account_address = None

        def status(self):
            return {"state": "stopped"}

    snap = build_snapshot(_Ap())
    assert "quest" not in snap
    assert "journal_tail" not in snap


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
