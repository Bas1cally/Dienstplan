#!/usr/bin/env python3
"""Testet die News-Pipeline isoliert: Quellen abrufen, scoren, Risiko-Level zeigen.

  python news_monitor.py            # einmalig abrufen und bewerten
  python news_monitor.py --watch    # Dauerbetrieb wie im Bot
"""

import argparse
import logging
import time

from bot.config import load_config
from bot.news.guard import MarketGuard, RiskLevel
from bot.news.sentiment import aggregate_score

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--watch", action="store_true", help="Dauerbetrieb statt Einmal-Abruf")
    args = p.parse_args()

    cfg = load_config()
    guard = MarketGuard(cfg.news, cfg.shock)  # ohne client: nur News, kein Schock-Check

    while True:
        guard._poll_news()
        score = aggregate_score(guard._scored, cfg.news.half_life_minutes)
        level = guard._check_news()
        print(f"\nAggregierter Risiko-Score: {score:.1f} -> {RiskLevel(level).name}")
        if guard._scored:
            print("Relevante Schlagzeilen (Score >= 2):")
            for s in sorted(guard._scored, key=lambda s: -s.score)[:15]:
                print(f"  [{s.score:4.1f}] {s.item.title[:110]}")
        else:
            print("Keine risikorelevanten Schlagzeilen gefunden.")
        if not args.watch:
            break
        time.sleep(cfg.news.poll_seconds)


if __name__ == "__main__":
    main()
