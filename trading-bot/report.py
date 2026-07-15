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

logging.basicConfig(level=logging.WARNING)
RUNTIME = Path(__file__).parent / "runtime"


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


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _asset_buckets(journal: list[dict]) -> dict:
    """Abgeschlossene Quest-Zyklen nach Coin-Präfix bucketen ('xyz:...' = Aktie).
    Fehlt das coin-Feld (alte Zyklen vor dem coin-Tracking-Fix), landet der
    Eintrag EXPLIZIT in 'unbekannt' - nie still als Krypto gezählt."""
    kinds = {"sprint_tp", "sprint_bust", "sprint_cycle_end"}
    buckets = {k: {"n": 0, "pnl": 0.0, "won": 0}
               for k in ("Krypto", "Aktien", "unbekannt")}
    for e in journal:
        if e.get("kind") not in kinds:
            continue
        coin = e.get("coin")
        key = "unbekannt" if not coin else ("Aktien" if ":" in coin else "Krypto")
        b = buckets[key]
        b["n"] += 1
        pnl = float(e.get("pnl", 0))
        b["pnl"] += pnl
        if pnl > 0:
            b["won"] += 1
    return buckets


def build_report(offline: bool = False, horizon: float = 24, fast_horizon: float = 1.0,
                 veto_hold: float = 2.0, runtime_dir: Path | None = None) -> str:
    """Quest-Bot-Report: es gibt nur noch den Quest-Bot, der die Schatztruhe
    füllt. Rein aus lokalen Runtime-Dateien (kein Netz nötig) - genutzt von
    main() (python report.py) UND vom Telegram-Befehl /fullreport. Die Signatur-
    Parameter (offline/horizon/…) bleiben aus Kompatibilität, werden für den
    Quest-Report aber nicht mehr gebraucht. `runtime_dir` ist injizierbar fürs
    Testen (sonst RUNTIME neben diesem Skript)."""
    rt = runtime_dir or RUNTIME
    out: list[str] = []

    def add(line: str = "") -> None:
        out.append(line)

    st = _load_json(rt / "sprint_cycles.json")
    bk = _load_json(rt / "sprint_book.json")
    journal = load_jsonl(rt / "trades.jsonl")
    pool = _load_json(rt / "sprint_leaders.json")
    pool_n = len(pool) if isinstance(pool, list) else 0

    if not st and not bk and not journal:
        return "\nNoch keine Quest-Daten in runtime/ - erst den Bot laufen lassen.\n"

    won, busted = int(st.get("won", 0)), int(st.get("busted", 0))
    total = won + busted
    banked = float(st.get("banked", 0.0))
    cur_trades = int(bk.get("trades", 0))
    avg = (int(st.get("total_trades", 0)) + cur_trades) / max(1, total + 1)
    eq_real = float(bk.get("initial_equity", 1000)) + float(bk.get("realized_pnl", 0))

    add("\n=== Quest-Bot Report ===\n")
    add(f"  Schatztruhe        {banked:+,.2f} $")
    wr = f", Trefferquote {won / total * 100:.0f}%" if total else ""
    add(f"  Zyklen             {total} abgeschlossen ({won}x Ziel, {busted}x geplatzt{wr})")
    add(f"  Ø Trades/Zyklus    {avg:.1f}  (Churn-Frühwarnung: einstellig = gesund)")
    add(f"  Aktueller Zyklus   Equity {eq_real:,.2f} (realisiert, {cur_trades} Trades)")

    buckets = _asset_buckets(journal)
    if any(b["n"] for b in buckets.values()):
        add("\n  Krypto vs. Aktien-Perps:")
        for name in ("Krypto", "Aktien", "unbekannt"):
            b = buckets[name]
            if not b["n"]:
                continue
            wr2 = b["won"] / b["n"] * 100
            add(f"    {name:10s} {b['n']:>3} Zyklen  PnL {b['pnl']:+,.2f} $  "
                f"(Trefferquote {wr2:.0f}%)")
        if buckets["unbekannt"]["n"]:
            add("    ('unbekannt' = alte Zyklen vor dem coin-Tracking-Fix)")

    strikes = st.get("strikes") or {}
    banned = st.get("banned") or []
    confidence = st.get("confidence") or {}
    stars = [a for a, n in confidence.items() if n >= 100]
    if strikes or banned or confidence:
        add("\n  LARP-Filter:")
        if stars:
            add(f"    ⭐ Stars           {', '.join(a[:10] for a in stars)}")
        if confidence:
            top = sorted(confidence.items(), key=lambda t: -t[1])[:5]
            add("    Confidence        " + ", ".join(f"{a[:10]}:{n}" for a, n in top))
        if strikes:
            add(f"    Strikes           {len(strikes)} Leader mit offenen Strikes")
        if banned:
            add(f"    Gesperrt          {len(banned)} Leader (enttarnte LARPs)")

    add(f"\n  Pool               {pool_n} Leader scanbar")
    add("\n  (Kopier-Buch stillgelegt, nur noch Feed - es gibt nur den Quest-Bot.)")
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
