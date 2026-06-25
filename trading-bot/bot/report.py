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
import math
from collections import Counter

log = logging.getLogger(__name__)


def _dedup_episodes(entries: list[dict], horizon_hours: float) -> list[dict]:
    """Gegen Pseudo-Replikation: dieselbe Wallet/derselbe Coin/dieselbe Richtung
    innerhalb eines Horizont-Fensters ist EINE Episode, kein neues Sample. Sonst
    bläht eine lang gehaltene Position evaluated und win_share auf und jede
    Konfidenzaussage überschätzt die Sicherheit dramatisch.
    """
    window = horizon_hours * 3600
    kept, last_t = [], {}
    for e in sorted(entries, key=lambda e: e.get("t", 0)):
        key = (e.get("coin"), e.get("side"),
               e.get("address") or round(float(e.get("price") or 0), 6))
        t = e.get("t", 0)
        prev = last_t.get(key)
        if prev is not None and t - prev < window:
            continue  # überlappt mit bereits gezähltem Sample
        last_t[key] = t
        kept.append(e)
    return kept


def _return_stats(returns: list[float], horizon_hours: float) -> dict:
    """Mittel, Streuung, 95%-KI und Signifikanz (KI schließt 0 nicht ein).

    Der Kern gegen 'Rauschen zu Urteilen runden': ohne KI ist +0,06% auf 30
    Trades nicht von 0 unterscheidbar - das muss der Report sichtbar machen.
    """
    n = len(returns)
    if n == 0:
        return {"evaluated": 0, "avg_return_pct": 0.0, "win_share": 0.0,
                "ci_low_pct": 0.0, "ci_high_pct": 0.0, "significant": False,
                "se_pct": 0.0, "horizon_hours": horizon_hours}
    mean = sum(returns) / n
    if n > 1:
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        se = math.sqrt(var) / math.sqrt(n)
    else:
        se = 0.0
    # konservativer t-Faktor (zweiseitig, ~95%) je nach Stichprobengröße
    t = 2.78 if n < 5 else 2.09 if n < 20 else 2.0 if n < 40 else 1.96
    ci_low, ci_high = mean - t * se, mean + t * se
    wins = sum(1 for r in returns if r > 0)
    significant = n >= 5 and (ci_low > 0 or ci_high < 0)
    return {
        "evaluated": n,
        "avg_return_pct": round(mean * 100, 3),
        "win_share": round(wins / n, 2),
        "ci_low_pct": round(ci_low * 100, 3),
        "ci_high_pct": round(ci_high * 100, 3),
        "se_pct": round(se * 100, 3),
        "significant": significant,
        "horizon_hours": horizon_hours,
    }


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
    returns: list[float] = []
    # Erst Episoden deduplizieren (gegen Pseudo-Replikation), dann ältester
    # zuerst bewerten: nur ausgereifte Einträge (t + Horizont in der
    # Vergangenheit) liefern einen Zukunftspreis.
    for v in _dedup_episodes(vetoes, horizon_hours):
        if len(returns) >= max_samples:
            break
        coin, side, t0 = v.get("coin"), v.get("side"), v.get("t")
        p0 = v.get("price") or (price_fn(coin, t0) if coin and t0 else None)
        p1 = price_fn(coin, t0 + horizon_hours * 3600) if coin and t0 else None
        if not p0 or not p1:
            continue
        direction = 1 if side == "LONG" else -1
        returns.append(direction * (p1 / p0 - 1))
    return _return_stats(returns, horizon_hours)


def anomaly_outcomes(anomalies: list[dict], price_fn, horizon_hours: float = 24,
                     max_samples: int = 100) -> dict:
    """Hatten die 'verdächtigen' Wallets recht? Misst die Kursbewegung in ihre
    Positionsrichtung nach `horizon_hours` ab dem Meldezeitpunkt.

    Identische Mechanik wie veto_outcomes (coin/side/price/t je Eintrag) - das
    ist die Datenbasis für die Entscheidung, ob der Scout je ans Copy-Trading darf.
    """
    return veto_outcomes(anomalies, price_fn, horizon_hours, max_samples)


def _validator_verdict(summary: dict, veto_stats: dict | None,
                       shadow_stats: dict | None) -> list[str]:
    """EIN ehrliches Validator-Urteil aus zwei Messungen.

    Veto-Outcome ist ein grober Proxy (Einstieg + 24h stur halten, kein Stop);
    die Shadow-Variante ohne_validator ist die Vollsimulation (gleiche Engine,
    nur ohne Filter). Beide adressieren dieselbe Frage. Widersprechen sie sich,
    ist das fast immer ein Zeichen zu kleiner Stichprobe - dann wird der
    Widerspruch BENANNT, statt zu gegensätzlichen Ratschlägen zu führen.
    Richtung: +1 = Filter schadet (blockt Gewinner), -1 = Filter rettet PnL.
    """
    days = summary.get("days", 0)
    veto_dir = 0
    # Nur ein STATISTISCH signifikantes Veto-Outcome (KI schließt 0 nicht ein)
    # zählt als Signal - sonst runden wir Rauschen zu einem Urteil.
    if veto_stats and veto_stats.get("significant") and veto_stats.get("evaluated", 0) >= 10:
        veto_dir = 1 if veto_stats["avg_return_pct"] > 0 else -1
    shadow_dir, edge = 0, None
    if shadow_stats and shadow_stats.get("baseline"):
        ov = (shadow_stats.get("variants") or {}).get("ohne_validator")
        if ov and ov.get("trades", 0) >= 10:
            edge = (ov["equity"] / shadow_stats["baseline"] - 1) * 100
            shadow_dir = 1 if edge >= 1.0 else -1 if edge <= -1.0 else 0

    if veto_dir == 0 and shadow_dir == 0:
        return []

    if veto_dir * shadow_dir < 0:  # echter Widerspruch
        return [("Validator-Signale WIDERSPRECHEN sich (Veto-Outcome: Filter "
                 + ("schadet" if veto_dir > 0 else "rettet PnL")
                 + f"; Shadow ohne_validator {edge:+.1f}%: Filter "
                 + ("schadet" if shadow_dir > 0 else "rettet PnL")
                 + "). Beide messen unterschiedlich - typisch für eine zu kleine "
                 "Stichprobe. NICHTS abschalten: eine Risiko-Schicht entfernt man nicht "
                 "auf widersprüchlicher Evidenz, erst recht ohne Stress-Regime im Sample.")]

    if veto_dir > 0 or shadow_dir > 0:  # einig: Filter schadet
        conf = "Beide Signale einig" if veto_dir > 0 and shadow_dir > 0 else "Ein Signal (schwach)"
        if days < 14:
            return [f"{conf}: der Validator kostet hier PnL - aber erst {days:.0f} Tage und "
                    "kein Abverkauf im Sample. Vor dem Lockern 2-3 Wochen inkl. Stressphase "
                    "abwarten, dann validation.min_score 2 -> 1 als milder erster Schritt "
                    "(NICHT gleich enabled:false)."]
        return [f"{conf}: der Validator kostet PnL über ein ausreichendes Fenster - "
                "validation.min_score 2 -> 1 testen (Crash-Versicherung bleibt, deshalb "
                "nicht gleich enabled:false)."]

    # einig: Filter rettet PnL
    conf = "Beide Signale einig" if veto_dir < 0 and shadow_dir < 0 else "Ein Signal"
    return [f"{conf}: der Validator rettet PnL (geblockte Trades wären negativ gewesen) - "
            "Filter behalten, ggf. sogar verschärfen."]


def recommendations(summary: dict, veto_stats: dict | None = None,
                    shadow_stats: dict | None = None) -> list[str]:
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

    if summary.get("veto_per_order", 0) > 5:
        recs.extend(_validator_verdict(summary, veto_stats, shadow_stats))

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
