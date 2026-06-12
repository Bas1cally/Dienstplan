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
        self.leaders: list[dict] = []
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
            "/anomalies": self._cmd_anomalies,
            "/stop": self._cmd_stop,
            "/start": self._cmd_start,
            "/help": self._cmd_help,
        })

    # ---------- Telegram-Befehle (Antworten als HTML-String) ----------

    def _cmd_help(self) -> str:
        return ("<b>Befehle</b>\n/status – Zustand & Equity\n/report – Auswertung\n"
                "/leaders – Leader + ROI\n/anomalies – Scout-Funde\n"
                "/stop /start – Autopilot steuern")

    def _cmd_status(self) -> str:
        s = self.status()
        eq = self.copier.last_equity if self.copier else None
        day_t = int(time.time()) - 86_400
        orders = sum(1 for e in self.journal.tail(2000)
                     if e.get("kind") == "order" and e.get("t", 0) >= day_t)
        return (f"<b>Status</b>: {s.get('state', '?')}\n"
                f"Equity: {f'{eq:,.2f}' if eq else 'n/a'}\n"
                f"Orders (24h): {orders}\n"
                f"Risiko: {self.guard.last_level.name if self.guard else 'NORMAL'}\n"
                f"Leader: {len(self.leaders)} | WS: {'an' if self.feed and self.feed.connected else 'aus'}")

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
        return "\n".join(lines)

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
        # Leader-Positionen über ALLE DEXs verfolgen - sie handeln auch TSLA/Gold/Öl
        tracker = LeaderTracker(leader_info, [l["address"] for l in self.leaders],
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

    def _load_or_analyze_leaders(self) -> None:
        path = Path(self.cfg.copytrade.leaders_file)
        if path.exists():
            self.leaders = json.loads(path.read_text())[: self.cfg.copytrade.max_leaders]
            self._last_analysis = path.stat().st_mtime
            log.info("Leaders aus %s geladen (%d)", path, len(self.leaders))
        else:
            self._reanalyze()

    def _maybe_reanalyze(self) -> None:
        hours = self.cfg.autopilot.reanalyze_hours
        if time.time() - self._last_analysis >= hours * 3600:
            self._reanalyze()

    def _reanalyze(self) -> None:
        log.info("Starte Leaderboard-Analyse (Funnel + LARP-Filter) ...")
        an = self.cfg.copytrade.analysis
        try:
            candidates = fetch_candidates(
                min_account_value=an.min_account_value, min_volume=an.min_volume,
                top_n=an.top_n, top_percent=an.top_percent,
            )
            info = Info(api_url(testnet=False), skip_ws=True)
            analyzer = TraderAnalyzer(info, days=an.days)
            larp = LarpFilter(LarpConfig(**(an.larp or {})))
            ranked = analyzer.rank([c.address for c in candidates], min_score=an.min_score, larp=larp)
        except Exception:
            log.exception("Analyse fehlgeschlagen - behalte bisherige Leader")
            self._last_analysis = time.time()
            return

        new_leaders = rotate_leaders(
            self.leaders, ranked, self.cfg.copytrade.max_leaders, self.cfg.autopilot.min_keep_score,
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
                self.copier.tracker.addresses = [l["address"] for l in new_leaders]
            if self.feed:
                self.feed.resubscribe([l["address"] for l in new_leaders])
        self._last_analysis = time.time()

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
        if equity and self._digest_equity:
            pct = (equity / self._digest_equity - 1) * 100
            delta = f"\nEquity: {equity:,.2f} ({pct:+.2f}% 24h)"
        msg = (f"📊 <b>Tagesbericht</b>{delta}\n"
               f"Orders: {orders} | Vetos: {vetoes}"
               + (f" | Scalp-PnL: {scalp_pnl:+,.2f}" if scalp_pnl else "")
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
