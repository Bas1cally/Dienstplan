#!/usr/bin/env python3
"""Preflight-Check: prüft, ob der Bot startklar ist - VOR dem ersten Start ausführen.

  python doctor.py              # alle Checks
  python doctor.py --notify     # zusätzlich Telegram-Testnachricht senden

Exit-Code 0 = alle Pflicht-Checks bestanden (Warnungen sind ok).
"""

import argparse
import logging
import os
import sys
import time

logging.disable(logging.ERROR)  # saubere Checkliste statt Library-Tracebacks

OK, WARN, FAIL, SKIP = "  \033[92m✓\033[0m", "  \033[93m!\033[0m", "  \033[91m✗\033[0m", "  −"
failures = 0


def check(label: str, fn, mandatory: bool = True):
    global failures
    try:
        result = fn()
        if result is None:
            print(f"{OK} {label}")
        else:
            print(f"{OK} {label} — {result}")
    except SkipCheck as e:
        print(f"{SKIP} {label} — {e}")
    except Exception as e:
        msg = str(e).split("\n")[0][:100]
        if mandatory:
            failures += 1
            print(f"{FAIL} {label} — {msg}")
        else:
            print(f"{WARN} {label} — {msg} (optional)")


class SkipCheck(Exception):
    pass


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--notify", action="store_true", help="Telegram-Testnachricht senden")
    args = p.parse_args()

    print("\n=== Hyperliquid Trading Bot - Preflight-Check ===\n")

    # --- Pflicht: Code & Konfiguration ---
    print("Konfiguration:")

    def deps():
        import anthropic  # noqa: F401
        import fastapi  # noqa: F401
        import hyperliquid  # noqa: F401
        import pandas  # noqa: F401
        import uvicorn  # noqa: F401

    check("Dependencies installiert", deps)

    def config():
        from bot.config import load_config

        cfg = load_config()
        mode = f"{'dry-run/paper' if cfg.dry_run else 'LIVE'} auf {cfg.network}"
        if not cfg.dry_run and not cfg.is_testnet:
            return mode + " — ACHTUNG: echtes Geld!"
        return mode

    check("config.yaml lädt und validiert", config)

    def env_file():
        from bot.config import ROOT, load_config

        cfg = load_config()
        env = ROOT / ".env"
        if cfg.dry_run:
            if not env.exists():
                raise SkipCheck("nicht nötig im Dry-Run (für Live: .env.example kopieren)")
            return "vorhanden"
        from bot.config import load_credentials

        load_credentials()
        return "Agent-Key und Adresse vorhanden"

    check(".env / Credentials", env_file)

    def tests():
        import subprocess

        for t in ("test_copytrade", "test_larp_news", "test_autopilot", "test_convergence", "test_validator"):
            r = subprocess.run([sys.executable, f"tests/{t}.py"], capture_output=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"{t} schlägt fehl")
        return "5 Suiten grün"

    check("Unit-Tests", tests)

    # --- Pflicht: Hyperliquid-Anbindung ---
    print("\nHyperliquid:")

    def hl_api():
        from hyperliquid.info import Info

        from bot.config import load_config
        from bot.exchange import api_url

        cfg = load_config()
        t0 = time.time()
        info = Info(api_url(cfg.is_testnet), skip_ws=True)
        mids = info.all_mids()
        return f"{len(mids)} Märkte, {1000 * (time.time() - t0):.0f}ms ({cfg.network})"

    check("API erreichbar (Orders/Candles)", hl_api)

    def hl_mainnet():
        from hyperliquid.info import Info

        from bot.exchange import api_url

        info = Info(api_url(False), skip_ws=True)
        end = int(time.time() * 1000)
        candles = info.candles_snapshot("BTC", "15m", end - 3 * 3600_000, end)
        if len(candles) < 5:
            raise RuntimeError("zu wenige Candles")
        return f"Leader-Daten + Validator-Candles OK ({len(candles)} Candles)"

    check("Mainnet-Daten (Leader + Validator)", hl_mainnet)

    def multidex():
        from bot.config import load_config
        from bot.exchange import HyperliquidClient

        cfg = load_config()
        if cfg.market.dexs == "main":
            raise SkipCheck("market.dexs: main (nur Krypto)")
        client = HyperliquidClient(testnet=False, dexs=cfg.market.dexs)
        n_dexs = len(client.dexs)
        n_assets = len(client.market.coin_to_asset)
        builder = [c for c in client.market.coin_to_asset if ":" in c][:6]
        sample = f", z.B. {', '.join(builder)}" if builder else ""
        return f"{n_dexs} DEXs, {n_assets} Märkte{sample}"

    check("Multi-DEX (Aktien/Gold/Öl)", multidex, mandatory=False)

    def leaderboard():
        from bot.copytrade.leaderboard import LEADERBOARD_URL

        import requests

        r = requests.get(LEADERBOARD_URL, timeout=20)
        r.raise_for_status()
        n = len(r.json().get("leaderboardRows", []))
        if n < 100:
            raise RuntimeError(f"nur {n} Einträge")
        return f"{n} Trader"

    check("Leaderboard (Trader-Discovery)", leaderboard)

    # --- Optional: Zusatzdienste ---
    print("\nZusatzdienste (optional):")

    def news():
        from bot.config import load_config
        from bot.news.sources import RssSource

        cfg = load_config()
        if not cfg.news.enabled:
            raise SkipCheck("news.enabled: false")
        items = RssSource(cfg.news.rss_feeds[:1]).fetch()
        if not items:
            raise RuntimeError("Feed leer")
        return f"{len(items)} Schlagzeilen vom ersten Feed"

    check("News-Feeds (RSS)", news, mandatory=False)

    def convergence():
        from bot.config import load_config
        from bot.convergence import ConvergenceEngine

        cfg = load_config()
        if not cfg.convergence.enabled:
            raise SkipCheck("convergence.enabled: false")
        avg, n = ConvergenceEngine(cfg.convergence)._external("BTC")
        if n == 0:
            raise RuntimeError("keine Quelle erreichbar")
        return f"{n}/{len(cfg.convergence.sources)} Quellen, BTC extern {avg:+.2f}"

    check("Konvergenz (Binance/OKX/Bybit)", convergence, mandatory=False)

    def claude():
        from bot.config import load_config

        cfg = load_config()
        if not (cfg.news.llm_enabled or cfg.validation.llm_enabled):
            raise SkipCheck("llm_enabled: false (News + Validator)")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("llm_enabled aber ANTHROPIC_API_KEY fehlt in .env")
        return "API-Key vorhanden"

    check("Claude-KI", claude, mandatory=False)

    def websocket():
        from bot.config import load_config

        cfg = load_config()
        if not cfg.autopilot.realtime:
            raise SkipCheck("autopilot.realtime: false")
        from bot.realtime import RealtimeFeed

        feed = RealtimeFeed()
        ok = feed.connected
        feed.close()
        if not ok:
            raise RuntimeError("WebSocket nicht verbindbar - Bot fällt auf Polling zurück")
        return "verbunden (Echtzeit-Fills aktiv)"

    check("WebSocket-Echtzeit", websocket, mandatory=False)

    def telegram():
        from bot.notify import Notifier

        n = Notifier()
        if not n.enabled:
            raise SkipCheck("TELEGRAM_* nicht gesetzt")
        if args.notify:
            n.send("✅ Doctor: Testnachricht - der Bot kann dich erreichen.")
            return "Testnachricht gesendet"
        return "konfiguriert (Test: --notify)"

    check("Telegram-Alerts", telegram, mandatory=False)

    def paper_state():
        from bot.paper import PaperBroker

        from bot.config import load_config

        cfg = load_config()
        broker = PaperBroker(cfg.backtest.initial_equity, cfg.backtest.fee_rate)
        if broker.trades:
            return (f"bestehendes Paper-Konto: {broker.trades} Trades, "
                    f"PnL {broker.realized_pnl:+.2f} (Reset: POST /api/paper/reset)")
        return f"frisches Paper-Konto mit {cfg.backtest.initial_equity:.0f} USD"

    check("Paper-Konto", paper_state, mandatory=False)

    print()
    if failures:
        print(f"❌ {failures} Pflicht-Check(s) fehlgeschlagen - erst beheben, dann starten.\n")
        sys.exit(1)
    print("✅ Startklar. Los geht's mit:  python server.py\n")


if __name__ == "__main__":
    main()
