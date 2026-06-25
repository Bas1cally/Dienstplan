"""Strategie-Labor: eigene Signale im Paper-Schatten messen, bevor Kapital fließt.

Das Copy-Trading folgt fremden Tradern - ein Follower hat per Konstruktion kein
originäres Alpha. Hier laufen EIGENE Strategien parallel im Paper-Modus, jede
mit eigenem Konto, damit der Report schwarz auf weiß zeigt, ob das Signal einen
Edge hat. NICHTS davon handelt mit echtem Geld; es ist eine Messung.

  TrendLab    - die "meistgenudelte" Strategie: EMA-Crossover + RSI-Filter,
                ATR-Stop, Take-Profit. Wiederverwendet bot/strategy.py.
                Ehrliche Erwartung: Standard-TA ist nach Fees meist nahe null -
                genau das wollen wir messen, nicht glauben.
  FundingLab  - Funding-Capture: bei extremer Funding-Rate die KASSIERENDE Seite
                halten und das Funding einsammeln. WICHTIG: ohne Spot-Hedge ist
                das NICHT delta-neutrale Arbitrage, sondern funding-getiltetes
                Directional-Trading - der Carry ist der Edge, das Kursrisiko
                bleibt (durch Stop + Zeitlimit begrenzt). Echte Arb bräuchte das
                Spot-Bein, das ist ein größerer Schritt.

Fleet-Muster wie bot/shadow.py: tick() je Loop, stats()/persist_stats() für
den Report (runtime/labs.json).
"""

import logging
import time
from pathlib import Path

from .paper import PaperBroker
from .strategy import Signal, TrendStrategy

log = logging.getLogger(__name__)

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"
FUNDING_HOURLY_TO_APR = 24 * 365


class TrendLab:
    """Eigene EMA/RSI-Trendstrategie auf einem Paper-Konto (ein Markt)."""

    name = "eigene_ta"

    def __init__(self, cfg, paper: PaperBroker, strategy: TrendStrategy):
        self.cfg = cfg                  # TrendLabConfig
        self.paper = paper
        self.strategy = strategy
        self._last_candle_t = 0
        self._stop: dict[str, float] = {}    # coin -> Stop-Preis
        self._tp: dict[str, float] = {}      # coin -> Take-Profit-Preis

    def on_price(self, coin: str, price: float) -> None:
        """Bei jedem Tick: Stop/Take-Profit prüfen (intrabar-Schutz)."""
        size = self.paper.sizes().get(coin, 0.0)
        if size == 0 or price <= 0:
            return
        stop, tp = self._stop.get(coin), self._tp.get(coin)
        hit_stop = stop is not None and ((size > 0 and price <= stop) or (size < 0 and price >= stop))
        hit_tp = tp is not None and ((size > 0 and price >= tp) or (size < 0 and price <= tp))
        if hit_stop or hit_tp:
            self.paper.execute(coin, -size, price)
            self._stop.pop(coin, None)
            self._tp.pop(coin, None)
            log.info("TrendLab %s: %s @ %.4f", coin, "Stop" if hit_stop else "Take-Profit", price)

    def on_candle(self, coin: str, df, equity: float) -> None:
        """Bei neuer abgeschlossener Candle: Signal auswerten und handeln."""
        if len(df) < self.strategy.min_candles:
            return
        candle_t = int(df.iloc[-1]["time"])
        if candle_t <= self._last_candle_t:
            return
        self._last_candle_t = candle_t
        analysis = self.strategy.analyze(df)
        price = analysis.close
        size = self.paper.sizes().get(coin, 0.0)

        want_long = analysis.signal == Signal.LONG
        want_short = analysis.signal == Signal.SHORT
        want_flat = analysis.signal == Signal.FLAT

        # Gegensignal oder Flat: bestehende Position schließen
        if size != 0 and (want_flat or (want_long and size < 0) or (want_short and size > 0)):
            self.paper.execute(coin, -size, price)
            size = 0.0
            self._stop.pop(coin, None)
            self._tp.pop(coin, None)

        if (want_long or want_short) and size == 0 and analysis.atr > 0:
            direction = 1 if want_long else -1
            stop_dist = self.cfg.atr_stop_mult * analysis.atr
            target_size = (self.cfg.risk_frac * equity) / stop_dist
            notional_cap = self.cfg.max_notional_frac * equity
            target_size = min(target_size, notional_cap / price) * direction
            if abs(target_size) * price >= self.cfg.min_notional:
                self.paper.execute(coin, target_size, price)
                self._stop[coin] = price - direction * stop_dist
                self._tp[coin] = price + direction * self.cfg.take_profit_r * stop_dist
                log.info("TrendLab %s: %s @ %.4f (RSI %.0f)",
                         coin, "LONG" if want_long else "SHORT", price, analysis.rsi)

    def equity(self, prices: dict[str, float]) -> float:
        return self.paper.equity(prices)


class FundingLab:
    """Funding-Capture: die kassierende Seite halten, solange Funding extrem ist.

    Kein Arbitrage-Versprechen ohne Spot-Hedge - der Carry ist der Edge, das
    Kursrisiko begrenzen Stop und Zeitlimit. Reine Messung im Paper-Modus.
    """

    name = "funding_capture"

    def __init__(self, cfg, paper: PaperBroker):
        self.cfg = cfg                  # FundingLabConfig
        self.paper = paper
        self._entry: dict[str, dict] = {}    # coin -> {"price", "t", "stop"}

    def on_pulse(self, pulses: list, prices: dict[str, float], equity: float,
                 now: float | None = None) -> None:
        now = now if now is not None else time.time()
        apr_by_coin = {p.coin: p.funding_apr for p in pulses}
        mark_by_coin = {p.coin: p.mark for p in pulses}

        # 1. Bestehende Positionen prüfen: Funding gutschreiben, dann Exit?
        exited: set[str] = set()
        for coin in list(self._entry):
            size = self.paper.sizes().get(coin, 0.0)
            price = prices.get(coin) or mark_by_coin.get(coin)
            if size == 0 or not price:
                self._entry.pop(coin, None)
                continue
            apr = apr_by_coin.get(coin, 0.0)
            ent = self._entry[coin]
            # Funding buchen (der eigentliche Edge!): funding_pnl = -size*price*fh*h.
            # Positiv für die kassierende Seite. Ohne das misst der Lab nur Kursrisiko.
            elapsed_h = max(0.0, (now - ent.get("funding_t", now)) / 3600)
            if elapsed_h > 0:
                funding_hourly = apr / FUNDING_HOURLY_TO_APR
                self.paper.credit(-size * price * funding_hourly * elapsed_h)
            ent["funding_t"] = now
            still_collecting = self._collecting_side(apr) == (1 if size > 0 else -1) and abs(apr) >= self.cfg.exit_apr
            timed_out = (now - ent["t"]) >= self.cfg.max_hold_hours * 3600
            hit_stop = (size > 0 and price <= ent["stop"]) or (size < 0 and price >= ent["stop"])
            if not still_collecting or timed_out or hit_stop:
                self.paper.execute(coin, -size, price)
                self._entry.pop(coin, None)
                exited.add(coin)  # nicht im selben Tick neu eröffnen
                reason = "Stop" if hit_stop else "Zeitlimit" if timed_out else "Funding normalisiert"
                log.info("FundingLab %s: Exit (%s) @ %.4f", coin, reason, price)

        # 2. Neue Einstiege: extremes Funding + noch keine Position
        if len(self._entry) >= self.cfg.max_positions:
            return
        for p in sorted(pulses, key=lambda p: abs(p.funding_apr), reverse=True):
            if len(self._entry) >= self.cfg.max_positions:
                break
            coin = p.coin
            if coin in self._entry or coin in exited:
                continue
            if self.cfg.coins and coin not in self.cfg.coins:
                continue
            if abs(p.funding_apr) < self.cfg.entry_apr:
                continue
            price = prices.get(coin) or p.mark
            if not price:
                continue
            direction = self._collecting_side(p.funding_apr)
            size = (self.cfg.risk_frac * equity) / price * direction
            if abs(size) * price < self.cfg.min_notional:
                continue
            self.paper.execute(coin, size, price)
            stop = price * (1 - direction * self.cfg.stop_frac)
            self._entry[coin] = {"price": price, "t": now, "stop": stop, "funding_t": now}
            log.info("FundingLab %s: %s @ %.4f (Funding %+.0f%% p.a., kassiert)",
                     coin, "LONG" if direction > 0 else "SHORT", price, p.funding_apr * 100)

    @staticmethod
    def _collecting_side(apr: float) -> int:
        """Positives Funding = Longs zahlen -> SHORT kassiert. Und umgekehrt."""
        return -1 if apr > 0 else 1

    def equity(self, prices: dict[str, float]) -> float:
        return self.paper.equity(prices)


class LabFleet:
    """Koordiniert die Paper-Strategien: Daten holen, ticken, Stats persistieren."""

    def __init__(self, cfg, client, initial_equity: float, fee_rate: float,
                 runtime: Path | None = None, clock=time.time):
        self.cfg = cfg                  # LabsConfig
        self.client = client
        self.clock = clock
        runtime = runtime or RUNTIME
        self.labs = []
        if cfg.trend.enabled:
            paper = PaperBroker(initial_equity, fee_rate, path=runtime / "lab_trend.json")
            self.labs.append(TrendLab(cfg.trend, paper, TrendStrategy(cfg.trend.strategy)))
        if cfg.funding.enabled:
            paper = PaperBroker(initial_equity, fee_rate, path=runtime / "lab_funding.json")
            self.labs.append(FundingLab(cfg.funding, paper))
        self._last_pulse = 0.0
        self._pulse_cache: list = []
        self._last_candle_fetch = 0.0

    def tick(self, prices: dict[str, float]) -> None:
        equity_prices = prices or {}
        for lab in self.labs:
            try:
                if isinstance(lab, TrendLab):
                    self._tick_trend(lab, equity_prices)
                elif isinstance(lab, FundingLab):
                    self._tick_funding(lab, equity_prices)
            except Exception:
                log.debug("Lab %s tick fehlgeschlagen", lab.name, exc_info=True)

    def _tick_trend(self, lab: TrendLab, prices: dict[str, float]) -> None:
        coin = lab.cfg.coin
        price = prices.get(coin)
        if price:
            lab.on_price(coin, price)  # Stop/TP jeden Tick prüfen (günstig, kein API-Call)
        # Candles nur gedrosselt holen - eine neue 1h-Candle kommt eh nur stündlich,
        # jeder Tick (~10s) wäre verschwendete API-Last (Rate-Limit-Hygiene).
        now = self.clock()
        if now - self._last_candle_fetch < self.cfg.candle_fetch_seconds:
            return
        self._last_candle_fetch = now
        df = self.client.candles(coin, lab.cfg.interval, lab.cfg.lookback)
        lab.on_candle(coin, df, lab.equity(prices))

    def _tick_funding(self, lab: FundingLab, prices: dict[str, float]) -> None:
        now = self.clock()
        if now - self._last_pulse >= self.cfg.funding.cache_seconds or not self._pulse_cache:
            from .investigator import market_pulse

            self._pulse_cache = market_pulse(self.client.market, lab.cfg.coins or None)
            self._last_pulse = now
        lab.on_pulse(self._pulse_cache, prices, lab.equity(prices), now=now)

    def stats(self, prices: dict[str, float]) -> dict[str, dict]:
        out = {}
        for lab in self.labs:
            b = lab.paper
            out[lab.name] = {
                "equity": round(lab.equity(prices), 2),
                "trades": b.trades,
                "realized_pnl": round(b.realized_pnl, 2),
                "fees_paid": round(b.fees_paid, 2),
                "open_positions": len(b.positions),
            }
        return out

    def persist_stats(self, prices: dict[str, float]) -> None:
        try:
            import json

            RUNTIME.mkdir(exist_ok=True)
            (RUNTIME / "labs.json").write_text(
                json.dumps({"updated": int(self.clock()), "labs": self.stats(prices)}, indent=2))
        except OSError:
            log.debug("labs.json nicht schreibbar", exc_info=True)
