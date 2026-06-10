"""VolScalper: handelt die Gegenbewegung nach Volatilitäts-Schocks (opt-in).

Idee: Nach Liquidations-Kaskaden überschießt der Preis und kehrt oft teilweise
zurück. Unser Schock-Detektor erkennt diese Events ohnehin - der Scalper
nutzt sie offensiv, während der Copy-Bot im Cooldown sicher draußen ist.

Ablauf je Schock-Event (maximal EIN Scalp pro Event):
  1. Schock feuert (z.B. -3% in 5min) -> Scalper ist "armed"
  2. Stabilisierung abwarten: mind. `stabilize_minutes` vergangen UND die
     letzten `confirm_candles` 1m-Candles machen kein neues Extrem mehr
  3. Einstieg GEGEN den Move (Crash -> long, Pump -> short)
     Stop  = Extrem des Moves +/- Puffer (eng!)
     TP    = Retrace-Anteil des Spikes (default 38.2%)
  4. Exit: Stop, TP oder Zeit-Stop nach `max_holding_minutes` - ein Scalp
     wird NIE zur Bauchgefühl-Position

Ehrliche Einordnung: Das ist die schwierigste Strategie im Bot. Fees+Slippage
(~0.1% Round-Trip) fressen dünne Edges, und manchmal fällt das Messer weiter -
deshalb default DEAKTIVIERT, Mini-Risiko (0.5% Equity), und erst nach
überzeugendem Paper-Lauf überhaupt erwägen.
"""

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class ScalpPosition:
    coin: str
    size: float          # signiert
    entry: float
    stop: float
    take_profit: float
    opened_at: float


class VolScalper:
    def __init__(self, cfg, client, shock, paper=None, journal=None,
                 notifier=None, equity_fn=None, clock=time.time, signals=None):
        self.cfg = cfg                  # ScalpConfig
        self.full_risk = None           # wird vom Autopilot gesetzt (RiskConfig)
        self.client = client
        self.shock = shock              # ShockDetector (liefert last_event)
        self.paper = paper              # PaperBroker im Dry-Run, sonst None
        self.journal = journal
        self.notifier = notifier
        self.signals = signals
        self.equity_fn = equity_fn or (lambda: 0.0)
        self._clock = clock
        self.position: ScalpPosition | None = None
        self._consumed_event: float | None = None

    def inventory(self) -> dict[str, float]:
        """Scalp-Bestand, den der Copier von seiner Reconciliation ausnimmt."""
        return {self.position.coin: self.position.size} if self.position else {}

    # ---------- Hauptschleife ----------

    def tick(self) -> None:
        if not self.cfg.enabled:
            return
        try:
            if self.position:
                self._manage()
            else:
                self._maybe_enter()
        except Exception:
            log.exception("Scalper-Tick fehlgeschlagen")

    # ---------- Einstieg ----------

    def _maybe_enter(self) -> None:
        ev = self.shock.last_event
        if not ev or ev["time"] == self._consumed_event:
            return
        now = self._clock()
        elapsed_min = (now - ev["time"]) / 60
        if elapsed_min < self.cfg.stabilize_minutes:
            return
        if elapsed_min > self.cfg.entry_window_minutes:
            self._consumed_event = ev["time"]  # Fenster verpasst - Event abhaken
            return

        coin = self.cfg.coin
        df = self.client.candles(coin, "1m", 60)
        if len(df) < self.cfg.confirm_candles + 5:
            return
        window = max(int(elapsed_min) + 2, self.cfg.confirm_candles + 2)
        recent = df.tail(window)
        is_crash = ev["move"] < 0
        extreme = float(recent["low"].min()) if is_crash else float(recent["high"].max())
        confirm = df.tail(self.cfg.confirm_candles)
        price = float(df["close"].iloc[-1])

        # Stabilisierung: die letzten Candles machen kein neues Extrem mehr
        if is_crash:
            stable = float(confirm["low"].min()) > extreme and price > extreme
        else:
            stable = float(confirm["high"].max()) < extreme and price < extreme
        if not stable:
            return

        direction = 1 if is_crash else -1          # Gegenbewegung handeln
        stop = extreme * (1 - direction * self.cfg.stop_buffer)
        spike = abs(price / extreme - 1) + abs(ev["move"])
        take_profit = price * (1 + direction * abs(ev["move"]) * self.cfg.retrace_target)
        stop_dist = abs(price - stop)
        if stop_dist <= 0 or spike <= 0:
            return

        equity = self.equity_fn() or 0.0
        risk_usd = equity * self.cfg.risk_per_scalp
        size = direction * (risk_usd / stop_dist)
        notional = abs(size) * price
        max_notional = equity * self.cfg.max_notional_frac
        if notional > max_notional:
            size *= max_notional / notional
            notional = max_notional
        if notional < 10:
            self._consumed_event = ev["time"]
            return

        self._execute(coin, size, price)
        self.position = ScalpPosition(coin=coin, size=size, entry=price, stop=stop,
                                      take_profit=take_profit, opened_at=self._clock())
        self._consumed_event = ev["time"]
        side = "LONG" if direction > 0 else "SHORT"
        log.info("SCALP %s %s %.5f @ %.2f | Stop %.2f | TP %.2f | Risiko %.0f USD",
                 side, coin, abs(size), price, stop, take_profit, risk_usd)
        if self.journal:
            self.journal.record("scalp_open", coin=coin, side=side, size=round(size, 6),
                                price=price, stop=round(stop, 2), tp=round(take_profit, 2))
        if self.notifier:
            self.notifier.send(f"⚡ <b>Scalp {side} {coin}</b> @ {price:,.2f}\n"
                               f"Stop {stop:,.2f} | TP {take_profit:,.2f}")
        if self.signals:
            self.signals.emit("scalp", coin, "BUY" if direction > 0 else "SELL",
                              size, price, stop=stop, take_profit=take_profit,
                              reason="Vol-Schock Mean-Reversion")

    # ---------- Verwaltung ----------

    def _manage(self) -> None:
        pos = self.position
        price = float(self.client.all_mids()[pos.coin])
        is_long = pos.size > 0
        reason = None
        if (is_long and price <= pos.stop) or (not is_long and price >= pos.stop):
            reason = "stop"
        elif (is_long and price >= pos.take_profit) or (not is_long and price <= pos.take_profit):
            reason = "take_profit"
        elif (self._clock() - pos.opened_at) / 60 >= self.cfg.max_holding_minutes:
            reason = "time_stop"
        if not reason:
            return

        self._execute(pos.coin, -pos.size, price)
        pnl = pos.size * (price - pos.entry)
        log.info("SCALP EXIT %s (%s): PnL %+.2f USD", pos.coin, reason, pnl)
        if self.journal:
            self.journal.record("scalp_close", coin=pos.coin, reason=reason,
                                price=price, pnl=round(pnl, 2))
        if self.notifier:
            icon = "✅" if pnl >= 0 else "❌"
            self.notifier.send(f"{icon} Scalp {pos.coin} zu ({reason}): {pnl:+,.2f} USD")
        if self.signals:
            self.signals.emit("scalp", pos.coin, "SELL" if pos.size > 0 else "BUY",
                              pos.size, price, reason=f"Scalp-Exit ({reason})")
        self.position = None

    def _execute(self, coin: str, delta_size: float, price: float) -> None:
        if self.paper is not None:
            self.paper.execute(coin, delta_size, price)
        else:
            self.client.market_open(coin, delta_size > 0, abs(delta_size),
                                    self.full_risk.slippage if self.full_risk else 0.005)
