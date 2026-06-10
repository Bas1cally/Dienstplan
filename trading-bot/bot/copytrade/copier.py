"""Copy-Engine: leitet aus den Leader-Snapshots ein Ziel-Portfolio ab und
gleicht das eigene Konto dagegen ab (Reconciliation statt Event-Kopie).

Ablauf pro Tick:
  1. Snapshots aller Leader holen
  2. Ziel-Exposure je Coin berechnen:
       target = Σ_leader  exposure_leader(coin) * gewicht_leader * copy_ratio
     (exposure = signierter Positionswert / Leader-Equity)
  3. Risiko-Caps anwenden: max. Allokation pro Coin, Gesamt-Leverage-Deckel
  4. Differenz zum Ist-Bestand bestimmen; nur handeln, wenn die Abweichung
     den Rebalance-Schwellwert übersteigt (verhindert Fee-Fressen durch Churn)

Eigene Sicherungen bleiben aktiv: Tagesverlust-Circuit-Breaker, dry_run.
"""

import logging
from dataclasses import dataclass

from ..config import CopytradeConfig, RiskConfig
from ..journal import Journal
from ..paper import PaperBroker
from ..risk import RiskManager
from .tracker import LeaderSnapshot

log = logging.getLogger(__name__)


@dataclass
class RebalanceOrder:
    coin: str
    delta_size: float        # zu handelnde Größe in Coin (signiert)
    target_notional: float   # gewünschter signierter Notional in USD
    current_notional: float
    price: float

    @property
    def is_buy(self) -> bool:
        return self.delta_size > 0


def compute_targets(
    snapshots: list[LeaderSnapshot],
    weights: dict[str, float],
    equity: float,
    cfg: CopytradeConfig,
    risk: RiskConfig,
    convergence=None,
) -> dict[str, float]:
    """Ziel-Notional (signiert, USD) je Coin für UNSER Konto."""
    targets: dict[str, float] = {}
    for snap in snapshots:
        w = weights.get(snap.address, 0.0)
        if w <= 0:
            continue
        for coin in snap.positions:
            exposure = snap.exposure(coin)  # z.B. +0.8 = 80% der Leader-Equity long
            targets[coin] = targets.get(coin, 0.0) + exposure * w * cfg.copy_ratio * equity

    # Multi-Exchange-Konvergenz: Boost bei Übereinstimmung, Dämpfung bei Widerspruch.
    # VOR den Caps, damit Coin-Limit und Leverage-Deckel auch den Boost begrenzen.
    if convergence is not None:
        for coin, notional in list(targets.items()):
            v = convergence.vote(coin, 1.0 if notional > 0 else -1.0)
            if v.factor != 1.0:
                log.info("Konvergenz %s: Faktor %.2f (extern %+.2f, %d Quellen)",
                         coin, v.factor, v.external or 0.0, v.sources)
                targets[coin] = notional * v.factor

    # Cap je Coin
    max_coin = cfg.max_alloc_per_coin * equity
    for coin, notional in targets.items():
        if abs(notional) > max_coin:
            targets[coin] = max_coin if notional > 0 else -max_coin

    # Gesamt-Leverage-Deckel: proportional herunterskalieren
    gross = sum(abs(v) for v in targets.values())
    max_gross = risk.max_leverage * equity
    if gross > max_gross and gross > 0:
        scale = max_gross / gross
        targets = {c: v * scale for c, v in targets.items()}
        log.warning("Ziel-Portfolio überschreitet %sx Leverage - skaliere mit %.2f", risk.max_leverage, scale)

    return targets


def exempt_inventory(current: dict[str, float], exempt: dict[str, float]) -> dict[str, float]:
    """Zieht fremden Bestand (z.B. Scalper-Position) vom Konto-Buch ab.

    Ohne diese Ausnahme würde die Reconciliation jede Scalp-Position sofort
    als Abweichung vom Leader-Ziel "wegrebalancen".
    """
    book = dict(current)
    for coin, size in exempt.items():
        book[coin] = book.get(coin, 0.0) - size
        if abs(book[coin]) < 1e-12:
            book.pop(coin, None)
    return book


def plan_rebalance(
    targets: dict[str, float],
    current: dict[str, float],     # coin -> signierte Größe in Coin
    prices: dict[str, float],
    equity: float,
    cfg: CopytradeConfig,
) -> list[RebalanceOrder]:
    """Vergleicht Ziel und Ist, liefert nötige Orders."""
    orders: list[RebalanceOrder] = []
    threshold = max(cfg.min_notional, cfg.rebalance_threshold * equity)

    for coin in sorted(set(targets) | set(current)):
        price = prices.get(coin)
        if not price or price <= 0:
            continue
        target_notional = targets.get(coin, 0.0)
        current_notional = current.get(coin, 0.0) * price
        diff = target_notional - current_notional
        # Komplettes Schließen immer erlauben, sonst Schwellwert anwenden
        closing = target_notional == 0.0 and current_notional != 0.0
        if abs(diff) < threshold and not closing:
            continue
        if abs(diff) < cfg.min_notional:
            continue
        orders.append(
            RebalanceOrder(
                coin=coin,
                delta_size=diff / price,
                target_notional=target_notional,
                current_notional=current_notional,
                price=price,
            )
        )
    return orders


class CopyTrader:
    def __init__(self, cfg, client, tracker, weights: dict[str, float], guard=None,
                 convergence=None, validator=None, signals=None):
        self.cfg = cfg                      # vollständige Config
        self.ct: CopytradeConfig = cfg.copytrade
        self.client = client
        self.tracker = tracker
        self.weights = weights
        self.guard = guard                  # MarketGuard (optional)
        self.convergence = convergence      # ConvergenceEngine (optional)
        self.validator = validator          # TradeValidator (optional, Bot 2)
        self.signals = signals              # SignalBridge (optional, Prop-Modus)
        self.shadows = None                 # ShadowFleet (optional, Paper-Modus)
        self.start_equity: float | None = None  # für den Max-Drawdown-Halt
        self.last_snapshots: list[LeaderSnapshot] = []  # für Performance-Tracking
        self.last_prices: dict[str, float] = {}
        self.last_equity: float | None = None
        self.scalp_inventory: dict[str, float] = {}  # vom Scalper gehaltener Bestand
        self.risk = RiskManager(cfg.risk)
        self.journal = Journal()
        self.day_start_equity = 0.0
        self.day = ""
        self.halted = False
        # Paper-Broker simuliert das Konto im Dry-Run (persistiert über Neustarts)
        self.paper = PaperBroker(cfg.backtest.initial_equity, cfg.backtest.fee_rate) if cfg.dry_run else None

    def tick(self) -> None:
        if self.halted:
            return
        prices = {c: float(p) for c, p in self.client.all_mids().items()}
        self.last_prices = prices
        equity = self._equity(prices)
        self.last_equity = equity
        self._roll_day(equity)
        if self.start_equity is None:
            self.start_equity = equity
        if self.risk.total_drawdown_exceeded(self.start_equity, equity):
            log.error("MAX-DRAWDOWN-HALT: %.1f%% vom Startkapital verloren. "
                      "Schließe alles, Bot pausiert bis Neustart.",
                      self.cfg.risk.max_total_drawdown * 100)
            self.journal.record("max_drawdown_halt", equity=round(equity, 2),
                                start=round(self.start_equity, 2))
            self._flatten(prices)
            self.halted = True
            return
        if self.risk.daily_loss_exceeded(self.day_start_equity, equity):
            log.error("CIRCUIT BREAKER: Tagesverlust-Limit erreicht. Schließe alles, pausiere.")
            self.journal.record("circuit_breaker", equity=round(equity, 2),
                                day_start=round(self.day_start_equity, 2))
            self._flatten(prices)
            self.halted = True
            return

        # News-/Schock-Lage: RISK_OFF = alles glattstellen, CAUTION = nur reduzieren
        caution = False
        if self.guard:
            from ..news.guard import RiskLevel

            level = self.guard.level()
            if level == RiskLevel.RISK_OFF:
                if self._book():  # nur handeln/loggen, wenn es etwas glattzustellen gibt
                    log.warning("RISK_OFF: stelle Copy-Portfolio glatt")
                    self.journal.record("flatten", reason="risk_off")
                    self._flatten(prices)
                    if self.shadows:
                        self.shadows.flatten(prices)
                return
            caution = level == RiskLevel.CAUTION

        snapshots = self.tracker.snapshot_all()
        if not snapshots:
            return
        self.last_snapshots = snapshots
        targets = compute_targets(snapshots, self.weights, equity, self.ct, self.cfg.risk,
                                  convergence=self.convergence)
        # Shadow-Varianten laufen auf denselben Targets/Preisen mit (A/B-Tuning)
        if self.shadows:
            self.shadows.tick(targets, prices)
            self.shadows.persist_stats(prices)

        orders = plan_rebalance(targets, self._book(), prices, equity, self.ct)
        if caution:
            # Im Vorsichtsmodus nur Orders ausführen, die das Exposure senken
            orders = [o for o in orders if abs(o.target_notional) < abs(o.current_notional)]
        orders = self._validate_orders(orders)

        for o in orders:
            side = "BUY" if o.is_buy else "SELL"
            log.info("REBALANCE %s %s %.5f @ ~%.2f (Ist %.0f -> Ziel %.0f USD)",
                     side, o.coin, abs(o.delta_size), o.price,
                     o.current_notional, o.target_notional)
            if self.cfg.dry_run:
                self.paper.execute(o.coin, o.delta_size, o.price)
                self.journal.record("order", coin=o.coin, side=side, size=round(o.delta_size, 6),
                                    price=o.price, target=round(o.target_notional, 2),
                                    mode="paper")
                if self.signals:
                    self.signals.emit("copy", o.coin, side, o.delta_size, o.price,
                                      reason=f"Ziel {o.target_notional:,.0f} USD")
            else:
                try:
                    self.client.market_open(o.coin, o.is_buy, abs(o.delta_size), self.cfg.risk.slippage)
                    self.journal.record("order", coin=o.coin, side=side, size=round(o.delta_size, 6),
                                        price=o.price, target=round(o.target_notional, 2),
                                        mode="live")
                except Exception:
                    log.exception("Order für %s fehlgeschlagen", o.coin)
                    self.journal.record("order_failed", coin=o.coin, side=side)

    # ---------- Zwei-Bot-Prinzip: Validator prüft jeden Einstieg ----------

    def _validate_orders(self, orders: list[RebalanceOrder]) -> list[RebalanceOrder]:
        """Bot 2 (Validator) muss jeder Exposure-ERHÖHUNG zustimmen.

        Reduzierungen und Schließungen laufen IMMER durch - Risikoabbau
        braucht keine Genehmigung.
        """
        if not self.validator:
            return orders
        out = []
        for o in orders:
            increases = abs(o.target_notional) > abs(o.current_notional)
            if not increases:
                out.append(o)
                continue
            verdict = self.validator.check(o.coin, is_long=o.target_notional > 0)
            if verdict.ok:
                log.info("Validator %s: %s", o.coin, verdict.summary())
                out.append(o)
            else:
                log.info("Validator blockt %s-Einstieg: %s", o.coin, verdict.summary())
                if self.journal:
                    # price mitschreiben: Basis für die Veto-Outcome-Analyse im Report
                    self.journal.record("veto", coin=o.coin,
                                        side="LONG" if o.target_notional > 0 else "SHORT",
                                        target=round(o.target_notional, 2),
                                        price=o.price,
                                        reasons=verdict.reasons[:4])
        return out

    # ---------- Hilfsfunktionen ----------

    def _current_positions(self) -> dict[str, float]:
        if self.cfg.dry_run:
            return self.paper.sizes()
        state = self.client.merged_user_state(self.client.account_address)
        out = {}
        for p in state.get("assetPositions", []):
            pos = p["position"]
            if float(pos["szi"]) != 0:
                out[pos["coin"]] = float(pos["szi"])
        return out

    def _book(self) -> dict[str, float]:
        """Konto-Bestand ohne den Scalper-Anteil - die Basis der Reconciliation."""
        return exempt_inventory(self._current_positions(), self.scalp_inventory)

    def _flatten(self, prices: dict[str, float] | None = None) -> None:
        """Stellt das Copy-Buch glatt - Scalper-Bestand bleibt unberührt."""
        prices = prices or self.last_prices
        for coin, size in self._book().items():
            price = prices.get(coin)
            log.info("Schließe %s (size %.5f)", coin, size)
            if self.cfg.dry_run:
                if price:
                    self.paper.execute(coin, -size, price)
                    if self.signals:
                        self.signals.emit("flatten", coin, "SELL" if size > 0 else "BUY",
                                          size, price, reason="Glattstellung (Risk-Off/Halt)")
            else:
                try:
                    self.client.market_open(coin, size < 0, abs(size), self.cfg.risk.slippage)
                except Exception:
                    log.exception("Schließen von %s fehlgeschlagen", coin)

    def _equity(self, prices: dict[str, float] | None = None) -> float:
        if self.cfg.dry_run:
            return self.paper.equity(prices or self.last_prices)
        return self.client.equity()

    def _roll_day(self, equity: float) -> None:
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
            log.info("Neuer Handelstag %s, Start-Equity %.2f", today, equity)
