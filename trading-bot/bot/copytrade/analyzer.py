"""Quantitative Analyse einzelner Trader anhand ihrer öffentlichen Fill-Historie.

Warum nicht einfach das Leaderboard-Ranking übernehmen? Weil dort ein einziger
Glückstreffer mit 40x Leverage ganz oben stehen kann. Wir wollen Trader, deren
Edge REPRODUZIERBAR ist. Dafür zerlegen wir die Fill-Historie in Kennzahlen:

  Profitabilität   - Netto-PnL (nach Fees!) relativ zum Kontowert
  Treffsicherheit  - Win-Rate und Profit Factor über viele Trades
  Konsistenz       - Anteil profitabler Tage (ein guter Monat != ein guter Trader)
  Risiko           - Max Drawdown der realisierten PnL-Kurve
  Stichprobe       - genug Trades, um Zufall auszuschließen

Der Score gewichtet diese Dimensionen und bestraft hartes Risiko überproportional.
"""

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class TraderMetrics:
    address: str
    account_value: float
    days: int
    # Profitabilität
    realized_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    roi: float = 0.0                # net_pnl / account_value
    # Treffsicherheit
    closed_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    # Konsistenz
    active_days: int = 0
    profitable_day_share: float = 0.0
    # Risiko
    max_drawdown: float = 0.0       # relativ zum Kontowert, >= 0
    # Aktivität
    coins: list[str] = field(default_factory=list)
    trades_per_day: float = 0.0
    # Aktualität: Zeitpunkt des JÜNGSTEN Fills (ms) und daraus abgeleitet, wie
    # viele Tage die Wallet schon NICHT mehr getradet hat. Gegen "historisch
    # gut, jetzt Schläfer": ein Trader, der in Woche 1 des Fensters aktiv war
    # und seither still, besteht sonst das Gate über Alt-Aktivität und blockiert
    # als toter Slot den Pool (Nutzer-Befund: 20 flache Schläfer-Wallets).
    last_fill_ms: int = 0
    days_since_last_trade: float = 999.0
    # LARP-relevante Tiefenmetriken (aus rekonstruierten Round-Trips)
    round_trips: int = 0
    median_holding_minutes: float = 0.0
    max_trade_share: float = 1.0    # Anteil des größten Trades am Brutto-Gewinn
    profitable_week_share: float = 0.0
    score: float = 0.0
    # Richtungs-Score fürs Sprint-Buch: dort zählt NICHT, wie viel Profit der
    # Leader selbst aus dem Trade holt (wir nehmen +10% und sind raus), sondern
    # wie oft er die Richtung richtig erkennt. Gewichtet Trefferquote statt ROI.
    sprint_score: float = 0.0
    # Offenes Buch (aus user_state, kein Extra-API-Call): Positions-Trader
    # realisieren selten - ihr Richtungs-Beweis sitzt UNREALISIERT in offenen
    # Positionen. Fills-Metriken bestrafen sonst genau "Gewinner laufen lassen".
    open_positions: int = 0
    open_green_share: float = 0.0   # wertgewichteter Anteil der Positionen im Plus
    open_unrealized: float = 0.0
    # userFillsByTime liefert max ~2000 Fills: ist das Fenster voll, sehen wir nur
    # einen Ausschnitt - alle Metriken wären verzerrt (betrifft HFT/MM-Konten).
    fills_truncated: bool = False


@dataclass
class RoundTrip:
    """Ein vollständiger Trade: Position von 0 auf 0 zurück."""
    coin: str
    open_time: int
    close_time: int
    net_pnl: float

    @property
    def holding_minutes(self) -> float:
        return (self.close_time - self.open_time) / 60_000


def reconstruct_trades(fills: list[dict]) -> list[RoundTrip]:
    """Rekonstruiert Round-Trips aus der Fill-Historie.

    Läuft die signierte Positionsgröße je Coin mit: Übergang 0 -> !=0 öffnet
    einen Trade, Rückkehr auf ~0 schließt ihn. Teilschließungen sammeln sich
    im selben Round-Trip. Positionen, die vor dem Fenster geöffnet wurden
    (Start mitten im Trade), werden verworfen - lieber weniger, aber saubere
    Datenpunkte.
    """
    trades: list[RoundTrip] = []
    pos: dict[str, float] = {}
    open_time: dict[str, int] = {}
    pnl: dict[str, float] = defaultdict(float)
    dirty: set[str] = set()  # Coin startete mitten in einer Alt-Position

    for f in sorted(fills, key=lambda f: f["time"]):
        coin = f["coin"]
        t = int(f["time"])
        delta = float(f["sz"]) * (1 if f.get("side") == "B" else -1)
        if coin not in pos:  # erster Fill: Laufposition aus startPosition seeden
            pos[coin] = float(f.get("startPosition", 0))
            if pos[coin] != 0:
                dirty.add(coin)
        prev = pos[coin]
        new = prev + delta
        if abs(new) < 1e-9 * max(abs(delta), 1.0):
            new = 0.0
        pos[coin] = new

        if prev == 0 and new != 0:
            open_time[coin] = t
            pnl[coin] = 0.0
        pnl[coin] += float(f.get("closedPnl", 0)) - float(f.get("fee", 0))

        if prev != 0 and new == 0:
            if coin in dirty:  # Alt-Position abgeschlossen: verwerfen, ab jetzt sauber
                dirty.discard(coin)
            elif coin in open_time:
                trades.append(RoundTrip(coin, open_time.pop(coin), t, pnl[coin]))
            pnl[coin] = 0.0
    return trades


def analyze_fills(address: str, fills: list[dict], account_value: float, days: int) -> TraderMetrics:
    """Berechnet Metriken aus rohen userFillsByTime-Fills (zeitlich sortiert)."""
    m = TraderMetrics(address=address, account_value=account_value, days=days)
    if not fills or account_value <= 0:
        return m

    fills = sorted(fills, key=lambda f: f["time"])
    daily_pnl: dict[str, float] = defaultdict(float)
    coins: set[str] = set()
    gross_win = gross_loss = 0.0
    wins = 0
    cum = peak = 0.0

    for f in fills:
        coins.add(f["coin"])
        closed = float(f.get("closedPnl", 0))
        fee = float(f.get("fee", 0))
        m.fees += fee
        net = closed - fee
        day = _day_key(int(f["time"]))
        daily_pnl[day] += net

        if closed != 0:  # schließender Fill = abgeschlossener (Teil-)Trade
            m.realized_pnl += closed
            m.closed_trades += 1
            if closed > 0:
                wins += 1
                gross_win += closed
            else:
                gross_loss += -closed

        # Drawdown auf der realisierten Netto-Kurve
        cum += net
        peak = max(peak, cum)
        m.max_drawdown = max(m.max_drawdown, (peak - cum) / account_value)

    m.net_pnl = m.realized_pnl - m.fees
    m.roi = m.net_pnl / account_value
    m.coins = sorted(coins)
    m.last_fill_ms = int(fills[-1]["time"])   # fills sind zeitlich sortiert
    m.active_days = len(daily_pnl)
    m.trades_per_day = len(fills) / max(days, 1)
    if m.closed_trades:
        m.win_rate = wins / m.closed_trades
    m.profit_factor = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    if daily_pnl:
        m.profitable_day_share = sum(1 for v in daily_pnl.values() if v > 0) / len(daily_pnl)

    # Tiefenmetriken aus Round-Trips
    trips = reconstruct_trades(fills)
    m.round_trips = len(trips)
    if trips:
        holdings = sorted(t.holding_minutes for t in trips)
        m.median_holding_minutes = holdings[len(holdings) // 2]
        winners = [t.net_pnl for t in trips if t.net_pnl > 0]
        if winners:
            m.max_trade_share = max(winners) / sum(winners)

    # Wochen-Konsistenz: Anteil profitabler Kalenderwochen (nur aktive Wochen)
    weekly: dict[str, float] = defaultdict(float)
    for day, v in daily_pnl.items():
        from datetime import datetime
        iso = datetime.strptime(day, "%Y-%m-%d").isocalendar()
        weekly[f"{iso[0]}-W{iso[1]:02d}"] += v
    if weekly:
        m.profitable_week_share = sum(1 for v in weekly.values() if v > 0) / len(weekly)

    m.score = _score(m)
    m.sprint_score = _sprint_score(m)
    return m


def _day_key(ts_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _score(m: TraderMetrics) -> float:
    """Composite-Score 0..100. Konservativ: Risiko und dünne Datenlage drücken hart."""
    if m.closed_trades < 5 or m.net_pnl <= 0:
        return 0.0

    # ROI: bei 10% im Fenster voll ausgereizt (alles darüber ist meist Leverage-Glück)
    roi_part = min(m.roi / 0.10, 1.0)
    # Profit Factor: 2.0 gilt als exzellent
    pf = min(m.profit_factor, 5.0)
    pf_part = min(pf / 2.0, 1.0)
    consistency_part = m.profitable_day_share
    # Drawdown: 15% relativ zum Konto = Score-Anteil 0
    risk_part = max(0.0, 1.0 - m.max_drawdown / 0.15)
    # Stichprobengröße: ab 50 abgeschlossenen Trades volle Punktzahl
    sample = min(m.closed_trades / 50.0, 1.0)

    raw = (0.30 * roi_part + 0.25 * pf_part + 0.20 * consistency_part + 0.25 * risk_part)
    return round(100 * raw * (0.5 + 0.5 * sample), 1)


def _sprint_score(m: TraderMetrics) -> float:
    """Richtungs-Score 0..100 fürs Sprint-Buch (Nutzer-Vorgabe: 'das wichtigste
    ist nicht wie viel Profit sie machen, sondern dass sie die richtige Richtung
    erkennen'). Sprint nimmt +10% (bei 10x = ~1% Kursbewegung) und ist raus -
    ROI-Größe, Lucky-Punch-Anteil und Drawdown des Leaders sind dafür egal.

    Zwei Wege, Richtung zu beweisen (es zählt der bessere):
    - Fills-Pfad: Trefferquote geschlossener Trades + Konsistenz/Stichprobe.
    - Positions-Pfad: grünes offenes Buch. Positions-Trader realisieren selten
      (Gewinner laufen lassen!) - ihre laufenden Positionen im Plus sind der
      direkteste Richtungs-Beweis, den es gibt."""
    fills_part = 0.0
    if m.round_trips >= 5 and m.win_rate > 0:
        # Trefferquote: 50% = Münzwurf = 0 Punkte, ab 65% voll ausgereizt
        wr_part = max(0.0, min((m.win_rate - 0.50) / 0.15, 1.0))
        fills_part = 100 * (0.50 * wr_part + 0.20 * m.profitable_day_share
                            + 0.20 * min(m.round_trips / 60.0, 1.0)
                            + 0.10 * min(m.active_days / max(m.days, 1), 1.0))
    pos_part = 0.0
    if m.open_positions > 0:
        # 50% grün = Münzwurf = 0; Breite (mehrere grüne Coins) gibt Bonus
        green_part = max(0.0, min((m.open_green_share - 0.5) / 0.5, 1.0))
        breadth = min(m.open_positions / 4.0, 1.0)
        pos_part = 100 * green_part * (0.70 + 0.30 * breadth)
    return round(max(fills_part, pos_part), 1)


class TraderAnalyzer:
    """Holt Fill-Historien über die Info-API und bewertet Trader.

    Rate-Limit-Hygiene: Hyperliquid drosselt pro IP. Zwischen den Wallets wird
    deshalb pausiert (`throttle_s`), und ein 429 wird mit Backoff wiederholt
    statt die Wallet still zu verwerfen.
    """

    def __init__(self, info, days: int, throttle_s: float = 1.0):
        self.info = info
        self.days = days
        self.throttle_s = throttle_s

    def _call(self, fn, *args, tries: int = 5):
        import time as _time

        delay = 2.0
        for attempt in range(tries):
            try:
                return fn(*args)
            except Exception as e:
                rate_limited = getattr(e, "status_code", None) == 429 or "429" in str(e)[:120]
                if not rate_limited or attempt == tries - 1:
                    raise
                log.warning("Rate-Limit (429) - warte %.0fs und versuche erneut", delay)
                _time.sleep(delay)
                delay = min(delay * 2, 60.0)

    def analyze(self, address: str) -> TraderMetrics:
        import time as _time

        end = int(_time.time() * 1000)
        start = end - self.days * 86_400_000
        fills = self._call(self.info.user_fills_by_time, address, start, end)
        days = self.days
        # Fenster-LEITER statt Wegwerfen: HL deckelt userFills auf ~2000, und
        # wir messen in FILLS, nicht Trades - ein aktiver Trader mit 50 Trades/
        # Tag erzeugt über Teil-Ausführungen locker 500+ Fills/Tag und sprengt
        # sogar 7 Tage (Live-Befund: 27 von 70 Kandidaten 'zu aktiv', darunter
        # genau die Wochen-Board-Aktiven, die wir holen wollten). Also so lange
        # verkürzen, bis das Fenster passt: 30 -> 7 -> 2 -> 1 Tage. Nur wer
        # selbst EINEN Tag sprengt (2000+ Fills/Tag), ist wirklich HFT/MM.
        for shorter in (7, 2, 1):
            if len(fills or []) < 2000 or shorter >= days:
                break
            fills = self._call(self.info.user_fills_by_time,
                               address, end - shorter * 86_400_000, end)
            days = shorter
        state = self._call(self.info.user_state, address)
        account_value = float(state["marginSummary"]["accountValue"])
        m = analyze_fills(address, fills, account_value, days)
        m.fills_truncated = len(fills or []) >= 2000
        # Offenes Buch aus derselben user_state-Antwort (kein Extra-Call):
        # Richtungs-Beweis für Positions-Trader, deren Gewinn unrealisiert läuft.
        total_val = green_val = unrealized = 0.0
        n_open = 0
        for p in state.get("assetPositions", []):
            pos = p.get("position", {})
            try:
                if float(pos.get("szi", 0) or 0) == 0:
                    continue
                val = abs(float(pos.get("positionValue", 0) or 0))
                upnl = float(pos.get("unrealizedPnl", 0) or 0)
            except (TypeError, ValueError):
                continue
            n_open += 1
            total_val += val
            unrealized += upnl
            if upnl > 0:
                green_val += val
        m.open_positions = n_open
        m.open_unrealized = unrealized
        m.open_green_share = green_val / total_val if total_val > 0 else 0.0
        # Aktualität relativ zu JETZT: Tage seit dem jüngsten Fill (für den
        # Schläfer-Filter im Pool-Aufbau). Kein Fill im Fenster -> bleibt beim
        # Default 999 (= uralt), wird also als Schläfer behandelt.
        if m.last_fill_ms:
            m.days_since_last_trade = (end - m.last_fill_ms) / 86_400_000
        m.sprint_score = _sprint_score(m)   # mit Positions-Pfad neu bewerten
        return m

    def rank(self, addresses: list[str], min_score: float, larp=None,
             report: dict | None = None) -> list[TraderMetrics]:
        """Bewertet Wallets; `larp` (LarpFilter) sortiert Blender vorab hart aus.

        `report` (optional, mutable) sammelt die Trichter-Diagnose: analyzed/
        errors/larp_ko/truncated-Zähler und die Top-Scores ALLER Bewerteten -
        damit sichtbar ist, WARUM Kandidaten sterben (statt nur '0 qualifiziert')."""
        import time as _time

        rep = report if report is not None else {}
        rep.setdefault("analyzed", 0); rep.setdefault("errors", 0)
        rep.setdefault("larp_ko", 0); rep.setdefault("truncated", 0)
        rep.setdefault("scores", []); rep.setdefault("metrics", [])
        rep.setdefault("larp_reasons", {})
        results = []
        for i, addr in enumerate(addresses):
            if i and self.throttle_s:
                _time.sleep(self.throttle_s)
            try:
                m = self.analyze(addr)
                log.info("Analysiert %s: score=%.1f sprint=%.1f wr=%.0f%% roi=%.1f%% pf=%.2f trips=%d dd=%.1f%% hold=%.0fmin%s",
                         addr[:10], m.score, m.sprint_score, m.win_rate * 100,
                         m.roi * 100, m.profit_factor, m.round_trips,
                         m.max_drawdown * 100, m.median_holding_minutes,
                         " [Fills-Fenster VOLL]" if m.fills_truncated else "")
                rep["analyzed"] += 1
                if m.fills_truncated:
                    # Auch das 7-Tage-Fenster voll (~285+ Fills/Tag): echtes
                    # HFT/MM - unmessbar UND unkopierbar (Churn frisst Fees).
                    rep["truncated"] += 1
                    continue
                # ALLE messbaren Wallets fürs Sprint-Pool-Gate aufheben - der
                # Sprint-Pool nutzt ein eigenes, richtungs-orientiertes LARP-Gate
                # und darf Haupt-LARP-K.O.s enthalten (z.B. Swing-Trader).
                rep["metrics"].append(m)
                if larp:
                    verdict = larp.check(m)
                    if not verdict.passed:
                        log.info("  LARP-Filter K.O. für %s: %s", addr[:10], "; ".join(verdict.reasons))
                        rep["larp_ko"] += 1
                        key = verdict.reasons[0].split(":")[0].split(" (")[0][:24]
                        rep["larp_reasons"][key] = rep["larp_reasons"].get(key, 0) + 1
                        continue
                rep["scores"].append((addr, m.score))
                if m.score >= min_score:
                    results.append(m)
            except Exception:
                rep["errors"] += 1
                log.exception("Analyse fehlgeschlagen für %s", addr)
        rep["scores"].sort(key=lambda t: t[1], reverse=True)
        results.sort(key=lambda m: m.score, reverse=True)
        return results
