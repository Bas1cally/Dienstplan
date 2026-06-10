#!/usr/bin/env python3
"""Findet und bewertet profitable Hyperliquid-Trader als Copy-Kandidaten.

  python analyze_traders.py                          # Leaderboard -> Analyse -> leaders.json
  python analyze_traders.py --address 0xabc 0xdef    # bestimmte Wallets analysieren
  python analyze_traders.py --days 60 --min-score 50

Schreibt die Top-Trader (inkl. Metriken und Gewichtung) nach leaders.json,
das copy_bot.py als Leader-Liste einliest.
"""

import argparse
import json
import logging
from pathlib import Path

from hyperliquid.info import Info

from bot.config import load_config
from bot.copytrade.analyzer import TraderAnalyzer
from bot.copytrade.leaderboard import fetch_candidates
from bot.exchange import api_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--address", nargs="*", default=None,
                   help="Wallets direkt analysieren statt Leaderboard zu laden")
    p.add_argument("--days", type=int, default=None, help="Analysefenster in Tagen")
    p.add_argument("--min-score", type=float, default=None)
    p.add_argument("--out", type=str, default=None, help="Ziel-Datei (Default: leaders_file aus config)")
    args = p.parse_args()

    cfg = load_config()
    an_cfg = cfg.copytrade.analysis
    days = args.days or an_cfg.days
    min_score = args.min_score if args.min_score is not None else an_cfg.min_score

    # Analyse läuft immer gegen Mainnet-Daten - dort sind die echten Trader.
    info = Info(api_url(testnet=False), skip_ws=True)

    if args.address:
        addresses = args.address
    else:
        candidates = fetch_candidates(
            min_account_value=an_cfg.min_account_value,
            min_volume=an_cfg.min_volume,
            top_n=an_cfg.top_n,
        )
        addresses = [c.address for c in candidates]
        if not addresses:
            log.error("Keine Kandidaten gefunden - Filter zu streng oder Leaderboard nicht erreichbar.")
            return

    analyzer = TraderAnalyzer(info, days=days)
    ranked = analyzer.rank(addresses, min_score=min_score)

    if not ranked:
        log.warning("Kein Trader hat den Mindest-Score von %.0f erreicht.", min_score)
        return

    top = ranked[: cfg.copytrade.max_leaders]
    total = sum(m.score for m in top)
    leaders = [
        {
            "address": m.address,
            "weight": round(m.score / total, 4),
            "score": m.score,
            "roi_pct": round(m.roi * 100, 2),
            "profit_factor": round(min(m.profit_factor, 999), 2),
            "win_rate": round(m.win_rate, 3),
            "closed_trades": m.closed_trades,
            "max_drawdown_pct": round(m.max_drawdown * 100, 2),
            "profitable_day_share": round(m.profitable_day_share, 3),
            "account_value": round(m.account_value, 0),
            "coins": m.coins[:10],
            "analysis_days": days,
        }
        for m in top
    ]

    out = Path(args.out or cfg.copytrade.leaders_file)
    out.write_text(json.dumps(leaders, indent=2))

    print(f"\n=== Top {len(leaders)} Copy-Kandidaten (Fenster: {days} Tage) ===")
    for l in leaders:
        print(f"  {l['address']}  score={l['score']:5.1f}  gewicht={l['weight']:.2f}  "
              f"roi={l['roi_pct']:+6.2f}%  pf={l['profit_factor']:.2f}  "
              f"trades={l['closed_trades']}  dd={l['max_drawdown_pct']:.1f}%")
    print(f"\nGespeichert nach {out} - Datei prüfen/editieren, dann: python copy_bot.py")


if __name__ == "__main__":
    main()
