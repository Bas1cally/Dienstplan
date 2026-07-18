"""Unit-Tests für LARP-Filter, Trade-Rekonstruktion, Sentiment und Schock-Detektor."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from bot.config import ShockConfig
from bot.copytrade.analyzer import analyze_fills, reconstruct_trades
from bot.copytrade.larp import LarpConfig, LarpFilter
from bot.news.sentiment import aggregate_score, score_item
from bot.news.shock import ShockDetector
from bot.news.sources import NewsItem

DAY = 86_400_000
MIN = 60_000


def fill(t, coin="BTC", side="B", sz=1.0, closed=0.0, fee=0.5, start=None):
    f = {"time": t, "coin": coin, "side": side, "sz": str(sz),
         "closedPnl": str(closed), "fee": str(fee)}
    if start is not None:
        f["startPosition"] = str(start)
    return f


# ---------- Trade-Rekonstruktion ----------

def test_reconstruct_simple_long():
    fills = [
        fill(0, side="B", sz=2.0, start=0),                 # open long 2
        fill(90 * MIN, side="A", sz=2.0, closed=500, start=2),  # close
    ]
    trips = reconstruct_trades(fills)
    assert len(trips) == 1
    assert abs(trips[0].holding_minutes - 90) < 1e-9
    assert abs(trips[0].net_pnl - 499) < 1e-9  # 500 - 2x 0.5 Fee


def test_reconstruct_partial_close_and_short():
    fills = [
        fill(0, side="A", sz=4.0, start=0),                   # open short 4
        fill(30 * MIN, side="B", sz=2.0, closed=100, start=-4),  # halb zu
        fill(60 * MIN, side="B", sz=2.0, closed=100, start=-2),  # ganz zu
    ]
    trips = reconstruct_trades(fills)
    assert len(trips) == 1
    assert abs(trips[0].holding_minutes - 60) < 1e-9
    assert abs(trips[0].net_pnl - 198.5) < 1e-9


def test_reconstruct_ignores_preexisting_position():
    # Fenster beginnt mitten in einer Position -> dieser Trade wird verworfen
    fills = [
        fill(0, side="A", sz=3.0, closed=900, start=3),    # schließt Alt-Position
        fill(DAY, side="B", sz=1.0, start=0),              # sauberer neuer Trade
        fill(DAY + 45 * MIN, side="A", sz=1.0, closed=50, start=1),
    ]
    trips = reconstruct_trades(fills)
    assert len(trips) == 1
    assert abs(trips[0].net_pnl - 49.0) < 1e-9  # 50 - 2x 0.5 Fee


def test_analyze_fills_excludes_spot_trades():
    """Nutzer-Fund 18.07. ('tracken wir Wallets nicht, die x2 oder Spot
    handeln?'): userFillsByTime liefert Perp- UND Spot-Fills gemischt, Spot-
    Paare per HL-Konvention mit '@<Index>' statt einem Ticker. Unser Live-
    Tracking (user_state) sieht NUR Perps - ein Wallet, dessen Aktivität rein
    aus Spot-Fills besteht, muss deshalb wie ein KOMPLETT UNAKTIVES Perp-
    Wallet bewertet werden (0 Trips), nicht wie ein aktiver Trader."""
    spot_only = [
        fill(0, coin="@107", side="B", sz=2.0, start=0),
        fill(60 * MIN, coin="@107", side="A", sz=2.0, closed=500, start=2),
    ]
    m = analyze_fills("0xspot", spot_only, account_value=10000, days=30)
    assert m.round_trips == 0 and m.net_pnl == 0 and m.coins == [], \
        "reine Spot-Aktivität darf NICHT als Perp-Aktivität durchgehen"


def test_analyze_fills_keeps_perp_ignores_mixed_in_spot():
    """Ein Wallet, das BEIDES handelt: nur der Perp-Anteil zählt für unsere
    Metriken - der Spot-Anteil ist für Live-Tracking sowieso unsichtbar und
    würde die Zahlen (Trips/Trefferquote/aktive Tage) sonst künstlich aufblähen."""
    mixed = [
        fill(0, coin="BTC", side="B", sz=1.0, start=0),
        fill(90 * MIN, coin="BTC", side="A", sz=1.0, closed=100, start=1),
        fill(2 * DAY, coin="@107", side="B", sz=5.0, start=0),
        fill(2 * DAY + 30 * MIN, coin="@107", side="A", sz=5.0, closed=9_000, start=5),
    ]
    m = analyze_fills("0xmixed", mixed, account_value=10000, days=30)
    assert m.round_trips == 1, "nur der eine Perp-Trip zählt"
    assert abs(m.net_pnl - 99.0) < 1e-9, "Spot-PnL (9000) fließt NICHT ein"
    assert m.coins == ["BTC"]


# ---------- LARP-Filter ----------

def steady_fills(weeks=5, trades_per_week=10, win=80.0, loss=-40.0):
    """Konsistenter Trader: jede Woche profitabel, 2h-Haltedauer, kein Ausreißer."""
    fills = []
    t = 0
    for w in range(weeks):
        for i in range(trades_per_week):
            t = w * 7 * DAY + i * 12 * 3600 * 1000
            pnl = win if i % 3 else loss
            fills.append(fill(t, side="B", sz=1.0, start=0, fee=0.1))
            fills.append(fill(t + 120 * MIN, side="A", sz=1.0, closed=pnl, start=1, fee=0.1))
    return fills


def test_larp_passes_steady_trader():
    m = analyze_fills("0xsteady", steady_fills(), account_value=10000, days=35)
    verdict = LarpFilter().check(m)
    assert verdict.passed, f"Konsistenter Trader fälschlich aussortiert: {verdict.reasons}"


def test_larp_rejects_lucky_punch():
    fills = steady_fills(weeks=5, trades_per_week=6, win=10.0, loss=-5.0)
    # Ein Mega-Trade dominiert den Gesamt-PnL
    fills.append(fill(36 * DAY, side="B", sz=5.0, start=0))
    fills.append(fill(36 * DAY + 60 * MIN, side="A", sz=5.0, closed=5000, start=5))
    m = analyze_fills("0xluck", fills, account_value=10000, days=40)
    verdict = LarpFilter().check(m)
    assert not verdict.passed
    assert any("Lucky Punch" in r for r in verdict.reasons)


def test_larp_rejects_scalper():
    fills = []
    for i in range(50):
        t = i * 6 * 3600 * 1000
        fills.append(fill(t, side="B", sz=1.0, start=0, fee=0.1))
        fills.append(fill(t + 3 * MIN, side="A", sz=1.0, closed=30, start=1, fee=0.1))
    m = analyze_fills("0xscalp", fills, account_value=10000, days=15)
    verdict = LarpFilter().check(m)
    assert not verdict.passed
    assert any("Scalper" in r for r in verdict.reasons)


def test_larp_rejects_thin_history():
    fills = steady_fills(weeks=1, trades_per_week=5)
    m = analyze_fills("0xthin", fills, account_value=10000, days=30)
    verdict = LarpFilter().check(m)
    assert not verdict.passed
    assert any("Round-Trips" in r for r in verdict.reasons)


def test_larp_rejects_overleveraged():
    m = analyze_fills("0xdegen", steady_fills(), account_value=10000, days=35)
    m.max_drawdown = 0.40
    verdict = LarpFilter().check(m)
    assert not verdict.passed
    assert any("überhebelt" in r for r in verdict.reasons)


# ---------- Sprint-Gate: Richtung zählt, Profit-Größe nicht ----------

def test_sprint_gate_passes_main_larp_kos():
    """Lucky-Puncher + moderat Überhebelter: fürs HAUPTBUCH K.O., fürs Sprint-
    Buch ok - Sprint nimmt +10% und ist raus, der Profit des Leaders selbst
    ist egal. ABER (Nutzer-Fund 17.07., Gambler-Filter): Drawdown ist NICHT
    mehr unbegrenzt egal - 40% liegt hier noch unter der lockeren Sprint-
    Schwelle (50%), siehe test_sprint_gate_rejects_gambler_extreme_drawdown."""
    from bot.copytrade.larp import check_sprint

    fills = steady_fills(weeks=5, trades_per_week=6, win=10.0, loss=-5.0)
    fills.append(fill(36 * DAY, side="B", sz=5.0, start=0))
    fills.append(fill(36 * DAY + 60 * MIN, side="A", sz=5000, closed=5000, start=5))
    m = analyze_fills("0xluck", fills, account_value=10000, days=40)
    m.max_drawdown = 0.40   # moderat überhebelt - unter der Sprint-Gambler-Schwelle
    assert not LarpFilter().check(m).passed, "Hauptbuch lehnt ab"
    v = check_sprint(m)
    assert v.passed, f"Sprint-Gate muss durchlassen: {v.reasons}"


def test_sprint_gate_rejects_gambler_extreme_drawdown():
    """Nutzer-Fund (17.07., Live-Vorfall): eine Wallet mit 90% Trefferquote und
    53 Trips kam mit 61% historischem Drawdown locker durch Pfad A - '90%
    Trefferquote, 53 Trips, aber das ist ganz klar ein Gambler'. Harter K.O.
    VOR beiden Qualifikationspfaden, unabhängig davon wie gut Trefferquote/
    Aktivität sonst aussehen - stabile Richtungs-Erkennung reicht nicht,
    wenn das Konto selbst schon mal fast geplatzt ist."""
    from bot.copytrade.larp import check_sprint

    m = analyze_fills("0xgambler", steady_fills(), account_value=10000, days=35)
    assert check_sprint(m).passed, "Vorbedingung: ohne Drawdown-Übertreibung durchlässig"
    m.max_drawdown = 0.61
    v = check_sprint(m)
    assert not v.passed and any("Gambler" in r for r in v.reasons)


def test_sprint_gate_looser_active_days_than_main_book():
    """Nutzer-Fund (17.07.): min_active_days war der größte Volumen-Killer im
    Sprint-Funnel - starke Trefferquote-Kandidaten fielen laufend nur wegen
    'nur 2-8 aktive Tage (< 10)' raus, weil bisher dieselbe Hauptbuch-Schwelle
    galt. Sprint braucht Richtungs-Beweis, keine lange Historie."""
    from bot.copytrade.larp import check_sprint

    fills = steady_fills(weeks=1, trades_per_week=10)
    m = analyze_fills("0xshort_history", fills, account_value=10000, days=35)
    assert m.active_days < 10, f"Vorbedingung: unter Hauptbuch-Schwelle, war {m.active_days}"
    assert not LarpFilter().check(m).passed, "Hauptbuch lehnt wegen zu kurzer Historie ab"
    v = check_sprint(m)
    assert v.passed, f"Sprint-Gate muss trotzdem durchlassen: {v.reasons}"


def test_sprint_gate_rejects_systematic_loser_allows_coinflip():
    """Philosophie-Wechsel (Nutzer): Strikes sind der echte Filter - ein
    Münzwurf-Trader (50%) darf in den PAPIER-Pool (max. 2 Verlust-Ritte, dann
    Bann), nur SYSTEMATISCHE Falsch-Trader (deutlich unter 45%) bleiben draußen."""
    from bot.copytrade.larp import check_sprint

    def trader(win_every):
        fills = []
        for i in range(40):
            t = i * 18 * 3600 * 1000
            pnl = 300.0 if i % win_every == 0 else -30.0
            fills.append(fill(t, side="B", sz=1.0, start=0, fee=0.1))
            fills.append(fill(t + 120 * MIN, side="A", sz=1.0, closed=pnl, start=1, fee=0.1))
        return analyze_fills("0x", fills, account_value=10000, days=30)

    loser = trader(win_every=3)     # ~33% Trefferquote: systematisch falsch
    v = check_sprint(loser)
    assert not v.passed and any("Trefferquote" in r for r in v.reasons)

    coinflip = trader(win_every=2)  # 50%: rein damit, Strikes urteilen
    v_flip = check_sprint(coinflip)
    assert v_flip.passed, "Münzwurf darf in den Papier-Pool"
    assert v_flip.path == "A", "aktiver Trader qualifiziert über Pfad A"


def test_sprint_gate_still_rejects_scalper_and_thin_history():
    """Anti-Zufalls-Gates bleiben rigoros: Scalper (kein 1%-Fenster) und dünne
    Historie (Zufall) fliegen auch fürs Sprint-Buch raus."""
    from bot.copytrade.larp import check_sprint

    scalp = []
    for i in range(50):
        t = i * 6 * 3600 * 1000
        scalp.append(fill(t, side="B", sz=1.0, start=0, fee=0.1))
        scalp.append(fill(t + 3 * MIN, side="A", sz=1.0, closed=30, start=1, fee=0.1))
    m = analyze_fills("0xscalp", scalp, account_value=10000, days=15)
    assert not check_sprint(m).passed

    thin = steady_fills(weeks=1, trades_per_week=5)
    m2 = analyze_fills("0xthin", thin, account_value=10000, days=30)
    v2 = check_sprint(m2)
    assert not v2.passed and any("Round-Trips" in r for r in v2.reasons)


def test_sprint_gate_position_path_green_open_book():
    """Positions-Trader (Live-Befund: 25/34 'netto unprofitabel', weil der
    Gewinn unrealisiert läuft): 1 Round-Trip, realisiert negativ - aber offenes
    Buch satt im Plus -> Sprint-tauglich über den Positions-Pfad."""
    from bot.copytrade.analyzer import TraderMetrics, _sprint_score
    from bot.copytrade.larp import check_sprint

    m = TraderMetrics(address="0xswing", account_value=200_000, days=30)
    m.round_trips, m.win_rate = 1, 0.0          # Aktiv-Pfad chancenlos
    m.net_pnl = -800.0                          # realisiert: kleine Verluste
    m.open_positions, m.open_green_share = 2, 0.85
    m.open_unrealized = 45_000.0                # der Gewinn LÄUFT noch
    v = check_sprint(m)
    assert v.passed, f"grünes offenes Buch muss qualifizieren: {v.reasons}"
    assert v.path == "B", "Positions-Trader qualifiziert über Pfad B, nicht A"
    m.sprint_score = _sprint_score(m)
    assert m.sprint_score >= 25, "Positions-Pfad muss auch den Score-Boden schaffen"

    m.open_green_share = 0.30                   # rotes Buch -> Richtung falsch
    assert not check_sprint(m).passed
    m.open_green_share, m.open_unrealized = 0.85, -60_000.0
    assert not check_sprint(m).passed, "Gesamt-PnL inkl. unrealisiert muss > 0 sein"


def test_sprint_score_ranks_direction_over_profit():
    """Hoher Richtungs-Treffer schlägt hohen Profit: der 65%-Trefferquoten-
    Trader mit kleinen Gewinnen rankt fürs Sprint-Buch über dem ROI-Monster
    mit 53% Quote - auch wenn der Haupt-Score es andersrum sieht."""
    from bot.copytrade.analyzer import TraderMetrics, _sprint_score

    sniper = TraderMetrics(address="0xsniper", account_value=20_000, days=30)
    sniper.win_rate, sniper.round_trips = 0.65, 60
    sniper.profitable_day_share, sniper.active_days = 0.6, 20
    whale = TraderMetrics(address="0xwhale", account_value=1_000_000, days=30)
    whale.win_rate, whale.round_trips = 0.53, 60
    whale.profitable_day_share, whale.active_days = 0.6, 20
    whale.roi, whale.profit_factor = 3.0, 4.0   # Profit-Monster
    assert _sprint_score(sniper) > _sprint_score(whale)


def test_analyzer_window_ladder_for_active_traders():
    """Fenster-LEITER 30->7->2->1 Tage: wir messen in FILLS, nicht Trades -
    aktive Trader mit vielen Teil-Ausführungen sprengen auch 7d. Statt sie
    ungewertet wegzuwerfen ('zu aktiv'), wird das Fenster verkürzt, bis es
    passt. Nur wer selbst EINEN Tag sprengt, ist wirklich HFT."""
    from bot.copytrade.analyzer import TraderAnalyzer

    def mk_fills(n):
        return [fill(i * MIN, closed=(5.0 if i % 2 else -4.0), start=0)
                for i in range(n)]

    class _Info:
        def __init__(self, batches):
            self.calls = []
            self.batches = batches

        def user_fills_by_time(self, addr, start, end):
            self.calls.append((start, end))
            return self.batches[min(len(self.calls) - 1, len(self.batches) - 1)]

        def user_state(self, addr):
            return {"marginSummary": {"accountValue": "50000"}}

    # 30d voll -> 7d misst sauber
    info = _Info([mk_fills(2000), mk_fills(300)])
    m = TraderAnalyzer(info, days=30, throttle_s=0).analyze("0xactive")
    assert len(info.calls) == 2 and m.days == 7 and not m.fills_truncated
    assert (info.calls[0][1] - info.calls[0][0]) > (info.calls[1][1] - info.calls[1][0])
    assert m.open_positions == 0 and m.open_green_share == 0.0

    # 30d UND 7d voll -> 2d misst sauber (vorher: ungewertet 'zu aktiv')
    info2 = _Info([mk_fills(2000), mk_fills(2000), mk_fills(500)])
    m2 = TraderAnalyzer(info2, days=30, throttle_s=0).analyze("0xbusy")
    assert len(info2.calls) == 3 and m2.days == 2 and not m2.fills_truncated

    # selbst 1d voll (2000+ Fills/Tag) -> wirklich HFT, bleibt 'zu aktiv'
    info3 = _Info([mk_fills(2000)])
    m3 = TraderAnalyzer(info3, days=30, throttle_s=0).analyze("0xhft")
    assert len(info3.calls) == 4, "Leiter komplett durchprobiert (30/7/2/1)"
    assert m3.days == 1 and m3.fills_truncated


def test_analyzer_parses_open_book_from_user_state():
    """Offene Positionen kommen aus derselben user_state-Antwort (kein Extra-
    Call) und speisen den Positions-Pfad des Sprint-Gates."""
    from bot.copytrade.analyzer import TraderAnalyzer

    class _Info:
        def user_fills_by_time(self, addr, start, end):
            return [fill(0, closed=10, start=0)]

        def user_state(self, addr):
            return {"marginSummary": {"accountValue": "80000"},
                    "assetPositions": [
                        {"position": {"szi": "2.0", "positionValue": "60000",
                                      "unrealizedPnl": "4200"}},
                        {"position": {"szi": "-1.0", "positionValue": "20000",
                                      "unrealizedPnl": "-500"}},
                        {"position": {"szi": "0", "positionValue": "0",
                                      "unrealizedPnl": "0"}},   # flach: ignorieren
                    ]}

    m = TraderAnalyzer(_Info(), days=30, throttle_s=0).analyze("0xbook")
    assert m.open_positions == 2
    assert abs(m.open_green_share - 0.75) < 1e-9, "60k von 80k im Plus"
    assert abs(m.open_unrealized - 3700) < 1e-9


# ---------- Sentiment ----------

def item(title, age_min=0, now=1_000_000_000_000):
    return NewsItem(source="test", title=title, time_ms=now - age_min * MIN)


def test_sentiment_critical_news():
    s = score_item(item("Major exchange hacked, $400 million stolen from hot wallets"))
    assert s.score >= 9


def test_sentiment_trump_tariff_boosted():
    plain = score_item(item("New tariff package announced against chip imports"))
    trump = score_item(item("Trump announces new tariff package against chip imports"))
    assert trump.score > plain.score >= 5


def test_sentiment_benign_news_low():
    s = score_item(item("Bitcoin ETF inflows continue as institutions accumulate"))
    assert s.score < 2


def test_sentiment_decay():
    now = 1_000_000_000_000
    fresh = [score_item(item("Markets crash as war fears escalate", age_min=0, now=now))]
    old = [score_item(item("Markets crash as war fears escalate", age_min=120, now=now))]
    assert aggregate_score(fresh, half_life_minutes=30, now_ms=now) >= 8
    assert aggregate_score(old, half_life_minutes=30, now_ms=now) < 1


# ---------- Schock-Detektor ----------

def candles_1m(closes):
    return pd.DataFrame({"close": closes})


def test_shock_detects_crash():
    np.random.seed(1)
    calm = list(100000 * np.exp(np.cumsum(np.random.normal(0, 0.0003, 100))))
    crash = calm + [calm[-1] * (1 - 0.01 * i) for i in range(1, 6)]  # -5% in 5min
    det = ShockDetector(ShockConfig())
    state = det.check(candles_1m(crash), now=1000)
    assert state.triggered
    assert state.move_pct < -0.025


def test_shock_quiet_market_no_trigger():
    np.random.seed(2)
    calm = list(100000 * np.exp(np.cumsum(np.random.normal(0, 0.0003, 120))))
    det = ShockDetector(ShockConfig())
    assert not det.check(candles_1m(calm), now=1000).triggered


def test_shock_cooldown_persists():
    np.random.seed(3)
    calm = list(100000 * np.exp(np.cumsum(np.random.normal(0, 0.0003, 100))))
    crash = calm + [calm[-1] * (1 - 0.01 * i) for i in range(1, 6)]
    det = ShockDetector(ShockConfig(cooldown_minutes=30))
    assert det.check(candles_1m(crash), now=1000).triggered
    # Markt wieder ruhig, aber Cooldown hält RISK_OFF aufrecht
    assert det.check(candles_1m(calm), now=1000 + 60).triggered
    assert not det.check(candles_1m(calm), now=1000 + 31 * 60).triggered


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
