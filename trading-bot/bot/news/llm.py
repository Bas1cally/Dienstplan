"""Claude-basierte News-Klassifizierung (optional, zweite Meinung zur Keyword-Engine).

Die Keyword-Regeln in sentiment.py sind schnell und deterministisch, aber
begrenzt: "Federal Reserve announces emergency meeting on sunday" triggert
kein Keyword, ist aber hochriskant. Wenn ANTHROPIC_API_KEY gesetzt und
news.llm_enabled aktiv ist, bewertet Claude jede neue Schlagzeile zusätzlich;
der finale Score ist das Maximum aus Keyword- und KI-Bewertung (konservativ:
die wachsamere Stimme gewinnt).

Fällt die API aus, läuft die Keyword-Engine unverändert weiter - die
KI ist ein Verstärker, keine Abhängigkeit.
"""

import json
import logging
import os

from .sources import NewsItem

log = logging.getLogger(__name__)

SYSTEM = (
    "You are a risk analyst for a crypto perpetual-futures trading desk. "
    "For each news headline, assess the immediate downside risk it implies for "
    "crypto markets (BTC/ETH perps) on a 0-10 scale:\n"
    "0-1: irrelevant or bullish\n"
    "2-3: mildly concerning, monitor\n"
    "4-6: significant risk-off signal (regulation, macro shock, large liquidations)\n"
    "7-8: severe (exchange insolvency, major hack, war escalation, emergency Fed action)\n"
    "9-10: extreme, immediate flight to safety (systemic collapse, depeg of major stablecoin)\n"
    "Judge only market impact, not sentiment. Be conservative: when ambiguous, score lower."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "risk": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "risk", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


class LlmClassifier:
    """Bewertet Schlagzeilen-Batches mit Claude. None-Rückgabe = nicht verfügbar."""

    BATCH = 25  # Schlagzeilen pro API-Call

    def __init__(self, model: str):
        self.model = model
        self.client = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic

                self.client = anthropic.Anthropic()
                log.info("Claude-News-Klassifizierung aktiv (Modell: %s)", model)
            except Exception:
                log.exception("Anthropic-SDK nicht initialisierbar - nur Keyword-Engine aktiv")
        else:
            log.info("ANTHROPIC_API_KEY nicht gesetzt - nur Keyword-Engine aktiv")

    @property
    def available(self) -> bool:
        return self.client is not None

    def classify(self, items: list[NewsItem]) -> dict[int, tuple[float, str]]:
        """Liefert {index: (risk_score, begründung)} für eine Schlagzeilen-Liste."""
        if not self.client or not items:
            return {}
        results: dict[int, tuple[float, str]] = {}
        for start in range(0, len(items), self.BATCH):
            batch = items[start : start + self.BATCH]
            try:
                results.update(self._classify_batch(batch, offset=start))
            except Exception:
                log.exception("Claude-Klassifizierung fehlgeschlagen - Keyword-Scores bleiben maßgeblich")
        return results

    def _classify_batch(self, batch: list[NewsItem], offset: int) -> dict[int, tuple[float, str]]:
        payload = json.dumps(
            [{"index": offset + i, "headline": it.title[:300]} for i, it in enumerate(batch)],
            ensure_ascii=False,
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM,
            messages=[{"role": "user", "content": payload}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
        out = {}
        for row in json.loads(text)["items"]:
            score = max(0.0, min(10.0, float(row["risk"])))
            out[int(row["index"])] = (score, row.get("reason", ""))
        return out
