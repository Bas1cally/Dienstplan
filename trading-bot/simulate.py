#!/usr/bin/env python3
"""End-to-End-Generalprobe: kompletter Autopilot-Stack gegen simulierten Markt.

Läuft OHNE Netzwerk - ein FakeClient liefert synthetische Preise, Candles und
Leader-Wallets. Durchgespielt wird ein ganzer "Tag" im Zeitraffer:

  Phase 1  ruhiger Aufwärtsmarkt, Leader eröffnen BTC long / ETH short
  Phase 2  Leader stocken auf, Rebalancing + Validator greifen
  Phase 3  Flash-Crash -12% in Minuten -> Schock-Detektor muss RISK_OFF
           auslösen und das Paper-Portfolio glattstellen
  Phase 4  Erholung, Cooldown läuft ab, Leader steigen wieder ein

Geprüft wird das ZUSAMMENSPIEL (Tracker -> Targets -> Validator -> Paper-
Broker -> Journal -> Guard), nicht die Einzelteile - dafür sind die Unit-Tests.

  python simulate.py            # Generalprobe laufen lassen
"""

import json
import logging
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from bot.config import load_config
from bot.copytrade.copier import CopyTrader
from bot.copytrade.tracker import LeaderTracker
from bot.journal import Journal
from bot.news.guard import MarketGuard
from bot.paper import PaperBroker
from bot.validator import TradeValidator

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("simulate")

SIM_DIR = Path(__file__).parent / "runtime" / "sim"
HISTORY = 4500   # Minuten Vorgeschichte (Validator braucht 60+ Stunden-Candles)
MINUTES = 720    # simulierte Handels-Minuten
CRASH_AT = 400   # relativ zum Sim-Start
RECOVER_AT = 460


def build_market() -> dict[str, np.ndarray]:
    """Minuten-Preise: lange Aufwärts-Vorgeschichte, Flash-Crash, Erholung."""
    rng = np.random.default_rng(42)
    total = HISTORY + MINUTES
    out = {}
    for coin, start in (("BTC", 100_000.0), ("ETH", 3_500.0)):
        # Realistisches Verhältnis Drift/Rauschen: zu wenig Rauschen lässt den
        # RSI auf 15m ins Extrem laufen und das Überkauft-Veto dauerhaft greifen
        rets = rng.normal(0.0, 0.0012, total)
        rets[HISTORY : HISTORY + CRASH_AT] += 0.00015   # moderater Bull ab Sim-Start
        c = HISTORY + CRASH_AT
        rets[c : c + 12] = -0.011                            # Phase 3: -12% in 12 Minuten
        r = HISTORY + RECOVER_AT
        rets[r : r + 60] += 0.0012                           # Phase 4: Erholung
        out[coin] = start * np.exp(np.cumsum(rets))
    return out


class FakeClient:
    """Ahmt die vom Stack genutzte HyperliquidClient-Oberfläche nach."""

    def __init__(self, market: dict[str, np.ndarray]):
        self.market = market
        self.minute = HISTORY
        self.account_address = None
        self.info = self  # copier nutzt client.info.all_mids()

    @property
    def rel_minute(self) -> int:
        return self.minute - HISTORY

    def all_mids(self):
        return {c: str(p[self.minute]) for c, p in self.market.items()}

    def candles(self, coin: str, interval: str, lookback: int) -> pd.DataFrame:
        step = {"1m": 1, "15m": 15, "1h": 60}[interval]
        series = self.market[coin][: self.minute + 1]
        closes = series[::-1][::step][::-1][-lookback:]  # letzte `lookback` Buckets
        if len(closes) < 2:
            closes = series[-2:]
        o = np.roll(closes, 1); o[0] = closes[0]
        return pd.DataFrame({
            "time": np.arange(len(closes)) * step * 60_000,
            "open": o, "high": np.maximum(o, closes) * 1.0005,
            "low": np.minimum(o, closes) * 0.9995, "close": closes,
            "volume": np.full(len(closes), 100.0),
        })


class FakeLeaders:
    """Zwei Leader-Wallets, deren Positionen sich über den Tag verändern."""

    def __init__(self, client: FakeClient):
        self.client = client

    def state(self, rel_minute: int) -> dict[str, dict]:
        minute = rel_minute  # Phasen-Logik ist sim-relativ
        px = {c: p[self.client.minute] for c, p in self.client.market.items()}
        a, b = {}, {}
        if minute >= 70:                       # Phase 1: Einstiege
            a["BTC"] = 8.0                     # Leader A: 80% long BTC (Equity 1M)
            b["ETH"] = -120.0                  # Leader B: short ETH
        if minute >= 200:                      # Phase 2: A stockt auf
            a["BTC"] = 12.0
        if CRASH_AT <= minute < RECOVER_AT + 30:   # Leader fliehen im Crash
            a.pop("BTC", None)
        if minute >= RECOVER_AT + 60:          # Phase 4: Wiedereinstieg
            a["BTC"] = 6.0
        def mk(positions, equity):
            return {
                "marginSummary": {"accountValue": str(equity)},
                "assetPositions": [{
                    "position": {"coin": c, "szi": str(s), "entryPx": str(px[c]),
                                 "positionValue": str(abs(s) * px[c]),
                                 "leverage": {"value": 2}, "unrealizedPnl": "0"}
                } for c, s in positions.items()],
            }
        return {"0xleader_a": mk(a, 1_000_000), "0xleader_b": mk(b, 500_000)}

    def user_state(self, address: str) -> dict:
        return self.state(self.client.rel_minute)[address]


def main() -> None:
    shutil.rmtree(SIM_DIR, ignore_errors=True)
    SIM_DIR.mkdir(parents=True)

    cfg = load_config()
    assert cfg.dry_run, "Generalprobe nur im dry_run-Modus"
    cfg.news.enabled = False          # kein Netz: News aus, Schock-Detektor an
    cfg.validation.llm_enabled = False
    cfg.validation.cache_seconds = 0
    # Im Normalbetrieb steht copytrade.feed_only=true (Quest-Bot: Kopier-Buch
    # handelt nicht mehr selbst). Die Generalprobe prüft aber genau diese
    # Kopier-/Reconciliation-/RISK_OFF-Maschinerie - die es als Code weiter gibt -
    # also hier explizit einschalten, sonst hätte der Copier nichts zu tun.
    cfg.copytrade.feed_only = False

    market = build_market()
    client = FakeClient(market)
    leaders = FakeLeaders(client)

    tracker = LeaderTracker(leaders, ["0xleader_a", "0xleader_b"])
    guard = MarketGuard(cfg.news, cfg.shock, client=client, coin="BTC")
    guard.shock._clock = lambda: client.minute * 60.0  # Sim-Uhr statt Wanduhr
    validator = TradeValidator(cfg.validation, client)
    copier = CopyTrader(cfg, client, tracker,
                        {"0xleader_a": 0.6, "0xleader_b": 0.4},
                        guard=guard, convergence=None, validator=validator)
    copier.paper = PaperBroker(cfg.backtest.initial_equity, cfg.backtest.fee_rate,
                               path=SIM_DIR / "paper.json")
    copier.journal = Journal(path=SIM_DIR / "trades.jsonl")

    equity_curve, risk_levels = [], []
    flatten_minute = None
    t0 = time.time()
    for rel in range(MINUTES):
        client.minute = HISTORY + rel
        copier.tick()
        eq = copier.last_equity or cfg.backtest.initial_equity
        equity_curve.append(eq)
        level = guard.last_level.name
        risk_levels.append(level)
        if level == "RISK_OFF" and flatten_minute is None:
            flatten_minute = rel

    journal = copier.journal.tail(10_000)
    orders = [e for e in journal if e["kind"] == "order"]
    vetoes = [e for e in journal if e["kind"] == "veto"]
    flattens = [e for e in journal if e["kind"] == "flatten"]

    print(f"\n=== Generalprobe: {MINUTES} simulierte Minuten in {time.time() - t0:.1f}s ===\n")
    print(f"  Paper-Trades ausgeführt      {len(orders)}")
    print(f"  Validator-Vetos              {len(vetoes)}")
    print(f"  Risk-Off-Glattstellungen     {len(flattens)}")
    print(f"  Equity Start -> Ende         {equity_curve[0]:.2f} -> {equity_curve[-1]:.2f}")
    print(f"  Fees bezahlt                 {copier.paper.fees_paid:.2f}")
    print(f"  Offene Positionen am Ende    {copier.paper.sizes() or '–'}")

    # --- Härte-Checks: das Zusammenspiel muss stimmen ---
    checks = [
        ("Orders wurden ausgeführt", len(orders) >= 3),
        ("Schock-Detektor hat den Crash erkannt",
         flatten_minute is not None and CRASH_AT <= flatten_minute <= CRASH_AT + 15),
        ("RISK_OFF hat glattgestellt", len(flattens) >= 1),
        ("Cooldown hielt den Bot aus dem Markt",
         risk_levels.count("RISK_OFF") >= cfg.shock.cooldown_minutes - 5),
        # Wiedereinstieg: im chronologischen Journal muss NACH der Glattstellung
        # wieder mindestens eine Order kommen
        ("Wiedereinstieg nach RISK_OFF", (lambda chrono: (
            (fl := next((i for i, e in enumerate(chrono) if e["kind"] == "flatten"), None)) is not None
            and any(e["kind"] == "order" for e in chrono[fl + 1:])
        ))(list(reversed(journal)))),
        ("Equity-Drawdown blieb über dem Tageslimit",
         min(equity_curve) > cfg.backtest.initial_equity * (1 - cfg.risk.max_daily_loss)
         or copier.halted),
        ("Paper-State persistiert", (SIM_DIR / "paper.json").exists()),
        ("Journal lückenlos", len(journal) == len(orders) + len(vetoes) + len(flattens)),
    ]

    print()
    failed = 0
    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
        failed += 0 if ok else 1

    print(f"\n  Risiko-Level-Verlauf: NORMAL {risk_levels.count('NORMAL')}min, "
          f"RISK_OFF {risk_levels.count('RISK_OFF')}min (Crash ab Minute {CRASH_AT})")
    if failed:
        print(f"\n❌ {failed} Check(s) fehlgeschlagen.\n")
        sys.exit(1)
    print("\n✅ Generalprobe bestanden - der Stack arbeitet als Ganzes korrekt.\n")


if __name__ == "__main__":
    main()
