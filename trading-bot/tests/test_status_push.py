"""Tests für den Status-Spiegel (bot/status_push.py): schreibt einen JSON-
Schnappschuss übers GitHub-Repo, damit Claude den Bot-Zustand direkt lesen
kann statt ihn manuell aus Telegram kopiert zu bekommen (Nutzer 17.07.).
Netzfrei - requests.get/post/patch werden gefaked.

push_snapshot() 'amended' den letzten Commit über die Git-Data-API (Wave-2-
Audit-Fund 17.07.: eine simple Contents-API-PUT hätte bei alle-5min-Pushes
im 24/7-Betrieb unbegrenzt viele Commits erzeugt) - die Fakes hier bilden
den kompletten Fünf-Schritt-Ablauf nach (GET ref -> GET commit -> POST tree
-> POST commit -> PATCH ref, force)."""

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


def _fake_git_api(calls, ref_status=200, commit_status=200, tree_status=201,
                  commit_create_status=201, patch_status=200, parents=None):
    """Baut fake get/post/patch-Funktionen für den Amend-Ablauf. `parents`:
    Liste von Parent-SHAs des HEAD-Commits (Default: ein Parent)."""
    parents = [{"sha": "parent1"}] if parents is None else parents

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(("GET", url))
        if url.endswith("/git/refs/heads/status-feed"):
            if ref_status != 200:
                return _FakeResp(status=ref_status, text="not found")
            return _FakeResp(status=200, payload={"object": {"sha": "head1"}})
        if "/git/commits/head1" in url:
            if commit_status != 200:
                return _FakeResp(status=commit_status, text="error")
            return _FakeResp(status=200, payload={
                "tree": {"sha": "tree1"}, "parents": parents})
        raise AssertionError(f"unerwartetes GET {url}")

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(("POST", url, json))
        if url.endswith("/git/trees"):
            if tree_status not in (200, 201):
                return _FakeResp(status=tree_status, text="tree error")
            return _FakeResp(status=tree_status, payload={"sha": "tree2"})
        if url.endswith("/git/commits"):
            if commit_create_status not in (200, 201):
                return _FakeResp(status=commit_create_status, text="commit error")
            return _FakeResp(status=commit_create_status, payload={"sha": "commit2"})
        raise AssertionError(f"unerwartetes POST {url}")

    def fake_patch(url, headers=None, json=None, timeout=None):
        calls.append(("PATCH", url, json))
        return _FakeResp(status=patch_status, text="" if patch_status in (200, 201) else "conflict")

    return fake_get, fake_post, fake_patch


def _patch_requests(sp, fake_get, fake_post, fake_patch):
    orig = (sp.requests.get, sp.requests.post, sp.requests.patch, dict(sp.os.environ))
    sp.requests.get, sp.requests.post, sp.requests.patch = fake_get, fake_post, fake_patch
    sp.os.environ[TOKEN_ENV] = "ghp_test"
    return orig


def _restore_requests(sp, orig):
    sp.requests.get, sp.requests.post, sp.requests.patch = orig[0], orig[1], orig[2]
    sp.os.environ.clear()
    sp.os.environ.update(orig[3])


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


def test_push_amends_head_commit_with_same_parent():
    """Kernverhalten: der neue Commit bekommt DENSELBEN Parent wie der
    aktuelle HEAD (Amend, kein Anhängen) und der Ref wird per force
    umgebogen - der Branch wächst dadurch nie über ~2 Commits hinaus."""
    import bot.status_push as sp

    calls = []
    fake_get, fake_post, fake_patch = _fake_git_api(calls)
    orig = _patch_requests(sp, fake_get, fake_post, fake_patch)
    try:
        ok = push_snapshot({"quest": "ok"}, repo="x/y", branch="status-feed",
                           path="trading-bot/status/quest_status.json")
    finally:
        _restore_requests(sp, orig)

    assert ok is True
    kinds = [c[0] for c in calls]
    assert kinds == ["GET", "GET", "POST", "POST", "PATCH"]
    tree_call = calls[2]
    assert tree_call[1].endswith("/git/trees")
    assert tree_call[2]["base_tree"] == "tree1"
    leaf = tree_call[2]["tree"][0]
    assert leaf["path"] == "trading-bot/status/quest_status.json"
    assert json.loads(leaf["content"]) == {"quest": "ok"}, "Klartext, kein base64 nötig"
    commit_call = calls[3]
    assert commit_call[1].endswith("/git/commits")
    assert commit_call[2]["parents"] == ["parent1"], "amend: gleicher Parent wie HEAD"
    assert commit_call[2]["tree"] == "tree2"
    patch_call = calls[4]
    assert patch_call[2] == {"sha": "commit2", "force": True}


def test_push_root_commit_without_parents_stays_rootless():
    """Randfall: HEAD hat keinen Parent (Root-Commit) - der neue Commit
    bekommt dann ebenfalls keinen, statt an einem nicht-existenten Parent
    zu crashen."""
    import bot.status_push as sp

    calls = []
    fake_get, fake_post, fake_patch = _fake_git_api(calls, parents=[])
    orig = _patch_requests(sp, fake_get, fake_post, fake_patch)
    try:
        ok = push_snapshot({"a": 1}, repo="x/y", branch="status-feed", path="p.json")
    finally:
        _restore_requests(sp, orig)
    assert ok is True
    commit_call = next(c for c in calls if c[0] == "POST" and c[1].endswith("/git/commits"))
    assert commit_call[2]["parents"] == []


def test_push_returns_false_when_ref_unreadable():
    import bot.status_push as sp

    calls = []
    fake_get, fake_post, fake_patch = _fake_git_api(calls, ref_status=404)
    orig = _patch_requests(sp, fake_get, fake_post, fake_patch)
    try:
        ok = push_snapshot({"a": 1}, repo="x/y", branch="status-feed", path="p.json")
    finally:
        _restore_requests(sp, orig)
    assert ok is False
    assert len(calls) == 1, "kein weiterer Call, wenn der Branch-Ref schon fehlschlägt"


def test_push_returns_false_when_tree_creation_fails():
    import bot.status_push as sp

    calls = []
    fake_get, fake_post, fake_patch = _fake_git_api(calls, tree_status=422)
    orig = _patch_requests(sp, fake_get, fake_post, fake_patch)
    try:
        ok = push_snapshot({"a": 1}, repo="x/y", branch="status-feed", path="p.json")
    finally:
        _restore_requests(sp, orig)
    assert ok is False


def test_push_returns_false_on_ref_conflict_without_raising():
    """Zwei gleichzeitige Pushes (Haupt-Loop + manuelles /statuspush) könnten
    um den Ref konkurrieren - ein Fehlschlag beim finalen PATCH darf nie
    eine Exception werfen, nur False liefern."""
    import bot.status_push as sp

    calls = []
    fake_get, fake_post, fake_patch = _fake_git_api(calls, patch_status=409)
    orig = _patch_requests(sp, fake_get, fake_post, fake_patch)
    try:
        ok = push_snapshot({"a": 1}, repo="x/y", branch="status-feed", path="p.json")
    finally:
        _restore_requests(sp, orig)
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


class _FakeTracker:
    last_fresh = 18
    last_total = 20
    last_stale = 2


def _fake_copier(snapshots_age_s):
    import time as _t

    class _C:
        last_prices = {"BTC": 100.0}
        last_snapshots_t = _t.time() - snapshots_age_s
        tracker = _FakeTracker()

    return _C()


class _FakeGuard:
    class _Level:
        name = "NORMAL"
    last_level = _Level()


def test_build_snapshot_bundles_autopilot_quest_and_journal():
    import time as _t

    now = _t.time()

    class _Ap:
        sprint = _FakeSprint()
        copier = _fake_copier(5.0)
        journal = _FakeJournal()
        guard = _FakeGuard()
        sprint_leaders = [{"address": "0xabc", "score": 42}]
        account_address = "0xwallet"
        _analysis_funnel = {"candidates": 10}
        _pool_funnel = {"final_pool": 5}
        _analysis_note = "17 Kandidaten"
        _start_time = now - 3600
        _last_tick_t = now
        _tick_error_count = 0
        _last_tick_error = None

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
    assert snap["risk_level"] == "NORMAL"
    assert snap["analysis_note"] == "17 Kandidaten"
    assert snap["feed"]["fresh"] == 18 and snap["feed"]["total"] == 20 and snap["feed"]["stale"] == 2
    assert 4.0 <= snap["feed"]["snapshot_age_s"] <= 6.0
    assert snap["tick_health"]["error_count"] == 0 and snap["tick_health"]["last_error"] is None
    assert 0.0 <= snap["tick_health"]["last_tick_ago_s"] <= 1.0
    assert 3599.0 <= snap["uptime_s"] <= 3601.0
    assert "sha" in snap["deploy"]   # netzfrei, aber git-Aufruf läuft im echten Repo
    assert isinstance(snap["log_tail"], list)   # fehlende autopilot.log -> []


def test_build_snapshot_without_sprint_or_journal_omits_sections():
    class _Ap:
        sprint = None
        copier = None
        journal = None
        guard = None
        sprint_leaders = []
        account_address = None
        _start_time = 1_700_000_000.0
        _last_tick_t = 0.0
        _tick_error_count = 0
        _last_tick_error = None

        def status(self):
            return {"state": "stopped"}

    snap = build_snapshot(_Ap())
    assert "quest" not in snap
    assert "journal_tail" not in snap
    assert snap["risk_level"] is None
    assert snap["feed"] == {"snapshot_age_s": None, "fresh": None, "total": None,
                            "stale": None, "dex_calls": None}
    assert snap["tick_health"]["last_tick_ago_s"] is None, "0.0 heißt 'noch nie' -> None, kein Fake-Alter"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
