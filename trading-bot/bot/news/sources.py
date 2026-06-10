"""News-Quellen mit einheitlichem Interface: fetch() -> list[NewsItem].

Realitätscheck zu den Quellen (Stand der Pricing-Modelle):
  RSS          - kostenlos, 1-5 Min. Latenz. Gut für Kontext, zu langsam für Schocks.
  CryptoPanic  - kostenloser API-Key, aggregiert Crypto-News, ähnliche Latenz.
  X/Twitter    - Filtered Stream (Echtzeit) erst im Pro-Tier (~5.000 USD/Monat);
                 der Basic-Tier (~200 USD/Monat) erlaubt nur Polling der Search-API
                 -> faktisch auch nur Minuten-Latenz. Trump postet zudem primär
                 auf Truth Social, nicht auf X.

  Konsequenz: KEINE News-Quelle schlägt den Markt. Die Schock-Erkennung läuft
  deshalb zusätzlich direkt auf den Preisdaten (shock.py) - die reagiert in
  Sekunden, egal wer was wo postet. News-Quellen liefern das "Warum" und
  schalten den Bot in den Vorsichtsmodus, bevor sich Lagen zuspitzen.
"""

import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger(__name__)


@dataclass
class NewsItem:
    source: str
    title: str
    time_ms: int
    url: str = ""


class RssSource:
    """Liest beliebige RSS/Atom-Feeds (CoinDesk, Cointelegraph, Reuters, ...)."""

    def __init__(self, feeds: list[str], timeout: int = 15):
        self.feeds = feeds
        self.timeout = timeout

    def fetch(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        for url in self.feeds:
            try:
                resp = requests.get(url, timeout=self.timeout,
                                    headers={"User-Agent": "trading-bot/1.0"})
                resp.raise_for_status()
                items.extend(self._parse(url, resp.text))
            except Exception:
                log.exception("RSS-Feed %s nicht lesbar", url)
        return items

    @staticmethod
    def _parse(feed_url: str, xml_text: str) -> list[NewsItem]:
        root = ET.fromstring(xml_text)
        out = []
        for item in root.iter("item"):  # RSS 2.0
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = item.findtext("pubDate")
            try:
                ts = int(parsedate_to_datetime(pub).timestamp() * 1000) if pub else int(time.time() * 1000)
            except Exception:
                ts = int(time.time() * 1000)
            if title:
                out.append(NewsItem(source=feed_url, title=title, time_ms=ts, url=link))
        return out


class CryptoPanicSource:
    """CryptoPanic-Aggregator. Kostenloser Token: https://cryptopanic.com/developers/api/"""

    URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self, timeout: int = 15):
        self.token = os.environ.get("CRYPTOPANIC_TOKEN", "")

    def fetch(self) -> list[NewsItem]:
        if not self.token:
            log.warning("CRYPTOPANIC_TOKEN fehlt in .env - Quelle übersprungen")
            return []
        resp = requests.get(self.URL, params={"auth_token": self.token, "filter": "important"}, timeout=15)
        resp.raise_for_status()
        out = []
        for post in resp.json().get("results", []):
            ts = post.get("published_at", "")
            try:
                from datetime import datetime
                t = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                t = int(time.time() * 1000)
            out.append(NewsItem(source="cryptopanic", title=post.get("title", ""),
                                time_ms=t, url=post.get("url", "")))
        return out


class TwitterSource:
    """X/Twitter Recent-Search (Polling). Benötigt TWITTER_BEARER_TOKEN in .env.

    Achtung Kosten/Latenz: siehe Modul-Docstring. Diese Quelle ist optional und
    bewusst als Polling implementiert - den Echtzeit-Stream gibt es nur im
    Pro-Tier.
    """

    URL = "https://api.twitter.com/2/tweets/search/recent"

    def __init__(self, query: str, timeout: int = 15):
        self.query = query
        self.token = os.environ.get("TWITTER_BEARER_TOKEN", "")
        self.timeout = timeout

    def fetch(self) -> list[NewsItem]:
        if not self.token:
            log.warning("TWITTER_BEARER_TOKEN fehlt in .env - Quelle übersprungen")
            return []
        resp = requests.get(
            self.URL,
            params={"query": self.query, "max_results": 25, "tweet.fields": "created_at"},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        out = []
        for tw in resp.json().get("data", []):
            try:
                from datetime import datetime
                t = int(datetime.fromisoformat(tw["created_at"].replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                t = int(time.time() * 1000)
            out.append(NewsItem(source="twitter", title=tw.get("text", ""), time_ms=t))
        return out


def build_sources(cfg) -> list:
    """Stellt die aktiven Quellen gemäß NewsConfig zusammen."""
    sources: list = []
    if cfg.rss_feeds:
        sources.append(RssSource(cfg.rss_feeds))
    if cfg.cryptopanic:
        sources.append(CryptoPanicSource())
    if cfg.twitter:
        sources.append(TwitterSource(cfg.twitter_query))
    return sources
