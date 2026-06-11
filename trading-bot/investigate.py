#!/usr/bin/env python3
"""Investigator-CLI: komplette Akte zu einer beliebigen Hyperliquid-Wallet.

  python investigate.py 0xWALLET             # Dossier: Positionen, Metriken, LARP
  python investigate.py 0xWALLET --days 60   # längeres Analysefenster
  python investigate.py --pulse              # Markt-Puls: Funding/OI der Top-Coins
"""

import argparse
import logging

from hyperliquid.info import Info

from bot.config import load_config
from bot.copytrade.analyzer import TraderAnalyzer
from bot.copytrade.larp import LarpConfig, LarpFilter
from bot.exchange import api_url
from bot.investigator import market_pulse

logging.basicConfig(level=logging.WARNING)


def dossier(info, address: str, days: int, larp_cfg: dict) -> None:
    state = info.user_state(address)
    equity = float(state["marginSummary"]["accountValue"])
    print(f"\n=== Dossier {address} ===\n")
    print(f"  Equity                 {equity:,.2f} USD")

    positions = [p["position"] for p in state.get("assetPositions", [])
                 if float(p["position"]["szi"]) != 0]
    if positions:
        print(f"\n  Offene Positionen ({len(positions)}):")
        for pos in positions:
            size = float(pos["szi"])
            value = abs(float(pos.get("positionValue") or 0))
            lev = pos.get("leverage", {}).get("value", "?")
            upnl = float(pos.get("unrealizedPnl", 0))
            print(f"    {'LONG ' if size > 0 else 'SHORT'} {pos['coin']:6s} "
                  f"{value:>12,.0f} USD  {lev}x  Entry {float(pos.get('entryPx') or 0):,.2f}  "
                  f"uPnL {upnl:+,.0f}")
    else:
        print("\n  Keine offenen Positionen.")

    m = TraderAnalyzer(info, days=days).analyze(address)
    print(f"\n  Analyse ({days} Tage):")
    print(f"    Score                {m.score}")
    print(f"    Netto-PnL (n. Fees)  {m.net_pnl:+,.2f} USD  (ROI {m.roi * 100:+.2f}%)")
    print(f"    Round-Trips          {m.round_trips}  |  Win-Rate {m.win_rate:.0%}  |  PF {min(m.profit_factor, 999):.2f}")
    print(f"    mediane Haltedauer   {m.median_holding_minutes / 60:.1f}h")
    print(f"    Max Drawdown         {m.max_drawdown:.1%}  |  prof. Wochen {m.profitable_week_share:.0%}")
    print(f"    größter Trade        {m.max_trade_share:.0%} des Brutto-Gewinns")
    print(f"    Coins                {', '.join(m.coins[:12])}")

    verdict = LarpFilter(LarpConfig(**larp_cfg)).check(m)
    if verdict.passed:
        print("\n  LARP-Filter: ✓ BESTANDEN - käme als Copy-Leader infrage")
    else:
        print("\n  LARP-Filter: ✗ AUSSORTIERT")
        for r in verdict.reasons:
            print(f"    - {r}")
    print()


def pulse(info) -> None:
    print("\n=== Markt-Puls: Funding & Open Interest ===\n")
    print("  Positives Funding = Longs zahlen = Crowd ist long (MMs kassieren)\n")
    rows = market_pulse(info)
    rows.sort(key=lambda p: -p.oi_usd)
    print(f"  {'Coin':8s} {'Funding p.a.':>13s} {'OI (USD)':>15s}  Crowd")
    for p in rows[:20]:
        flag = f"⚠ {p.crowded.upper()} gecrowdet" if p.crowded else ""
        print(f"  {p.coin:8s} {p.funding_apr:>12.1%} {p.oi_usd:>15,.0f}  {flag}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("address", nargs="?", help="zu durchleuchtende Wallet")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--pulse", action="store_true", help="Markt-Puls statt Wallet-Dossier")
    args = ap.parse_args()

    cfg = load_config()
    info = Info(api_url(testnet=False), skip_ws=True)
    if args.pulse:
        pulse(info)
        return
    if not args.address:
        ap.error("Adresse angeben oder --pulse nutzen")
    dossier(info, args.address, args.days or cfg.copytrade.analysis.days,
            cfg.copytrade.analysis.larp or {})


if __name__ == "__main__":
    main()
