"""Unit-Tests für die Copy-Trading-Logik (ohne Netzwerkzugriff)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile

from bot.config import AnalysisConfig, CopytradeConfig, RiskConfig
from bot.copytrade.analyzer import analyze_fills
from bot.copytrade.copier import CopyTrader, compute_targets, plan_rebalance
from bot.copytrade.tracker import LeaderPosition, LeaderSnapshot

DAY = 86_400_000

RISK = RiskConfig(risk_per_trade=0.01, atr_stop_mult=2.0, take_profit_r=2.0,
                  max_leverage=3, max_daily_loss=0.05, slippage=0.005)
CT = CopytradeConfig(analysis=AnalysisConfig())


def fill(t, coin="BTC", closed=0.0, fee=1.0):
    side = "A" if closed else "B"  # schließende Fills verkaufen, öffnende kaufen
    return {"time": t, "coin": coin, "closedPnl": str(closed), "fee": str(fee),
            "side": side, "sz": "1.0", "startPosition": "0"}


# ---------- Analyzer ----------

def test_analyzer_basic_metrics():
    fills = []
    t = 0
    # 10 Tage: je 1 Gewinn (+300) und 1 Verlust (-100), Fees 1 USD pro Fill
    for d in range(10):
        t = d * DAY
        fills.append(fill(t, closed=300))
        fills.append(fill(t + 1000, closed=-100))
    m = analyze_fills("0xabc", fills, account_value=10000, days=30)
    assert m.closed_trades == 20
    assert m.win_rate == 0.5
    assert abs(m.realized_pnl - 2000) < 1e-9
    assert abs(m.fees - 20) < 1e-9
    assert abs(m.net_pnl - 1980) < 1e-9
    assert abs(m.profit_factor - 3.0) < 1e-9
    assert m.profitable_day_share == 1.0       # jeder Tag netto positiv
    assert m.score > 50, f"Konsistenter Trader muss hoch scoren, war {m.score}"


def test_analyzer_lucky_punch_scores_low():
    # 1 riesiger Gewinn nach 6 Verlusten: kaum Stichprobe, niedrige Konsistenz
    fills = [fill(d * DAY, closed=-200) for d in range(6)]
    fills.append(fill(7 * DAY, closed=5000))
    lucky = analyze_fills("0xluck", fills, account_value=10000, days=30)

    steady = analyze_fills(
        "0xsteady",
        [fill(d * DAY, closed=80 if d % 3 else -40) for d in range(60)],
        account_value=10000,
        days=30,
    )
    assert lucky.score < steady.score, "Lucky Punch darf konsistenten Trader nicht schlagen"


def test_analyzer_drawdown():
    # -1500 kumuliert bei 10000 Konto = 15% Drawdown -> Risiko-Anteil 0
    fills = [fill(d * DAY, closed=-500, fee=0) for d in range(3)]
    fills += [fill((d + 3) * DAY, closed=400, fee=0) for d in range(5)]
    m = analyze_fills("0xdd", fills, account_value=10000, days=30)
    assert abs(m.max_drawdown - 0.15) < 1e-9
    assert m.closed_trades == 8


def test_analyzer_unprofitable_scores_zero():
    fills = [fill(d * DAY, closed=-50) for d in range(20)]
    m = analyze_fills("0xloss", fills, account_value=10000, days=30)
    assert m.score == 0.0


# ---------- Tracker / Exposure ----------

def snap(addr, equity, **positions):
    pos = {
        coin: LeaderPosition(coin=coin, size=size, entry=100.0,
                             position_value=abs(size) * 100.0, leverage=2)
        for coin, size in positions.items()
    }
    return LeaderSnapshot(address=addr, equity=equity, positions=pos)


def test_exposure_signed():
    s = snap("0xa", 1000, BTC=5, ETH=-3)   # BTC: 500 long, ETH: 300 short
    assert abs(s.exposure("BTC") - 0.5) < 1e-9
    assert abs(s.exposure("ETH") + 0.3) < 1e-9
    assert s.exposure("SOL") == 0.0


# ---------- Copier: Ziel-Portfolio ----------

def test_targets_scaled_by_weight_and_ratio():
    # Leader hat 40% long BTC; Gewicht 1.0, copy_ratio 0.5, Equity 10000 -> 2000 USD
    targets = compute_targets([snap("0xa", 1000, BTC=4)], {"0xa": 1.0}, 10000, CT, RISK)
    assert abs(targets["BTC"] - 2000) < 1e-9


def test_targets_coin_cap():
    # Leader 300% long (3x Leverage) -> ungecappt 15000, Cap 25% von 10000 = 2500
    targets = compute_targets([snap("0xa", 1000, BTC=30)], {"0xa": 1.0}, 10000, CT, RISK)
    assert abs(targets["BTC"] - 2500) < 1e-9


def test_targets_gross_leverage_cap():
    # Viele Coins je am Cap -> Summe würde max_leverage sprengen -> Skalierung
    coins = {f"C{i}": 30 for i in range(20)}
    targets = compute_targets([snap("0xa", 1000, **coins)], {"0xa": 1.0}, 10000, CT, RISK)
    gross = sum(abs(v) for v in targets.values())
    assert gross <= 3 * 10000 + 1e-6


def test_targets_multiple_leaders_aggregate():
    snaps = [snap("0xa", 1000, BTC=8), snap("0xb", 1000, BTC=-8)]
    targets = compute_targets(snaps, {"0xa": 0.5, "0xb": 0.5}, 10000, CT, RISK)
    assert abs(targets["BTC"]) < 1e-9  # entgegengesetzte Leader neutralisieren sich


# ---------- Copier: Rebalancing ----------

def test_rebalance_respects_threshold():
    # Abweichung 100 USD bei 10000 Equity (1%) < threshold 2% -> keine Order
    orders = plan_rebalance({"BTC": 2100}, {"BTC": 0.02}, {"BTC": 100000}, 10000, CT)
    assert orders == []


def test_rebalance_opens_and_sizes_correctly():
    orders = plan_rebalance({"BTC": 5000}, {}, {"BTC": 100000}, 10000, CT)
    assert len(orders) == 1
    o = orders[0]
    assert o.is_buy and abs(o.delta_size - 0.05) < 1e-9


def test_rebalance_always_closes_orphans():
    # Leader ist raus (target 0), wir halten noch 150 USD -> schließen trotz Threshold
    orders = plan_rebalance({}, {"ETH": 0.05}, {"ETH": 3000}, 10000, CT)
    assert len(orders) == 1
    assert not orders[0].is_buy
    assert abs(orders[0].delta_size + 0.05) < 1e-9


def test_rebalance_short_target():
    orders = plan_rebalance({"BTC": -3000}, {"BTC": 0.01}, {"BTC": 100000}, 10000, CT)
    assert len(orders) == 1
    assert not orders[0].is_buy
    assert abs(orders[0].delta_size + 0.04) < 1e-9  # 1000 long -> 3000 short = -4000 USD


def _bare_copier(path):
    ct = CopyTrader.__new__(CopyTrader)
    ct._risk_state_path = path
    ct.start_equity, ct.day, ct.day_start_equity, ct.halted = None, "", 0.0, False
    ct.last_equity = None
    return ct


def test_risk_state_survives_restart():
    """Bug-Fix: Drawdown-Baseline + Halt-Flag müssen Neustarts überleben."""
    path = Path(tempfile.mkdtemp()) / "risk_state.json"
    ct = _bare_copier(path)
    ct.start_equity, ct.day, ct.day_start_equity, ct.halted = 10_000.0, "2026-06-25", 9_500.0, True
    ct._save_risk_state()

    fresh = _bare_copier(path)          # Neustart: lädt vom Datenträger
    fresh._load_risk_state()
    assert fresh.start_equity == 10_000.0, "Drawdown-Basis darf nicht re-baselinen"
    assert fresh.halted is True, "ein gestoppter Bot bleibt nach Reboot gestoppt"
    assert fresh.day == "2026-06-25" and fresh.day_start_equity == 9_500.0


def test_resume_clears_halt_and_rebaselines():
    path = Path(tempfile.mkdtemp()) / "risk_state.json"
    ct = _bare_copier(path)
    ct.halted, ct.last_equity = True, 9_000.0
    ct.resume()
    assert ct.halted is False and ct.start_equity == 9_000.0
    reloaded = _bare_copier(path)
    reloaded._load_risk_state()
    assert reloaded.halted is False, "Resume wird persistiert"


# ---------- Feed-Only (Quest-Bot: Kopier-Buch handelt nicht mehr selbst) ----------

class _FeedClient:
    def all_mids(self):
        return {"BTC": "100.0", "ETH": "100.0"}


class _FeedTracker:
    addresses = ["0xa"]
    last_fresh = last_stale = last_total = 0

    def snapshot_all(self):
        # Große Position (50% Exposure) -> Ziel klar über rebalance_threshold,
        # im Normalmodus also ein echter Trade (Gegenprobe unten braucht das).
        return [snap("0xa", 10_000, BTC=50)]


def _feed_cfg(feed_only):
    from bot.config import load_config
    cfg = load_config("config.yaml")
    cfg.dry_run = True
    cfg.copytrade.feed_only = feed_only
    return cfg


def _isolated_copier(feed_only):
    """Copier mit eigenem Paper-Buch/Risk-State im Temp-Verzeichnis - sonst
    schrieben die Tests in das gemeinsame runtime/paper_state.json und
    verunreinigten sich gegenseitig über Läufe hinweg."""
    from bot.copytrade.copier import CopyTrader
    from bot.paper import PaperBroker

    ct = CopyTrader(_feed_cfg(feed_only), _FeedClient(), _FeedTracker(), {"0xa": 1.0})
    tmp = Path(tempfile.mkdtemp())
    ct.paper = PaperBroker(10_000, 0.00045, path=tmp / "paper.json")
    ct._risk_state_path = tmp / "risk.json"
    ct.start_equity, ct.halted, ct.last_equity = None, False, None
    return ct


def test_feed_only_snapshots_but_does_not_trade():
    """Quest-Bot-Umstellung: bei feed_only liefert der Copier weiter Preise UND
    Leader-Snapshots (der Feed, von dem der Quest-Bot lebt), handelt aber NICHT
    mehr selbst - kein eigenes Paper-Trade, obwohl ein frisches Signal anliegt."""
    ct = _isolated_copier(feed_only=True)
    ct.tick()
    assert ct.last_prices.get("BTC") == 100.0, "Preise werden geliefert (Feed lebt)"
    assert ct.last_snapshots, "Snapshots werden geliefert (Feed lebt)"
    assert ct.paper.trades == 0, "aber KEIN eigenes Kopier-Trade im Feed-Only-Modus"


def test_not_feed_only_still_trades():
    """Gegenprobe: ohne feed_only handelt der Copier wie gehabt (sonst würde der
    Test oben auch grün, wenn tick() aus einem anderen Grund nichts tut)."""
    ct = _isolated_copier(feed_only=False)
    ct.tick()
    assert ct.paper.trades > 0, "im Normalmodus spiegelt der Copier das Leader-Signal"


# ---------- Tracker: kein Phantom-Snapshot, Cache statt Lücke ----------

def _state(equity, **positions):
    return {"marginSummary": {"accountValue": str(equity)},
            "assetPositions": [
                {"position": {"coin": c, "szi": str(s), "entryPx": "100",
                              "positionValue": str(abs(s) * 100), "leverage": {"value": 2}}}
                for c, s in positions.items()]}


class _FlakyInfo:
    """Haupt-DEX-Call schlägt für konfigurierbare Adressen fehl (429-Simulation)."""

    def __init__(self, fail: set):
        self.fail = fail

    def user_state(self, address, dex=None):
        if address in self.fail:
            raise RuntimeError("429 Too Many Requests")
        return _state(50_000, BTC=500)


def test_tracker_no_phantom_flat_snapshot():
    """Audit-Befund: schlug der Haupt-DEX fehl, lieferte snapshot() ein Phantom
    (Equity 0, leeres Buch) statt eines Fehlers - falsche 'Leader raus'-Exits
    und zerstörte Baselines (alles sähe danach 'frisch' aus). Jetzt: raise."""
    from bot.copytrade.tracker import LeaderTracker

    t = LeaderTracker(_FlakyInfo(fail={"0xdead"}), ["0xdead"], throttle_s=0)
    try:
        t.snapshot("0xdead")
        assert False, "Haupt-DEX-Fehler muss raisen, kein Phantom-Snapshot"
    except RuntimeError:
        pass


def test_tracker_snapshot_all_uses_last_good_cache():
    """Fällt eine Adresse aus (429), liefert der Cache den letzten guten Stand -
    Weglassen hieße: Copier flattened fälschlich (Ziel 0) und Sprint verliert
    die Baseline. Abdeckungszähler machen den Zustand sichtbar."""
    from bot.copytrade.tracker import LeaderTracker

    info = _FlakyInfo(fail=set())
    t = LeaderTracker(info, ["0xa", "0xb"], throttle_s=0)
    snaps = t.snapshot_all()
    assert len(snaps) == 2 and t.last_fresh == 2 and t.last_stale == 0

    info.fail = {"0xb"}                       # 0xb fällt aus
    snaps = t.snapshot_all()
    assert len(snaps) == 2, "Cache hält 0xb im Feed"
    assert t.last_fresh == 1 and t.last_stale == 1 and t.last_total == 2
    assert snaps[1].positions.get("BTC") is not None, "letzter guter Stand, kein Phantom"

    t2 = LeaderTracker(_FlakyInfo(fail={"0xa"}), ["0xa"], throttle_s=0)
    assert t2.snapshot_all() == [], "ohne je einen guten Stand: überspringen"
    assert t2.last_fresh == 0 and t2.last_stale == 0


def test_tracker_coverage_consistent_when_analysis_swaps_addresses_mid_round():
    """Live-Anzeige 'Abdeckung 13/11': die Hintergrund-Analyse tauschte die
    Adressliste MITTEN in einer Snapshot-Runde. Die Runde muss gegen ihre
    EIGENE Listen-Kopie zählen, nicht gegen die schon getauschte."""
    from bot.copytrade.tracker import LeaderTracker

    class _SwappingInfo:
        def __init__(self):
            self.tracker = None

        def user_state(self, address, dex=None):
            if self.tracker is not None:
                self.tracker.addresses = ["0xnur-noch-einer"]   # Analyse swappt
            return _state(50_000, BTC=500)

    info = _SwappingInfo()
    t = LeaderTracker(info, ["0xa", "0xb", "0xc"], throttle_s=0)
    info.tracker = t
    snaps = t.snapshot_all()
    assert len(snaps) == 3, "laufende Runde arbeitet ihre Liste komplett ab"
    assert t.last_fresh == 3 and t.last_total == 3, \
        "Zähler konsistent zur eigenen Runde (nie wieder '13/11')"
    assert t.addresses == ["0xnur-noch-einer"], "Swap selbst bleibt wirksam"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
