"""Paper-Broker: macht aus dem Dry-Run einen echten Paper-Trading-Modus.

Vorher hielt der Dry-Run Positionen nur im RAM (nach Neustart weg) und die
Equity blieb statisch - damit lässt sich nichts validieren. Der Paper-Broker
simuliert das Konto vollständig:

  - Fills zum Mid-Preis inkl. Taker-Fee (wie im Backtest)
  - Positions-Tracking mit Durchschnitts-Entry
  - realisierter PnL beim Reduzieren/Drehen, unrealisierter mark-to-market
  - Equity = Startkapital + realisiert + unrealisiert (Fees sind abgezogen)
  - State persistiert nach runtime/paper_state.json - übersteht Neustarts

Damit laufen Equity-Kurve, Circuit Breaker und Leader-Tracking im Paper-Modus
genauso wie live - der Testlauf prüft das echte Verhalten.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


class PaperBroker:
    def __init__(self, initial_equity: float, fee_rate: float, path: Path | None = None):
        self.initial_equity = initial_equity
        self.fee_rate = fee_rate
        self.path = path or RUNTIME / "paper_state.json"
        self.positions: dict[str, dict] = {}   # coin -> {"size": signiert, "entry": Schnitt}
        self.realized_pnl = 0.0                # inkl. Fees
        self.fees_paid = 0.0
        self.trades = 0
        self._load()

    # ---------- Ausführung ----------

    def execute(self, coin: str, delta_size: float, price: float,
                fee_rate: float | None = None) -> None:
        """Simulierter Fill: delta_size signiert, zum Mid-Preis + Fee.
        fee_rate-Override erlaubt Maker-Fees (Maker-first-Simulation)."""
        if delta_size == 0 or price <= 0:
            return
        fee = abs(delta_size) * price * (self.fee_rate if fee_rate is None else fee_rate)
        self.fees_paid += fee
        self.realized_pnl -= fee
        self.trades += 1

        pos = self.positions.get(coin, {"size": 0.0, "entry": 0.0})
        old_size, entry = pos["size"], pos["entry"]
        new_size = old_size + delta_size

        if old_size == 0 or (old_size > 0) == (delta_size > 0):
            # Eröffnen oder Aufstocken: Durchschnitts-Entry fortschreiben
            total = abs(old_size) + abs(delta_size)
            pos["entry"] = (entry * abs(old_size) + price * abs(delta_size)) / total
            pos["size"] = new_size
        else:
            # Reduzieren, Schließen oder Drehen: realisierten PnL buchen
            closed = min(abs(old_size), abs(delta_size))
            direction = 1 if old_size > 0 else -1
            self.realized_pnl += direction * closed * (price - entry)
            if abs(new_size) < 1e-12:
                self.positions.pop(coin, None)
                self._save()
                return
            if (new_size > 0) != (old_size > 0):
                pos["entry"] = price  # gedreht: Rest ist neue Position zum Fill-Preis
            pos["size"] = new_size

        if abs(pos["size"]) < 1e-12:
            self.positions.pop(coin, None)
        else:
            self.positions[coin] = pos
        self._save()

    def credit(self, amount: float) -> None:
        """Bucht einen Cash-Flow ohne Trade (z.B. eingesammeltes/gezahltes Funding)."""
        if amount:
            self.realized_pnl += amount
            self._save()

    def flatten(self, prices: dict[str, float]) -> None:
        for coin in list(self.positions):
            price = prices.get(coin)
            if price:
                self.execute(coin, -self.positions[coin]["size"], price)

    # ---------- Bewertung ----------

    def equity(self, prices: dict[str, float]) -> float:
        return self.initial_equity + self.realized_pnl + self.unrealized(prices)

    def unrealized(self, prices: dict[str, float]) -> float:
        out = 0.0
        for coin, pos in self.positions.items():
            price = prices.get(coin)
            if price:
                direction = 1 if pos["size"] > 0 else -1
                out += direction * abs(pos["size"]) * (price - pos["entry"])
        return out

    def sizes(self) -> dict[str, float]:
        return {c: p["size"] for c, p in self.positions.items()}

    def position_rows(self, prices: dict[str, float]) -> list[dict]:
        """Positions-Ansicht fürs Dashboard, gleiche Form wie live."""
        rows = []
        for coin, pos in self.positions.items():
            price = prices.get(coin, pos["entry"])
            direction = 1 if pos["size"] > 0 else -1
            rows.append({
                "coin": coin, "size": pos["size"], "entry": round(pos["entry"], 4),
                "unrealized_pnl": round(direction * abs(pos["size"]) * (price - pos["entry"]), 2),
            })
        return rows

    # ---------- Persistenz ----------

    def reset(self) -> None:
        self.positions, self.realized_pnl, self.fees_paid, self.trades = {}, 0.0, 0.0, 0
        self.path.unlink(missing_ok=True)
        log.info("Paper-Konto zurückgesetzt (Start-Equity %.2f)", self.initial_equity)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(exist_ok=True)
            self.path.write_text(json.dumps({
                "updated": int(time.time()), "initial_equity": self.initial_equity,
                "positions": self.positions, "realized_pnl": round(self.realized_pnl, 6),
                "fees_paid": round(self.fees_paid, 6), "trades": self.trades,
            }, indent=2))
        except OSError:
            log.exception("Paper-State nicht speicherbar")

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.positions = data.get("positions", {})
            self.realized_pnl = float(data.get("realized_pnl", 0))
            self.fees_paid = float(data.get("fees_paid", 0))
            self.trades = int(data.get("trades", 0))
            if self.positions or self.trades:
                log.info("Paper-Konto geladen: %d Trades, PnL %.2f, %d offene Positionen",
                         self.trades, self.realized_pnl, len(self.positions))
        except (OSError, ValueError):
            pass
