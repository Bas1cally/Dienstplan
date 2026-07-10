"""Shadow-Varianten: alternative Konfigurationen parallel im Schatten testen.

Das Problem beim Tunen: Eine Schraube drehen und Tage warten ist langsam, und
man vergleicht immer gegen einen ANDEREN Marktzeitraum. Shadow-Varianten
lösen das - sie laufen simultan auf exakt denselben Live-Daten:

  Haupt-Buch (baseline)   = aktuelle Config inkl. Validator
  shadow: ohne_validator  = jede geplante Order wird ausgeführt
  shadow: validator_locker= Einstiege schon ab Technik-Score >= 1

Jede Variante rekonsolidiert ihr EIGENES Paper-Konto gegen dieselben
Ziel-Portfolios (gleiche Leader, gleiche Konvergenz, gleiche Preise) - der
einzige Unterschied ist der Einstiegs-Filter. Nach ein paar Tagen zeigt
report.py / das Dashboard, welche Variante vorn liegt: A/B-Test im
Livebetrieb statt Bauchgefühl.

Hinweis zur Näherung: 'validator_locker' nutzt den Technik-Score des echten
Validators (gecacht, keine Extra-API-Calls) und ignoriert dessen finale
min_score-Schwelle - harte RSI-Vetos können dadurch durchrutschen. Für den
Konfigurations-Vergleich ist das gewollt grob.
"""

import json
import logging
import time
from pathlib import Path

from .copytrade.copier import is_exposure_increase, plan_rebalance
from .paper import PaperBroker

log = logging.getLogger(__name__)

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


class ShadowVariant:
    def __init__(self, name: str, broker: PaperBroker, order_filter):
        self.name = name
        self.broker = broker
        self.order_filter = order_filter  # (order) -> bool, nur für Exposure-Erhöhungen


class ShadowFleet:
    def __init__(self, ct, initial_equity: float, fee_rate: float,
                 validator=None, runtime: Path | None = None):
        self.ct = ct
        runtime = runtime or RUNTIME
        self.variants: list[ShadowVariant] = [
            ShadowVariant(
                "ohne_validator",
                PaperBroker(initial_equity, fee_rate, path=runtime / "shadow_ohne_validator.json"),
                lambda o: True,
            ),
        ]
        if validator is not None:
            self.variants.append(ShadowVariant(
                "validator_locker",
                PaperBroker(initial_equity, fee_rate, path=runtime / "shadow_validator_locker.json"),
                lambda o: validator.check(o.coin, is_long=o.target_notional > 0).score >= 1,
            ))
            # Isoliert die These "nur die Trend-Score-Blockade ist teuer": behält
            # die harten RSI-Crash-Vetos, entfernt die Trend-Blockade. Holt diese
            # Variante ohne_validator ein, ist der Score-Block das Problem - dann
            # kann das Haupt-Buch auf crash_only umstellen, OHNE die Versicherung
            # zu verlieren.
            self.variants.append(ShadowVariant(
                "validator_crash_only",
                PaperBroker(initial_equity, fee_rate, path=runtime / "shadow_validator_crash_only.json"),
                lambda o: validator.crash_only(o.coin, o.target_notional > 0),
            ))

    def tick(self, targets: dict[str, float], prices: dict[str, float]) -> None:
        """Rekonsolidiert jede Variante gegen die aktuellen Ziel-Portfolios."""
        for v in self.variants:
            try:
                equity = v.broker.equity(prices)
                orders = plan_rebalance(targets, v.broker.sizes(), prices, equity, self.ct)
                for o in orders:
                    # Flip (long->short) ist ein Einstieg, auch wenn betragsmäßig
                    # kleiner - muss durch den Filter (gleiche Logik wie im Copier).
                    if is_exposure_increase(o.target_notional, o.current_notional) and not v.order_filter(o):
                        continue
                    v.broker.execute(o.coin, o.delta_size, o.price)
            except Exception:
                log.exception("Shadow-Variante %s fehlgeschlagen", v.name)

    def flatten(self, prices: dict[str, float]) -> None:
        """RISK_OFF/Halt gilt für alle Varianten gleichermaßen (fairer Vergleich)."""
        for v in self.variants:
            v.broker.flatten(prices)

    def stats(self, prices: dict[str, float]) -> dict[str, dict]:
        out = {}
        for v in self.variants:
            out[v.name] = {
                "equity": round(v.broker.equity(prices), 2),
                "trades": v.broker.trades,
                "realized_pnl": round(v.broker.realized_pnl, 2),
                "age_days": round(v.broker.age_days, 1),
                # Rendite seit EIGENEM Start - fair unabhängig von der Lebenszeit
                "return_pct": round((v.broker.equity(prices) / v.broker.initial_equity - 1) * 100, 2),
            }
        return out

    def persist_stats(self, prices: dict[str, float]) -> None:
        try:
            RUNTIME.mkdir(exist_ok=True)
            (RUNTIME / "shadows.json").write_text(
                json.dumps({"t": int(time.time()), "variants": self.stats(prices)}, indent=2))
        except OSError:
            pass


def shadow_recommendations(baseline_equity: float, shadows: dict[str, dict],
                           min_trades: int = 10, min_edge_pct: float = 1.0,
                           baseline_age_days: float | None = None,
                           min_age_days: float = 14.0) -> list[str]:
    """Vergleicht Varianten gegen das Haupt-Buch und formuliert Konsequenzen.

    Alters-bewusst: eine Variante, die erst wenige Tage läuft, gegen ein
    langlebiges Haupt-Buch zu vergleichen mischt Zeiträume - der Vorsprung ist
    dann teils Artefakt (die Variante hat frühe Verluste nie mitgemacht). Solche
    Varianten bekommen keine Handlungs-Empfehlung, nur einen Warte-Hinweis.
    """
    recs = []
    for name, s in shadows.items():
        if s["trades"] < min_trades or not baseline_equity:
            continue
        edge = (s["equity"] / baseline_equity - 1) * 100
        age = s.get("age_days")
        too_young = age is not None and (age < min_age_days or
                    (baseline_age_days and age < 0.7 * baseline_age_days))
        if edge >= min_edge_pct:
            if too_young:
                bl = f" (Haupt-Buch ~{baseline_age_days:.0f})" if baseline_age_days else ""
                recs.append(f"Shadow '{name}' liegt SCHEINBAR {edge:+.1f}% vorn, läuft aber erst "
                            f"{age:.0f} Tage{bl} - der Vergleich mischt Lebenszeiten. Erst gleiche "
                            f"Laufzeit (>= {min_age_days:.0f} Tage inkl. Stresstag) abwarten, DANN "
                            f"entscheiden. Seit eigenem Start: {s.get('return_pct', 0):+.2f}%.")
            else:
                action = {
                    "ohne_validator": "validation.enabled: false erwägen (aber Crash-Schutz geht verloren)",
                    "validator_crash_only": "validation.mode: crash_only erwägen (Trend-Block raus, RSI-Versicherung bleibt)",
                    "validator_locker": "validation.min_score 2 -> 1 setzen",
                }.get(name, "Filter dieser Variante übernehmen")
                age_str = f", {age:.0f} Tage" if age is not None else ""
                recs.append(f"Shadow '{name}' liegt {edge:+.1f}% vor dem Haupt-Buch "
                            f"({s['trades']} Trades{age_str}): {action}.")
        elif edge <= -min_edge_pct and not too_young:
            recs.append(f"Shadow '{name}' liegt {edge:+.1f}% HINTER dem Haupt-Buch: "
                        f"aktuelle Filter behalten.")
    return recs
