"""Autopilot: der vollautomatische Betriebsmodus.

Kein manuelles analyze_traders.py + copy_bot.py mehr - der Autopilot macht alles:

  1. Analysiert das Leaderboard beim Start und danach alle `reanalyze_hours`
     (Funnel + LARP-Filter wie gehabt) und ROTIERT die Leader automatisch:
     Leader, die unter den Mindest-Score fallen, fliegen raus; Plätze werden
     mit den besten Neuen aufgefüllt. Bestehende Leader haben leichten
     Bestandsschutz, damit nicht bei jedem Lauf das halbe Portfolio dreht.
  2. Spiegelt die Leader-Positionen (Reconciliation, copier.py)
  3. Überwacht News + Preis-Schocks (MarketGuard) mit CAUTION/RISK_OFF
  4. Schreibt den Live-Zustand nach runtime/status.json (Frontend liest das)

Threads: ein Worker-Loop; start()/stop() für die Steuerung über das Web-UI.
"""

import json
import logging
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from hyperliquid.info import Info

from .config import Config, load_credentials
from .convergence import ConvergenceEngine
from .copytrade.analyzer import TraderAnalyzer, TraderMetrics
from .copytrade.copier import CopyTrader
from .copytrade.larp import LarpConfig, LarpFilter
from .copytrade.leaderboard import fetch_candidates
from .copytrade.tracker import LeaderTracker
from .exchange import HyperliquidClient, api_url
from .investigator import WalletWatcher
from .journal import Journal
from .news.guard import MarketGuard
from .notify import Notifier

log = logging.getLogger(__name__)

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def set_env_var(key: str, value: str, env_path: Path = ENV_FILE) -> None:
    """Setzt/ersetzt EINE Zeile `key=value` in der .env, andere Zeilen bleiben.
    chmod 600. Für per-Telegram gesetzte Read-only-Tokens (nicht für Wallet-Keys)."""
    lines = []
    if env_path.exists():
        lines = [l for l in env_path.read_text().splitlines()
                 if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def chunk_for_telegram(text: str, limit: int = 3800) -> list[str]:
    """Teilt Text in Telegram-taugliche Häppchen (Hard-Limit der API: 4096
    Zeichen/Nachricht, `limit` lässt Puffer für den '📊 Report i/N'-Header).
    Bricht NUR an Zeilengrenzen (nie mitten im Wort/einer Zahl) - eine
    Ausnahme-lange Einzelzeile wird als letzter Ausweg hart geschnitten."""
    if not text:
        return [""]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def flush() -> None:
        # Eine geflushte Gruppe aus nur leeren Zeilen ergibt beim Join "" -
        # das darf NIE als eigener (unsichtbarer) Chunk landen: eine Telegram-
        # Nachricht nur mit '📊 Report i/N'-Header ohne Inhalt, plus ein
        # aufgeblähtes N (Regressionsfund: führende Leerzeile + fast
        # limit-lange Folgezeile).
        if cur:
            joined = "\n".join(cur)
            if joined:
                chunks.append(joined)
            cur.clear()

    for line in text.split("\n"):
        if cur and cur_len + len(line) + 1 > limit:
            flush()
            cur_len = 0
        if len(line) > limit:
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        cur.append(line)
        cur_len += len(line) + 1
    flush()
    return chunks or [""]




def cap_path_b_admission(
    path_a: list[TraderMetrics],
    path_b: list[TraderMetrics],
    pool_size: int,
    max_share: float,
) -> tuple[list[TraderMetrics], int]:
    """Deckelt, wie viele Pfad-B-Wallets ('Positions-Trader mit grünem
    offenen Buch') in den Sprint-Pool aufgenommen werden - fest als Anteil
    von pool_size, NICHT relativ zur Pfad-A-Anzahl.

    Live-Befund (18.07., zweiter Fund derselben Diagnose): die reine
    Nachrang-Sortierung in _build_sprint_pool griff faktisch nie, weil
    sprint_ok fast immer UNTER pool_size lag - ohne Überangebot schneidet
    [:n] nichts ab. Dieser Deckel greift dagegen an der ADMISSION selbst,
    unabhängig vom Kandidaten-Angebot. Pfad B bleibt gültig (Nutzer will
    'mehr Wallets'), kann den SPRINT-DISKUTIERTEN Pool aber nicht mehr mit
    strukturell signal-unfähigen Positions-Sitzern volllaufen.

    GILT NICHT für forced_main in _build_sprint_pool (Wave-3-Audit-Fund
    18.07.): ein Haupt-Buch-Leader wird dort IMMER angehängt, selbst wenn
    er hier als überzähliger Pfad-B-Kandidat gedeckelt wurde - das ist
    KEIN Leck, sondern ein älteres, bewusstes Invariant ('Sprint darf
    Haupt-Leader nie aus den Augen verlieren', siehe dortiger Docstring).
    Ein Haupt-Leader hat zudem die STRENGEREN Hauptbuch-Kriterien (30+
    Round-Trips, 10+ aktive Tage, <25% Drawdown, 60%+ profitable Wochen)
    bereits bestanden - ein grundlegend anderes Risikoprofil als ein
    frisch entdeckter Pfad-B-Kandidat mit z.B. 0 Trips. Der Deckel hier
    bezieht sich also nur auf sprint_ok, nicht auf den finalen Pool
    inklusive Haupt-Leader-Zwangsergänzung.

    Gibt (zugelassene Wallets, Anzahl gedeckelter Pfad-B-Wallets) zurück.
    """
    b_cap = max(0, round(pool_size * max_share))
    path_b_sorted = sorted(path_b, key=lambda m: m.sprint_score, reverse=True)
    ko = max(0, len(path_b_sorted) - b_cap)
    return path_a + path_b_sorted[:b_cap], ko


def rotate_leaders(
    current: list[dict],
    ranked: list[TraderMetrics],
    max_leaders: int,
    min_keep_score: float,
    keep_bonus: float = 5.0,
) -> list[dict]:
    """Entscheidet die neue Leader-Liste nach einer Re-Analyse.

    Bestandsschutz: aktuelle Leader bekommen `keep_bonus` Punkte auf ihren
    frischen Score, bevor verglichen wird - das verhindert Churn, wenn zwei
    Trader praktisch gleichauf liegen. Wer unter `min_keep_score` fällt,
    fliegt trotzdem kompromisslos raus.
    """
    current_addrs = {l["address"] for l in current}
    by_addr = {m.address: m for m in ranked}

    pool = []
    for m in ranked:
        if m.score < min_keep_score and m.address in current_addrs:
            log.info("Rotation: %s fällt raus (Score %.1f < %.1f)", m.address[:10], m.score, min_keep_score)
            continue
        eff = m.score + (keep_bonus if m.address in current_addrs else 0.0)
        pool.append((eff, m))
    # Aktuelle Leader, die in der neuen Analyse gar nicht mehr auftauchen
    # (z.B. aus dem Top-1% gefallen), werden NICHT blind behalten - raus.
    pool.sort(key=lambda t: t[0], reverse=True)
    top = [m for _, m in pool[:max_leaders]]

    total = sum(m.score for m in top) or 1.0
    return [
        {
            "address": m.address,
            "weight": round(m.score / total, 4),
            "score": m.score,
            "roi_pct": round(m.roi * 100, 2),
            "profit_factor": round(min(m.profit_factor, 999), 2),
            "max_drawdown_pct": round(m.max_drawdown * 100, 2),
            "closed_trades": m.closed_trades,
        }
        for m in top
    ]


class Autopilot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict = {"state": "stopped"}
        self._lock = threading.Lock()
        self._last_analysis = 0.0
        self._last_status_push = 0.0   # siehe _maybe_push_status
        self._status_push_running = False   # verhindert Parallel-Pushes im Hintergrund-Thread
        self._status_push_thread: threading.Thread | None = None
        # Tick-Loop-Gesundheit fürs Status-Spiegel (Nutzer 17.07., 'kein blinder
        # Fleck mehr') - bisher nur im Server-Log sichtbar, wenn ein Tick
        # crasht; jetzt auch im Snapshot, ohne dass jemand den Log lesen muss.
        self._last_tick_t = 0.0
        self._last_tick_error: str | None = None
        self._tick_error_count = 0
        self._force_analysis = False   # /analyze: nächster Loop-Tick analysiert sofort
        self._analysis_note = ""       # Trichter der letzten Analyse (für /status)
        self._analysis_running = False # Analyse läuft im Hintergrund-Thread -
        self._analysis_started = 0.0   # /status zeigt "läuft seit X min"
        self._analysis_thread: threading.Thread | None = None
        self._fullreport_thread: threading.Thread | None = None
        self._fullreport_running = False
        self._stale_feed_warned = False  # Sprint scannt Standbilder -> einmal warnen
        self._market_open: bool | None = None  # zuletzt bekannter US-Börsen-Zustand (Worldclock)
        # Trichter-Zahlen für /quest funnel (Nutzer: 'proper Analyse-Tools, die
        # ich selbst durchspiegeln kann') - je Filterstufe, wie viele Kandidaten
        # übrig blieben. Zwei Teile: die Analyse selbst (_reanalyze_body, läuft
        # alle reanalyze_hours bzw. per /analyze) und der Pool-Aufbau
        # (_build_sprint_pool, läuft danach + bei jedem Gong). Persistiert
        # (Nutzer 17.07., 'kein blinder Fleck mehr'): sonst sind diese Zahlen
        # nach JEDEM Neustart bis zu reanalyze_hours lang blank, weil
        # _load_or_analyze_leaders() den Pool aus der Datei lädt statt neu zu
        # bauen - genau dann, wenn eine Diagnose sie am ehesten bräuchte.
        _cache = self._load_funnel_cache()
        self._analysis_funnel: dict = _cache.get("analysis_funnel", {})
        self._pool_funnel: dict = _cache.get("pool_funnel", {})
        self.leaders: list[dict] = []
        # Eigener, breiterer Leader-Pool nur fürs Sprint-Buch (siehe sprint.pool_size).
        # Obermenge der Haupt-Leader; das Hauptbuch bleibt bei self.leaders.
        self.sprint_leaders: list[dict] = []
        self.client: HyperliquidClient | None = None
        self.copier: CopyTrader | None = None
        self.guard: MarketGuard | None = None
        self.account_address: str | None = None
        self.error: str | None = None
        self.notifier = Notifier()
        self.journal = Journal()
        self._prev_risk = "NORMAL"
        self._prev_halted = False
        self._last_history_write = 0.0
        self._leader_perf: dict = self._load_perf()
        self.watcher: WalletWatcher | None = None
        self.scout = None
        self.poly_scout = None
        self.book_scout = None
        self.twap_scout = None
        self.labs = None
        self.sprint = None
        self.lighter = None
        self.scalper = None
        self.feed = None
        self._last_watch_poll = 0.0
        self._digest_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._digest_equity: float | None = None
        self._watchdog_warned = False
        self._start_time = time.time()
        self.commander = self._build_commander()

    def _build_commander(self):
        """Telegram-Fernsteuerung (nur aktiv, wenn TELEGRAM_* gesetzt)."""
        from .telecmd import TelegramCommander

        return TelegramCommander({
            "/status": self._cmd_status,
            "/report": self._cmd_report,
            "/fullreport": self._cmd_fullreport,
            "/leaders": self._cmd_leaders,
            "/positions": self._cmd_positions,
            "/anomalies": self._cmd_anomalies,
            "/orderbook": self._cmd_orderbook,
            "/twap": self._cmd_twap,
            "/quest": self._cmd_sprint,
            "/sprint": self._cmd_sprint,   # Alt-Alias (Muskelgedächtnis) - Quest-Bot ist der neue Name
            "/polymarket": self._cmd_polymarket,
            "/stop": self._cmd_stop,
            "/start": self._cmd_start,
            "/resume": self._cmd_resume,
            "/update": self._cmd_update,
            "/probe": self._cmd_probe,
            "/lighter": self._cmd_lighter,
            "/cmm": self._cmd_cmm,
            "/setcmm": self._cmd_setcmm,
            "/setstatuspush": self._cmd_set_status_push,
            "/statuspush": self._cmd_status_push,
            "/analyze": self._cmd_analyze,
            "/help": self._cmd_help,
        })

    # ---------- Telegram-Befehle (Antworten als HTML-String) ----------

    def _cmd_help(self) -> str:
        return ("<b>Befehle</b>\n/status – Zustand & Schatztruhe\n"
                "/quest – Quest-Bot (1000$, Hebel je Coin, Ziel +100$/Zyklus)\n"
                "/quest pool – Quest-Pool mit Richtungs-Scores\n"
                "/quest assets – Krypto vs. Aktien-Perps PnL-Vergleich\n"
                "/quest funnel – Trichter-Diagnose (warum ist der Pool so klein/idle?)\n"
                "/quest reset – Bilanz (Zyklus/Schatztruhe) auf 0, Strikes/Bans bleiben\n"
                "/report – Quest-Auswertung\n/positions – offene Positionen + PnL\n"
                "/leaders – Quest-Pool + ROI\n"
                "/update – Update ziehen + neu starten\n"
                "/probe – Multi-DEX-Scan-Probe (Extended/Lighter/…)\n"
                "/lighter &lt;ref&gt; – Lighter-Konto prüfen (Verifikation)\n"
                "/setcmm &lt;token&gt; [slot] – HyperTracker-API-Token setzen "
                "(slot 2-5 = Fallback bei ausgeschöpftem Tageslimit)\n"
                "/cmm – HyperTracker-Leaderboard live proben\n"
                "/setstatuspush &lt;token&gt; – GitHub-PAT für den Status-Spiegel setzen\n"
                "/statuspush – Status-Spiegel sofort schreiben (Test)\n"
                "/analyze – Leader-Analyse sofort anstoßen\n"
                "/fullreport [offline] – kompletter Quest-Report zum Copy-Paste\n"
                "/stop /start /resume – Autopilot/Halt steuern\n"
                "<i>(/sprint funktioniert weiter als Alt-Name für /quest)</i>")

    def _cmd_status(self) -> str:
        if self._analysis_running:
            mins = (time.time() - self._analysis_started) / 60
            analysis = (f"Analyse: ⏳ LÄUFT seit {mins:.0f} min (50 Wallets dauern "
                        f"einige Minuten, bei API-Drossel länger)")
        elif self._force_analysis:
            analysis = "Analyse: angefordert - startet im nächsten Tick"
        else:
            age_h = (time.time() - self._last_analysis) / 3600 if self._last_analysis else None
            next_h = max(0.0, self.cfg.autopilot.reanalyze_hours - age_h) if age_h is not None else None
            analysis = (f"Analyse: vor {age_h:.1f}h (nächste in {next_h:.1f}h)"
                        if age_h is not None else "Analyse: noch keine gelaufen")
        if self._analysis_note and not self._analysis_running:
            analysis += f"\n{self._analysis_note}"
        # Quest-Bot ist das ganze System - /status dreht sich nur noch um ihn
        # und die Schatztruhe, kein Kopier-Buch mehr (das läuft still als Feed).
        risk = self.guard.last_level.name if self.guard else "NORMAL"
        ws = "an" if self.feed and self.feed.connected else "aus"
        if not self.sprint:
            return (f"<b>Status</b>: Quest-Bot nicht aktiv (sprint.enabled/dry_run prüfen)\n"
                    f"Risiko: {risk} | Pool: {len(self.sprint_leaders)} | WS: {ws}\n{analysis}")
        q = self.sprint.stats(self.copier.last_prices if self.copier else {})
        return (f"<b>Status Quest-Bot</b>: {q['state']}\n"
                f"Schatztruhe: {q['banked']:+,.2f} $ | Zyklus {q['cycle']} "
                f"({q['won']}✅ {q['busted']}💥)\n"
                f"Risiko: {risk} | Pool: {len(self.sprint_leaders)} scanbar | WS: {ws}\n"
                f"{analysis}\n"
                f"<i>/quest = Details | /analyze = Analyse sofort anstoßen</i>")

    def _quest_state(self) -> dict:
        """State-Dict für quest_scorecard aus dem laufenden Buch (statt die
        JSON-Datei neu zu lesen) - so ist der Report immer taufrisch."""
        return {
            "won": self.sprint.won, "busted": self.sprint.busted,
            "strikes": self.sprint.strikes, "banned": sorted(self.sprint.banned),
            "confidence": self.sprint.confidence,
        }

    def _journal_chrono(self, n: int = 5000) -> list[dict]:
        """Journal in CHRONOLOGISCHER Reihenfolge (älteste zuerst) - journal.tail()
        liefert neueste-zuerst, quest_scorecard braucht aber die Zyklus-Folge in
        echter Zeit-Ordnung (die letzten won+busted = aktuelle Ära). Bricht auch
        Gleich-Sekunden-Ties korrekt (Datei-Anhänge-Reihenfolge = Zyklus-Folge)."""
        return list(reversed(self.journal.tail(n)))

    def _cmd_report(self) -> str:
        """Quest-Auswertung mit dem eigentlichen Nutzen: welche Wallet hat
        verdient, welche verkackt (gesamte Historie) + Krypto-vs-Aktien der
        aktuellen Ära. Der Report, der den Bot besser macht."""
        if not self.sprint:
            return "Quest-Bot nicht aktiv (sprint.enabled / dry_run prüfen)."
        from .report import quest_scorecard

        s = self.sprint.stats(self.copier.last_prices if self.copier else {})
        sc = quest_scorecard(self._journal_chrono(), self._quest_state())
        total = s["won"] + s["busted"]
        wr = (s["won"] / total * 100) if total else 0.0
        lines = [
            "<b>Quest-Report</b>",
            f"Schatztruhe: {s['banked']:+,.2f} $ | Zyklen {total} "
            f"({s['won']}✅ {s['busted']}💥" + (f", {wr:.0f}%" if total else "") + ")",
        ]

        def _mark(l):
            if l["banned"]:
                return " 🚫"
            if l["star"]:
                return " ⭐"
            return f" ({l['strikes']}S)" if l["strikes"] else ""

        leaders = sc["leaders"]
        if leaders:
            lines.append("\n<b>Beste Leader</b> (Netto-PnL, gesamte Historie):")
            for l in leaders[:3]:
                lines.append(f"  <code>{l['addr'][:10]}…</code> {l['pnl']:+,.2f}$ "
                             f"({l['rides']}R {l['wins']}-{l['losses']}){_mark(l)}")
            losers = [l for l in leaders if l["pnl"] < 0]
            if losers:
                lines.append("<b>Schwächste</b> (Prune-Kandidaten):")
                for l in losers[-3:][::-1]:
                    lines.append(f"  <code>{l['addr'][:10]}…</code> {l['pnl']:+,.2f}$ "
                                 f"({l['rides']}R {l['wins']}-{l['losses']}){_mark(l)}")

        assets = sc["assets"]
        if any(b["n"] for b in assets.values()):
            lines.append(f"\n<b>Krypto vs. Aktien</b> (aktuelle Ära, {sc['era_cycles']} Zyklen):")
            for name in ("Krypto", "Aktien", "unbekannt"):
                b = assets[name]
                if not b["n"]:
                    continue
                wr2 = b["won"] / b["n"] * 100
                lines.append(f"  {name}: {b['n']} Zyklen, {b['pnl']:+,.2f}$ ({wr2:.0f}%)")

        lines.append("\n<i>/fullreport = volle Leader-Tabelle zum Copy-Paste</i>")
        return "\n".join(lines)

    def _cmd_fullreport(self, arg: str = "") -> str:
        """Kompletter Report (Inhalt von `python report.py`: Vetos mit Signifikanz,
        alle Scouts, Strategie-Labor, Sprint-Buch, Lighter-Schatten, Shadow-
        Varianten, Empfehlungen) per Telegram zum Copy-Paste - für den Fall, dass
        SSH/Konsole gerade nicht erreichbar sind, der Bot aber antwortet. Läuft im
        Hintergrund (Netz-Preis-Lookups für die Signifikanz-Analyse können dauern),
        kommt in mehreren Nachrichten (Telegrams 4096-Zeichen-Limit je Nachricht).
        Arg 'offline' = schnell, ohne die netzbasierte Signifikanz-Analyse."""
        if not (self.notifier and self.notifier.enabled):
            return "Voller Report braucht Telegram-Push (Ergebnis wird gesendet)."
        if self._fullreport_running:
            # Ohne Sperre würde ein Doppel-Tap (naheliegend genau in der Panik-
            # Situation, für die dieses Feature gebaut ist - die Bestätigung
            # verrät nicht, dass schon einer läuft) einen zweiten, parallelen
            # Netz-Lauf gegen die HL-API starten und verschachtelte Telegram-
            # Sends aus zwei Threads erzeugen.
            return "⏳ Voller Report läuft bereits - Ergebnis kommt gleich."
        offline = arg.strip().lower() == "offline"
        self._fullreport_running = True   # vor Thread-Start setzen (kein Race)

        def run():
            try:
                try:
                    import report as report_mod

                    text = report_mod.build_report(offline=offline)
                except Exception as e:
                    text = f"⚠️ Voller Report fehlgeschlagen: {str(e)[:300]}"
                chunks = chunk_for_telegram(text)
                for i, chunk in enumerate(chunks, 1):
                    header = f"📊 Report {i}/{len(chunks)}\n\n" if len(chunks) > 1 else "📊 Report\n\n"
                    # html=False: der Report-Text ist ungeprüfter Freitext (Adressen,
                    # Prozentzeichen, Klammern) - ohne parse_mode gibt's kein 400-Risiko
                    self.notifier.send(header + chunk, html=False)
                    if i < len(chunks):
                        time.sleep(0.4)   # Telegram-Rate-Limit-Hygiene zwischen Nachrichten
            finally:
                self._fullreport_running = False

        # Referenz auf self (statt nur lokal) - Tests können sauber .join(),
        # ohne den Thread per threading.enumerate() suchen zu müssen (race-anfällig
        # bei schnell durchlaufenden Mock-Funktionen).
        self._fullreport_thread = threading.Thread(target=run, daemon=True, name="fullreport")
        self._fullreport_thread.start()
        mode = "offline, schnell" if offline else "online mit Signifikanz-Analyse, kann 1-2 Min dauern"
        return (f"📊 Voller Report wird gebaut ({mode}) … kommt gleich in mehreren "
                f"Nachrichten zum Copy-Paste.\n<i>/fullreport offline = ohne Netz-Analyse, schneller</i>")

    def _cmd_positions(self) -> str:
        """Was hält der Quest-Bot gerade? Offene Positionen mit unrealisiertem PnL.
        Kopier-Buch/Labs sind stillgelegt - es gibt nur noch den Quest-Bot."""
        prices = self.copier.last_prices if self.copier else {}
        out: list[str] = []
        if self.sprint:
            srows = self.sprint.paper.position_rows(prices)
            if srows:
                out.append("<b>Quest-Bot</b>")
                for r in srows:
                    side = "LONG" if r["size"] > 0 else "SHORT"
                    out.append(f"{side} {r['coin']}: {abs(r['size']):.4f} @ {r['entry']:.4f} "
                               f"(PnL {r['unrealized_pnl']:+,.2f})")
        return "\n".join(out) if out else "Quest-Bot hält aktuell nichts (wartet auf frisches Signal)."

    def _cmd_leaders(self) -> str:
        if not self.leaders:
            return "Noch keine Leader (Analyse läuft?)."
        out = ["<b>Leader</b>"]
        for l in self._with_performance(self.leaders):
            roi = l.get("roi_since_copy_pct")
            out.append(f"<code>{l['address'][:10]}…</code> Score {l.get('score', '?')}"
                       + (f" | {roi:+.2f}% seit Kopie" if roi is not None else ""))
        return "\n".join(out)

    def _cmd_anomalies(self) -> str:
        flagged = self.scout.flagged[-5:] if self.scout else []
        if not flagged:
            return "Noch keine Anomalien gemeldet."
        out = ["<b>Anomalie-Scout</b>"]
        for f in reversed(flagged):
            out.append(f"{f['side']} {f['coin']} ${f['notional']:,.0f} "
                       f"<code>{f['address'][:10]}…</code>")
        return "\n".join(out)

    def _cmd_orderbook(self) -> str:
        flagged = self.book_scout.flagged[-6:] if self.book_scout else []
        if not flagged:
            return "Noch keine Orderbuch-Signale (oder Scout aus)."
        out = ["<b>Orderbuch-Scout</b>"]
        for f in reversed(flagged):
            out.append(f"{f['coin']}: Imbalance {f['imbalance']:+.2f}"
                       + (f" | Wall {f['wall']}" if f.get("wall") else ""))
        return "\n".join(out)

    def _cmd_sprint(self, arg: str = "") -> str:
        if not self.sprint:
            return "Quest-Bot nicht aktiv (sprint.enabled / dry_run prüfen)."
        prices = self.copier.last_prices if self.copier else {}
        if arg.lower().strip() == "close":
            n = self.sprint.close(prices)
            # Nutzer-Fund (17.07., Wave-2-Audit): "kein Strike" stimmte seit dem
            # Entfernen von "manual" aus _STRIKE_EXEMPT nicht mehr - ein manueller
            # Close im Minus striked den Leader jetzt wie jeder andere Verlust-Ritt.
            return (f"⏹ Quest-Zyklus manuell beendet ({n} Position(en)) - sofort "
                    "verbucht (Verlust striked den Leader wie jeder andere Ritt, "
                    "Gewinn heilt). Nächster Zyklus wartet auf frisches Signal."
                    if n else "Quest-Bot hält gerade nichts.")
        if arg.lower().strip() == "reset":
            # verbucht wie /quest close (s.o.) - Verlust striked, Gewinn heilt
            n = self.sprint.close(prices)   # offene Position(en) zuerst sauber raus
            self.sprint.reset_bilanz()
            return ("🧹 <b>Bilanz zurückgesetzt</b>: Zyklus 1, Schatztruhe 0,00 $"
                    + (f" ({n} offene Position(en) davor geschlossen)" if n else "")
                    + ".\nStrikes/Bans/Confidence bleiben erhalten.")
        if arg.lower().strip() in ("amnestie", "amnesty"):
            # Nutzer 19.07.: frische Datensammlung unter den Trail-Regeln -
            # die alten Strikes entstanden unterm alten Exit-Regime (Ritte
            # standen im Plus, wurden trotzdem im Minus beendet - der Strike
            # traf den Leader für UNSER Exit-Timing).
            n_strikes, n_bans = self.sprint.amnesty()
            self._force_analysis = True   # Ex-Gebannte sofort zurück in den Kandidatenkreis
            return (f"🕊 <b>Amnestie</b>: {n_strikes} Strike-Konten und {n_bans} Bans "
                    f"gelöscht - alle Leader starten unter den neuen Trail-Regeln "
                    f"bei null. Confidence/Gewinn-Historie bleiben. "
                    f"Frische Analyse angestoßen (Ex-Gebannte können zurück in den Pool).")
        if arg.lower().strip() == "pool":
            if not self.sprint_leaders:
                return "Quest-Pool ist leer (nächste Analyse: /analyze)."
            from .sprint import STAR_THRESHOLD

            banned = self.sprint.banned
            idle = self.sprint.idle_addrs(self.cfg.sprint.rotate_idle_hours * 3600)
            # Halten die Pool-Wallets gerade überhaupt Positionen? (aus den
            # Live-Snapshots) - macht den Schläfer-Verdacht direkt sichtbar:
            # 2/20 mit Position = fast alles Schläfer.
            snaps = getattr(self.copier, "last_snapshots", []) if self.copier else []
            holds = {s.address.lower(): bool(s.positions) for s in snaps}
            n_hold = sum(1 for l in self.sprint_leaders
                         if holds.get(str(l.get("address", "")).lower()))
            lines = [f"<b>Quest-Pool</b> ({n_hold}/{len(self.sprint_leaders)} halten "
                     f"gerade Positionen)"]
            any_confidence = False
            for l in self.sprint_leaders:
                a = str(l.get("address", ""))
                badges = []
                if a.lower() in banned:
                    badges.append("🚫")
                elif self.sprint.is_star(a):
                    badges.append("⭐")
                else:
                    # Nutzer-Fund (17.07.): Confidence-Fortschritt vor dem
                    # ⭐-Sprung war komplett unsichtbar ("2 erfolgreiche
                    # Trader, aber keine Punkte gesehen") - jetzt als
                    # Fortschritt zum Star sichtbar, nicht erst binär bei 100.
                    conf = self.sprint.confidence_of(a)
                    if conf:
                        badges.append(f"🔸{conf}/{STAR_THRESHOLD}")
                        any_confidence = True
                if a.lower() in idle and a.lower() not in banned:
                    badges.append("💤")   # stumm, rotiert beim nächsten Rebuild nach hinten
                mark = (" " + " ".join(badges)) if badges else ""
                pos = "📈" if holds.get(a.lower()) else "· "   # hält gerade / flach
                lines.append(f"{pos}<code>{a[:12]}…</code> Score {l.get('score', '?')}{mark}")
            lines.append("<i>📈 = hält gerade eine Position | · = flach</i>"
                         + (f" | 💤 = >{self.cfg.sprint.rotate_idle_hours:.0f}h stumm" if idle else "")
                         + (" | 🔸 = Confidence-Fortschritt zum ⭐" if any_confidence else ""))
            # Lighter-Signale (Nutzer 17.07., Option B) separat anzeigen - sie
            # stehen nie in self.sprint_leaders (nur ad-hoc pro Tick beigemischt,
            # siehe _sprint_tick_inputs), wären hier sonst unsichtbar obwohl sie
            # tatsächlich mitscannen.
            if self.lighter and self.cfg.lighter.sprint_promote:
                extra_leaders, extra_snaps = self.lighter.sprint_snapshots(prices)
                if extra_leaders:
                    extra_holds = {s.address.lower(): bool(s.positions) for s in extra_snaps}
                    lines.append(f"\n<b>+ Lighter-Signale</b> "
                                f"({sum(extra_holds.values())}/{len(extra_leaders)} halten "
                                f"Positionen, kein Vorab-Filter - nur Strikes/Zeit-Cut)")
                    for l in extra_leaders:
                        a = str(l["address"])
                        mark = " 🚫" if a.lower() in banned else ""
                        pos = "📈" if extra_holds.get(a.lower()) else "· "
                        lines.append(f"{pos}<code>{a}</code> Score {l['score']:.0f}{mark}")
            return "\n".join(lines)
        if arg.lower().strip() == "assets":
            return self._sprint_asset_breakdown()
        if arg.lower().strip() == "funnel":
            return self._cmd_quest_funnel()
        if arg.lower().strip() in ("cohorts", "kohorten"):
            return self._cmd_quest_cohorts()
        s = self.sprint.stats(prices)
        lead = (f"<code>{s['leader'][:10]}…</code>" + (" ⭐" if s.get("leader_is_star") else "")
                if s.get("leader") else "n/a")
        # Einzelne Positionen mit Entry + eigenem unrealisiertem PnL (nicht nur
        # 'LONG HYPE' ohne Zahlen). Zyklus = Ritt (v3): keine separate Ritt-PnL
        # mehr nötig, cycle_pnl IST die PnL des laufenden Ritts.
        if s.get("positions"):
            pos_lines = "\n".join(
                f"  {'LONG' if p['size'] > 0 else 'SHORT'} {p['coin']}: "
                f"{abs(p['size']):.4f} @ {p['entry']:.4f} (PnL {p['unrealized_pnl']:+,.2f} $)"
                + (f" — <code>{p['leader']}…</code>" if p.get("leader") else "")
                for p in s["positions"])
            pos_block = f"Positionen:\n{pos_lines}"
        else:
            pos_block = "Positionen: -"
        equity_line = (f"Ritte: {len(s.get('positions') or [])} offen "
                       f"(je 1.000$-Basis, Ziel +100$/Ritt) | "
                       f"Σ offene PnL {s['cycle_pnl']:+,.2f} $"
                       if s.get("parallel") else
                       f"Equity: {s['equity']:,.2f} / Ziel {s['target']:,.0f} "
                       f"(Zyklus-PnL {s['cycle_pnl']:+,.2f} $)")

        # Bestätigungsfenster (Flip-Flopper-Schutz + Star-Preemption): nur
        # anzeigen, wenn gerade wirklich etwas wartet - sonst reine Leerzeile.
        pending = s.get("pending") or []
        pending_block = ""
        if pending:
            delay = self.sprint.cfg.confirm_delay_s
            # Live-Befund: ein Kandidat blieb weit über dem Fenster hängen,
            # weil kein Preis für den Coin ankam - weder Promotion noch
            # Reject möglich. 'kein Preis' macht das sofort sichtbar, statt
            # rätseln zu müssen, warum die Bestätigung ewig läuft.
            p_lines = "\n".join(
                f"  {p['coin']} via <code>{p['leader']}…</code> ({p['wait_s']:.0f}s/{delay:.0f}s)"
                + ("" if p.get("hat_preis", True) else " ⚠️ kein Preis")
                for p in pending)
            pending_block = f"\n⏳ Bestätigung läuft:\n{p_lines}"

        # Strikes/Bann nur als ZAHL - die einzelnen Adressen sind fürs
        # Tagesgeschäft Rauschen (Nutzer-Feedback), Details stehen in
        # /sprint pool. Stars bleiben namentlich, das ist die positive,
        # kurze Liste, die man tatsächlich lesen will.
        badges = []
        n_strikes = len(s.get("strikes") or {})
        n_banned = len(s.get("banned") or [])
        if n_strikes or n_banned:
            bits = [f"{n_strikes} mit Strikes"] if n_strikes else []
            if n_banned:
                bits.append(f"🚫 {n_banned} gesperrt")
            badges.append(", ".join(bits) + " (Details: /sprint pool)")
        if s.get("stars"):
            badges.append("⭐ Stars: " + ", ".join(s["stars"]))
        badges_block = f"\n{' | '.join(badges)}" if badges else ""

        # Scan-Telemetrie: unterscheidet 'kein Signal kam' (gesund, nur ruhig)
        # von 'Signale kamen, wurden verworfen' und 'Feed eingefroren' (kaputt)
        sc = s.get("scan", {})
        rej = ", ".join(f"{k} ×{n}" for k, n in sc.get("rejected", {}).items()) or "keine"
        seen = sc.get("fresh_seen", 0)
        last_fresh = (f"vor {sc['last_fresh_min']:.0f} min"
                      if sc.get("last_fresh_min") is not None else "noch keins")
        feed_age = (time.time() - self.copier.last_snapshots_t
                    if self.copier and self.copier.last_snapshots_t else None)
        feed = (f"vor {feed_age:.0f}s" if feed_age is not None and feed_age < 120
                else f"⚠️ vor {feed_age / 60:.0f} min" if feed_age is not None
                else "n/a")
        tracker = getattr(self.copier, "tracker", None)
        if tracker is not None and getattr(tracker, "last_total", 0):
            cov = f" | Abdeckung {tracker.last_fresh}/{tracker.last_total}"
            if tracker.last_stale:
                cov += f" ({tracker.last_stale} stale)"
            feed += cov

        return (f"<b>Quest-Bot</b> (Zyklus {s['cycle']}): {s['state']}\n"
                f"{pos_block}\n"
                f"{equity_line}"
                f"{pending_block}\n"
                f"\n"
                f"Bilanz: {s['won']}✅ {s['busted']}💥 | Schatztruhe {s['banked']:+,.2f} $"
                f"{badges_block}\n"
                f"\n"
                f"Leader: {lead} | Pool: {len(self.sprint_leaders)} scanbar | "
                f"Trades: {s['trades']} (Ø {s['avg_trades_per_cycle']}/Zyklus)\n"
                f"\n"
                f"Scan seit Start: {seen} frische Signale (letztes: {last_fresh}) | "
                f"verworfen: {rej}\n"
                f"Feed: Snapshots {feed}\n"
                f"Baselines: {s.get('baseline_status', 'n/a')}\n"
                f"Beobachtet: {s['watch']['tracked']} Wallets, "
                f"{s['watch']['holding_now']} halten gerade eine Position "
                f"(nur Close+Reopen/Flip/Aufstocken kann noch triggern), "
                f"{s['watch']['flat_now']} sind flach (jeder frische Einstieg triggert)\n"
                f"\n"
                f"<i>/quest close = schließen | /quest pool = Pool-Liste | "
                f"/quest cohorts = Kohorten (Quelle/Grund/Asset) | "
                f"/quest assets = Krypto vs. Aktien | /quest funnel = Trichter-Diagnose | "
                f"/quest reset = Bilanz auf 0 | /quest amnestie = Strikes/Bans löschen</i>")

    def _sprint_asset_breakdown(self) -> str:
        """Krypto vs. Aktien-Perps NUR für die aktuelle Ära (seit dem letzten
        Reset) - die Mess-Woche-Zyklen unter alten Regeln verwässern die Frage
        nicht mehr. Scoping über quest_scorecard (letzte won+busted Zyklen)."""
        if not self.sprint:
            return "Quest-Bot nicht aktiv."
        from .report import quest_scorecard

        sc = quest_scorecard(self._journal_chrono(), self._quest_state())
        assets = sc["assets"]
        if not any(b["n"] for b in assets.values()):
            return "Noch keine abgeschlossenen Quest-Zyklen in dieser Ära."
        lines = [f"<b>Quest: Krypto vs. Aktien</b> (aktuelle Ära, {sc['era_cycles']} Zyklen)"]
        for name in ("Krypto", "Aktien", "unbekannt"):
            b = assets[name]
            if b["n"] == 0:
                continue
            wr = b["won"] / b["n"] * 100
            lines.append(f"{name}: {b['n']} Zyklen, PnL {b['pnl']:+,.2f} $ "
                         f"(Trefferquote {wr:.0f}%)")
        if assets["unbekannt"]["n"]:
            lines.append("<i>'unbekannt' = Zyklen ohne coin-Feld</i>")
        return "\n".join(lines)

    def _cmd_quest_cohorts(self) -> str:
        """Kohorten-Auswertung der AKTUELLEN Ära (Masterplan Phase C, Nutzer:
        'Entscheidungen aus Daten, nicht Anekdoten'): Winrate/PnL je Quelle
        (HL vs Lighter), Exit-Grund, Asset-Klasse + Top/Flop-Leader. Beantwortet
        die Steuerfragen (Lighter zulassen? Zeit-Cut richtig? Trail richtig?)
        direkt aus dem vollen Journal statt aus dem Spiegel-Tail."""
        if not self.sprint:
            return "Quest-Bot nicht aktiv."
        kinds = {"sprint_tp", "sprint_bust", "sprint_cycle_end"}
        cycles = sorted((e for e in self._journal_chrono() if e.get("kind") in kinds),
                        key=lambda e: e.get("t", 0))
        n_era = self.sprint.won + self.sprint.busted
        era = cycles[-n_era:] if n_era else []
        if not era:
            return "Noch keine abgeschlossenen Quest-Zyklen in dieser Ära."

        def agg(keyfn):
            out: dict[str, dict] = {}
            for e in era:
                d = out.setdefault(keyfn(e), {"n": 0, "won": 0, "pnl": 0.0})
                pnl = float(e.get("pnl", 0))
                d["n"] += 1
                d["pnl"] += pnl
                if pnl > 0:
                    d["won"] += 1
            return out

        def fmt(title, data):
            block = [f"<b>{title}</b>"]
            for k, d in sorted(data.items(), key=lambda t: -t[1]["pnl"]):
                block.append(f"  {k}: {d['n']}x, {d['won'] / d['n'] * 100:.0f}% grün, "
                             f"{d['pnl']:+,.0f} $ (Ø {d['pnl'] / d['n']:+,.1f})")
            return block

        lines = [f"<b>Quest-Kohorten</b> (aktuelle Ära, {len(era)} Zyklen)", ""]
        def source_of(e):
            ldr = str(e.get("leader", ""))
            if ldr.startswith("counter:"):
                return "Counter (Toxic Flow)"
            return "Lighter" if ldr.startswith("lighter:") else "Hyperliquid"

        lines += fmt("Quelle", agg(source_of))
        lines.append("")
        lines += fmt("Exit-Grund", agg(lambda e: e.get("reason", "?")))
        lines.append("")
        lines += fmt("Asset-Klasse", agg(lambda e: "unbekannt" if not e.get("coin")
                     else ("Aktien" if ":" in str(e.get("coin")) else "Krypto")))
        per = {k: d for k, d in agg(
            lambda e: str(e.get("leader") or "?")[:14]).items() if d["n"] >= 2}
        if per:
            ranked = sorted(per.items(), key=lambda t: -t[1]["pnl"])
            lines.append("")
            lines.append("<b>Top/Flop-Leader</b> (>= 2 Zyklen diese Ära):")
            for k, d in ranked[:3]:
                lines.append(f"  <code>{k}</code> {d['pnl']:+,.0f} $ "
                             f"({d['n']}x, {d['won']}-{d['n'] - d['won']})")
            flops = [t for t in ranked if t[1]["pnl"] < 0][-3:]
            for k, d in reversed(flops):
                lines.append(f"  <code>{k}</code> {d['pnl']:+,.0f} $ "
                             f"({d['n']}x, {d['won']}-{d['n'] - d['won']})")
        return "\n".join(lines)

    def _cmd_quest_funnel(self) -> str:
        """Trichter-Diagnose (Nutzer: 'proper Analyse-Tools, die ich selbst
        durchspiegeln kann'): zeigt je Filterstufe, wie viele Kandidaten übrig
        blieben - von der Discovery bis zum fertigen Pool. Beantwortet direkt
        Fragen wie 'warum sind's nur 14/20, und warum fliegt Idle nicht raus'."""
        af = self._analysis_funnel
        pf = self._pool_funnel
        if not af and not pf:
            return "Noch keine Analyse gelaufen (/analyze anstoßen)."
        lines = ["<b>Quest-Trichter</b>"]
        if af:
            lines.append(f"1. Discovery: {af['candidates']} Kandidaten ({af['source']})")
            lines.append(f"2. Haupt-Gate (≥{af['min_score']:g}): {af['main_ranked']} bestanden")
            lines.append(f"3. Sprint-LARP-Gate: {af['sprint_gate_ko']} raus"
                         + (f" ({', '.join(f'{k}×{n}' for k, n in sorted(af['larp_reasons'].items(), key=lambda t: -t[1])[:3])})"
                            if af.get("larp_reasons") else ""))
            lines.append(f"4. Richtung <{af['pool_min_score']:g}: {af['sprint_score_ko']} raus")
            lines.append(f"5. Schläfer (kein Trade, keine Position): {af['sprint_idle_ko']} raus")
            lines.append(f"6. Sitzer-Deckel (Pfad B über {self.cfg.sprint.path_b_max_share:.0%} "
                         f"Poolanteil): {af.get('sprint_path_b_ko', 0)} raus")
            lines.append(f"   → {af['sprint_ok']} Sprint-tauglich nach der Analyse")
            if af.get("errors"):
                lines.append(f"   ⚠️ {af['errors']} Wallets nicht abrufbar (übersprungen)")
        if pf:
            lines.append("")
            lines.append(f"7. Gesperrt (LARP enttarnt): {pf['banned_out']} raus")
            regime = f"8. Regime-Filter ({'Börse zu' if pf['crypto_only'] else 'Börse offen'}): " \
                     f"{pf['regime_out']} raus"
            lines.append(regime + (" (überwiegend Aktien-Trader)" if pf["regime_out"] else ""))
            lines.append(f"   → {pf['cands_after_filters']} Kandidaten für {pf['pool_size_target']} Pool-Slots")
            if pf["truncated_by_size"]:
                lines.append(f"9. Zu viele Kandidaten: {pf['truncated_by_size']} nicht reingepasst "
                             f"(niedrigster Score/idle zuerst raus)")
            elif pf["idle_total"]:
                lines.append(f"9. Pool NICHT voll ({pf['final_pool'] - pf['forced_main']}/"
                             f"{pf['pool_size_target']}) - Idle-Rotation kann nicht abschneiden, "
                             f"es gibt niemand zum Nachrücken ({pf['idle_in_pool']} von "
                             f"{pf['idle_total']} idle Wallets sitzen trotzdem noch drin)")
            if pf["forced_main"]:
                lines.append(f"   + {pf['forced_main']} Haupt-Buch-Leader zwangsergänzt")
            lines.append(f"→ finaler Pool: {pf['final_pool']}")
        lines.append("\n<i>/analyze = Trichter neu laufen lassen</i>")
        return "\n".join(lines)

    def _cmd_twap(self) -> str:
        flagged = self.twap_scout.flagged[-6:] if self.twap_scout else []
        if not flagged:
            return "Keine laufenden TWAPs erkannt (oder Scout aus)."
        out = ["<b>TWAP-Scout</b>"]
        for f in reversed(flagged):
            out.append(f"{f['side']} {f['coin']}: {f['slices']} Slices, ${f['notional']:,.0f} "
                       f"<code>{f['address'][:10]}…</code>")
        return "\n".join(out)

    def _cmd_polymarket(self) -> str:
        flagged = self.poly_scout.flagged[-5:] if self.poly_scout else []
        if not flagged:
            return "Noch keine Polymarket-Funde (oder Scout aus)."
        out = ["<b>Polymarket-Scout</b>"]
        for f in reversed(flagged):
            out.append(f"${f['bet_usdc']:,.0f} auf {f['outcome']} — „{f['market'][:40]}\" "
                       f"(PnL ${f['realized_pnl']:,.0f})")
        return "\n".join(out)

    def _cmd_probe(self) -> str:
        """Multi-DEX-Scan-Probe vom Handy: prüft, welche Perp-DEXs fremde Trader
        hergeben. Läuft im Hintergrund (Netz-Calls), Ergebnis kommt per Push -
        so bleibt der Bot währenddessen antwortbereit."""
        if not (self.notifier and self.notifier.enabled):
            return "Probe braucht Telegram-Push (Ergebnis wird gesendet)."

        def run():
            try:
                import probe_dexs

                msg = probe_dexs.summarize()
            except Exception as e:
                msg = f"⚠️ Probe fehlgeschlagen: {str(e)[:250]}"
            self.notifier.send(msg)

        threading.Thread(target=run, daemon=True, name="probe").start()
        return "🔍 Probe läuft (bis ~1 Min bei Timeouts) … Ergebnis kommt gleich als Nachricht."

    def _cmd_lighter(self, arg: str = "") -> str:
        """/lighter <index|0x-adresse>: liest ein echtes Lighter-Konto und zeigt
        das geparste Snapshot - zum Verifizieren, ob Long/Short/Größe stimmen,
        bevor wir darauf messen. Ohne Argument: Watchlist-Status."""
        from .config import load_config
        from .sources.lighter import LighterClient, LighterSource

        cfg = load_config().lighter
        if not arg:
            if self.lighter and self.copier:
                s = self.lighter.stats(self.copier.last_prices)
                leaders = ", ".join(a[:8] for a in s.get("leaders", [])) or "sucht…"
                note = f"\n⚠️ {s['note']}" if s.get("note") else ""
                return (f"<b>Lighter-Schatten</b> (Auto-Discovery)\n"
                        f"Equity: {s['equity']:,.2f} ({s['return_pct']:+.2f}%) | "
                        f"{s['trades']} Trades, {s['open_positions']} offen\n"
                        f"Aktuelle Top-Konten: {leaders}{note}\n"
                        f"Einzeltest: <code>/lighter &lt;index&gt;</code>")
            return (f"<b>Lighter</b> ({'aktiv' if cfg.enabled else 'aus'})\n"
                    f"Auto-Discovery: {cfg.auto_discover}\n"
                    f"Test: <code>/lighter &lt;index oder 0x-adresse&gt;</code>")
        try:
            src = LighterSource(cfg, LighterClient(cfg.base_url))
            snap = src.snapshot(arg)
        except Exception as e:
            return f"⚠️ Lighter-Abruf fehlgeschlagen: {str(e)[:200]}"
        if not snap.positions:
            return (f"<b>Lighter {arg[:14]}</b>\nEquity ${snap.equity:,.0f} | "
                    f"keine offenen (auf HL handelbaren) Positionen")
        lines = [f"<b>Lighter {arg[:14]}</b> — Equity ${snap.equity:,.0f}"]
        for coin, p in snap.positions.items():
            side = "LONG" if p.size > 0 else "SHORT"
            lines.append(f"{side} {coin}: ${p.position_value:,.0f} @ {p.entry:.4f} "
                         f"({snap.exposure(coin) * 100:+.0f}% der Equity)")
        lines.append("\n✅ Stimmen Richtung/Größe? Dann können wir darauf messen.")
        return "\n".join(lines)

    def _cmd_setcmm(self, arg: str = "") -> str:
        """Setzt einen HyperTracker/CoinMarketMan-API-Token in die .env UND live
        in den Prozess - vom Handy, ohne SSH. NUR für read-only Free-Tier-Tokens,
        NICHT für Wallet-Keys. Chat-ID-geschützt.

        Optionaler zweiter Parameter (Slot 1-5, Default 1): Nutzer-Entscheidung
        (17.07., mehrere Free-Tier-Tokens rotieren lassen) - Slot 1 ist der
        Haupt-Token (COINMARKETMAN_TOKEN), Slot 2-5 sind Fallback-Tokens
        (COINMARKETMAN_TOKEN_2.._5), auf die automatisch gewechselt wird, wenn
        der vorherige Slot sein 100/Tag-Limit erreicht (siehe coinmarketman.py)."""
        parts = arg.strip().split()
        if not parts:
            return ("Nutzung: <code>/setcmm &lt;token&gt; [slot]</code> (JWT von "
                    "coinmarketman.com; slot 1 = Haupt-Token, 2-5 = Fallback bei "
                    "ausgeschöpftem Tageslimit, Default 1)")
        tok = parts[0]
        from .sources.coinmarketman import MAX_FALLBACK_TOKENS, TOKEN_ENV, CMMClient

        slot = 1
        if len(parts) > 1:
            if not parts[1].isdigit() or not 1 <= int(parts[1]) <= MAX_FALLBACK_TOKENS:
                return f"Slot muss eine Zahl zwischen 1 und {MAX_FALLBACK_TOKENS} sein."
            slot = int(parts[1])
        if tok.count(".") != 2 or len(tok) < 40:
            return "Das sieht nicht nach einem JWT aus (drei punkt-getrennte Teile erwartet)."
        key = TOKEN_ENV if slot == 1 else f"{TOKEN_ENV}_{slot}"
        set_env_var(key, tok)
        os.environ[key] = tok   # sofort live, kein Neustart nötig
        n = len(CMMClient.tokens())
        return (f"✅ Token für Slot {slot} gespeichert (…{tok[-6:]}) und live geladen.\n"
                f"Aktuell konfiguriert: {n} Token(s). Jetzt <code>/cmm</code> für die "
                f"Live-Probe der API.")

    def _cmd_cmm(self, arg: str = "") -> str:
        """Live-Probe der HyperTracker-API: holt das Perp-PnL-Leaderboard und zeigt
        die ROHE Response-Struktur (Keys, Beispielzeile) - damit wir das echte
        Format sehen, bevor der Parser scharf geht. Läuft vom VPS (Sandbox blockt
        coinmarketman.com). Optionales Arg: Zeitfenster (pnlDay/Week/Month/AllTime)."""
        if not (self.notifier and self.notifier.enabled):
            return "CMM-Probe braucht Telegram-Push (Ergebnis wird gesendet)."
        from .config import load_config
        from .sources.coinmarketman import CMMClient, probe as cmm_probe

        if not CMMClient.token():
            return ("Kein Token gesetzt. Erst <code>/setcmm &lt;token&gt;</code> senden.")
        cfg = load_config().coinmarketman
        period = arg.strip() or cfg.period

        def run():
            try:
                msg = cmm_probe(cfg.base_url, period=period,
                                limit=min(cfg.limit, 25), timeout=cfg.timeout)
            except Exception as e:
                msg = f"⚠️ CMM-Probe fehlgeschlagen: {str(e)[:250]}"
            self.notifier.send(msg)

        threading.Thread(target=run, daemon=True, name="cmm-probe").start()
        return f"🔎 CMM-Probe läuft ({period}) … Ergebnis kommt gleich als Nachricht."

    def _cmd_set_status_push(self, arg: str = "") -> str:
        """Setzt den GitHub-PAT (STATUS_PUSH_TOKEN) für den Status-Spiegel in
        die .env UND live in den Prozess - vom Handy, ohne SSH (Nutzer 17.07.,
        Muster wie /setcmm). NUR ein fine-grained Token mit 'Contents: Read
        and write' auf GENAU dieses Repo, NICHT für Wallet-Keys."""
        tok = arg.strip()
        if not tok:
            return ("Nutzung: <code>/setstatuspush &lt;token&gt;</code> (fine-grained "
                    "GitHub-PAT, nur 'Contents: Read and write' auf dieses Repo)")
        if not (tok.startswith("github_pat_") or tok.startswith("ghp_")) or len(tok) < 20:
            return ("Das sieht nicht nach einem GitHub-PAT aus (erwartet "
                    "'github_pat_…' oder 'ghp_…').")
        from .status_push import TOKEN_ENV

        set_env_var(TOKEN_ENV, tok)
        os.environ[TOKEN_ENV] = tok   # sofort live, kein Neustart nötig
        return (f"✅ Token gespeichert (…{tok[-6:]}) und live geladen.\n"
                f"status_push.enabled muss zusätzlich in config.yaml an sein "
                f"(ist es per Default nach diesem Update). Jetzt "
                f"<code>/statuspush</code> zum Test.")

    def _cmd_status_push(self) -> str:
        """Schreibt den Status-Spiegel SOFORT (ignoriert die Intervall-Bremse) -
        Test-Knopf fürs Handy: sofort sehen, ob Token/Branch/Pfad stimmen,
        statt bis zu 5 Minuten auf den nächsten Tick zu warten."""
        if not self.cfg.status_push.enabled:
            return "status_push.enabled ist aus (config.yaml) - erst an, dann erneut senden."
        from .status_push import TOKEN_ENV, build_snapshot, push_snapshot

        if not os.environ.get(TOKEN_ENV, "").strip():
            return ("Kein Token gesetzt. Erst <code>/setstatuspush &lt;token&gt;</code> "
                    "senden.")
        sp = self.cfg.status_push
        ok = push_snapshot(build_snapshot(self), repo=sp.repo, branch=sp.branch, path=sp.path)
        self._last_status_push = time.time()
        return (f"✅ Status-Spiegel geschrieben: {sp.repo}@{sp.branch}/{sp.path}"
                if ok else "⚠️ Push fehlgeschlagen - siehe Server-Log (Token/Branch/Rechte prüfen).")

    def _cmd_analyze(self) -> str:
        """Leader-Analyse sofort anstoßen statt auf den 6h-Takt zu warten. Läuft
        thread-sicher im Loop (nächster Tick), nicht im Telegram-Thread - zwei
        gleichzeitige Analysen würden sich sonst die Leader-Liste zerschreiben."""
        if not self.running:
            return "Autopilot läuft nicht - erst /start."
        if self._analysis_running:
            mins = (time.time() - self._analysis_started) / 60
            return f"⏳ Analyse läuft bereits (seit {mins:.0f} min) - Ergebnis kommt als Push."
        self._force_analysis = True
        return ("🔬 Analyse angestoßen - startet im nächsten Tick und dauert bei "
                "~50 Wallets EINIGE MINUTEN (API-Drossel).\n"
                "Das Ergebnis kommt automatisch als Push; /status zeigt derweil "
                "'läuft seit X min'.")

    def _cmd_update(self) -> str:
        """Zieht das neueste Update (git pull) und startet neu - per Telegram vom
        Handy. Neustart über Prozess-Exit: systemd (Restart=always) bringt den
        Bot auf neuem Code zurück, autostart fährt den Autopiloten wieder hoch."""
        from .config import ROOT

        try:
            r = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only"],
                               capture_output=True, text=True, timeout=90)
        except Exception as e:
            return f"⚠️ git pull fehlgeschlagen: {str(e)[:200]}"
        out = (r.stdout + r.stderr).strip()[-600:]
        if r.returncode != 0:
            return ("⚠️ Update fehlgeschlagen (evtl. lokale Änderungen) - bitte am PC "
                    f"prüfen:\n<code>{out}</code>")
        if "up to date" in out.lower() or "up-to-date" in out.lower():
            return f"Schon aktuell.\n<code>{out}</code>"
        # Erfolg -> Prozess beenden, systemd startet auf neuem Code neu.
        threading.Timer(2.0, lambda: os._exit(0)).start()
        return f"✅ Update gezogen, starte neu (~15s)…\n<code>{out}</code>"

    def _cmd_resume(self) -> str:
        if not self.copier:
            return "Kein Copier aktiv."
        if not self.copier.halted:
            return "Kein Risiko-Halt aktiv."
        self.copier.resume()
        return "✅ Risiko-Halt aufgehoben, Baselines neu gesetzt."

    def _cmd_stop(self) -> str:
        if not self.running:
            return "Autopilot läuft nicht."
        self.stop()
        return "⏹ Autopilot wird gestoppt."

    def _cmd_start(self) -> str:
        if self.running:
            return "Autopilot läuft bereits."
        self.start()
        return "▶️ Autopilot wird gestartet."

    # ---------- Lebenszyklus ----------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, account_address: str | None = None) -> None:
        # Fernsteuerung läuft unabhängig vom Worker - so erreichbar, selbst wenn
        # das Setup gerade in der Retry-Schleife hängt.
        self.commander.start()
        if self.running:
            return
        if account_address:
            self.account_address = account_address
        self._stop.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="autopilot")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._set_status(state="stopping")

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    # ---------- Hauptschleife ----------

    def _wait_for_next_tick(self) -> None:
        """Wartet bis zum nächsten vollen Tick (copytrade.poll_seconds) -
        ereignisgesteuert, ein Leader-Fill per WebSocket weckt sofort.

        Fast-Path (Spiegel-Fund 22.07.: Plus-Lock schoss trotz Floor bis zu
        -33.57$ durch, weil der volle Tick nur alle poll_seconds - typ. 20s -
        lief; ein 10x-Ritt kann sich in dieser Zeit weiter bewegen als der
        Floor-Puffer gibt). Läuft ein Mess-Ritt UND ist sprint.fast_check_
        seconds an, wird die Wartezeit in kleinere Stücke zerlegt und
        zwischendurch NUR der Exit-Reflex (SprintBook.fast_exit_check) mit
        bereits vorhandenen WS-Mids nachgeholt - kein Leader-Scan, kein
        zusätzlicher Netz-Call. Ohne offene Mess-Ritte oder mit
        fast_check_seconds=0 (Default) exakt das alte Verhalten."""
        poll_s = self.cfg.copytrade.poll_seconds
        fast_s = self.cfg.sprint.fast_check_seconds if self.sprint else 0
        if not (fast_s > 0 and self.sprint.paper.sizes()):
            if self.feed and self.feed.connected:
                if self.feed.wait(poll_s):
                    log.info("Leader-Fill per WebSocket - Tick sofort (Copy-Lag minimiert)")
            else:
                self._stop.wait(poll_s)
            return
        remaining = poll_s
        while remaining > 0 and not self._stop.is_set():
            chunk = min(fast_s, remaining)
            if self.feed and self.feed.connected:
                if self.feed.wait(chunk):
                    log.info("Leader-Fill per WebSocket - Tick sofort (Copy-Lag minimiert)")
                    return
            else:
                self._stop.wait(chunk)
            remaining -= chunk
            if self._stop.is_set():
                return
            mids = self.feed.mids() if self.feed else None
            if mids:
                prices = dict(self.copier.last_prices) if self.copier else {}
                prices.update(mids)
                self.sprint.fast_exit_check(prices)

    def _run(self) -> None:
        # Setup darf NIE endgültig sterben (z.B. 429-Rate-Limit beim Hochfahren):
        # mit Backoff weiterprobieren, beim ersten Fehlschlag einmal alarmieren.
        delay, alerted = 60.0, False
        while True:
            try:
                self._setup()
                break
            except Exception as e:
                log.exception("Autopilot-Setup fehlgeschlagen - neuer Versuch in %.0fs", delay)
                self.error = str(e)
                self._set_status(state="retrying", error=str(e), retry_in_s=int(delay))
                if not alerted:
                    self.notifier.send(
                        f"⚠️ <b>Autopilot-Setup fehlgeschlagen</b>\n{str(e)[:300]}\n"
                        f"Ich versuche es automatisch weiter (Backoff bis 15 min)."
                    )
                    alerted = True
                if self._stop.wait(delay):
                    self._set_status(state="stopped")
                    return
                delay = min(delay * 2, 900.0)

        self.error = None
        if alerted:
            self.notifier.send("✅ Setup im neuen Anlauf geglückt - Autopilot fährt hoch.")
        self._set_status(state="running")
        self.notifier.send(
            f"🚀 <b>Quest-Bot gestartet</b>\n"
            f"Modus: {'DRY-RUN' if self.cfg.dry_run else 'LIVE'} auf "
            f"{'Testnet' if self.cfg.is_testnet else 'Mainnet'}\n"
            + (f"Pool: {len(self.sprint_leaders)} Leader | 1000$ {self._leverage_desc()}, "
               f"Ziel +{self.cfg.sprint.target_profit:.0f}$/Zyklus"
               if self.cfg.sprint.enabled else "Quest-Bot aus (sprint.enabled prüfen)")
        )
        while not self._stop.is_set():
            try:
                self._maybe_reanalyze()
                # Auch ticken, wenn nur der Sprint-Pool Adressen hat: der Sprint-
                # Feed (last_snapshots) hängt am Copier - ein leeres Hauptbuch
                # darf die 13 Pool-Beobachter nicht einfrieren (Audit-Befund).
                if self.copier and (self.leaders or
                                    (self.cfg.sprint.enabled and self.sprint_leaders)):
                    self.copier.tick()
                if self.scalper:
                    self.scalper.tick()
                    self.copier.scalp_inventory = self.scalper.inventory()
                self._watch_wallets()
                if self.scout:
                    self.scout.tick()
                if self.book_scout:
                    self.book_scout.tick()
                if self.twap_scout:
                    self.twap_scout.tick()
                if self.poly_scout:
                    self.poly_scout.tick()
                if self.labs and self.copier:
                    self.labs.tick(self.copier.last_prices)
                    self.labs.persist_stats(self.copier.last_prices)
                if self.lighter and self.copier:
                    self.lighter.tick(self.copier.last_prices)
                if self.sprint and self.copier and self.copier.last_snapshots:
                    from .news.guard import RiskLevel

                    # Börsen-Gong VOR dem Tick: schaltet crypto_only um + sichert
                    # profitable Aktien beim Schluss, damit dieser Tick schon mit
                    # dem neuen Zustand scannt.
                    self._maybe_market_gong()
                    off = bool(self.guard and self.guard.last_level == RiskLevel.RISK_OFF)
                    sprint_leaders, sprint_snaps = self._sprint_tick_inputs()
                    self.sprint.tick(sprint_leaders, sprint_snaps,
                                     self.copier.last_prices, risk_off=off)
                    self._maybe_warn_stale_feed()
                self._maybe_digest()
                self._maybe_watchdog()
                self._maybe_push_status()
                self._publish()
                self._last_tick_t = time.time()
            except Exception:
                # Blinder Fleck (Nutzer 17.07., 'kein blinder Fleck mehr'):
                # bisher nur im Server-Log sichtbar, den Claude nicht lesen
                # kann - jetzt zusätzlich im Status-Spiegel (siehe status_push.py).
                self._tick_error_count += 1
                self._last_tick_error = traceback.format_exc()[-2000:]
                log.exception("Autopilot-Tick fehlgeschlagen")
            self._wait_for_next_tick()
            if self._stop.is_set():
                break
        if self.feed:
            self.feed.close()
        self._set_status(state="stopped")
        self.notifier.send("⏹ Autopilot gestoppt.")

    def _setup(self) -> None:
        key = addr = None
        if not self.cfg.dry_run:
            key, addr = load_credentials()
        self.client = HyperliquidClient(
            testnet=self.cfg.is_testnet, private_key=key,
            account_address=addr or self.account_address,
            dexs=self.cfg.market.dexs,
        )
        self.guard = MarketGuard(self.cfg.news, self.cfg.shock, client=self.client, coin="BTC")
        self._load_or_analyze_leaders()
        leader_info = Info(api_url(testnet=False), skip_ws=True)
        # Leader-Positionen über ALLE DEXs verfolgen - sie handeln auch TSLA/Gold/Öl.
        # Adressen = Haupt-Leader + breiterer Sprint-Pool (falls Sprint an).
        tracker = LeaderTracker(leader_info, self._tracked_addresses(),
                                dexs=self.client.dexs)
        weights = {l["address"]: float(l["weight"]) for l in self.leaders}
        convergence = ConvergenceEngine(self.cfg.convergence) if self.cfg.convergence.enabled else None
        validator = None
        if self.cfg.validation.enabled:
            from .validator import TradeValidator

            validator = TradeValidator(self.cfg.validation, self.client)
        signals = None
        if self.cfg.execution.mode == "signals":
            from .signals import SignalBridge

            signals = SignalBridge(notifier=self.notifier)
            log.info("Signal-Modus aktiv: Orders werden als Tickets emittiert "
                     "(Prop-Account), Tracking läuft im Paper-Modus")
        self.copier = CopyTrader(self.cfg, self.client, tracker, weights,
                                 guard=self.guard, convergence=convergence,
                                 validator=validator, signals=signals)
        if self.cfg.dry_run and self.cfg.autopilot.shadow_variants:
            from .shadow import ShadowFleet

            self.copier.shadows = ShadowFleet(
                self.cfg.copytrade, self.cfg.backtest.initial_equity,
                self.cfg.backtest.fee_rate, validator=validator,
            )
            log.info("Shadow-Varianten aktiv: %s",
                     [v.name for v in self.copier.shadows.variants])

        # Edge 3: Funding-Tilt (Carry) - nutzt die Mainnet-Marktdaten
        if self.cfg.funding_tilt.enabled:
            from .funding import FundingCache

            self.copier.funding = FundingCache(self.client.market, self.cfg.funding_tilt)

        # Edge 2: WebSocket-Echtzeit - Leader-Fills wecken den Loop sofort
        if self.cfg.autopilot.realtime:
            from .realtime import RealtimeFeed

            self.feed = RealtimeFeed(addresses=[l["address"] for l in self.leaders])
            self.copier.feed = self.feed
        if self.cfg.investigator.watchlist:
            self.watcher = WalletWatcher(leader_info, self.cfg.investigator.watchlist,
                                         self.cfg.investigator.min_notional_change)
            log.info("Investigator: beobachte %d Wallets", len(self.cfg.investigator.watchlist))
        self.scout = None
        if self.cfg.anomaly.enabled:
            from .anomaly import AnomalyScout

            self.scout = AnomalyScout(leader_info, self.cfg.anomaly,
                                      notifier=self.notifier, journal=self.journal)
            log.info("Anomalie-Scout aktiv: %s (nur Beobachtung, handelt nie)",
                     ", ".join(self.cfg.anomaly.coins))

        # Orderbuch-Scout: Mikrostruktur (Imbalance, Walls) - read-only
        self.book_scout = None
        if self.cfg.orderbook.enabled:
            from .orderbook import OrderBookScout

            self.book_scout = OrderBookScout(self.client.market, self.cfg.orderbook,
                                             notifier=self.notifier, journal=self.journal)
            log.info("Orderbuch-Scout aktiv: %s (Mikrostruktur, handelt nie)",
                     ", ".join(self.cfg.orderbook.coins))

        # TWAP-Scout: laufende TWAP-Ausführungen großer Wallets (read-only).
        # Kandidaten: Watchlist + bekannte Whales + Leader + frische Anomalie-Funde.
        self.twap_scout = None
        if self.cfg.twap.enabled:
            from .twap import TwapScout

            def twap_addresses():
                addrs = list(self.cfg.investigator.watchlist)
                addrs += [l["address"] for l in self.leaders]
                if self.scout:
                    addrs += [f["address"] for f in self.scout.flagged[-10:]]
                return addrs

            self.twap_scout = TwapScout(self.client.market, self.cfg.twap,
                                        address_source=twap_addresses,
                                        notifier=self.notifier, journal=self.journal)
            log.info("TWAP-Scout aktiv (read-only, handelt nie)")

        # Polymarket-Scout: erfahrenes Geld in Prediction Markets (read-only)
        self.poly_scout = None
        if self.cfg.polymarket.enabled:
            from .polymarket import PolymarketScout

            self.poly_scout = PolymarketScout(self.cfg.polymarket,
                                              notifier=self.notifier, journal=self.journal)
            log.info("Polymarket-Scout aktiv (read-only, handelt nie)")

        # Strategie-Labor: eigene Signale parallel im Paper-Schatten messen
        self.labs = None
        if self.cfg.dry_run and self.cfg.labs.enabled:
            from .labs import LabFleet

            self.labs = LabFleet(self.cfg.labs, self.client,
                                 self.cfg.backtest.initial_equity, self.cfg.backtest.fee_rate)
            log.info("Strategie-Labor aktiv: %s (Paper, eigene Signale zur Messung)",
                     ", ".join(l.name for l in self.labs.labs))
        # Sprint-Buch: 1000$ x Hebel auf den besten Leader, Ziel +100$ je Zyklus
        self.sprint = None
        if self.cfg.dry_run and self.cfg.sprint.enabled:
            from .sprint import SprintBook

            self.sprint = SprintBook(self.cfg.sprint, self.cfg.backtest.fee_rate,
                                     notifier=self.notifier, journal=self.journal,
                                     max_leverage_fn=self.client.max_leverage)
            log.info("Sprint-Buch aktiv: %.0f$ %s auf den besten Leader, Ziel +%.0f$/Zyklus",
                     self.cfg.sprint.equity, self._leverage_desc(),
                     self.cfg.sprint.target_profit)
            # Gebannte Leader aus dem beim Start geladenen Pool werfen (das Buch
            # mit seinen Bans existiert erst jetzt, nach _load_sprint_pool).
            self._prune_banned_from_pool()
        # Lighter-Schatten: fremde Lighter-Trader auto-entdecken + auf HL messen
        self.lighter = None
        if self.cfg.dry_run and self.cfg.lighter.enabled:
            from .sources.lighter import LighterShadow

            self.lighter = LighterShadow(self.cfg.lighter, self.cfg.backtest.fee_rate)
            log.info("Lighter-Schatten aktiv (Auto-Discovery=%s, Copy auf HL-Preisen)",
                     self.cfg.lighter.auto_discover)
        self.scalper = None
        if self.cfg.scalp.enabled:
            from .scalper import VolScalper

            self.scalper = VolScalper(
                self.cfg.scalp, self.client, self.guard.shock,
                paper=self.copier.paper, journal=self.journal, notifier=self.notifier,
                equity_fn=lambda: self.copier.last_equity or self.cfg.backtest.initial_equity,
                signals=signals,
            )
            self.scalper.full_risk = self.cfg.risk
            self.scalper.exec_cfg = self.cfg.execution
            self.scalper.paper_maker_fee = self.cfg.backtest.maker_fee_rate
            log.info("VolScalper aktiv (%s, Risiko %.1f%%/Scalp)",
                     self.cfg.scalp.coin, self.cfg.scalp.risk_per_scalp * 100)

    # ---------- Leader-Analyse & Rotation ----------

    _SPRINT_POOL_FILE = "sprint_leaders.json"

    def _sprint_tick_inputs(self) -> tuple[list[dict], list]:
        """Baut (leaders, snapshots) für sprint.tick(): der HL-Pool, plus
        optional Lighter-Signale (Nutzer-Entscheidung 17.07., Option B), wenn
        lighter.sprint_promote an ist. Eigene Methode statt Inline-Logik im
        Tick-Loop, damit der Merge ohne den ganzen Hintergrund-Loop testbar
        ist. Lighter-Adressen kommen mit eigenem Namensraum (lighter:-Präfix,
        siehe LighterShadow.sprint_snapshots) - kollidieren nie mit echten
        HL-Adressen in strikes/banned/confidence. Nutzt lighter.tick()s (im
        Aufrufer VOR diesem Call) ohnehin scan_seconds-gedrosselten Cache -
        kein zusätzlicher Lighter-Netz-Call hier."""
        leaders = self.sprint_leaders
        snaps = self.copier.last_snapshots if self.copier else []
        if self.lighter and self.cfg.lighter.sprint_promote:
            # Gerade gerittene Lighter-Leader bleiben IMMER im Feed (keep,
            # Live-Fund 19.07.): rank() wählt die Top-Liste je Scan neu -
            # Discovery-Churn warf einen Leader mitten im Ritt aus dem Feed
            # und der Ritt wurde als 'leader_rotated' zwangsgeschlossen
            # (BTC -14.30), obwohl der Leader real weiter existiert.
            riding: set[str] = set()
            if self.sprint:
                pre = "lighter:"
                for a in list(self.sprint.ride_leaders.values()) + [self.sprint.ride_leader]:
                    if a.startswith(pre):
                        riding.add(a[len(pre):])
            extra_leaders, extra_snaps = self.lighter.sprint_snapshots(
                self.copier.last_prices if self.copier else {}, keep=riding)
            leaders = leaders + extra_leaders
            snaps = snaps + extra_snaps
        return leaders, snaps

    def _load_or_analyze_leaders(self) -> None:
        path = Path(self.cfg.copytrade.leaders_file)
        if path.exists():
            self.leaders = json.loads(path.read_text())[: self.cfg.copytrade.max_leaders]
            self._last_analysis = path.stat().st_mtime
            log.info("Leaders aus %s geladen (%d)", path, len(self.leaders))
            self._load_sprint_pool()
        else:
            self._reanalyze()   # setzt self.sprint_leaders gleich mit

    def _tracked_addresses(self) -> list[str]:
        """Adressen, die der Tracker snapshotten muss: Haupt-Leader + (wenn Sprint
        an ist) der breitere Sprint-Pool. Reihenfolge stabil, keine Duplikate.
        Ist Sprint aus, bleibt es bei den Haupt-Leadern - keine Extra-API-Last."""
        addrs = [l["address"] for l in self.leaders]
        if not self.cfg.sprint.enabled:
            return addrs
        seen = set(addrs)
        for l in self.sprint_leaders:
            a = l.get("address", "")
            if a and a not in seen:
                seen.add(a)
                addrs.append(a)
        # Toxic-Watch (Nutzer 20.07., Counter-Trading): toxische HL-Wallets
        # (gebannt ODER Record-Defizit, siehe toxic_addrs) BLEIBEN beobachtet,
        # obwohl sie keinen Pool-Slot mehr haben - ihre frischen Signale sind
        # jetzt Kontra-Indikatoren. toxic_addrs() ist AMNESTIE-FEST (Spiegel-
        # Fund 20.07.: reine self.sprint.banned wurde bei /quest amnestie
        # komplett geleert, Toxic Flow hatte 78min kein Futter mehr, obwohl
        # dieselben Leader ihre miese Langzeit-Bilanz nie verloren hatten).
        # Gedeckelt (12), damit die Watch-Liste das Snapshot-Budget nicht
        # auffrisst; Lighter-Toxische brauchen keinen Slot hier (kommen über
        # den Lighter-Feed, solange sie in dessen Top-Liste stehen).
        if self.cfg.sprint.counter_toxic and self.sprint:
            have = {x.lower() for x in addrs}
            for a in sorted(self.sprint.toxic_addrs()):
                # Bestätigter Flip-Flopper (Original UND Gegenwette
                # gebannt, Nutzer 21.07.): weder Folgen noch Kontern
                # funktioniert - keinen der raren 12 Watch-Slots mehr
                # dafür verschwenden. toxic_addrs() selbst bleibt
                # unverändert (steuert weiterhin den Pool-Ausschluss).
                if f"counter:{a}" in self.sprint.banned:
                    continue
                if a.startswith("0x") and a not in have:
                    addrs.append(a)
                    have.add(a)
                    if len(have) - len(seen) >= 12:
                        break
        return addrs

    def _sprint_banned(self) -> set[str]:
        """Adressen, die vom Sprint-Pool ausgeschlossen bleiben (lowercase).
        Bei aktivem Toxic Flow (counter_toxic) ist das toxic_addrs() - Bann
        ODER Record-Defizit (Spiegel-Fund 20.07.: ein Leader mit miesem
        Langzeit-Record, aber gerade nicht formell gebannt, darf nicht
        gleichzeitig FOLGEND im Pool sitzen UND als toxisch gekontert werden).
        Sonst nur self.sprint.banned (altes Verhalten). Leer, wenn Sprint aus
        ist oder das Buch noch nicht existiert."""
        if not self.sprint:
            return set()
        if self.cfg.sprint.counter_toxic:
            return self.sprint.toxic_addrs()
        return self.sprint.banned

    def _leverage_desc(self) -> str:
        """Lesbare Hebel-Zusammenfassung fürs Log/Telegram: Basis + Coin-
        Overrides (z.B. 'x10 (BTC x20, ETH x15)'), oder nur 'x10' ohne Overrides."""
        sp = self.cfg.sprint
        base = f"x{sp.leverage:.0f}"
        if not sp.leverage_overrides:
            return base
        over = ", ".join(f"{c} x{v:.0f}" for c, v in sorted(sp.leverage_overrides.items()))
        return f"{base} ({over})"

    def _is_stock_dominated(self, m) -> bool:
        """Überwiegend Aktien-Perp-Trader: der Anteil distinkter Krypto-Coins an
        der gesamten Handels-Historie liegt unter regime_min_crypto_share. Live-
        Fund: die alte Alles-oder-nichts-Regel (nur 100%-reine Aktien-Wallets
        raus) ließ 'fast nur Aktien, einmal Krypto probiert'-Wallets komplett
        durch - 50 frische Signale in Folge, alle verworfen, NULL Krypto-
        Einstiege. Solche Wallets liefern außerhalb der US-Börsenzeiten fast nur
        gesperrte Signale und sind dann totes Pool-Gewicht. Unbekannte Coin-
        Liste -> nicht ausschließen (sicherer Default)."""
        coins = getattr(m, "coins", None) or []
        if not coins:
            return False
        crypto_share = sum(1 for c in coins if ":" not in c) / len(coins)
        return crypto_share < self.cfg.sprint.regime_min_crypto_share

    def _is_sleeper(self, m) -> bool:
        """Schläfer-Wallet fürs Quest-Gate: hält KEINE offene Position UND hat
        seit >max_idle_days nicht getradet. Historisch-gut-aber-jetzt-still -
        ein toter Scan-Slot, der keine Signale liefert (Nutzer-Befund)."""
        max_idle = self.cfg.sprint.max_idle_days
        return (max_idle > 0 and getattr(m, "open_positions", 0) == 0
                and getattr(m, "days_since_last_trade", 999.0) > max_idle)

    def _build_sprint_pool(self, sprint_ok) -> list[dict]:
        """Top-pool_size nach RICHTUNGS-Score (sprint_score) aus den Sprint-
        tauglichen Wallets; Haupt-Leader immer enthalten (sie sind bewiesen gut
        genug fürs Hauptbuch - Sprint darf sie nie aus den Augen verlieren).

        Gebannte Leader fliegen RAUS - das ist der Sinn des Strike-Systems
        (Nutzer): ein enttarnter LARP darf keinen Pool-Slot mehr blockieren,
        der nächstbeste frische Kandidat rückt nach. Ohne diesen Filter blieb
        ein gebannter Leader für immer im Pool sitzen (fraß Scan-/Snapshot-
        Slot) und seine Signale wurden nur still verworfen, statt ersetzt.

        Idle-Rotation (Nutzer: 'scannen scannen Daten'): eine Wallet, die zu
        lange KEIN Signal gab (idle_addrs), rutscht nach HINTEN - frische
        Kandidaten kriegen Vorrang. Self-balancing: eine Stumme landet nur dann
        doch im Top-N, wenn es nicht genug aktive/neue Kandidaten gibt."""
        banned = self._sprint_banned()
        idle = self.sprint.idle_addrs(self.cfg.sprint.rotate_idle_hours * 3600) \
            if self.sprint else set()
        n = max(self.cfg.sprint.pool_size, len(self.leaders))
        # Regime-abhängiger Pool (Nutzer): ist der Aktien-Basket gerade zu
        # (crypto_only, außerhalb der US-Börsenzeiten - ~17.5h/Tag), fliegen
        # ÜBERWIEGEND-Aktien-Trader raus (nicht mehr nur 100%-reine - siehe
        # _is_stock_dominated). Sonst füllt sich der Pool mit Wallets, die die
        # ganzen geschlossenen Stunden nur gesperrte Aktien-Signale liefern und
        # der Bot läuft leer. Zum Eröffnungs-Gong (crypto_only=false) kommen sie
        # zurück.
        crypto_only = self.cfg.sprint.crypto_only
        banned_out = sum(1 for m in sprint_ok if m.address.lower() in banned)
        cands = [m for m in sprint_ok if m.address.lower() not in banned
                 and not (crypto_only and self._is_stock_dominated(m))]
        regime_out = len(sprint_ok) - banned_out - len(cands)
        # Sortierschlüssel: aktive/neue Wallets (nicht idle) zuerst, Stumme ans
        # Ende; danach Pfad-A (aktiver Trader) vor Pfad-B (Positions-Sitzer) -
        # Nutzer-Fund 18.07.: 11 von 18 Pool-Wallets hielten seit Wochen nur
        # eine grüne Position (Pfad B) und können strukturell NIE ein frisches
        # 0->Position-Signal geben, sie fressen nur Pool-Slots. Kein harter
        # Ausschluss (Pfad B bleibt gültig, siehe check_sprint-Docstring), nur
        # nachrangig - self-balancing wie die Idle-Rotation direkt daneben.
        # Zuletzt nach Richtungs-Score. [:n] schneidet die Nachrangigen ab,
        # SOLANGE genug bessere Kandidaten da sind.
        top = sorted(cands,
                     key=lambda m: (m.address.lower() not in idle,
                                     m.sprint_qualify_path != "B",
                                     m.sprint_score),
                     reverse=True)[:n]
        pool = [{"address": m.address, "score": round(m.sprint_score, 1)} for m in top]
        have = {p["address"] for p in pool}
        forced_main = 0
        # Zwangsergänzung IGNORIERT cap_path_b_admission bewusst (Wave-3-Audit-
        # Fund 18.07., explizit geklärt): ein Haupt-Leader hat bereits die
        # strengeren Hauptbuch-Kriterien bestanden - ein anderes Risikoprofil
        # als ein frisch entdeckter, gedeckelter Pfad-B-Kandidat. Der Deckel
        # gilt für sprint_ok, nicht für diese ältere 'nie aus den Augen
        # verlieren'-Garantie (siehe cap_path_b_admission-Docstring).
        for l in self.leaders:
            if l["address"] not in have and l["address"].lower() not in banned:
                pool.append({"address": l["address"], "score": l.get("score", 0)})
                forced_main += 1
        # Rohzahlen für /quest funnel (siehe _analysis_funnel) - zeigt, OB und WO
        # der Pool unter pool_size bleibt (Nutzer-Befund: 14/20, Idle-Rotation
        # schneidet dann nie ab, weil gar keine Konkurrenz um die Slots besteht).
        self._pool_funnel = {
            "sprint_ok_in": len(sprint_ok), "banned_out": banned_out,
            "regime_out": regime_out, "crypto_only": crypto_only,
            "cands_after_filters": len(cands),
            "idle_total": len(idle), "idle_in_pool": sum(1 for m in top if m.address.lower() in idle),
            "truncated_by_size": max(0, len(cands) - n),
            "pool_size_target": n, "forced_main": forced_main,
            "final_pool": len(pool),
        }
        self._save_funnel_cache()
        return pool

    def _prune_banned_from_pool(self) -> None:
        """Ein Sprint-Ban soll den LARP nicht nur stumm schalten, sondern seinen
        Pool-Slot freigeben (Nutzer: 'LARPs raus, neue Wallets rein'). Beim Start
        existiert das Sprint-Buch erst NACH _load_sprint_pool - hier also
        nachträglich die gebannten aus dem geladenen Pool werfen. Wurden dadurch
        Plätze frei, eine frische Analyse anfordern: die füllt sie mit NEUEN
        Kandidaten (der _build_sprint_pool-Filter oben hält die Gebannten dann
        dauerhaft draußen), statt den Pool bloß schrumpfen zu lassen."""
        banned = self._sprint_banned()
        if not banned:
            return
        before = len(self.sprint_leaders)
        self.sprint_leaders = [l for l in self.sprint_leaders
                               if str(l.get("address", "")).lower() not in banned]
        removed = before - len(self.sprint_leaders)
        if removed:
            log.info("Sprint: %d gebannte Leader aus dem Pool geworfen - fordere "
                     "frische Analyse an (neue Wallets rücken auf die freien Slots)",
                     removed)
            self._force_analysis = True
            if self.copier:
                self.copier.tracker.addresses = self._tracked_addresses()

    def _load_sprint_pool(self) -> None:
        """Sprint-Pool aus der letzten Analyse laden (überlebt Neustart), sonst auf
        die Haupt-Leader zurückfallen, bis die erste Rotation den Pool verbreitert."""
        pool: list[dict] = []
        path = RUNTIME / self._SPRINT_POOL_FILE
        if path.exists():
            try:
                pool = json.loads(path.read_text())[: self.cfg.sprint.pool_size]
            except (ValueError, OSError):
                pool = []
        have = {p.get("address") for p in pool}
        for l in self.leaders:   # Haupt-Leader immer im Pool (Datei evtl. veraltet)
            if l["address"] not in have:
                pool.append({"address": l["address"], "score": l.get("score", 0)})
        self.sprint_leaders = pool or list(self.leaders)

    def _maybe_reanalyze(self) -> None:
        hours = self.cfg.autopilot.reanalyze_hours
        forced = self._force_analysis
        if not (forced or time.time() - self._last_analysis >= hours * 3600):
            return
        if self._analysis_running:
            return   # läuft bereits - kein Parallel-Lauf (würde Leader-Listen zerschreiben)
        self._force_analysis = False
        # Analyse in den HINTERGRUND: sie dauert Minuten (70 Wallets x Drossel +
        # 429-Backoffs) und blockierte vorher den ganzen Loop - Copier/Sprint
        # waren währenddessen blind (Live-Befund: 'Snapshots ⚠️ vor 5 min').
        # Der Loop scannt mit dem alten Pool weiter; die Ergebnisse werden am
        # Ende als GANZE Objekte eingewechselt (self.leaders = ..., weights = {...},
        # tracker.addresses = [...]) - atomare Referenz-Swaps, kein Teilzustand.
        self._analysis_running = True   # sofort setzen: Loop-Tick soll nicht doppelt starten

        def run() -> None:
            try:
                self._reanalyze()
            finally:
                if forced:
                    # Explizit angefordert -> Ergebnis aktiv pushen statt den Nutzer
                    # /status pollen zu lassen
                    self.notifier.send("🔬 <b>Analyse fertig</b>\n"
                                       + (self._analysis_note or "keine Diagnose (Logs prüfen)"))

        self._analysis_thread = threading.Thread(target=run, daemon=True, name="analyse")
        self._analysis_thread.start()

    def _discover_candidates(self, an) -> tuple[list[str], str]:
        """Kandidaten-Adressen für die Tiefenanalyse: UNION aus HyperTracker-Board
        (kopierbare, vorgefilterte perp-pnl-Trader) und dem bisherigen HL-Funnel.
        Union statt entweder/oder: CMM verbreitert, HL liefert die bewährte
        Population weiter - wir sind nie schlechter als der alte Weg. Das
        Qualitäts-Gate (30-Tage-Analyse + LARP) läuft danach für alle gleich.
        Liefert (Adressen, Quellen-Notiz fürs /status)."""
        cm = self.cfg.coinmarketman
        cmm_addrs: list[str] = []
        if cm.enabled:
            try:
                from .sources.coinmarketman import fetch_cmm_candidates

                cmm_addrs = [c.address for c in fetch_cmm_candidates(cm)]
                log.info("CMM-Discovery: %d kopierbare Kandidaten (perp-pnl nach "
                         "%s, %d Seiten)", len(cmm_addrs), cm.period, cm.pages)
            except Exception as e:
                log.warning("CMM-Discovery fehlgeschlagen (%s) - nur HL-Funnel",
                            str(e)[:150])
        hl_addrs: list[str] = []
        try:
            hl_addrs = [c.address for c in fetch_candidates(
                min_account_value=an.min_account_value, min_volume=an.min_volume,
                top_n=an.top_n, top_percent=an.top_percent,
            )]
        except Exception as e:
            if not cmm_addrs:
                raise   # beide Quellen tot -> Analyse abbrechen (Aufrufer fängt)
            log.warning("HL-Funnel fehlgeschlagen (%s) - nur CMM-Kandidaten",
                        str(e)[:150])
        seen: set[str] = set()
        union: list[str] = []

        def add(a: str) -> None:
            k = a.lower()
            if k not in seen:
                seen.add(k)
                union.append(a)

        # 1. Bestands-Leader IMMER analysieren: rotate_leaders behält Verschwundene
        #    nicht ('vanished leader not kept') - ohne Analyse flöge der bewährte
        #    Leader blind raus, nur weil der Kandidaten-Deckel ihn verdrängt hat.
        for l in self.leaders:
            add(l["address"])
        # 2. INTERLEAVE statt Anhängen: bei 96 CMM + 38 HL und top_n=50 gingen
        #    sonst ALLE Plätze an CMM (grüne Positions-Trader = Selten-Öffner),
        #    die HL-Day-Trader (= Häufig-Öffner, Sprints Signalquelle!) fielen
        #    komplett raus - ein Hunger-Verstärker fürs Sprint-Buch.
        for i in range(max(len(cmm_addrs), len(hl_addrs))):
            if i < len(cmm_addrs):
                add(cmm_addrs[i])
            if i < len(hl_addrs):
                add(hl_addrs[i])
        note = f"CMM {len(cmm_addrs)} + HL {len(hl_addrs)}"
        return union[: an.top_n], note

    def _reanalyze(self) -> None:
        log.info("Starte Leaderboard-Analyse (Funnel + LARP-Filter) ...")
        an = self.cfg.copytrade.analysis
        report: dict = {}
        self._analysis_running = True
        self._analysis_started = time.time()
        try:
            self._reanalyze_body(an, report)
        finally:
            self._analysis_running = False
            self._last_analysis = time.time()
            # Zeitstempel auch über Neustarts persistieren: _load_or_analyze_leaders
            # liest die mtime der Leader-Datei. Ohne touch feuert nach JEDEM
            # /update sofort eine neue Analyse (Datei ändert sich nur bei
            # Leader-Wechsel) - das frisst CMM-Budget und blockiert den Loop.
            try:
                path = Path(self.cfg.copytrade.leaders_file)
                if path.exists():
                    path.touch()
            except OSError:
                pass

    def _reanalyze_body(self, an, report: dict) -> None:
        try:
            addresses, src_note = self._discover_candidates(an)
            info = Info(api_url(testnet=False), skip_ws=True)
            analyzer = TraderAnalyzer(info, days=an.days)
            larp = LarpFilter(LarpConfig(**(an.larp or {})))
            main_ranked = analyzer.rank(addresses, min_score=an.min_score,
                                        larp=larp, report=report)
        except Exception as e:
            log.exception("Analyse fehlgeschlagen - behalte bisherige Leader")
            self._analysis_note = f"⚠️ Analyse fehlgeschlagen: {str(e)[:150]}"
            return

        # Sprint-Pool: eigenes, richtungs-orientiertes Gate über ALLE messbaren
        # Wallets (auch Haupt-LARP-K.O.s wie Swing-Trader oder Lucky-Puncher) -
        # Sprint zählt Richtungs-Treffer, nicht Profit-Größe (Nutzer-Vorgabe).
        sprint_ok = []
        sprint_gate_ko = sprint_score_ko = sprint_idle_ko = sprint_path_b_ko = 0
        if self.cfg.sprint.enabled:
            from .copytrade.larp import check_sprint

            larp_cfg = LarpConfig(**(an.larp or {}))
            pool_min = self.cfg.sprint.pool_min_score
            path_a, path_b = [], []
            for m in report.get("metrics", []):
                # Schläfer-Filter zuerst (Nutzer: 'die keine positions sleeper
                # sind und kein Signal ist doch sus'): wer weder eine offene
                # Position hält NOCH in den letzten max_idle_days getradet hat,
                # ist ein toter Slot - egal wie gut die Alt-Historie. Raus, bevor
                # er den Pool blockiert.
                if self._is_sleeper(m):
                    sprint_idle_ko += 1
                    continue
                verdict = check_sprint(m, larp_cfg)
                if not verdict.passed:
                    sprint_gate_ko += 1
                elif m.sprint_score < pool_min:
                    sprint_score_ko += 1
                else:
                    m.sprint_qualify_path = verdict.path
                    (path_a if verdict.path == "A" else path_b).append(m)
            sprint_ok, sprint_path_b_ko = cap_path_b_admission(
                path_a, path_b, self.cfg.sprint.pool_size, self.cfg.sprint.path_b_max_share)
            sprint_ok.sort(key=lambda m: m.sprint_score, reverse=True)

        # Rohzahlen für /quest funnel - separat von der formatierten Notiz unten,
        # damit der Befehl sie sauber tabellarisch zeigen kann statt Text zu parsen.
        self._analysis_funnel = {
            "candidates": len(addresses), "source": src_note,
            "main_ranked": len(main_ranked), "min_score": an.min_score,
            "sprint_gate_ko": sprint_gate_ko, "sprint_score_ko": sprint_score_ko,
            "sprint_idle_ko": sprint_idle_ko, "sprint_path_b_ko": sprint_path_b_ko,
            "sprint_ok": len(sprint_ok), "pool_min_score": self.cfg.sprint.pool_min_score,
            "truncated": report.get("truncated", 0), "larp_ko": report.get("larp_ko", 0),
            "larp_reasons": dict(report.get("larp_reasons", {})),
            "errors": report.get("errors", 0),
        }
        self._save_funnel_cache()
        top_scores = "/".join(f"{s:.0f}" for _, s in report.get("scores", [])[:3]) or "-"
        larp_top = ", ".join(f"{k}×{n}" for k, n in sorted(
            report.get("larp_reasons", {}).items(), key=lambda t: -t[1])[:2]) or "-"
        # ACHTUNG: kein rohes '<' in dieser Notiz - sie geht mit parse_mode=HTML
        # an Telegram, und ein '<' ließ den Versand mit 400 platzen (Funkstille!)
        self._analysis_note = (
            f"{len(addresses)} Kandidaten ({src_note}) → {len(main_ranked)} Haupt"
            f"(≥{an.min_score:g}) / {len(sprint_ok)} Sprint-tauglich "
            f"(Gate-K.O. {sprint_gate_ko}, Richtung unter "
            f"{self.cfg.sprint.pool_min_score:g}: {sprint_score_ko}, "
            f"Schläfer: {sprint_idle_ko}, Sitzer-Deckel: {sprint_path_b_ko})\n"
            f"Aussortiert: {report.get('truncated', 0)} zu aktiv, "
            f"{report.get('larp_ko', 0)} LARP ({larp_top}), "
            f"{report.get('errors', 0)} Fehler | Top-Scores: {top_scores}")
        log.info("Analyse-Trichter: %s", self._analysis_note.replace("\n", " | "))

        new_leaders = rotate_leaders(
            self.leaders, main_ranked, self.cfg.copytrade.max_leaders, self.cfg.autopilot.min_keep_score,
        )
        if not new_leaders:
            log.warning("Analyse fand keine geeigneten Leader - behalte bisherige")
        else:
            old = {l["address"] for l in self.leaders}
            new = {l["address"] for l in new_leaders}
            if old != new:
                log.info("Leader-Rotation: raus %s | rein %s",
                         [a[:10] for a in old - new] or "-", [a[:10] for a in new - old] or "-")
                self.journal.record("rotation", removed=sorted(old - new), added=sorted(new - old))
                self.notifier.send(
                    "🔄 <b>Leader-Rotation</b>\n"
                    + "\n".join(f"➖ {a[:10]}…" for a in old - new)
                    + ("\n" if old - new and new - old else "")
                    + "\n".join(f"➕ {a[:10]}… (Score {next(l['score'] for l in new_leaders if l['address'] == a)})"
                                for a in new - old)
                )
            self.leaders = new_leaders
            Path(self.cfg.copytrade.leaders_file).write_text(json.dumps(new_leaders, indent=2))
            # copier.weights wird nur vom Rebalance-/Order-Pfad im Hauptbuch gelesen
            # (copier.py compute_targets) - bei feed_only=true (config.yaml, dauerhaft
            # an: "es gibt nur noch den Quest-Bot") wird dieser Pfad nie erreicht,
            # tick() kehrt vorher zurück. Bewusst NICHT entfernt (Wave-2-Audit-Fund
            # 17.07.): reine Zuweisung, kostet nichts, und greift sofort wieder
            # korrekt, falls feed_only je zurückgestellt wird.
            if self.copier:
                self.copier.weights = {l["address"]: float(l["weight"]) for l in new_leaders}
            if self.feed:
                self.feed.resubscribe([l["address"] for l in new_leaders])
        # Sprint-Pool UNABHÄNGIG vom Haupt-Rotations-Ausgang aktualisieren - sonst
        # bleibt der Pool bei einer leeren Haupt-Auswahl auf dem alten Stand hängen.
        if sprint_ok or main_ranked:
            self.sprint_leaders = self._build_sprint_pool(sprint_ok)
            try:
                (RUNTIME / self._SPRINT_POOL_FILE).write_text(
                    json.dumps(self.sprint_leaders, indent=2))
            except OSError:
                log.debug("sprint_leaders.json nicht schreibbar", exc_info=True)
        if self.copier:
            self.copier.tracker.addresses = self._tracked_addresses()

    # ---------- Status für das Frontend ----------

    def _publish(self) -> None:
        equity = positions = paper_stats = None
        try:
            if self.cfg.dry_run and self.copier and self.copier.paper:
                # Paper-Modus: simuliertes Konto ist die Wahrheit
                broker = self.copier.paper
                equity = self.copier.last_equity
                positions = broker.position_rows(self.copier.last_prices)
                paper_stats = {
                    "trades": broker.trades,
                    "realized_pnl": round(broker.realized_pnl, 2),
                    "fees_paid": round(broker.fees_paid, 2),
                }
            else:
                addr = self.client.account_address or self.account_address
                if addr:
                    state = self.client.merged_user_state(addr)
                    equity = float(state["marginSummary"]["accountValue"])
                    positions = [
                        {
                            "coin": p["position"]["coin"],
                            "size": float(p["position"]["szi"]),
                            "entry": float(p["position"]["entryPx"] or 0),
                            "unrealized_pnl": float(p["position"]["unrealizedPnl"]),
                        }
                        for p in state.get("assetPositions", [])
                        if float(p["position"]["szi"]) != 0
                    ]
        except Exception:
            log.debug("Account-Status nicht abrufbar", exc_info=True)

        # Alerts bei Zustandswechseln (Risiko-Level, Circuit Breaker)
        risk = self.guard.last_level.name if self.guard else "NORMAL"
        if risk != self._prev_risk:
            icon = {"NORMAL": "🟢", "CAUTION": "🟡", "RISK_OFF": "🔴"}.get(risk, "")
            self.notifier.send(f"{icon} Risiko-Level: <b>{self._prev_risk} → {risk}</b>")
            self._prev_risk = risk
        halted = bool(self.copier and self.copier.halted)
        if halted and not self._prev_halted:
            self.notifier.send("⛔ <b>CIRCUIT BREAKER</b> – Tagesverlust-Limit erreicht, "
                               "alle Positionen geschlossen, Bot pausiert bis Neustart.")
        self._prev_halted = halted

        self._record_history(equity)
        leaders = self._with_performance(self.leaders)

        self._set_status(
            state="halted" if (self.copier and self.copier.halted) else "running",
            mode="dry_run" if self.cfg.dry_run else "live",
            network="testnet" if self.cfg.is_testnet else "mainnet",
            risk_level=self.guard.last_level.name if self.guard else "NORMAL",
            realtime=bool(self.feed and self.feed.connected),
            ws_fills=self.feed.fills_seen if self.feed else 0,
            anomalies=list(self.scout.flagged[-5:]) if self.scout else [],
            orderbook=list(self.book_scout.flagged[-5:]) if self.book_scout else [],
            twap=list(self.twap_scout.flagged[-5:]) if self.twap_scout else [],
            polymarket=list(self.poly_scout.flagged[-5:]) if self.poly_scout else [],
            labs=(self.labs.stats(self.copier.last_prices)
                  if self.labs and self.copier and self.copier.last_prices else None),
            sprint=(self.sprint.stats(self.copier.last_prices)
                    if self.sprint and self.copier and self.copier.last_prices else None),
            lighter=(self.lighter.stats(self.copier.last_prices)
                     if self.lighter and self.copier and self.copier.last_prices else None),
            equity=equity,
            positions=positions or [],
            paper=paper_stats,
            shadows=(self.copier.shadows.stats(self.copier.last_prices)
                     if self.copier and self.copier.shadows and self.copier.last_prices else None),
            leaders=leaders,
            account=self.client.account_address or self.account_address,
            next_analysis_in_h=round(
                max(0.0, self.cfg.autopilot.reanalyze_hours - (time.time() - self._last_analysis) / 3600), 1
            ),
        )

    # ---------- Investigator: Watchlist-Wallets beobachten ----------

    def _watch_wallets(self) -> None:
        if not self.watcher or time.time() - self._last_watch_poll < self.cfg.investigator.poll_seconds:
            return
        self._last_watch_poll = time.time()
        for ev in self.watcher.poll():
            log.info("Investigator: %s", ev.text())
            self.journal.record("watchlist", address=ev.address, coin=ev.coin, event=ev.kind,
                                old=round(ev.old_notional, 0), new=round(ev.new_notional, 0))
            self.notifier.send(f"🔍 <b>Watchlist</b>\n{ev.text()}")

    # ---------- Tages-Digest & Inaktivitäts-Watchdog ----------

    def _maybe_digest(self) -> None:
        """Einmal täglich: kompakter Quest-Lagebericht - damit '+1$ nach 4
        Wochen' spätestens am Tag 2 auffällt, nicht am Tag 28. Dreht sich nur
        noch um den Quest-Bot und die Schatztruhe (kein Kopier-Buch mehr)."""
        if not self.cfg.autopilot.daily_digest:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today == self._digest_date:
            return
        self._digest_date = today
        risk = self.guard.last_level.name if self.guard else "NORMAL"
        if not self.sprint:
            self.notifier.send(f"📊 <b>Tagesbericht</b>\nQuest-Bot aus. Risiko: {risk}")
            return
        sp = self.sprint.stats(self.copier.last_prices if self.copier else {})
        sc = sp.get("scan", {})
        # Schatztruhe-Delta über 24h: macht '+1$ nach Wochen' früh sichtbar
        day_line = ""
        if self._digest_equity is not None:
            day_line = f" ({sp['banked'] - self._digest_equity:+,.2f}$ / 24h)"
        msg = (f"📊 <b>Quest-Tagesbericht</b>\n"
               f"Schatztruhe: {sp['banked']:+,.2f} ${day_line}\n"
               f"Zyklus {sp['cycle']} {sp['state']} ({sp['cycle_pnl']:+,.2f}$) "
               f"[{sp['won']}✅/{sp['busted']}💥]\n"
               f"Scan: {sc.get('fresh_seen', 0)} frische Signale seit Start | "
               f"Pool {len(self.sprint_leaders)} | Risiko: {risk}\n"
               f"Auswertung: /report")
        log.info("Quest-Tagesbericht: Schatztruhe %+.2f, Zyklus %d",
                 sp["banked"], sp["cycle"])
        self.notifier.send(msg)
        self._digest_equity = sp["banked"]

    def _maybe_push_status(self) -> None:
        """Status-Spiegel (Nutzer 17.07.): schreibt periodisch einen JSON-
        Schnappschuss übers Repo, damit der aktuelle Bot-Zustand direkt per
        Lesezugriff verfügbar ist statt manuell aus Telegram kopiert werden zu
        müssen. Kein Token gesetzt oder push fehlgeschlagen -> niemals fatal,
        einfach beim nächsten Intervall erneut versuchen (siehe status_push.py).

        Netzwerk-Teil (push_snapshot, mehrere GitHub-API-Calls) läuft im
        HINTERGRUND (Wave-2-Audit-Fund 17.07.): synchron blockierte das eine
        langsame GitHub-Antwort bis zu ~30s den einzigen Tick-Thread - genau
        das Muster, das _maybe_reanalyze schon lange als Hintergrund-Job löst
        (Kommentar dort: 'blockierte vorher den ganzen Loop'). build_snapshot()
        selbst bleibt synchron (nur Attribut-Reads + ein schneller git-Aufruf) -
        der Schnappschuss soll den Zustand GENAU JETZT zeigen, nicht den vom
        Ende eines womöglich sekundenlangen Netzwerk-Calls."""
        sp = self.cfg.status_push
        if not sp.enabled:
            return
        if time.time() - self._last_status_push < sp.interval_minutes * 60:
            return
        if self._status_push_running:
            return   # vorheriger Push (Netzwerk) läuft noch - kein Parallel-Push
        self._last_status_push = time.time()
        from .status_push import build_snapshot, push_snapshot

        snapshot = build_snapshot(self)
        self._status_push_running = True

        def run() -> None:
            try:
                ok = push_snapshot(snapshot, repo=sp.repo, branch=sp.branch, path=sp.path)
                if not ok:
                    log.debug("Status-Push nicht geschrieben (kein Token oder Fehler, siehe Log)")
            finally:
                self._status_push_running = False

        self._status_push_thread = threading.Thread(target=run, daemon=True, name="status-push")
        self._status_push_thread.start()

    def _maybe_market_gong(self, now=None) -> None:
        """Worldclock (Nutzer): zum US-Börsen-GONG den Aktien-Basket auf/zu.
        Eröffnung -> crypto_only=false + /analyze (frische, gerade aktive Aktien-
        Trader in den Pool). Schluss -> crypto_only=true + /analyze + profitable
        Aktien-Positionen sichern (Verlust fängt die 2h-Regel). Feuert nur an der
        FLANKE (Zustandswechsel), nicht jeden Tick. `now` injizierbar fürs Testen."""
        if not (self.sprint and self.cfg.sprint.stock_market_hours):
            return
        from .market_hours import us_market_open

        now_open = us_market_open(now)
        if self._market_open is None:
            # Erststart: crypto_only nur mit dem aktuellen Marktzustand
            # synchronisieren - KEIN Gong (kein /analyze, kein Schließen).
            self.cfg.sprint.crypto_only = not now_open
            self._market_open = now_open
            return
        if now_open == self._market_open:
            return
        self._market_open = now_open
        self._force_analysis = True   # Pool für das neue Regime neu bauen
        if now_open:
            self.cfg.sprint.crypto_only = False
            self.notifier.send("🔔 <b>Börse offen</b> - Aktien-Basket aktiv, "
                               "frische Analyse läuft.")
        else:
            self.cfg.sprint.crypto_only = True
            prices = self.copier.last_prices if self.copier else {}
            n = self.sprint.close_stock_winners(prices)
            self.notifier.send(
                "🔔 <b>Börse zu</b> - Aktien-Basket aus, frische Analyse läuft."
                + (f"\n{n} profitable Aktien-Position(en) gesichert." if n else "")
                + ("\nOffene Verlust-Aktien fängt die 2h-Regel." if not n
                   and self.sprint.paper.sizes() else ""))

    def _maybe_warn_stale_feed(self) -> None:
        """Warnt, wenn Sprint auf eingefrorenen Snapshots scannt (Copier pausiert
        z.B. bei HALTED oder Dauerfehlern) - sonst sähe 'wartet auf frisches
        Signal' gesund aus, während der Bot in Wahrheit Standbilder anstarrt."""
        if not (self.copier and self.copier.last_snapshots_t):
            return
        age = time.time() - self.copier.last_snapshots_t
        if age > 300 and not self._stale_feed_warned:
            self._stale_feed_warned = True
            log.warning("Quest-Feed eingefroren: Snapshots %.0f min alt", age / 60)
            self.notifier.send(
                f"⚠️ <b>Quest-Feed eingefroren</b>: Leader-Snapshots sind "
                f"{age / 60:.0f} min alt - der Scan sieht keine neuen Signale.\n"
                f"Mögliche Ursachen: Circuit-Breaker (/resume), API-Störung. /status prüfen."
            )
        elif age < 60 and self._stale_feed_warned:
            self._stale_feed_warned = False   # wieder frisch -> Warnung neu scharf
            self.notifier.send("✅ Quest-Feed wieder frisch (Snapshots aktuell).")

    def _maybe_watchdog(self) -> None:
        """Meldet sich, wenn der QUEST-Bot auffällig lange keinen Ritt eröffnet
        hat. Quest-Ära: das Kopier-Buch tradet feed-only nicht mehr, 'keine
        Order' wäre immer wahr - der ehrliche Inaktivitäts-Alarm ist jetzt
        'kein Quest-Einstieg', und der deutet meist auf einen toten/schlafenden
        Pool (genau der Fall, den es zu fangen gilt)."""
        hours = self.cfg.autopilot.watchdog_hours
        if hours <= 0 or not self.sprint:
            return
        entries = self.journal.tail(500)
        last_entry = next((e["t"] for e in entries if e.get("kind") == "sprint_entry"), None)
        ref = last_entry or self._start_time
        if last_entry and self._watchdog_warned:
            if time.time() - last_entry < hours * 3600:
                self._watchdog_warned = False  # wieder aktiv -> Alarm scharf stellen
        if time.time() - ref < hours * 3600 or self._watchdog_warned:
            return
        self._watchdog_warned = True
        diagnosis = self._diagnose_quest_idle()
        log.warning("Watchdog: kein Quest-Einstieg seit %.0fh - %s", hours, diagnosis)
        self.notifier.send(f"⏰ <b>Watchdog</b>: kein Quest-Einstieg seit {hours:.0f}h.\n{diagnosis}")

    def _diagnose_quest_idle(self) -> str:
        """Warum eröffnet der Quest-Bot nichts? Toter/schlafender Pool, Signale
        die verworfen werden, gesperrte Leader oder eingefrorener Feed."""
        parts: list[str] = []
        snaps = getattr(self.copier, "last_snapshots", []) if self.copier else []
        holds = {s.address.lower(): bool(s.positions) for s in snaps}
        n = len(self.sprint_leaders)
        n_hold = sum(1 for l in self.sprint_leaders
                     if holds.get(str(l.get("address", "")).lower()))
        if n and n_hold == 0:
            parts.append(f"KEINE der {n} Pool-Wallets hält gerade eine Position - "
                         "toter/schlafender Pool, /analyze holt frische Kandidaten")
        elif n:
            parts.append(f"{n_hold}/{n} Pool-Wallets halten Positionen")
        st = self.sprint.stats(self.copier.last_prices if self.copier else {})
        sc = st.get("scan", {})
        seen = sc.get("fresh_seen", 0)
        rej = sc.get("rejected", {})
        if seen == 0:
            parts.append("0 frische Signale seit Start - der Pool eröffnet nichts "
                         "(/quest pool zeigt, wer flach ist)")
        elif rej:
            top = ", ".join(f"{k} ×{v}" for k, v in
                            sorted(rej.items(), key=lambda t: -t[1])[:2])
            parts.append(f"{seen} Signale gesehen, aber verworfen: {top}")
        if st.get("banned"):
            parts.append(f"{len(st['banned'])} Leader gesperrt")
        if self.copier and self.copier.last_snapshots_t:
            age = time.time() - self.copier.last_snapshots_t
            if age > 300:
                parts.append(f"⚠️ Feed {age / 60:.0f} min alt - Snapshots eingefroren (/resume?)")
        return " | ".join(parts) if parts else "keine eindeutige Ursache - /quest prüfen"

    # ---------- Equity-Historie & Leader-Performance ----------

    def _record_history(self, equity: float | None) -> None:
        """Hängt alle 60s einen Equity-Punkt an runtime/history.jsonl (Frontend-Chart)."""
        if equity is None or time.time() - self._last_history_write < 60:
            return
        self._last_history_write = time.time()
        try:
            RUNTIME.mkdir(exist_ok=True)
            path = RUNTIME / "history.jsonl"
            with open(path, "a") as f:
                f.write(json.dumps({"t": int(time.time()), "equity": round(equity, 2)}) + "\n")
            # Datei begrenzen: bei >20k Punkten auf die letzten 10k kürzen
            if path.stat().st_size > 2_000_000:
                lines = path.read_text().splitlines()[-10_000:]
                path.write_text("\n".join(lines) + "\n")
        except OSError:
            pass

    def _with_performance(self, leaders: list[dict]) -> list[dict]:
        """Reichert die Leader-Liste um die gemessene ROI seit Kopie-Beginn an.

        Achtung Messgrenze: Equity-basiert - Ein-/Auszahlungen des Leaders
        verfälschen den Wert. Trotzdem das ehrlichste Live-Maß dafür, ob ein
        Leader seit Aufnahme tatsächlich liefert.
        """
        snaps = {s.address: s.equity for s in (self.copier.last_snapshots if self.copier else [])}
        out = []
        changed = False
        for l in leaders:
            l = dict(l)
            eq = snaps.get(l["address"])
            perf = self._leader_perf.get(l["address"])
            if eq and eq > 0:
                if not perf:
                    self._leader_perf[l["address"]] = {"start_equity": eq, "start": int(time.time())}
                    changed = True
                else:
                    l["roi_since_copy_pct"] = round((eq / perf["start_equity"] - 1) * 100, 2)
            out.append(l)
        if changed:
            self._save_perf()
        return out

    def _load_perf(self) -> dict:
        try:
            return json.loads((RUNTIME / "leader_perf.json").read_text())
        except (OSError, ValueError):
            return {}

    def _load_funnel_cache(self) -> dict:
        """analysis_funnel/pool_funnel überleben einen Neustart (Nutzer 17.07.,
        'kein blinder Fleck mehr') - siehe Kommentar am Zuweisungsort in __init__."""
        try:
            return json.loads((RUNTIME / "funnel_cache.json").read_text())
        except (OSError, ValueError):
            return {}

    def _save_funnel_cache(self) -> None:
        try:
            RUNTIME.mkdir(exist_ok=True)
            (RUNTIME / "funnel_cache.json").write_text(json.dumps(
                {"analysis_funnel": self._analysis_funnel,
                 "pool_funnel": self._pool_funnel,
                 "saved_at": datetime.now(timezone.utc).isoformat()}, indent=2))
        except OSError:
            pass

    def _save_perf(self) -> None:
        try:
            RUNTIME.mkdir(exist_ok=True)
            (RUNTIME / "leader_perf.json").write_text(json.dumps(self._leader_perf, indent=2))
        except OSError:
            pass

    def _set_status(self, **kwargs) -> None:
        with self._lock:
            self._status = {"updated": datetime.now(timezone.utc).isoformat(), **kwargs}
            try:
                RUNTIME.mkdir(exist_ok=True)
                (RUNTIME / "status.json").write_text(json.dumps(self._status, indent=2))
            except OSError:
                pass
