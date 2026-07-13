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


def diagnose_inactivity(entries: list[dict], since_t: float, copier=None) -> str:
    """Erklärt, WARUM keine Orders kommen - die Watchdog-Diagnose."""
    recent = [e for e in entries if e.get("t", 0) >= since_t]
    vetoes = [e for e in recent if e.get("kind") == "veto"]
    parts = []
    if vetoes:
        from collections import Counter

        reasons = Counter()
        for v in vetoes:
            joined = " ".join(v.get("reasons", []))
            key = "RSI" if "RSI" in joined else "Trend" if "Trend" in joined else \
                "Claude" if "Claude" in joined else "Daten" if "wenig" in joined or "verfügbar" in joined else "sonstige"
            reasons[key] += 1
        parts.append(f"{len(vetoes)} Vetos ({', '.join(f'{k}: {n}' for k, n in reasons.most_common(3))}) "
                     "-> Validator blockt; report.py zeigt, ob zu Recht")
    leader_positions = 0
    if copier and copier.last_snapshots:
        leader_positions = sum(len(s.positions) for s in copier.last_snapshots)
    if not vetoes and leader_positions == 0:
        parts.append("Leader halten keine Positionen -> es gibt nichts zu kopieren; "
                     "Rotation abwarten oder analysis.top_percent erhöhen")
    elif not vetoes and leader_positions > 0:
        parts.append(f"Leader halten {leader_positions} Positionen, aber Buch ist im Ziel "
                     "(keine Abweichung über rebalance_threshold) - kein Fehler")
    if copier and copier.halted:
        parts.append("ACHTUNG: Bot ist HALTED (Circuit Breaker/Max-DD) - Neustart nötig")
    return " | ".join(parts) if parts else "keine eindeutige Ursache - Logs prüfen"


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
        self._force_analysis = False   # /analyze: nächster Loop-Tick analysiert sofort
        self._analysis_note = ""       # Trichter der letzten Analyse (für /status)
        self._analysis_running = False # Analyse blockiert den Loop minutenlang -
        self._analysis_started = 0.0   # /status muss "läuft gerade" zeigen können
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
            "/leaders": self._cmd_leaders,
            "/positions": self._cmd_positions,
            "/anomalies": self._cmd_anomalies,
            "/orderbook": self._cmd_orderbook,
            "/twap": self._cmd_twap,
            "/sprint": self._cmd_sprint,
            "/polymarket": self._cmd_polymarket,
            "/stop": self._cmd_stop,
            "/start": self._cmd_start,
            "/resume": self._cmd_resume,
            "/update": self._cmd_update,
            "/probe": self._cmd_probe,
            "/lighter": self._cmd_lighter,
            "/cmm": self._cmd_cmm,
            "/setcmm": self._cmd_setcmm,
            "/analyze": self._cmd_analyze,
            "/help": self._cmd_help,
        })

    # ---------- Telegram-Befehle (Antworten als HTML-String) ----------

    def _cmd_help(self) -> str:
        return ("<b>Befehle</b>\n/status – Zustand & Equity\n/report – Auswertung\n"
                "/positions – offene Positionen + PnL\n/leaders – Leader + ROI\n/anomalies – HL-Scout-Funde\n"
                "/orderbook – Mikrostruktur-Signale\n/twap – laufende Whale-TWAPs\n"
                "/sprint – Sprint-Buch (1000$ x10, Ziel +100$)\n"
                "/polymarket – Prediction-Market-Funde\n"
                "/update – Update ziehen + neu starten\n"
                "/probe – Multi-DEX-Scan-Probe (Extended/Lighter/…)\n"
                "/lighter &lt;ref&gt; – Lighter-Konto prüfen (Verifikation)\n"
                "/setcmm &lt;token&gt; – HyperTracker-API-Token setzen\n"
                "/cmm – HyperTracker-Leaderboard live proben\n"
                "/analyze – Leader-Analyse sofort anstoßen\n"
                "/stop /start /resume – Autopilot/Halt steuern")

    def _cmd_status(self) -> str:
        s = self.status()
        eq = self.copier.last_equity if self.copier else None
        day_t = int(time.time()) - 86_400
        orders = sum(1 for e in self.journal.tail(2000)
                     if e.get("kind") == "order" and e.get("t", 0) >= day_t)
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
        return (f"<b>Status</b>: {s.get('state', '?')}\n"
                f"Equity: {f'{eq:,.2f}' if eq else 'n/a'}\n"
                f"Orders (24h): {orders}\n"
                f"Risiko: {self.guard.last_level.name if self.guard else 'NORMAL'}\n"
                f"Leader: {len(self.leaders)} | Pool: {len(self.sprint_leaders)} | "
                f"WS: {'an' if self.feed and self.feed.connected else 'aus'}\n"
                f"{analysis}\n"
                f"<i>/analyze = Analyse sofort anstoßen</i>")

    def _cmd_report(self) -> str:
        from .report import summarize
        journal = self.journal.tail(5000)
        history = []
        try:
            hist_path = RUNTIME / "history.jsonl"
            if hist_path.exists():
                for line in hist_path.read_text().splitlines()[-1000:]:
                    try:
                        history.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        paper = None
        if self.copier and self.copier.paper:
            b = self.copier.paper
            paper = {"trades": b.trades, "realized_pnl": round(b.realized_pnl, 2),
                     "fees_paid": round(b.fees_paid, 2)}
        s = summarize(journal, history, paper)
        lines = [f"<b>Report</b>"]
        if "days" in s:
            lines.append(f"{s['days']}T: {s['equity_start']:,.0f}→{s['equity_end']:,.0f} "
                         f"({s['return_pct']:+.2f}%)")
        lines.append(f"Orders: {s['orders']} | Vetos: {s['vetoes']}")
        if s.get("maker_share") is not None:
            lines.append(f"Maker-Quote: {s['maker_share']:.0%}")
        if paper:
            lines.append(f"PnL: {s.get('realized_pnl', 0):+,.2f} | Fees: {s.get('fees_paid', 0):,.2f}")
        if self.labs and self.copier and self.copier.last_prices:
            lines.append("<b>Spuren:</b>")
            for name, st in self.labs.stats(self.copier.last_prices).items():
                lines.append(f"  {name}: {st['realized_pnl']:+,.2f} ({st['trades']} Tr.)")
        return "\n".join(lines)

    def _cmd_positions(self) -> str:
        """Was steckt der Bot gerade drin? Offene Positionen mit unrealisiertem PnL."""
        prices = self.copier.last_prices if self.copier else {}
        out: list[str] = []
        rows = self.status().get("positions") or []
        if rows:
            out.append("<b>Copy-Buch</b>")
            total = 0.0
            for r in rows:
                size = r.get("size", 0)
                side = "LONG" if size > 0 else "SHORT"
                entry = r.get("entry", 0)
                px = prices.get(r["coin"], entry) or entry
                pnl = r.get("unrealized_pnl", 0)
                out.append(f"{side} {r['coin']}: {abs(size):.4f} @ {entry:.4f} "
                           f"(${abs(size) * px:,.0f}, PnL {pnl:+,.2f})")
                total += pnl
            out.append(f"Σ unrealisiert: {total:+,.2f}")
        # Eigene Strategien (Paper) - was sie aktuell halten
        if self.labs:
            for lab in self.labs.labs:
                lrows = lab.paper.position_rows(prices)
                if lrows:
                    out.append(f"<b>{lab.name}</b>")
                    for r in lrows:
                        side = "LONG" if r["size"] > 0 else "SHORT"
                        out.append(f"{side} {r['coin']}: {abs(r['size']):.4f} @ {r['entry']:.4f} "
                                   f"(PnL {r['unrealized_pnl']:+,.2f})")
        if self.sprint:
            srows = self.sprint.paper.position_rows(prices)
            if srows:
                out.append("<b>Sprint-Buch (x10)</b>")
                for r in srows:
                    side = "LONG" if r["size"] > 0 else "SHORT"
                    out.append(f"{side} {r['coin']}: {abs(r['size']):.4f} @ {r['entry']:.4f} "
                               f"(PnL {r['unrealized_pnl']:+,.2f})")
        return "\n".join(out) if out else "Aktuell keine offenen Positionen."

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
            return "Sprint-Buch nicht aktiv (sprint.enabled / dry_run prüfen)."
        prices = self.copier.last_prices if self.copier else {}
        if arg.lower().strip() == "close":
            n = self.sprint.close(prices)
            return (f"⏹ Sprint-Zyklus manuell beendet ({n} Position(en)) - sofort "
                    "verbucht, kein Strike. Nächster Zyklus wartet auf frisches Signal."
                    if n else "Sprint-Buch hält gerade nichts.")
        s = self.sprint.stats(prices)
        lead = f"<code>{s['leader'][:10]}…</code>" if s.get("leader") else "n/a"
        strikes = ", ".join(f"{a}:{n}" for a, n in s.get("strikes", {}).items()) or "-"
        banned = ", ".join(s.get("banned", [])) or "-"
        # Einzelne Positionen mit Entry + eigenem unrealisiertem PnL (nicht nur
        # 'LONG HYPE' ohne Zahlen). Zyklus = Ritt (v3): keine separate Ritt-PnL
        # mehr nötig, cycle_pnl IST die PnL des laufenden Ritts.
        if s.get("positions"):
            pos_lines = "\n".join(
                f"  {'LONG' if p['size'] > 0 else 'SHORT'} {p['coin']}: "
                f"{abs(p['size']):.4f} @ {p['entry']:.4f} (PnL {p['unrealized_pnl']:+,.2f} $)"
                for p in s["positions"])
            pos_block = f"Positionen:\n{pos_lines}"
        else:
            pos_block = "Positionen: -"
        return (f"<b>Sprint-Buch</b> (Zyklus {s['cycle']}): {s['state']}\n"
                f"{pos_block}\n"
                f"Equity: {s['equity']:,.2f} / Ziel {s['target']:,.0f} "
                f"(Zyklus-PnL {s['cycle_pnl']:+,.2f} $)\n"
                f"Bilanz: {s['won']}✅ {s['busted']}💥 | banked {s['banked']:+,.2f} $\n"
                f"Strikes: {strikes} | 🚫 gesperrt: {banned}\n"
                f"Leader: {lead} | Pool: {len(self.sprint_leaders)} scanbar | "
                f"Trades: {s['trades']} (Ø {s['avg_trades_per_cycle']}/Zyklus)\n"
                f"<i>/sprint close = Ritt manuell schließen</i>")

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
        """Setzt den HyperTracker/CoinMarketMan-API-Token (COINMARKETMAN_TOKEN) in
        die .env UND live in den Prozess - vom Handy, ohne SSH. NUR für diesen
        read-only Free-Tier-Token, NICHT für Wallet-Keys. Chat-ID-geschützt."""
        tok = arg.strip()
        if not tok:
            return "Nutzung: <code>/setcmm &lt;token&gt;</code> (JWT von coinmarketman.com)"
        if tok.count(".") != 2 or len(tok) < 40:
            return "Das sieht nicht nach einem JWT aus (drei punkt-getrennte Teile erwartet)."
        from .sources.coinmarketman import TOKEN_ENV

        set_env_var(TOKEN_ENV, tok)
        os.environ[TOKEN_ENV] = tok   # sofort live, kein Neustart nötig
        return (f"✅ Token gespeichert (…{tok[-6:]}) und live geladen.\n"
                f"Jetzt <code>/cmm</code> für die Live-Probe der API.")

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
            f"🚀 <b>Autopilot gestartet</b>\n"
            f"Modus: {'DRY-RUN' if self.cfg.dry_run else 'LIVE'} auf "
            f"{'Testnet' if self.cfg.is_testnet else 'Mainnet'}\n"
            f"Leader: {len(self.leaders)} | max {self.cfg.risk.max_leverage}x"
        )
        while not self._stop.is_set():
            try:
                self._maybe_reanalyze()
                if self.copier and self.leaders:
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
                if self.sprint and self.copier and self.copier.last_snapshots:
                    from .news.guard import RiskLevel

                    off = bool(self.guard and self.guard.last_level == RiskLevel.RISK_OFF)
                    self.sprint.tick(self.sprint_leaders, self.copier.last_snapshots,
                                     self.copier.last_prices, risk_off=off)
                if self.lighter and self.copier:
                    self.lighter.tick(self.copier.last_prices)
                self._maybe_digest()
                self._maybe_watchdog()
                self._publish()
            except Exception:
                log.exception("Autopilot-Tick fehlgeschlagen")
            # Ereignisgesteuert: Leader-Fill weckt sofort, sonst normales Intervall
            if self.feed and self.feed.connected:
                if self.feed.wait(self.cfg.copytrade.poll_seconds):
                    log.info("Leader-Fill per WebSocket - Tick sofort (Copy-Lag minimiert)")
                if self._stop.is_set():
                    break
            else:
                self._stop.wait(self.cfg.copytrade.poll_seconds)
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
                                     notifier=self.notifier, journal=self.journal)
            log.info("Sprint-Buch aktiv: %.0f$ x%.0f auf den besten Leader, Ziel +%.0f$/Zyklus",
                     self.cfg.sprint.equity, self.cfg.sprint.leverage,
                     self.cfg.sprint.target_profit)
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
        return addrs

    def _build_sprint_pool(self, sprint_ok) -> list[dict]:
        """Top-pool_size nach RICHTUNGS-Score (sprint_score) aus den Sprint-
        tauglichen Wallets; Haupt-Leader immer enthalten (sie sind bewiesen gut
        genug fürs Hauptbuch - Sprint darf sie nie aus den Augen verlieren)."""
        n = max(self.cfg.sprint.pool_size, len(self.leaders))
        top = sorted(sprint_ok, key=lambda m: m.sprint_score, reverse=True)[:n]
        pool = [{"address": m.address, "score": round(m.sprint_score, 1)} for m in top]
        have = {p["address"] for p in pool}
        for l in self.leaders:
            if l["address"] not in have:
                pool.append({"address": l["address"], "score": l.get("score", 0)})
        return pool

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
        if forced or time.time() - self._last_analysis >= hours * 3600:
            self._force_analysis = False   # /analyze: einmalig, im Loop-Thread (kein Race)
            self._reanalyze()
            if forced:
                # Explizit angefordert -> Ergebnis aktiv pushen statt den Nutzer
                # /status pollen zu lassen (Analyse dauert Minuten, mobil nervig)
                self.notifier.send("🔬 <b>Analyse fertig</b>\n"
                                   + (self._analysis_note or "keine Diagnose (Logs prüfen)"))

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
        for a in cmm_addrs + hl_addrs:          # CMM zuerst (PnL-Rangfolge)
            k = a.lower()
            if k not in seen:
                seen.add(k)
                union.append(a)
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
        sprint_gate_ko = sprint_score_ko = 0
        if self.cfg.sprint.enabled:
            from .copytrade.larp import check_sprint

            larp_cfg = LarpConfig(**(an.larp or {}))
            pool_min = self.cfg.sprint.pool_min_score
            for m in report.get("metrics", []):
                if not check_sprint(m, larp_cfg).passed:
                    sprint_gate_ko += 1
                elif m.sprint_score < pool_min:
                    sprint_score_ko += 1
                else:
                    sprint_ok.append(m)
            sprint_ok.sort(key=lambda m: m.sprint_score, reverse=True)

        top_scores = "/".join(f"{s:.0f}" for _, s in report.get("scores", [])[:3]) or "-"
        larp_top = ", ".join(f"{k}×{n}" for k, n in sorted(
            report.get("larp_reasons", {}).items(), key=lambda t: -t[1])[:2]) or "-"
        self._analysis_note = (
            f"{len(addresses)} Kandidaten ({src_note}) → {len(main_ranked)} Haupt"
            f"(≥{an.min_score:g}) / {len(sprint_ok)} Sprint-tauglich "
            f"(Gate-K.O. {sprint_gate_ko}, Richtung<{self.cfg.sprint.pool_min_score:g}: "
            f"{sprint_score_ko})\n"
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
        """Einmal täglich: kompakter Lagebericht - damit '+1$ nach 4 Wochen'
        spätestens am Tag 2 auffällt, nicht am Tag 28."""
        if not self.cfg.autopilot.daily_digest:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        equity = self.copier.last_equity if self.copier else None
        if today == self._digest_date:
            if self._digest_equity is None and equity:
                self._digest_equity = equity
            return
        self._digest_date = today
        day_t = int(time.time()) - 86_400
        entries = [e for e in self.journal.tail(2000) if e.get("t", 0) >= day_t]
        orders = sum(1 for e in entries if e["kind"] == "order")
        vetoes = sum(1 for e in entries if e["kind"] == "veto")
        scalp_pnl = sum(float(e.get("pnl", 0)) for e in entries if e["kind"] == "scalp_close")
        delta = ""
        bleed = ""
        if equity and self._digest_equity:
            pct = (equity / self._digest_equity - 1) * 100
            delta = f"\nEquity: {equity:,.2f} ({pct:+.2f}% 24h)"
        # Kern-Edge: realisierter PnL des Copy-Buchs (nicht der Buchgewinn).
        if self.copier and self.copier.paper:
            rp = self.copier.paper.realized_pnl
            bleed = f"\nCopy realisiert: {rp:+,.2f}"
            if rp < -0.02 * self.cfg.backtest.initial_equity:
                bleed += " ⚠️ blutet"
        # Edge-Status je Strategie-Spur (das eigentliche Ziel des Digests)
        tracks = ""
        if self.labs and self.copier and self.copier.last_prices:
            for name, st in self.labs.stats(self.copier.last_prices).items():
                tracks += f"\n  {name}: {st['realized_pnl']:+,.2f} ({st['trades']} Tr.)"
        if self.sprint:
            sp = self.sprint.stats(self.copier.last_prices if self.copier else {})
            tracks += (f"\n  sprint: Zyklus {sp['cycle']} {sp['state']} "
                       f"({sp['cycle_pnl']:+,.2f}$), banked {sp['banked']:+,.2f}$ "
                       f"[{sp['won']}✅/{sp['busted']}💥]")
        if self.lighter and self.copier and self.copier.last_prices:
            li = self.lighter.stats(self.copier.last_prices)
            tracks += (f"\n  lighter-schatten: {li['realized_pnl']:+,.2f} "
                       f"({li['trades']} Tr., {li['return_pct']:+.2f}%)")
        msg = (f"📊 <b>Tagesbericht</b>{delta}{bleed}\n"
               f"Orders: {orders} | Vetos: {vetoes}"
               + (f" | Scalp-PnL: {scalp_pnl:+,.2f}" if scalp_pnl else "")
               + (f"\n<b>Spuren:</b>{tracks}" if tracks else "")
               + f"\nRisiko: {self.guard.last_level.name if self.guard else 'NORMAL'}"
               + "\nAuswertung: python report.py")
        log.info("Tagesbericht: %d Orders, %d Vetos", orders, vetoes)
        self.notifier.send(msg)
        self._digest_equity = equity

    def _maybe_watchdog(self) -> None:
        """Meldet sich von selbst, wenn der Bot auffällig lange nichts handelt."""
        hours = self.cfg.autopilot.watchdog_hours
        if hours <= 0:
            return
        entries = self.journal.tail(500)
        last_order = next((e["t"] for e in entries if e["kind"] in ("order", "scalp_open")), None)
        ref = last_order or self._start_time
        if last_order and self._watchdog_warned:
            if time.time() - last_order < hours * 3600:
                self._watchdog_warned = False  # wieder aktiv -> Alarm scharf stellen
        if time.time() - ref < hours * 3600 or self._watchdog_warned:
            return
        self._watchdog_warned = True
        diagnosis = diagnose_inactivity(entries, since_t=ref, copier=self.copier)
        log.warning("Watchdog: keine Order seit %.0fh - %s", hours, diagnosis)
        self.notifier.send(f"⏰ <b>Watchdog</b>: keine Order seit {hours:.0f}h.\n{diagnosis}")

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
