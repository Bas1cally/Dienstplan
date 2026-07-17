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
import base64
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
    """Schreibt `data` als JSON nach `path` auf `branch` von `repo`, via PUT
    /repos/{repo}/contents/{path} (GitHub Contents API). Holt vorher den
    aktuellen `sha` (nötig, um eine bestehende Datei zu überschreiben statt
    einen Konflikt zu produzieren); 404 = Datei existiert noch nicht, dann
    ohne sha anlegen. `branch` MUSS bereits existieren (die Contents-API legt
    keine Branches an). True = geschrieben, False = kein Token oder Fehler."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    try:
        r = requests.get(url, headers=headers, params={"ref": branch}, timeout=timeout)
        sha = r.json().get("sha") if r.status_code == 200 else None
        if r.status_code not in (200, 404):
            log.warning("Status-Push: GET %s -> %s: %s", path, r.status_code, r.text[:200])
            return False
        body = {
            "message": "status: Quest-Bot Snapshot",
            "content": base64.b64encode(
                json.dumps(data, default=str, indent=2).encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        r = requests.put(url, headers=headers, json=body, timeout=timeout)
        if r.status_code not in (200, 201):
            log.warning("Status-Push: PUT %s -> %s: %s", path, r.status_code, r.text[:200])
            return False
        return True
    except Exception:
        log.warning("Status-Push fehlgeschlagen", exc_info=True)
        return False
