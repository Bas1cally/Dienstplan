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
    def __init__(self, cfg, client, tracker, weights: dict[str, float], guard=None):
        self.cfg = cfg                      # vollständige Config
        self.ct: CopytradeConfig = cfg.copytrade
        self.client = client
        self.tracker = tracker
        self.weights = weights
        self.guard = guard                  # MarketGuard (optional)
        self.risk = RiskManager(cfg.risk)
        self.day_start_equity = 0.0
        self.day = ""
        self.halted = False
        # Eigener Bestand im Dry-Run (Simulation)
        self._paper_positions: dict[str, float] = {}

    def tick(self) -> None:
        if self.halted:
            return
        equity = self._equity()
        self._roll_day(equity)
        if self.risk.daily_loss_exceeded(self.day_start_equity, equity):
            log.error("CIRCUIT BREAKER: Tagesverlust-Limit erreicht. Schließe alles, pausiere.")
            self._flatten()
            self.halted = True
            return

        # News-/Schock-Lage: RISK_OFF = alles glattstellen, CAUTION = nur reduzieren
        caution = False
        if self.guard:
            from ..news.guard import RiskLevel

            level = self.guard.level()
            if level == RiskLevel.RISK_OFF:
                log.warning("RISK_OFF: stelle Copy-Portfolio glatt")
                self._flatten()
                return
            caution = level == RiskLevel.CAUTION

        snapshots = self.tracker.snapshot_all()
        if not snapshots:
            return
        prices = {c: float(p) for c, p in self.client.info.all_mids().items()}
        targets = compute_targets(snapshots, self.weights, equity, self.ct, self.cfg.risk)
        current = self._current_positions()
        orders = plan_rebalance(targets, current, prices, equity, self.ct)
        if caution:
            # Im Vorsichtsmodus nur Orders ausführen, die das Exposure senken
            orders = [o for o in orders if abs(o.target_notional) < abs(o.current_notional)]

        for o in orders:
            side = "BUY" if o.is_buy else "SELL"
            log.info("REBALANCE %s %s %.5f @ ~%.2f (Ist %.0f -> Ziel %.0f USD)",
                     side, o.coin, abs(o.delta_size), o.price,
                     o.current_notional, o.target_notional)
            if self.cfg.dry_run:
                self._paper_positions[o.coin] = self._paper_positions.get(o.coin, 0.0) + o.delta_size
                if abs(self._paper_positions[o.coin]) * o.price < 1:
                    self._paper_positions.pop(o.coin, None)
            else:
                try:
                    self.client.market_open(o.coin, o.is_buy, abs(o.delta_size), self.cfg.risk.slippage)
                except Exception:
                    log.exception("Order für %s fehlgeschlagen", o.coin)

    # ---------- Hilfsfunktionen ----------

    def _current_positions(self) -> dict[str, float]:
        if self.cfg.dry_run:
            return dict(self._paper_positions)
        state = self.client.info.user_state(self.client.account_address)
        out = {}
        for p in state.get("assetPositions", []):
            pos = p["position"]
            if float(pos["szi"]) != 0:
                out[pos["coin"]] = float(pos["szi"])
        return out

    def _flatten(self) -> None:
        for coin, size in self._current_positions().items():
            log.info("Schließe %s (size %.5f)", coin, size)
            if not self.cfg.dry_run:
                try:
                    self.client.market_close(coin, self.cfg.risk.slippage)
                except Exception:
                    log.exception("Schließen von %s fehlgeschlagen", coin)
        self._paper_positions.clear()

    def _equity(self) -> float:
        if self.cfg.dry_run and not self.client.account_address:
            return self.cfg.backtest.initial_equity
        return self.client.equity()

    def _roll_day(self, equity: float) -> None:
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
            log.info("Neuer Handelstag %s, Start-Equity %.2f", today, equity)
