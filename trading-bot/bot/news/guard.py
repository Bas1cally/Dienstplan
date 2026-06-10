"""MarketGuard: kombiniert News-Sentiment und Preis-Schock zu einem Risiko-Level.

  NORMAL   - handeln wie konfiguriert
  CAUTION  - keine neuen/aufstockenden Positionen, Reduzieren erlaubt
  RISK_OFF - alles schließen, Handel pausiert (Schock oder kritische News)

Der Guard ist bewusst stateless konsumierbar: Trader und Copier fragen pro
Tick level() ab und entscheiden selbst, was das für ihre Orders bedeutet.
"""

import logging
import time
from enum import IntEnum

from .sentiment import aggregate_score, score_item
from .shock import ShockDetector
from .sources import build_sources

log = logging.getLogger(__name__)


class RiskLevel(IntEnum):
    NORMAL = 0
    CAUTION = 1
    RISK_OFF = 2


class MarketGuard:
    def __init__(self, news_cfg, shock_cfg, client=None, coin: str = "BTC"):
        self.news_cfg = news_cfg
        self.shock = ShockDetector(shock_cfg)
        self.client = client          # HyperliquidClient für 1m-Candles
        self.coin = coin              # Referenzmarkt für die Schock-Erkennung
        self.sources = build_sources(news_cfg) if news_cfg.enabled else []
        self._scored: list = []
        self._last_news_poll = 0.0
        self._seen: set[str] = set()

    def level(self) -> RiskLevel:
        shock_level = self._check_shock()
        news_level = self._check_news()
        level = max(shock_level, news_level)
        if level > RiskLevel.NORMAL:
            log.warning("MarketGuard: %s (Schock=%s, News=%s)",
                        level.name, shock_level.name, news_level.name)
        return level

    # ---------- Preis-Schock ----------

    def _check_shock(self) -> RiskLevel:
        if not self.client:
            return RiskLevel.NORMAL
        try:
            candles = self.client.candles(self.coin, "1m", 90)
        except Exception:
            log.exception("Schock-Check: Candles nicht abrufbar - vorsichtshalber CAUTION")
            return RiskLevel.CAUTION
        state = self.shock.check(candles)
        if state.triggered:
            log.warning("Schock-Detektor: %s", state.reason)
            return RiskLevel.RISK_OFF
        return RiskLevel.NORMAL

    # ---------- News ----------

    def _check_news(self) -> RiskLevel:
        if not self.sources:
            return RiskLevel.NORMAL
        now = time.time()
        if now - self._last_news_poll >= self.news_cfg.poll_seconds:
            self._last_news_poll = now
            self._poll_news()
        score = aggregate_score(self._scored, self.news_cfg.half_life_minutes)
        if score >= self.news_cfg.risk_off_score:
            return RiskLevel.RISK_OFF
        if score >= self.news_cfg.caution_score:
            return RiskLevel.CAUTION
        return RiskLevel.NORMAL

    def _poll_news(self) -> None:
        for source in self.sources:
            try:
                for item in source.fetch():
                    key = f"{item.source}:{item.title[:80]}"
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    scored = score_item(item)
                    if scored.score >= 2:
                        log.info("News [%.1f] %s", scored.score, item.title[:120])
                        self._scored.append(scored)
            except Exception:
                log.exception("News-Quelle %s fehlgeschlagen", type(source).__name__)
        # Gedächtnis begrenzen
        if len(self._scored) > 200:
            self._scored = self._scored[-100:]
        if len(self._seen) > 5000:
            self._seen.clear()
