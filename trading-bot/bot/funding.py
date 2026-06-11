"""Funding-Tilt: das Buch zugunsten der Funding-kassierenden Seite neigen.

Perps zahlen stündlich Funding von der gecrowdeten an die Gegenseite. Private
Bots nutzen das als stillen Carry-Edge: Positionen, die Funding ZAHLEN würden,
werden verkleinert; Positionen, die Funding KASSIEREN, leicht vergrößert.
Das ändert nie die Richtung (die kommt von den Leadern), nur die Größe -
und die harten Caps (Coin-Limit, Gesamt-Leverage) greifen NACH dem Tilt.

Mechanik (Funding annualisiert, signiert; positiv = Longs zahlen):
  long  & apr > +min_apr  -> Position zahlt   -> Skalierung Richtung pay_scale
  short & apr < -min_apr  -> Position zahlt   -> Skalierung Richtung pay_scale
  long  & apr < -min_apr  -> Position kassiert-> Boost Richtung earn_boost
  short & apr > +min_apr  -> Position kassiert-> Boost Richtung earn_boost
Linear interpoliert zwischen min_apr und max_apr; |apr| < min_apr = neutral.
Builder-DEX-Assets (kein Funding-Feed) bleiben unberührt.
"""

import logging
import time

log = logging.getLogger(__name__)


def tilt_factor(signed_notional: float, apr: float | None, cfg) -> float:
    """Skalierungsfaktor für eine Zielposition anhand der Funding-Rate."""
    if not cfg.enabled or apr is None or signed_notional == 0:
        return 1.0
    is_long = signed_notional > 0
    # zahlt die Position Funding? (long zahlt bei apr>0, short bei apr<0)
    pays = (is_long and apr > 0) or (not is_long and apr < 0)
    magnitude = abs(apr)
    if magnitude <= cfg.min_apr:
        return 1.0
    # 0..1 zwischen min_apr und max_apr
    strength = min(1.0, (magnitude - cfg.min_apr) / max(cfg.max_apr - cfg.min_apr, 1e-9))
    if pays:
        return 1.0 + (cfg.pay_scale - 1.0) * strength    # runter Richtung pay_scale
    return 1.0 + (cfg.earn_boost - 1.0) * strength       # hoch Richtung earn_boost


def apply_funding_tilt(targets: dict[str, float], apr_by_coin: dict[str, float], cfg) -> dict[str, float]:
    """Skaliert Ziel-Notionals nach Funding. Pure Funktion, Richtung bleibt."""
    if not cfg.enabled:
        return targets
    out = {}
    for coin, notional in targets.items():
        apr = apr_by_coin.get(coin)
        factor = tilt_factor(notional, apr, cfg)
        if factor != 1.0:
            log.info("Funding-Tilt %s: %.2f (apr %+.0f%% p.a.)", coin, factor, (apr or 0) * 100)
        out[coin] = notional * factor
    return out


class FundingCache:
    """Liefert apr_by_coin aus market_pulse, gecacht; Ausfall = neutral."""

    def __init__(self, market_info, cfg, clock=time.time):
        self.market_info = market_info
        self.cfg = cfg
        self._clock = clock
        self._cached: dict[str, float] = {}
        self._fetched_at = 0.0

    def apr_by_coin(self) -> dict[str, float]:
        now = self._clock()
        if now - self._fetched_at >= self.cfg.cache_seconds:
            try:
                from .investigator import market_pulse

                self._cached = {p.coin: p.funding_apr for p in market_pulse(self.market_info)}
                self._fetched_at = now
            except Exception:
                log.debug("Funding-Daten nicht abrufbar - Tilt neutral", exc_info=True)
                self._fetched_at = now  # nicht jede Runde erneut versuchen
        return self._cached
