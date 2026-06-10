#!/usr/bin/env python3
"""Backtest der Strategie auf historischen Hyperliquid-Candles.

  python backtest.py                  # Coin/Intervall aus config.yaml
  python backtest.py --candles 2000   # mehr Historie
  python backtest.py --csv data.csv   # eigene OHLCV-CSV (time,open,high,low,close,volume)
"""

import argparse
import logging

import pandas as pd

from bot.backtester import Backtester
from bot.config import load_config
from bot.exchange import HyperliquidClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--candles", type=int, default=1500, help="Anzahl Candles Historie")
    p.add_argument("--csv", type=str, default=None, help="OHLCV-CSV statt API-Daten")
    args = p.parse_args()

    cfg = load_config()
    if args.csv:
        df = pd.read_csv(args.csv)
    else:
        client = HyperliquidClient(testnet=False)  # Mainnet-Daten, nur lesend
        df = client.candles(cfg.market.coin, cfg.market.interval, args.candles)

    print(f"\nBacktest: {cfg.market.coin} {cfg.market.interval}, {len(df)} Candles")
    result = Backtester(cfg).run(df)

    print("\n=== Ergebnis ===")
    for k, v in result.summary().items():
        print(f"  {k:28s} {v}")

    print("\nLetzte Trades:")
    for t in result.trades[-10:]:
        print(f"  {t.direction:5s} entry {t.entry:10.2f} -> exit {t.exit:10.2f}  "
              f"pnl {t.pnl:9.2f}  ({t.reason})")
    print("\nHinweis: Vergangene Performance garantiert keine zukünftigen Ergebnisse.")


if __name__ == "__main__":
    main()
