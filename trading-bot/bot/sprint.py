"""Sprint-Buch: 1000$ mit 10x Hebel auf den BESTEN Leader, Ziel +100$ je Zyklus.

Mechanik je Zyklus:
  1. Bester Leader = höchster Score der aktuellen Rotation (bestehendes System)
  2. Dessen Positionen werden mit `leverage`-fachem Exposure gespiegelt
     (compute_targets mit copy_ratio = leverage, Gross-Cap = leverage)
  3. Take-Profit: Equity >= equity + target_profit  -> alles glattstellen,
     Gewinn gebucht ("banked"), Konto auf equity zurückgesetzt, nächster Zyklus
     folgt wieder dem dann besten Leader (Rotation läuft normal weiter)
  4. Liquidations-Modell: fällt die Equity unter bust_frac (Default 5%),
     ist der Zyklus geplatzt (auf 10x wäre das reale Konto liquidiert) -
     Verlust gebucht, Konto zurückgesetzt, nächster Zyklus.

RISK_OFF (News/Schock) stellt auch das Sprint-Buch glatt, beendet aber keinen
Zyklus. Läuft nur im Paper-Modus; Fees = Taker (konservativ, kein Maker-Bonus).
"""

import json
import logging
import time

from .config import AnalysisConfig, CopytradeConfig, RiskConfig
from .copytrade.copier import compute_targets, plan_rebalance
from .journal import RUNTIME
from .paper import PaperBroker

log = logging.getLogger(__name__)


class SprintBook:
    def __init__(self, cfg, fee_rate: float, notifier=None, journal=None,
                 runtime_dir=None, clock=time.time):
        runtime = runtime_dir or RUNTIME
        self.cfg = cfg
        self.notifier = notifier
        self.journal = journal
        self.clock = clock
        self.paper = PaperBroker(cfg.equity, fee_rate, path=runtime / "sprint_book.json")
        self.state_path = runtime / "sprint_cycles.json"
        self.banked = 0.0
        self.won = 0
        self.busted = 0
        self.best_address = ""
        self._load_state()
        # Volle Hebel-Kopie EINES Leaders: copy_ratio = Hebel, Gross-Cap = Hebel,
        # Coin-Cap praktisch offen (der Gross-Cap deckelt).
        self._ct = CopytradeConfig(
            analysis=AnalysisConfig(), max_leaders=1,
            copy_ratio=float(cfg.leverage), max_alloc_per_coin=float(cfg.leverage),
            rebalance_threshold=cfg.rebalance_threshold, min_notional=cfg.min_notional,
        )
        self._risk = RiskConfig(risk_per_trade=0.01, atr_stop_mult=2.0, take_profit_r=2.0,
                                max_leverage=int(cfg.leverage), max_daily_loss=1.0,
                                slippage=0.005, max_total_drawdown=0.5)

    # ---------- Tick ----------

    def tick(self, leaders: list[dict], snapshots: list, prices: dict[str, float],
             risk_off: bool = False) -> None:
        if not prices:
            return
        if risk_off:
            if self.paper.sizes():
                log.warning("Sprint: RISK_OFF - stelle glatt (Zyklus läuft weiter)")
                self.paper.flatten(prices)
            return
        if not leaders or not snapshots:
            return
        best = max(leaders, key=lambda l: float(l.get("score", 0)))
        snap = next((s for s in snapshots
                     if s.address.lower() == str(best.get("address", "")).lower()), None)
        if snap is None:
            return
        self.best_address = snap.address

        equity = self.paper.equity(prices)
        targets = compute_targets([snap], {snap.address: 1.0}, equity, self._ct, self._risk)
        for o in plan_rebalance(targets, self.paper.sizes(), prices, equity, self._ct):
            self.paper.execute(o.coin, o.delta_size, o.price)

        eq = self.paper.equity(prices)
        if eq >= self.cfg.equity + self.cfg.target_profit:
            self._end_cycle(prices, won=True)
        elif eq <= self.cfg.equity * self.cfg.bust_frac:
            self._end_cycle(prices, won=False)

    # ---------- Zyklus-Ende ----------

    def _end_cycle(self, prices: dict[str, float], won: bool) -> None:
        self.paper.flatten(prices)
        final = self.paper.equity(prices)
        pnl = final - self.cfg.equity
        cycle = self.won + self.busted + 1
        self.banked += pnl
        if won:
            self.won += 1
        else:
            self.busted += 1
        kind = "sprint_tp" if won else "sprint_bust"
        log.warning("Sprint-Zyklus %d %s: PnL %+.2f (banked gesamt %+.2f)",
                    cycle, "ZIEL ERREICHT" if won else "GEPLATZT", pnl, self.banked)
        if self.journal:
            self.journal.record(kind, cycle=cycle, pnl=round(pnl, 2),
                                banked=round(self.banked, 2), leader=self.best_address)
        if self.notifier:
            if won:
                self.notifier.send(
                    f"🏁 <b>Sprint-Zyklus {cycle}: Ziel erreicht</b>\n"
                    f"PnL {pnl:+,.2f} $ | Bilanz: {self.won}✅ {self.busted}💥 "
                    f"| banked {self.banked:+,.2f} $\nNeuer Zyklus startet."
                )
            else:
                self.notifier.send(
                    f"💥 <b>Sprint-Zyklus {cycle}: geplatzt</b> (10x-Liquidations-Modell)\n"
                    f"PnL {pnl:+,.2f} $ | Bilanz: {self.won}✅ {self.busted}💥 "
                    f"| banked {self.banked:+,.2f} $\nNeuer Zyklus startet."
                )
        self.paper.reset()
        self._save_state()

    # ---------- Status & Persistenz ----------

    def stats(self, prices: dict[str, float]) -> dict:
        eq = self.paper.equity(prices) if prices else self.cfg.equity + self.paper.realized_pnl
        return {
            "equity": round(eq, 2),
            "cycle": self.won + self.busted + 1,
            "target": round(self.cfg.equity + self.cfg.target_profit, 2),
            "progress_pct": round((eq - self.cfg.equity) / self.cfg.target_profit * 100, 1),
            "banked": round(self.banked, 2),
            "won": self.won,
            "busted": self.busted,
            "trades": self.paper.trades,
            "leader": self.best_address,
        }

    def _load_state(self) -> None:
        try:
            d = json.loads(self.state_path.read_text())
            self.banked = float(d.get("banked", 0.0))
            self.won = int(d.get("won", 0))
            self.busted = int(d.get("busted", 0))
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(exist_ok=True)
            self.state_path.write_text(json.dumps({
                "updated": int(self.clock()), "banked": round(self.banked, 2),
                "won": self.won, "busted": self.busted,
            }))
        except OSError:
            log.exception("sprint_cycles.json nicht schreibbar")
