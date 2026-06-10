"""Regelbasiertes Risiko-Scoring von Schlagzeilen.

Bewusst KEIN Machine-Learning: Bei Markt-Schocks zählt Determinismus und
Geschwindigkeit. Jede Schlagzeile bekommt einen Risiko-Score 0-10; Quellen-
und Akteurs-Multiplikatoren (Trump, Fed, SEC, Whitehouse) verstärken.
Der aggregierte Score zerfällt exponentiell (Halbwertszeit konfigurierbar),
damit alte Schlagzeilen den Bot nicht ewig blockieren.
"""

import math
import re
import time
from dataclasses import dataclass

from .sources import NewsItem

# (Muster, Score) - das höchste zutreffende Muster zählt, Mehrfach-Treffer addieren leicht
CRITICAL = [
    (r"\b(nuclear|invasion|invades|declares war|martial law)\b", 10),
    (r"\b(hack(ed)?|exploit(ed)?|stolen|drained)\b.*\b(exchange|bridge|protocol|billion|million)\b", 9),
    (r"\b(insolven|bankrupt|collapse[sd]?|halts? withdrawals)\b", 9),
    (r"\b(flash ?crash|crash(es|ing)?|plunge[sd]?|plummet)\b", 8),
    (r"\b(depeg|de-peg)\b", 8),
]
HIGH = [
    (r"\b(tariff|trade war|sanction)\b", 6),
    (r"\b(ban(s|ned)?|crackdown|outlaw)\b.*\b(crypto|bitcoin|exchange|mining)\b", 6),
    (r"\b(sec|cftc|doj)\b.*\b(sues?|charges?|lawsuit|enforcement)\b", 5),
    (r"\b(rate hike|raises? (interest )?rates|hawkish)\b", 5),
    (r"\b(liquidat(ed|ion)s?)\b.*\b(billion|million)\b", 5),
    (r"\b(emergency|state of emergency)\b", 5),
]
MODERATE = [
    (r"\b(inflation|cpi|fomc|fed meeting|jobs report|nonfarm)\b", 3),
    (r"\b(sell-?off|correction|bear(ish)?)\b", 3),
    (r"\b(investigat(es?|ion)|probe[sd]?|subpoena)\b", 2),
]
# Akteure, deren Aussagen Märkte bewegen -> Multiplikator
ACTORS = re.compile(r"\b(trump|white ?house|federal reserve|fed chair|powell|sec chair|treasury|ecb)\b", re.I)


@dataclass
class ScoredItem:
    item: NewsItem
    score: float
    matched: list[str]


def score_item(item: NewsItem) -> ScoredItem:
    text = item.title.lower()
    score = 0.0
    matched: list[str] = []
    for rules in (CRITICAL, HIGH, MODERATE):
        for pattern, pts in rules:
            if re.search(pattern, text):
                # höchster Treffer dominiert, weitere addieren abgeschwächt
                score = max(score, pts) + (0.5 if score else 0)
                matched.append(pattern)
                break  # pro Kategorie nur ein Treffer
    if score and ACTORS.search(text):
        score *= 1.5
        matched.append("actor-boost")
    return ScoredItem(item=item, score=min(score, 10.0), matched=matched)


def aggregate_score(scored: list[ScoredItem], half_life_minutes: float, now_ms: int | None = None) -> float:
    """Zeitgewichteter Gesamt-Score: aktuelle Schocks zählen voll, alte zerfallen."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    total = 0.0
    for s in scored:
        age_min = max(0.0, (now - s.item.time_ms) / 60_000)
        decay = math.pow(0.5, age_min / half_life_minutes)
        total = max(total, s.score * decay)  # max statt Summe: ein 10er-Event reicht
    return total
