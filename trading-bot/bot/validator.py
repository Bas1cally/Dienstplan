"""Trade-Validator: der zweite Bot, der jeden Trade des ersten prüft.

Zwei-Bot-Prinzip:
  Bot 1 (Copy-Engine)  schlägt einen Trade vor, weil die Leader ihn halten.
  Bot 2 (Validator)    prüft den Vorschlag gegen die Markttechnik - erst wenn
                       BEIDE Ja sagen, wird die Position eröffnet/vergrößert.

Wichtige Asymmetrie: Der Validator blockiert NUR Exposure-Erhöhungen.
Reduzierungen und Schließungen laufen immer durch - Risikoabbau braucht
keine Genehmigung, sonst sitzt man im Crash auf einer Position fest.

Prüfebene 1 - deterministische Technik (immer aktiv):
  - Trend-Alignment: EMA(20) vs EMA(50) auf 15m UND 1h müssen zur
    Trade-Richtung passen (Mehrheits-Score)
  - Momentum: Preis relativ zur EMA(20) auf dem Trading-Timeframe
  - RSI-Extrem als hartes Veto: kein Long über 75, kein Short unter 25

Prüfebene 2 - Claude als zweite Meinung (optional, llm_enabled):
  Bekommt eine kompakte Markt-Zusammenfassung (EMAs, RSI, ATR%, 24h-Move)
  und den Trade-Vorschlag, antwortet strukturiert mit approve/reject.

Ehrlicher Disclaimer: EMA/RSI sind bewährte Heuristiken, keine bewiesenen
Edges. Der Wert des Validators liegt im Filtern offensichtlich schlechter
Einstiege (Long in fallendes Messer), nicht in Wahrsagerei.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field

from .indicators import atr, ema, rsi

log = logging.getLogger(__name__)

LLM_SYSTEM = (
    "You are the risk-control desk of a crypto perp trading operation. A copy-trading "
    "engine proposes a trade because top-ranked traders hold this position. Your job is "
    "to VETO entries that conflict with current market structure. Approve unless there "
    "is a clear technical reason against the trade (strong opposing trend, exhausted "
    "move, extreme overextension). You are a filter, not a signal generator."
)

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["approve", "confidence", "reason"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    ok: bool
    score: int = 0
    max_score: int = 0
    reasons: list = field(default_factory=list)

    def summary(self) -> str:
        return f"{'OK' if self.ok else 'VETO'} ({self.score}/{self.max_score}) " + "; ".join(self.reasons)


class TradeValidator:
    def __init__(self, cfg, client, full_cfg=None):
        self.cfg = cfg                # ValidationConfig
        self.client = client          # HyperliquidClient (Candles)
        self._cache: dict[tuple, tuple[float, Verdict]] = {}
        self.llm_client = None
        if cfg.llm_enabled and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic

                self.llm_client = anthropic.Anthropic()
                log.info("Validator: Claude-Zweitmeinung aktiv (%s)", cfg.llm_model)
            except Exception:
                log.exception("Validator: Anthropic-SDK nicht initialisierbar - nur Technik-Prüfung")

    def check(self, coin: str, is_long: bool) -> Verdict:
        """Prüft einen vorgeschlagenen Einstieg. Gecacht pro Coin+Richtung."""
        if not self.cfg.enabled:
            return Verdict(ok=True, reasons=["Validator deaktiviert"])
        key = (coin, is_long)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cfg.cache_seconds:
            return cached[1]
        try:
            verdict = self._technical(coin, is_long)
            if verdict.ok and self.llm_client:
                verdict = self._llm_opinion(coin, is_long, verdict)
        except Exception:
            # Validator-Ausfall darf den Bot nicht lahmlegen: konservativ = kein Veto-
            # System verfügbar -> Einstieg ablehnen (lieber Trade verpassen als blind rein)
            log.exception("Validator-Prüfung für %s fehlgeschlagen - Einstieg abgelehnt", coin)
            verdict = Verdict(ok=False, reasons=["Validator nicht verfügbar - konservativ abgelehnt"])
        self._cache[key] = (time.time(), verdict)
        return verdict

    # ---------- Ebene 1: deterministische Technik ----------

    def _technical(self, coin: str, is_long: bool) -> Verdict:
        v = Verdict(ok=False)
        direction = 1 if is_long else -1
        side = "Long" if is_long else "Short"
        self._features = {}

        for interval in (self.cfg.interval, self.cfg.confirm_interval):
            df = self.client.candles(coin, interval, self.cfg.lookback)
            if len(df) < 60:
                return Verdict(ok=False, reasons=[f"zu wenig {interval}-Daten für {coin}"])
            close = df["close"]
            e_fast, e_slow = ema(close, 20), ema(close, 50)
            r = rsi(close, 14)
            a = atr(df, 14)
            last = float(close.iloc[-1])
            self._features[interval] = {
                "close": last,
                "ema20": round(float(e_fast.iloc[-1]), 2),
                "ema50": round(float(e_slow.iloc[-1]), 2),
                "rsi": round(float(r.iloc[-1]), 1),
                "atr_pct": round(float(a.iloc[-1]) / last * 100, 3),
                "change_pct": round((last / float(close.iloc[0]) - 1) * 100, 2),
            }

            # Check 1: Trend-Alignment EMA20 vs EMA50 (je Timeframe ein Punkt)
            v.max_score += 1
            trend = 1 if e_fast.iloc[-1] > e_slow.iloc[-1] else -1
            if trend == direction:
                v.score += 1
                v.reasons.append(f"{interval}-Trend stützt {side}")
            else:
                v.reasons.append(f"{interval}-Trend ({'auf' if trend > 0 else 'ab'}) gegen {side}")

            # Hartes Veto: RSI-Extrem gegen die Trade-Richtung
            cur_rsi = float(r.iloc[-1])
            if is_long and cur_rsi > self.cfg.rsi_max_long:
                v.reasons.append(f"VETO: RSI {cur_rsi:.0f} überkauft ({interval})")
                return v
            if not is_long and cur_rsi < self.cfg.rsi_min_short:
                v.reasons.append(f"VETO: RSI {cur_rsi:.0f} überverkauft ({interval})")
                return v

        # Check 2: Momentum - Preis vs EMA20 auf dem Trading-Timeframe
        v.max_score += 1
        f = self._features[self.cfg.interval]
        if (f["close"] - f["ema20"]) * direction >= 0:
            v.score += 1
            v.reasons.append("Momentum passt")
        else:
            v.reasons.append("Preis auf falscher Seite der EMA20")

        v.ok = v.score >= self.cfg.min_score
        return v

    # ---------- Ebene 2: Claude-Zweitmeinung ----------

    def _llm_opinion(self, coin: str, is_long: bool, technical: Verdict) -> Verdict:
        payload = json.dumps(
            {
                "proposed_trade": {"coin": coin, "direction": "long" if is_long else "short"},
                "technical_check": {"score": technical.score, "max": technical.max_score,
                                    "notes": technical.reasons},
                "market": self._features,
            },
            ensure_ascii=False,
        )
        resp = self.llm_client.messages.create(
            model=self.cfg.llm_model,
            max_tokens=512,
            system=LLM_SYSTEM,
            messages=[{"role": "user", "content": payload}],
            output_config={"format": {"type": "json_schema", "schema": LLM_SCHEMA}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
        opinion = json.loads(text)
        if opinion["approve"]:
            technical.reasons.append(f"Claude: approve ({opinion['confidence']}/10)")
            return technical
        return Verdict(
            ok=False, score=technical.score, max_score=technical.max_score,
            reasons=technical.reasons + [f"Claude-VETO: {opinion['reason'][:120]}"],
        )
