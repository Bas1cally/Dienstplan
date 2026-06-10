"""Live-Trading-Loop: pollt Candles, verwaltet genau eine Position pro Coin.

Sicherheitsnetz in dieser Reihenfolge:
1. dry_run        - loggt nur, sendet keine Orders
2. Stop/TP-Check  - jede Poll-Runde gegen den Mid-Preis
3. Circuit Breaker- stoppt den Bot bei Überschreiten des Tagesverlust-Limits
"""

import logging
import time
from datetime import datetime, timezone

from .config import Config
from .exchange import INTERVAL_MS, HyperliquidClient
from .risk import PositionPlan, RiskManager
from .strategy import Signal, TrendStrategy

log = logging.getLogger(__name__)

POLL_SECONDS = 30


class Trader:
    def __init__(self, cfg: Config, client: HyperliquidClient, guard=None):
        self.cfg = cfg
        self.client = client
        self.guard = guard  # MarketGuard (optional): News- + Schock-Überwachung
        self.strategy = TrendStrategy(cfg.strategy)
        self.risk = RiskManager(cfg.risk)
        self.coin = cfg.market.coin
        self.plan: PositionPlan | None = None     # Stop/TP der offenen Position
        self.plan_is_long: bool = True
        self.last_candle_time: int = 0
        self.day: str = ""
        self.day_start_equity: float = 0.0
        self.halted = False

    # ---------- Hauptschleife ----------

    def run_forever(self) -> None:
        mode = "DRY-RUN" if self.cfg.dry_run else "LIVE"
        net = "TESTNET" if self.cfg.is_testnet else "MAINNET"
        log.info("Bot gestartet: %s auf %s | %s %s | max %sx Leverage",
                 mode, net, self.coin, self.cfg.market.interval, self.cfg.risk.max_leverage)
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                log.info("Beendet durch Benutzer.")
                return
            except Exception:
                log.exception("Fehler im Tick - weiter in %ss", POLL_SECONDS)
            time.sleep(POLL_SECONDS)

    def tick(self) -> None:
        if self.halted:
            return
        equity = self._equity()
        self._roll_day(equity)

        if self.risk.daily_loss_exceeded(self.day_start_equity, equity):
            log.error("CIRCUIT BREAKER: Tagesverlust-Limit erreicht (Equity %.2f). Schließe Position, Bot pausiert bis Neustart.", equity)
            self._close_position("circuit_breaker")
            self.halted = True
            return

        self._check_stops()

        # News-/Schock-Lage prüfen: RISK_OFF = raus aus dem Markt, CAUTION = nichts Neues
        risk_level = 0
        if self.guard:
            from .news.guard import RiskLevel

            risk_level = self.guard.level()
            if risk_level == RiskLevel.RISK_OFF:
                if self.plan or self._position():
                    log.warning("RISK_OFF: schließe Position sofort")
                    self._close_position("risk_off")
                return

        df = self.client.candles(self.coin, self.cfg.market.interval, self.cfg.market.lookback_candles)
        if df.empty or int(df.iloc[-1]["time"]) == self.last_candle_time:
            return  # noch keine neue abgeschlossene Candle
        self.last_candle_time = int(df.iloc[-1]["time"])

        analysis = self.strategy.analyze(df)
        log.info("Candle %s | close %.2f | EMA %.2f/%.2f | RSI %.1f | Signal: %s",
                 datetime.fromtimestamp(self.last_candle_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                 analysis.close, analysis.ema_fast, analysis.ema_slow, analysis.rsi, analysis.signal.value)

        self._act(analysis, equity, caution=risk_level >= 1)

    # ---------- Order-Logik ----------

    def _act(self, analysis, equity: float, caution: bool = False) -> None:
        pos = self._position()
        if analysis.signal == Signal.HOLD:
            return

        # Gegensignal oder FLAT: bestehende Position schließen
        if pos:
            pos_long = pos["size"] > 0
            if (analysis.signal == Signal.FLAT
                    or (analysis.signal == Signal.LONG and not pos_long)
                    or (analysis.signal == Signal.SHORT and pos_long)):
                self._close_position("signal_exit")
                pos = None

        if pos is None and analysis.signal in (Signal.LONG, Signal.SHORT):
            if caution:
                log.info("CAUTION (News-Lage): Einstiegssignal %s wird übersprungen", analysis.signal.value)
                return
            is_long = analysis.signal == Signal.LONG
            plan = self.risk.plan_position(equity, analysis.close, analysis.atr, is_long)
            if not plan:
                log.info("Kein Trade: Positionsplan unter Mindestgröße oder ungültig.")
                return
            self._open_position(plan, is_long)

    def _open_position(self, plan: PositionPlan, is_long: bool) -> None:
        side = "LONG" if is_long else "SHORT"
        log.info("ÖFFNE %s %s: size=%.5f (~%.0f USD, %sx) | SL %.2f | TP %.2f | Risiko %.2f USD",
                 side, self.coin, plan.size, plan.notional, plan.leverage,
                 plan.stop_loss, plan.take_profit, plan.risk_usd)
        if not self.cfg.dry_run:
            self.client.set_leverage(self.coin, plan.leverage)
            self.client.market_open(self.coin, is_long, plan.size, self.cfg.risk.slippage)
        self.plan = plan
        self.plan_is_long = is_long

    def _close_position(self, reason: str) -> None:
        if self.cfg.dry_run:
            if self.plan:
                log.info("SCHLIESSE Position (%s) [dry-run]", reason)
        else:
            pos = self.client.position(self.coin)
            if pos:
                log.info("SCHLIESSE Position (%s): size=%.5f entry=%.2f upnl=%.2f",
                         reason, pos["size"], pos["entry"], pos["unrealized_pnl"])
                self.client.market_close(self.coin, self.cfg.risk.slippage)
        self.plan = None

    def _check_stops(self) -> None:
        """Prüft Stop-Loss/Take-Profit gegen den aktuellen Mid-Preis."""
        if not self.plan:
            return
        price = self.client.mid_price(self.coin)
        reason = RiskManager.stop_hit(self.plan_is_long, price, self.plan.stop_loss, self.plan.take_profit)
        if reason:
            log.info("%s ausgelöst bei %.2f", reason.upper(), price)
            self._close_position(reason)

    # ---------- Hilfsfunktionen ----------

    def _equity(self) -> float:
        if self.cfg.dry_run and not self.client.account_address:
            return self.cfg.backtest.initial_equity
        return self.client.equity()

    def _position(self) -> dict | None:
        if self.cfg.dry_run:
            if self.plan:
                d = 1 if self.plan_is_long else -1
                return {"size": d * self.plan.size, "entry": self.plan.entry, "unrealized_pnl": 0.0}
            return None
        return self.client.position(self.coin)

    def _roll_day(self, equity: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
            log.info("Neuer Handelstag %s, Start-Equity %.2f", today, equity)
