#!/usr/bin/env python3
"""Startet den Trading-Bot gemäß config.yaml.

Sicherheits-Stufen:
  1. dry_run: true  + testnet  -> Signale werden nur geloggt (Standard)
  2. dry_run: false + testnet  -> echte Orders mit Spielgeld
  3. dry_run: false + mainnet  -> ECHTES GELD (erfordert --i-know-what-im-doing)
"""

import argparse
import logging
import sys

from bot.config import load_config, load_credentials
from bot.exchange import HyperliquidClient
from bot.trader import Trader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")],
)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--i-know-what-im-doing", action="store_true",
                   help="Erforderlich für Live-Trading auf Mainnet")
    args = p.parse_args()

    cfg = load_config()

    if not cfg.is_testnet and not cfg.dry_run and not args.i_know_what_im_doing:
        print("ABBRUCH: mainnet + dry_run=false handelt mit echtem Geld.\n"
              "Wenn du das wirklich willst: python run_bot.py --i-know-what-im-doing")
        sys.exit(1)

    key = addr = None
    if not cfg.dry_run:
        key, addr = load_credentials()

    client = HyperliquidClient(testnet=cfg.is_testnet, private_key=key, account_address=addr,
                               dexs=cfg.market.dexs)

    guard = None
    if cfg.news.enabled or cfg.shock.enabled:
        from bot.news.guard import MarketGuard

        guard = MarketGuard(cfg.news, cfg.shock, client=client, coin=cfg.market.coin)

    Trader(cfg, client, guard=guard).run_forever()


if __name__ == "__main__":
    main()
