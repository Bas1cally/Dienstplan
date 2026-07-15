#!/usr/bin/env python3
"""Paper-Lauf auswerten: Diagnose + konkrete Stellschrauben-Empfehlungen.

  python report.py                  # volle Auswertung inkl. Veto-Outcomes
  python report.py --offline        # ohne Netz (keine Veto-Outcome-Analyse)
  python report.py --horizon 4      # Veto-Bewertung nach 4h statt 24h
"""

import argparse
import json
import logging
from pathlib import Path

from bot.report import recommendations, summarize, veto_outcomes

logging.basicConfig(level=logging.WARNING)
RUNTIME = Path(__file__).parent / "runtime"


def _sig(st: dict) -> str:
    """Konfidenzintervall + Signifikanz-Urteil als Anhang an eine Outcome-Zeile."""
    n = st.get("evaluated", 0)
    if not n:
        return ""
    if n < 5:
        return f" (n={n}, zu wenig für Signifikanz)"
    ci = f"[95% KI {st.get('ci_low_pct', 0):+.2f}..{st.get('ci_high_pct', 0):+.2f}%]"
    tag = "SIGNIFIKANT" if st.get("significant") else "nicht von 0 unterscheidbar"
    return f" {ci} → {tag}"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def make_price_fn():
    """1h-Preis-Lookup (langsame Signale: Vetos, Whale-Positionen)."""
    from bot.exchange import HyperliquidClient

    return _price_fn_factory(HyperliquidClient(testnet=False, dexs="auto"), "1h")


_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def _price_fn_factory(client, interval: str, lookback_days: int = 0):
    ms = _INTERVAL_MS[interval]
    if not lookback_days:
        lookback_days = 90 if interval == "1h" else 21  # feine Raster: kürzer (Candle-Limit)
    cache: dict[str, list] = {}

    def price_fn(coin: str, t_unix: float) -> float | None:
        try:
            if coin not in cache:
                cache[coin] = client.market.candles_snapshot(
                    coin, interval, int((t_unix - lookback_days * 86_400) * 1000),
                    int(__import__("time").time() * 1000))
            for c in cache[coin]:
                if int(c["t"]) <= t_unix * 1000 < int(c["t"]) + ms:
                    return float(c["c"])
        except Exception:
            return None
        return None

    return price_fn


def build_report(offline: bool = False, horizon: float = 24, fast_horizon: float = 1.0,
                 veto_hold: float = 2.0, runtime_dir: Path | None = None) -> str:
    """Baut den kompletten Report-Text (identischer Inhalt wie die CLI-Ausgabe).

    Genutzt von main() (Terminal, python report.py) UND vom Telegram-Befehl
    /fullreport (bot/autopilot.py) - EINE Quelle der Wahrheit statt Text-Logik
    doppelt zu pflegen. `runtime_dir` ist injizierbar fürs Testen (sonst RUNTIME
    neben diesem Skript)."""
    rt = runtime_dir or RUNTIME
    out: list[str] = []

    def add(line: str = "") -> None:
        out.append(line)

    # Preis-Lookups einmal bauen: 1h-Raster für langsame, 15m für schnelle Signale.
    # Expliziter Timeout: build_report läuft u.a. im /fullreport-Hintergrund-Thread
    # (genau für den Fall gebaut, dass das VPS-Netz/SSH gerade zickt) - ohne
    # Timeout würde ein degradiertes HL-API den Thread für IMMER hängen lassen,
    # statt eine Fehlermeldung zurückzugeben (schlechtestmöglicher Ausgang für
    # einen Notfall-Fallback).
    _slow_pf = _fast_pf = None
    if not offline:
        from bot.exchange import HyperliquidClient

        _client = HyperliquidClient(testnet=False, dexs="auto", timeout=20.0)
        _slow_pf = _price_fn_factory(_client, "1h")
        _fast_pf = _price_fn_factory(_client, "15m")

    journal = load_jsonl(rt / "trades.jsonl")
    history = load_jsonl(rt / "history.jsonl")
    paper = None
    if (rt / "paper_state.json").exists():
        paper = json.loads((rt / "paper_state.json").read_text())

    if not journal and not history:
        return "\nNoch keine Daten in runtime/ - erst den Bot laufen lassen.\n"

    s = summarize(journal, history, paper)

    add("\n=== Paper-Lauf-Report ===\n")
    if "days" in s:
        add(f"  Zeitraum          {s['days']} Tage")
        add(f"  Equity            {s['equity_start']:,.2f} -> {s['equity_end']:,.2f}  ({s['return_pct']:+.2f}%)")
        add(f"  Max Drawdown      {s['max_drawdown_pct']:.2f}%")
    add(f"  Orders            {s['orders']}" + (f"  ({s.get('orders_per_day', 0)}/Tag)" if "orders_per_day" in s else "")
        + (f"  Maker-Quote {s['maker_share']:.0%}" if s.get("maker_share") is not None else ""))
    add(f"  Vetos             {s['vetoes']}  ({s['veto_per_order']}x je Order)")
    if s["veto_reasons"]:
        add(f"    Gründe          {s['veto_reasons']}")
    if paper:
        per_day = f", {s['fees_per_day']:.2f}/Tag" if s.get("fees_per_day") is not None else ""
        add(f"  Realisierter PnL  {s['realized_pnl']:+,.2f} USD  |  Fees {s['fees_paid']:,.2f} "
            f"({s['fee_share_pct']:.0f}% vom Brutto{per_day})")
    if s["scalp_trades"]:
        add(f"  Scalps            {s['scalp_trades']}  PnL {s['scalp_pnl']:+,.2f} USD")
    if s["risk_events"]:
        add(f"  Risk-Events       {s['risk_events']} (Flatten/Circuit-Breaker/Max-DD)")

    veto_stats = None
    vetoes = [e for e in journal if e.get("kind") == "veto"]
    if vetoes and not offline:
        add("\n  Bewerte geblockte Trades (Veto-Outcome) ...")
        # 24h-Naiv-Halt: methodisch falscher Maßstab für ein Reconciliation-Buch,
        # das beim Leader-Exit schließt - nur zur Einordnung.
        naive = veto_outcomes(vetoes, _slow_pf, horizon_hours=horizon)
        if naive["evaluated"]:
            add(f"  Veto 24h (naiv)   {naive['evaluated']} Ep.: {naive['avg_return_pct']:+.2f}% "
                f"(Treffer {naive['win_share']:.0%}){_sig(naive)}")
        # COPIER-TREU: kurzer Horizont nahe der Leader-Haltedauer (Default 2h, feines
        # Raster). Das ist der Maßstab, der zählt - er misst den Trade, den der Bot
        # wirklich handelt, und speist daher das Validator-Urteil.
        veto_stats = veto_outcomes(vetoes, _fast_pf, horizon_hours=veto_hold)
        if veto_stats["evaluated"]:
            add(f"  Veto {veto_hold:.0f}h (treu)  {veto_stats['evaluated']} Ep.: "
                f"{veto_stats['avg_return_pct']:+.2f}% (Treffer {veto_stats['win_share']:.0%})"
                f"{_sig(veto_stats)}  ← maßgeblich")
            if veto_stats.get("window_from"):
                import datetime as _dt
                a = _dt.datetime.utcfromtimestamp(veto_stats["window_from"]).strftime("%d.%m")
                b = _dt.datetime.utcfromtimestamp(veto_stats["window_to"]).strftime("%d.%m")
                add(f"                    Fenster: {a}–{b} (rollt mit; ändert es sich nie, klemmt die Messung)")

    # Anomalie-Scout: hatten die "verdächtigen" Wallets recht?
    anomalies = load_jsonl(rt / "anomalies.jsonl")
    if anomalies:
        add(f"\n  Anomalie-Scout      {len(anomalies)} gemeldete Wallets")
        if not offline:
            from bot.report import anomaly_outcomes

            ao = anomaly_outcomes(anomalies, _slow_pf, horizon_hours=horizon)
            if ao["evaluated"]:
                taugt = ao["significant"] and ao["avg_return_pct"] > 0
                add(f"    Follow-through    {ao['evaluated']} Episoden: im Schnitt "
                    f"{ao['avg_return_pct']:+.2f}% nach {horizon:.0f}h in Positionsrichtung "
                    f"(Trefferquote {ao['win_share']:.0%}){_sig(ao)}")
                add(f"                      {'taugt als Copy-Signal' if taugt else 'noch nicht überzeugend, weiter beobachten'}")

    # TWAP-Scout: lief der Kurs in TWAP-Richtung weiter? (anhaltender Flow-Edge)
    twaps = load_jsonl(rt / "twap.jsonl")
    if twaps:
        add(f"\n  TWAP-Scout           {len(twaps)} laufende Whale-TWAPs erkannt")
        for f in twaps[-3:]:
            add(f"    {f.get('side','?')} {f.get('coin','?')}: {f.get('slices',0)} Slices, "
                f"${f.get('notional',0):,.0f} ({str(f.get('address','?'))[:10]}…)")
        if not offline:
            from bot.report import anomaly_outcomes

            to = anomaly_outcomes(twaps, _fast_pf, horizon_hours=fast_horizon)
            if to["evaluated"]:
                add(f"    Follow-through    {to['evaluated']} Episoden: im Schnitt "
                    f"{to['avg_return_pct']:+.2f}% nach {fast_horizon:.1f}h in TWAP-Richtung "
                    f"(Trefferquote {to['win_share']:.0%}){_sig(to)}")

    # Orderbuch-Scout: hatte die Imbalance Vorhersagekraft? (grobes 1h-Raster)
    book = load_jsonl(rt / "orderbook.jsonl")
    if book:
        add(f"\n  Orderbuch-Scout      {len(book)} Mikrostruktur-Signale")
        if not offline:
            from bot.report import anomaly_outcomes  # gleiche coin/side/price/t-Mechanik

            bo = anomaly_outcomes(book, _fast_pf, horizon_hours=fast_horizon)
            if bo["evaluated"]:
                add(f"    Follow-through    {bo['evaluated']} Episoden: im Schnitt "
                    f"{bo['avg_return_pct']:+.2f}% nach {fast_horizon:.1f}h in Imbalance-Richtung "
                    f"(Trefferquote {bo['win_share']:.0%}){_sig(bo)}")

    # Polymarket-Scout: erfahrenes Geld in Prediction Markets (read-only)
    poly = load_jsonl(rt / "polymarket.jsonl")
    if poly:
        add(f"\n  Polymarket-Scout     {len(poly)} gemeldete Wetten erfahrener Wallets")
        for f in poly[-3:]:
            add(f"    ${f.get('bet_usdc', 0):,.0f} auf {f.get('outcome', '?')} — "
                f"„{str(f.get('market', '?'))[:45]}\" (Track-Record ${f.get('realized_pnl', 0):,.0f})")

    # Strategie-Labor: tragen eigene Signale (TA / Funding) einen Edge?
    labs_file = rt / "labs.json"
    if labs_file.exists():
        labs = json.loads(labs_file.read_text()).get("labs", {})
        if labs:
            init = paper.get("initial_equity", 10_000) if paper else 10_000
            add("\n  Strategie-Labor (eigene Signale, Paper):")
            for name, v in labs.items():
                edge = (v["equity"] / init - 1) * 100 if init else 0
                add(f"    {name:18s} {v['equity']:>10,.2f} $  ({edge:+.2f}%, {v['trades']} Trades, "
                    f"PnL {v['realized_pnl']:+,.2f}, {v['open_positions']} offen)")
                # Signifikanz je Episode (Fills != Episoden: eine Episode = open+close)
                rs = v.get("returns")
                if rs and rs.get("evaluated"):
                    add(f"      {'':16s}  {rs['evaluated']} Episoden, je Episode "
                        f"{rs['avg_return_pct']:+.3f}%{_sig(rs)}")

    # Sprint-Buch: 1000$ x Hebel auf den besten Leader, Ziel +100$ je Zyklus
    sprint_state = rt / "sprint_cycles.json"
    sprint_book = rt / "sprint_book.json"
    if sprint_state.exists() or sprint_book.exists():
        st = json.loads(sprint_state.read_text()) if sprint_state.exists() else {}
        bk = json.loads(sprint_book.read_text()) if sprint_book.exists() else {}
        won, busted = int(st.get("won", 0)), int(st.get("busted", 0))
        eq_real = float(bk.get("initial_equity", 1000)) + float(bk.get("realized_pnl", 0))
        cur_trades = int(bk.get("trades", 0))
        avg = (int(st.get("total_trades", 0)) + cur_trades) / max(1, won + busted + 1)
        add("\n  Sprint-Buch (x10 auf besten Leader, Ziel +100$/Zyklus):")
        add(f"    Zyklus {won + busted + 1} läuft: Equity {eq_real:,.2f} (realisiert, "
            f"{cur_trades} Trades)")
        add(f"    Bilanz: {won}x Ziel erreicht, {busted}x geplatzt, "
            f"banked {float(st.get('banked', 0)):+,.2f} $ | Ø {avg:.1f} Trades/Zyklus "
            f"(Churn-Frühwarnung: einstellig = gesund)")
        strikes = st.get("strikes") or {}
        if strikes or st.get("banned"):
            add(f"    LARP: Strikes {strikes} | gesperrt {st.get('banned', [])}")

    # Lighter-Schatten: fremde Lighter-Trader auf HL kopiert (Auto-Discovery)
    li_book = rt / "lighter_shadow.json"
    li_lead = rt / "lighter_leaders.json"
    if li_book.exists():
        bk = json.loads(li_book.read_text())
        init = float(bk.get("initial_equity", 10_000))
        eq = init + float(bk.get("realized_pnl", 0))
        disc = json.loads(li_lead.read_text()) if li_lead.exists() else {}
        add("\n  Lighter-Schatten (fremde Lighter-Trader auf HL, Paper):")
        add(f"    Equity {eq:,.2f} ({(eq / init - 1) * 100:+.2f}%, {int(bk.get('trades', 0))} Trades) "
            f"| {len(disc.get('leaders', []))} Top-Konten aus {disc.get('scanned', 0)} gescannt")

    # Shadow-Varianten: welche Config hätte mehr gemacht?
    shadow_recs = []
    shadow_stats = None
    shadows_file = rt / "shadows.json"
    if shadows_file.exists() and paper:
        from bot.shadow import shadow_recommendations

        shadows = json.loads(shadows_file.read_text()).get("variants", {})
        baseline = s.get("equity_end") or (10_000 + s.get("realized_pnl", 0))
        baseline_age = s.get("days")
        if shadows:
            add("\n  Shadow-Varianten (gleiche Daten, andere Filter):")
            for name, v in shadows.items():
                edge = (v["equity"] / baseline - 1) * 100 if baseline else 0
                age = v.get("age_days")
                # Junge Variante: fairer ist die Rendite seit EIGENEM Start
                young = age is not None and baseline_age and age < 0.7 * baseline_age
                tag = (f"  ⚠ erst {age:.0f}T, seit Start {v.get('return_pct', 0):+.2f}%"
                       if young else f", {age:.0f}T" if age is not None else "")
                add(f"    {name:20s} {v['equity']:>10,.2f} $  ({edge:+.2f}% vs. Haupt-Buch, "
                    f"{v['trades']} Trades{tag})")
            shadow_stats = {"baseline": baseline, "variants": shadows}
            shadow_recs = [r for r in shadow_recommendations(baseline, shadows,
                               baseline_age_days=baseline_age)
                           if "ohne_validator" not in r and "validator_locker" not in r]

    add("\n=== Empfehlungen ===\n")
    from bot.config import load_config
    try:
        _cfg = load_config()
        cfg_hint = {"rebalance_threshold": _cfg.copytrade.rebalance_threshold,
                    "poll_seconds": _cfg.copytrade.poll_seconds}
    except Exception:
        cfg_hint = None
    for r in recommendations(s, veto_stats, shadow_stats, cfg_hint) + shadow_recs:
        add(f"  • {r}")
    add()
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true", help="keine Veto-Outcome-Analyse (kein Netz)")
    p.add_argument("--horizon", type=float, default=24, help="Bewertung langsamer Signale nach X Stunden")
    p.add_argument("--fast-horizon", type=float, default=1.0,
                   help="Bewertung schneller Signale (Orderbuch/TWAP) nach X Stunden (feines 15m-Raster)")
    p.add_argument("--veto-hold", type=float, default=2.0,
                   help="Copier-treuer Veto-Horizont (~Leader-Haltedauer) für das Validator-Urteil")
    args = p.parse_args()
    print(build_report(offline=args.offline, horizon=args.horizon,
                       fast_horizon=args.fast_horizon, veto_hold=args.veto_hold))


if __name__ == "__main__":
    main()
