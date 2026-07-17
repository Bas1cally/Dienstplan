"""Status-Spiegel: schreibt einen JSON-Schnappschuss des Quest-Bots periodisch
über GitHubs Contents-API in einen dedizierten Branch des Repos.

Warum (Nutzer 17.07.): Claude läuft in einer Sandbox ohne SSH-/API-Zugriff auf
den VPS - jede Diagnose brauchte bisher manuell aus Telegram rüberkopierten
Text. Dieser Kanal macht den aktuellen Bot-Zustand (Positionen, Pool, Funnel-
Zahlen, Journal-Tail) direkt per GitHub-Repo-Lesezugriff verfügbar, den Claude
in dieser wie in jeder künftigen Session ohnehin schon hat - kein Tunnel, kein
neuer öffentlicher Endpunkt, kein Rühren am bestehenden manuellen `git pull`-
Deploy-Flow (die Contents-API schreibt direkt über die GitHub-API, NICHT über
lokale git-Befehle in der Arbeitskopie).

Nur AKTIV, wenn STATUS_PUSH_TOKEN in der .env gesetzt ist (fine-grained PAT,
NUR 'Contents: Read and write' auf genau dieses Repo) - lazy gelesen wie
COINMARKETMAN_TOKEN, damit ein per Telegram/`.env`-Edit gesetzter Token ohne
Neustart wirkt. Fehlt der Token oder schlägt der Push fehl: niemals fatal für
den Tick-Loop, nur eine Warnung im Log (wie jede andere optionale Quelle hier).
"""
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

TOKEN_ENV = "STATUS_PUSH_TOKEN"
GITHUB_API = "https://api.github.com"


def _git_commit(root) -> dict:
    """Kurzer Commit-Hash + Zeitpunkt des laufenden Deploys (Nutzer 17.07.,
    'kein blinder Fleck mehr') - beantwortet direkt 'läuft mein Fix schon',
    statt raten zu müssen, ob der letzte /update den fraglichen Commit traf.
    Läuft wie /update mit `-C root` (git findet .git selbst eine Ebene höher,
    siehe deploy/trading-bot.service)."""
    try:
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        when = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%cI"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
        return {"sha": sha or None, "committed_at": when or None}
    except Exception:
        return {"sha": None, "committed_at": None}


def _tail_file(path, max_lines: int = 150, max_bytes: int = 262_144) -> list[str]:
    """Letzte `max_lines` Zeilen von `path`, ohne bei einem großen File die
    ganze Datei einzulesen (nur die letzten `max_bytes`). Fehlt die Datei
    oder ist sie nicht lesbar: leere Liste, nie ein Fehler nach oben."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def build_snapshot(ap) -> dict:
    """Kompakter Zustands-Schnappschuss: dieselben Daten, die /status, /quest,
    /quest pool und /quest funnel per Telegram zeigen, hier als JSON statt
    formatiertem Text - Claude soll GENAU das sehen können, ohne dass es
    jemand abtippen/kopieren muss. Deckt auch das ab, was NUR im Server-Log
    stand (Deploy-Version, Tick-Fehler, Feed-/Risiko-Zustand, Log-Tail) -
    Nutzer 17.07.: 'keinen blinden Fleck mehr'."""
    from .config import ROOT

    prices = ap.copier.last_prices if ap.copier else {}
    snap: dict = {
        "autopilot": dict(ap.status()),
        "wallet": ap.account_address,
        "deploy": _git_commit(ROOT),
        "started_at": datetime.fromtimestamp(ap._start_time, tz=timezone.utc).isoformat(),
        "uptime_s": round(time.time() - ap._start_time, 0),
        "risk_level": ap.guard.last_level.name if ap.guard else None,
        "analysis_note": getattr(ap, "_analysis_note", ""),
        "tick_health": {
            "last_tick_ago_s": (round(time.time() - ap._last_tick_t, 0)
                                if ap._last_tick_t else None),
            "error_count": ap._tick_error_count,
            "last_error": ap._last_tick_error,
        },
    }
    tracker = getattr(ap.copier, "tracker", None) if ap.copier else None
    snap["feed"] = {
        "snapshot_age_s": (round(time.time() - ap.copier.last_snapshots_t, 0)
                           if ap.copier and ap.copier.last_snapshots_t else None),
        "fresh": getattr(tracker, "last_fresh", None),
        "total": getattr(tracker, "last_total", None),
        "stale": getattr(tracker, "last_stale", None),
    }
    if ap.sprint:
        quest = ap.sprint.stats(prices)
        quest["analysis_funnel"] = getattr(ap, "_analysis_funnel", {})
        quest["pool_funnel"] = getattr(ap, "_pool_funnel", {})
        quest["pool"] = ap.sprint_leaders
        snap["quest"] = quest
    if ap.journal:
        snap["journal_tail"] = ap.journal.tail(50)
    snap["log_tail"] = _tail_file(ROOT / "autopilot.log")
    return snap


def push_snapshot(data: dict, repo: str, branch: str, path: str,
                  timeout: float = 15.0) -> bool:
    """Schreibt `data` als JSON nach `path` auf `branch` von `repo` - per GIT
    DATA API als 'Amend' des jeweils letzten Commits, NICHT per einfachem PUT
    über die Contents-API.

    Wave-2-Audit-Fund (17.07.): ein simples 'PUT /contents' erzeugt bei GitHub
    strukturell IMMER einen neuen Commit - bei interval_minutes=5 im 24/7-
    Betrieb wären das ~288 Commits/Tag, unbegrenzt wachsend, ohne dass die
    alten Commits für den eigentlichen Zweck (Claude liest den JEWEILS
    AKTUELLEN Zustand) irgendeinen Wert hätten. Deshalb hier der Amend-Trick
    rein über die REST-API (rührt KEINE lokale git-Arbeitskopie an, genau wie
    vorher): neuer Commit bekommt denselben Eltern-Commit wie der aktuelle
    HEAD und der Branch-Ref wird per force darauf umgebogen - der Branch
    bleibt dauerhaft bei ~2 Commits (Ursprung + der eine, ständig ersetzte
    Snapshot-Commit) statt unbegrenzt zu wachsen.

    `branch` MUSS bereits existieren UND mindestens einen Commit haben (die
    Git-Data-API legt keine Branches an). True = geschrieben, False = kein
    Token oder Fehler (nie fatal für den Tick-Loop, siehe Aufrufer)."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    base = f"{GITHUB_API}/repos/{repo}"
    content = json.dumps(data, default=str, indent=2)
    try:
        r = requests.get(f"{base}/git/refs/heads/{branch}", headers=headers, timeout=timeout)
        if r.status_code != 200:
            log.warning("Status-Push: Branch-Ref %s nicht lesbar -> %s: %s",
                       branch, r.status_code, r.text[:200])
            return False
        head_sha = r.json()["object"]["sha"]

        r = requests.get(f"{base}/git/commits/{head_sha}", headers=headers, timeout=timeout)
        if r.status_code != 200:
            log.warning("Status-Push: HEAD-Commit nicht lesbar -> %s: %s",
                       r.status_code, r.text[:200])
            return False
        head_commit = r.json()
        parent_shas = [p["sha"] for p in head_commit.get("parents", [])]
        base_tree = head_commit["tree"]["sha"]

        r = requests.post(f"{base}/git/trees", headers=headers, timeout=timeout, json={
            "base_tree": base_tree,
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}],
        })
        if r.status_code not in (200, 201):
            log.warning("Status-Push: Baum-Erstellung fehlgeschlagen -> %s: %s",
                       r.status_code, r.text[:200])
            return False
        new_tree_sha = r.json()["sha"]

        r = requests.post(f"{base}/git/commits", headers=headers, timeout=timeout, json={
            "message": "status: Quest-Bot Snapshot",
            "tree": new_tree_sha,
            "parents": parent_shas,
        })
        if r.status_code not in (200, 201):
            log.warning("Status-Push: Commit-Erstellung fehlgeschlagen -> %s: %s",
                       r.status_code, r.text[:200])
            return False
        new_commit_sha = r.json()["sha"]

        r = requests.patch(f"{base}/git/refs/heads/{branch}", headers=headers, timeout=timeout,
                           json={"sha": new_commit_sha, "force": True})
        if r.status_code not in (200, 201):
            log.warning("Status-Push: Ref-Update fehlgeschlagen -> %s: %s",
                       r.status_code, r.text[:200])
            return False
        return True
    except Exception:
        log.warning("Status-Push fehlgeschlagen", exc_info=True)
        return False
