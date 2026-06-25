"""Multi-Exchange-Konvergenz: Positionierung der Top-Trader anderer Börsen.

Realitätscheck: Das alte Binance-Leaderboard mit einzelnen Trader-Positionen
wurde von Binance ABGESCHALTET - einzelne Fremd-Wallets lassen sich dort nicht
mehr verfolgen (und damit auch nicht LARP-filtern; das geht nur auf Hyperliquid,
wo alles on-chain ist). Was es offiziell und stabil gibt, ist aggregierte
Positionierung:

  Binance  - Top-Trader Long/Short Position Ratio (offizielles Futures-Data-API)
  OKX      - Long/Short Account Ratio (Rubik-Statistik-API)
  Bybit    - Account Ratio (v5 Market-API)

Daraus baut die ConvergenceEngine pro Coin eine Konvergenz-Stimme in [-1, +1]
(+1 = Top-Trader überall long). Der Copier skaliert dann jede Zielposition:

  Externe stimmen mit unseren HL-Leadern überein  -> Boost (default 1.25x)
  Externe widersprechen klar                       -> Dämpfung (default 0.4x)
  Neutral / keine Daten                            -> Faktor 1.0 (kein Einfluss)

Konvergenz ist ein VERSTÄRKER, kein eigener Trade-Auslöser: Ohne Leader-Signal
wird nie eine Position eröffnet, egal was Binance sagt. Caps (Coin-Limit,
Gesamt-Leverage) werden NACH dem Boost erneut angewendet.
"""

import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


def _conviction_from_ratio(ratio: float) -> float:
    """Long/Short-Ratio r -> Konvergenz-Stimme in [-1, +1] (r=1 -> 0)."""
    if ratio <= 0:
        return 0.0
    return max(-1.0, min(1.0, (ratio - 1.0) / (ratio + 1.0) * 2.0))


class BinanceTopTraders:
    """Offizielle Top-Trader-Positionierung (Positions-gewichtet, beste Konten)."""

    URL = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
    name = "binance"

    def conviction(self, coin: str, period: str, timeout: int = 10) -> float | None:
        resp = requests.get(self.URL, params={"symbol": f"{coin}USDT", "period": period, "limit": 1}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        return _conviction_from_ratio(float(data[0]["longShortRatio"]))


class OkxAccounts:
    URL = "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio"
    name = "okx"

    def conviction(self, coin: str, period: str, timeout: int = 10) -> float | None:
        okx_period = period.upper().replace("H", "H").replace("h", "H")
        resp = requests.get(self.URL, params={"ccy": coin, "period": okx_period}, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json().get("data", [])
        if not rows:
            return None
        return _conviction_from_ratio(float(rows[-1][1]))


class BybitAccounts:
    URL = "https://api.bybit.com/v5/market/account-ratio"
    name = "bybit"

    def conviction(self, coin: str, period: str, timeout: int = 10) -> float | None:
        resp = requests.get(self.URL, params={"category": "linear", "symbol": f"{coin}USDT", "period": period, "limit": 1}, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json().get("result", {}).get("list", [])
        if not rows:
            return None
        buy, sell = float(rows[0]["buyRatio"]), float(rows[0]["sellRatio"])
        return max(-1.0, min(1.0, (buy - sell) * 2.0))


SOURCES = {"binance": BinanceTopTraders, "okx": OkxAccounts, "bybit": BybitAccounts}


@dataclass
class ConvergenceVote:
    factor: float
    external: float | None   # gemittelte externe Stimme [-1,+1], None = keine Daten
    sources: int


class ConvergenceEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sources = [SOURCES[s]() for s in cfg.sources if s in SOURCES]
        self._cache: dict[str, tuple[float, float, int]] = {}  # coin -> (ts, avg, n)

    def vote(self, coin: str, our_sign: float) -> ConvergenceVote:
        """Konvergenz-Faktor für unsere Position (our_sign: +1 long, -1 short)."""
        if not self.cfg.enabled or not self.sources or our_sign == 0:
            return ConvergenceVote(1.0, None, 0)
        if ":" in coin:
            # Builder-DEX-Assets (Aktien, Gold, Öl) haben keine Binance/OKX/
            # Bybit-Perp-Ratios - neutral statt sinnlose API-Calls
            return ConvergenceVote(1.0, None, 0)
        avg, n = self._external(coin)
        if n == 0 or abs(avg) < self.cfg.neutral_band:
            return ConvergenceVote(1.0, avg if n else None, n)
        if (avg > 0) == (our_sign > 0):
            # Übereinstimmung: Boost wächst mit der Stärke der externen Stimme
            factor = 1.0 + (self.cfg.agree_boost - 1.0) * min(1.0, abs(avg) / 0.5)
        else:
            factor = self.cfg.disagree_scale
        return ConvergenceVote(factor, avg, n)

    def _external(self, coin: str) -> tuple[float, int]:
        now = time.time()
        cached = self._cache.get(coin)
        if cached and now - cached[0] < self.cfg.cache_seconds:
            return cached[1], cached[2]
        votes = []
        for src in self.sources:
            try:
                v = src.conviction(coin, self.cfg.period)
                if v is not None:
                    votes.append(v)
            except Exception:
                log.debug("Konvergenz-Quelle %s für %s nicht verfügbar", src.name, coin, exc_info=True)
        if not votes:
            # Total-Ausfall aller Quellen (Netz/Rate-Limit) NICHT cachen - sonst
            # bleibt die Konvergenz für cache_seconds blind, statt es beim
            # nächsten Tick erneut zu versuchen. (Echte Neutralität hätte votes.)
            return 0.0, 0
        avg = sum(votes) / len(votes)
        self._cache[coin] = (now, avg, len(votes))
        log.info("Konvergenz %s: extern %+.2f aus %d Quellen", coin, avg, len(votes))
        return avg, len(votes)
