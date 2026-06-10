#!/usr/bin/env python3
"""Copy-Trading-Bot: spiegelt die Positionen der in leaders.json gelisteten
Trader skaliert auf das eigene Konto (siehe bot/copytrade/copier.py).

Vorher ausführen: python analyze_traders.py

Sicherheits-Stufen wie beim Strategie-Bot:
  dry_run: true   -> nur loggen (Default)
  testnet         -> Spielgeld
  mainnet + flag  -> echtes Geld
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from bot.config import load_config, load_credentials
from bot.copytrade.copier import CopyTrader
from bot.copytrade.tracker import LeaderTracker
from bot.exchange import HyperliquidClient, api_url
from hyperliquid.info import Info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("copy_bot.log")],
)
log = logging.getLogger(__name__)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--i-know-what-im-doing", action="store_true")
    args = p.parse_args()

    cfg = load_config()
    if not cfg.is_testnet and not cfg.dry_run and not args.i_know_what_im_doing:
        print("ABBRUCH: mainnet + dry_run=false handelt mit echtem Geld.\n"
              "Wenn du das wirklich willst: python copy_bot.py --i-know-what-im-doing")
        sys.exit(1)

    leaders_path = Path(cfg.copytrade.leaders_file)
    if not leaders_path.exists():
        print(f"{leaders_path} fehlt. Erst Trader analysieren: python analyze_traders.py")
        sys.exit(1)
    leaders = json.loads(leaders_path.read_text())[: cfg.copytrade.max_leaders]
    weights = {l["address"]: float(l["weight"]) for l in leaders}
    log.info("Folge %d Leadern: %s", len(weights),
             ", ".join(f"{a[:10]}({w:.0%})" for a, w in weights.items()))

    key = addr = None
    if not cfg.dry_run:
        key, addr = load_credentials()
    client = HyperliquidClient(testnet=cfg.is_testnet, private_key=key, account_address=addr,
                               dexs=cfg.market.dexs)

    # Leader-Positionen kommen immer vom Mainnet - dort traden die Profis.
    leader_info = Info(api_url(testnet=False), skip_ws=True)
    tracker = LeaderTracker(leader_info, list(weights), dexs=client.dexs)

    guard = None
    if cfg.news.enabled or cfg.shock.enabled:
        from bot.news.guard import MarketGuard

        guard = MarketGuard(cfg.news, cfg.shock, client=client, coin="BTC")

    copier = CopyTrader(cfg, client, tracker, weights, guard=guard)

    mode = "DRY-RUN" if cfg.dry_run else "LIVE"
    log.info("Copy-Bot gestartet (%s, eigene Orders auf %s)", mode,
             "TESTNET" if cfg.is_testnet else "MAINNET")
    while True:
        try:
            copier.tick()
        except KeyboardInterrupt:
            log.info("Beendet durch Benutzer.")
            return
        except Exception:
            log.exception("Fehler im Tick")
        time.sleep(cfg.copytrade.poll_seconds)


if __name__ == "__main__":
    main()
