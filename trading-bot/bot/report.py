"""Auswertungs-Engine: verdichtet Journal, Equity-Historie und Paper-State
zu einer Diagnose mit konkreten Stellschrauben-Empfehlungen.

Das Ziel: Nach wenigen Tagen Paper-Lauf wissen, WARUM die Performance ist,
wie sie ist - nicht erst nach vier Wochen raten. Kernfragen:

  1. Handelt der Bot überhaupt? (Orders vs. Vetos vs. Leerlauf)
  2. Rettet der Validator PnL oder frisst er ihn? (Veto-Outcome-Analyse:
     was hätten die geblockten Trades nach H Stunden gebracht?)
  3. Fressen Fees den Edge? (Fee-Quote vs. Brutto-PnL)
  4. Liefern die Leader? (ROI seit Kopie)

Alle Funktionen sind pur und offline testbar; nur die Veto-Outcome-Analyse
braucht einen price_fn (Candles), den das CLI injiziert.
"""

import logging
from collections import Counter

log = logging.getLogger(__name__)


def summarize(journal: list[dict], history: list[dict], paper: dict | None) -> dict:
    """Grundstatistik aus Journal (chronologisch egal), Equity-Punkten, Paper-State."""
    orders = [e for e in journal if e.get("kind") == "order"]
    vetoes = [e for e in journal if e.get("kind") == "veto"]
    scalps = [e for e in journal if e.get("kind") == "scalp_close"]
    flattens = [e for e in journal if e.get("kind") in ("flatten", "circuit_breaker", "max_drawdown_halt")]

    execs = [e.get("exec") for e in orders if e.get("exec")]
    out = {
        "orders": len(orders),
        "maker_share": round(execs.count("maker") / len(execs), 2) if execs else None,
        "vetoes": len(vetoes),
        "veto_per_order": round(len(vetoes) / len(orders), 1) if orders else float("inf") if vetoes else 0.0,
        "scalp_trades": len(scalps),
        "scalp_pnl": round(sum(float(e.get("pnl", 0)) for e in scalps), 2),
        "risk_events": len(flattens),
        "veto_reasons": _veto_reasons(vetoes),
    }
    if history:
        eq = [p["equity"] for p in history]
        peak, max_dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            max_dd = max(max_dd, (peak - v) / peak if peak else 0)
        days = max((history[-1]["t"] - history[0]["t"]) / 86_400, 1e-9)
        out.update({
            "days": round(days, 1),
            "equity_start": eq[0], "equity_end": eq[-1],
            "return_pct": round((eq[-1] / eq[0] - 1) * 100, 2) if eq[0] else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "orders_per_day": round(len(orders) / days, 1),
        })
    if paper:
        out["fees_paid"] = round(float(paper.get("fees_paid", 0)), 2)
        out["realized_pnl"] = round(float(paper.get("realized_pnl", 0)), 2)
        gross = abs(out["realized_pnl"]) + out["fees_paid"]
        out["fee_share_pct"] = round(out["fees_paid"] / gross * 100, 1) if gross > 0 else 0.0
    return out


def _veto_reasons(vetoes: list[dict]) -> dict[str, int]:
    """Gruppiert Veto-Begründungen grob nach Ursache."""
    counter: Counter = Counter()
    for v in vetoes:
        reasons = " | ".join(v.get("reasons", []))
        if "RSI" in reasons:
            counter["rsi_extrem"] += 1
        elif "Trend" in reasons:
            counter["trend_gegen_richtung"] += 1
        elif "Claude" in reasons:
            counter["claude_veto"] += 1
        elif "wenig" in reasons or "verfügbar" in reasons:
            counter["keine_daten"] += 1
        else:
            counter["sonstige"] += 1
    return dict(counter)


def veto_outcomes(vetoes: list[dict], price_fn, horizon_hours: float = 24,
                  max_samples: int = 60) -> dict:
    """Was hätten die geblockten Einstiege gebracht?

    price_fn(coin, t_unix) -> Preis zu diesem Zeitpunkt oder None.
    Hypothese je Veto: Einstieg zum Veto-Preis in die geblockte Richtung,
    Bewertung nach `horizon_hours`. Bewusst OHNE Stop/TP - es geht um die
    Richtungsfrage "war das Veto richtig?", nicht um exakte Trade-Simulation.
    """
    evaluated, wins, total_ret = 0, 0, 0.0
    for v in vetoes[-max_samples:]:
        coin, side, t0 = v.get("coin"), v.get("side"), v.get("t")
        p0 = v.get("price") or (price_fn(coin, t0) if coin and t0 else None)
        p1 = price_fn(coin, t0 + horizon_hours * 3600) if coin and t0 else None
        if not p0 or not p1:
            continue
        direction = 1 if side == "LONG" else -1
        ret = direction * (p1 / p0 - 1)
        total_ret += ret
        wins += ret > 0
        evaluated += 1
    return {
        "evaluated": evaluated,
        "avg_return_pct": round(total_ret / evaluated * 100, 3) if evaluated else 0.0,
        "win_share": round(wins / evaluated, 2) if evaluated else 0.0,
        "horizon_hours": horizon_hours,
    }


def anomaly_outcomes(anomalies: list[dict], price_fn, horizon_hours: float = 24,
                     max_samples: int = 100) -> dict:
    """Hatten die 'verdächtigen' Wallets recht? Misst die Kursbewegung in ihre
    Positionsrichtung nach `horizon_hours` ab dem Meldezeitpunkt.

    Identische Mechanik wie veto_outcomes (coin/side/price/t je Eintrag) - das
    ist die Datenbasis für die Entscheidung, ob der Scout je ans Copy-Trading darf.
    """
    return veto_outcomes(anomalies, price_fn, horizon_hours, max_samples)


def recommendations(summary: dict, veto_stats: dict | None = None) -> list[str]:
    """Konkrete Stellschrauben-Vorschläge - die Antwort auf '+1$ nach 4 Wochen'."""
    recs: list[str] = []
    orders = summary.get("orders", 0)
    days = summary.get("days", 0)

    if days >= 2 and orders == 0:
        recs.append("Keine einzige Order: leaders.json prüfen (Leader überhaupt aktiv?), "
                    "analysis.top_percent erhöhen (z.B. 1.0 -> 2.0) und max_leaders 3 -> 5 "
                    "für mehr Signalquellen.")
    elif days >= 2 and summary.get("orders_per_day", 0) < 1:
        recs.append("Unter 1 Order/Tag: für Day-Trading zu passiv. Prüfe im Journal, ob die "
                    "Leader flach liegen (dann rotieren: min_keep_score erhöhen) oder ob "
                    "Vetos blocken (siehe Veto-Analyse).")

    if summary.get("veto_per_order", 0) > 5 and veto_stats and veto_stats.get("evaluated", 0) >= 10:
        if veto_stats["avg_return_pct"] > 0.1:
            recs.append(f"Validator blockt PROFITABLE Trades (geblockte Einstiege hätten im "
                        f"Schnitt {veto_stats['avg_return_pct']:+.2f}% nach "
                        f"{veto_stats['horizon_hours']:.0f}h gebracht, Trefferquote "
                        f"{veto_stats['win_share']:.0%}): validation.min_score 2 -> 1 oder "
                        f"rsi_max_long/rsi_min_short lockern (75/25 -> 80/20).")
        elif veto_stats["avg_return_pct"] < -0.1:
            recs.append(f"Validator rettet PnL (geblockte Trades hätten "
                        f"{veto_stats['avg_return_pct']:+.2f}% gebracht): Filter so lassen "
                        f"oder sogar verschärfen.")

    reasons = summary.get("veto_reasons", {})
    if reasons.get("keine_daten", 0) > orders:
        recs.append("Viele Vetos wegen fehlender Daten (fail-closed): Markt-Anbindung prüfen "
                    "(doctor.py) - das sind keine Strategie-, sondern Infrastruktur-Vetos.")

    maker = summary.get("maker_share")
    if maker is not None and maker < 0.5 and orders > 10:
        recs.append(f"Nur {maker:.0%} der Orders wurden als Maker gefüllt (Rest teurer "
                    f"Taker-Fallback): execution.maker_timeout_s erhöhen (20 -> 40).")

    if summary.get("fee_share_pct", 0) > 40 and orders > 10:
        recs.append(f"Fees fressen {summary['fee_share_pct']:.0f}% des Brutto-PnL: "
                    f"rebalance_threshold erhöhen (0.02 -> 0.03) und poll_seconds 10 -> 15, "
                    f"um Rebalance-Churn zu senken.")

    if summary.get("scalp_trades", 0) >= 5 and summary.get("scalp_pnl", 0) < 0:
        recs.append(f"Scalper nach {summary['scalp_trades']} Trades negativ "
                    f"({summary['scalp_pnl']:+.2f} USD): scalp.enabled: false setzen.")

    ret = summary.get("return_pct")
    if ret is not None and days >= 7 and abs(ret) < 0.5 and orders > 0:
        recs.append("Flat trotz Aktivität: Leader-Spalte 'seit Kopie' im Dashboard prüfen - "
                    "liefern die Leader seit Aufnahme nicht, min_keep_score erhöhen (35 -> 45) "
                    "für aggressivere Rotation. NICHT einfach Leverage erhöhen - das skaliert "
                    "eine flache Strategie nur in beide Richtungen.")

    if not recs:
        recs.append("Keine Auffälligkeiten - weiterlaufen lassen und Stichprobe wachsen lassen.")
    return recs
