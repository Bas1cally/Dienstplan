"""Lädt config.yaml und .env und stellt sie als typisierte Objekte bereit."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# .env sofort beim Import laden, damit Telegram/Claude/Dashboard-Token auch in
# doctor.py, autopilot.py (headless) und report.py greifen - nicht nur, wenn
# load_credentials() (Live-Modus) oder server.py sie explizit lädt.
load_dotenv(ROOT / ".env")


@dataclass
class MarketConfig:
    coin: str
    interval: str
    lookback_candles: int
    dexs: str = "auto"   # "auto" = alle Perp-DEXs (Krypto + Aktien/Gold/Öl) | Liste | "main"


@dataclass
class StrategyConfig:
    ema_fast: int
    ema_slow: int
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    atr_period: int
    allow_shorts: bool


@dataclass
class RiskConfig:
    risk_per_trade: float
    atr_stop_mult: float
    take_profit_r: float
    max_leverage: int
    max_daily_loss: float
    slippage: float
    max_total_drawdown: float = 0.10   # Gesamt-Drawdown-Halt (Prop-Firmen-Regel)


@dataclass
class BacktestConfig:
    fee_rate: float
    slippage: float
    initial_equity: float
    maker_fee_rate: float = 0.00015   # Paper-Annahme bei maker_first (leicht optimistisch)


@dataclass
class AnalysisConfig:
    days: int = 30
    min_account_value: float = 10000
    min_volume: float = 500000
    top_n: int = 30
    top_percent: float = 1.0     # nur das Top-x% (nach Monats-ROI) tiefenanalysieren
    min_score: float = 40
    larp: dict = None  # type: ignore[assignment]  # Overrides für LarpConfig


@dataclass
class NewsConfig:
    enabled: bool = True
    poll_seconds: int = 60
    rss_feeds: list = None  # type: ignore[assignment]
    cryptopanic: bool = False    # CRYPTOPANIC_TOKEN in .env
    twitter: bool = False        # TWITTER_BEARER_TOKEN in .env (X API, kostenpflichtig)
    twitter_query: str = '(from:WhiteHouse OR from:federalreserve OR bitcoin OR crypto) (crash OR tariff OR ban OR war OR hack) -is:retweet'
    caution_score: float = 4.0
    risk_off_score: float = 8.0
    half_life_minutes: float = 30.0
    llm_enabled: bool = False        # Claude bewertet Schlagzeilen zusätzlich (ANTHROPIC_API_KEY)
    llm_model: str = "claude-opus-4-8"  # günstigere Alternative: claude-haiku-4-5

    def __post_init__(self):
        if self.rss_feeds is None:
            self.rss_feeds = [
                "https://www.coindesk.com/arc/outboundfeeds/rss/",
                "https://cointelegraph.com/rss",
            ]


@dataclass
class AutopilotConfig:
    reanalyze_hours: float = 24      # wie oft das Leaderboard neu analysiert wird
    min_keep_score: float = 35       # Leader unter diesem Score werden rotiert
    daily_digest: bool = True        # Tagesbericht per Telegram/Log
    watchdog_hours: float = 24       # Alarm + Diagnose, wenn so lange keine Order kam
    shadow_variants: bool = True     # A/B-Tuning: Varianten parallel im Schatten testen
    realtime: bool = True            # WebSocket: Leader-Fills wecken den Loop sofort
    autostart: bool = False          # Autopilot beim Server-Start sofort loslegen (24/7-Betrieb)
    server_host: str = "127.0.0.1"
    server_port: int = 8000


@dataclass
class ValidationConfig:
    enabled: bool = True
    interval: str = "15m"            # Trading-Timeframe der Technik-Prüfung
    confirm_interval: str = "1h"     # Bestätigungs-Timeframe
    lookback: int = 120
    rsi_max_long: float = 75         # hartes Veto: kein Long darüber
    rsi_min_short: float = 25        # hartes Veto: kein Short darunter
    min_score: int = 2               # von 3 Technik-Checks müssen bestehen
    cache_seconds: int = 120
    llm_enabled: bool = False        # Claude-Zweitmeinung (ANTHROPIC_API_KEY)
    llm_model: str = "claude-opus-4-8"


@dataclass
class ExecutionConfig:
    # hyperliquid = Orders direkt (live/testnet) | signals = nur Order-Tickets
    # emittieren (Prop-Accounts ohne API, z.B. Breakout by Kraken)
    mode: str = "hyperliquid"
    maker_first: bool = True        # Post-Only-Limit zuerst, Market nur als Fallback
    maker_timeout_s: float = 20     # so lange auf den Maker-Fill warten


@dataclass
class FundingTiltConfig:
    enabled: bool = True
    min_apr: float = 0.10           # darunter neutral (10% p.a.)
    max_apr: float = 0.50           # ab hier voller Tilt
    earn_boost: float = 1.10        # Position kassiert Funding -> bis +10%
    pay_scale: float = 0.85         # Position zahlt Funding -> bis -15%
    cache_seconds: int = 300


@dataclass
class ScalpConfig:
    enabled: bool = False            # opt-in: erst nach überzeugendem Paper-Lauf!
    coin: str = "BTC"                # nur der liquideste Markt (Slippage)
    risk_per_scalp: float = 0.005    # max. 0.5% Equity Risiko pro Scalp
    stabilize_minutes: int = 3       # Mindestabstand zum Schock-Event
    confirm_candles: int = 3         # so viele 1m-Candles ohne neues Extrem
    entry_window_minutes: int = 25   # danach ist das Event verfallen
    stop_buffer: float = 0.002       # Stop knapp hinter dem Move-Extrem
    retrace_target: float = 0.382    # TP bei 38.2% Retrace des Spikes
    max_holding_minutes: int = 30    # Zeit-Stop: ein Scalp wird nie eine Position
    max_notional_frac: float = 0.15  # max. 15% Equity Notional pro Scalp


@dataclass
class InvestigatorConfig:
    watchlist: list = None  # type: ignore[assignment]  # Wallets (Whales/MMs) beobachten
    poll_seconds: int = 60
    min_notional_change: float = 25_000   # Alerts erst ab dieser Positionsänderung

    def __post_init__(self):
        if self.watchlist is None:
            self.watchlist = []


@dataclass
class AnomalyConfig:
    """Anomalie-Scout: frische Wallets mit großen, konzentrierten Wetten melden.

    Reiner Beobachter (öffentliche On-Chain-Daten) - handelt nie, meldet nur.
    """
    enabled: bool = True
    coins: list = None  # type: ignore[assignment]  # beobachtete Trade-Ströme
    poll_seconds: int = 300
    min_trade_notional: float = 100_000   # erst ab dieser Trade-Größe wird die Wallet geprüft
    min_position_notional: float = 100_000  # ... und nur ab dieser Positionsgröße gemeldet
    lookback_days: int = 7                # Historie-Fenster für "frisch"
    max_prior_fills: int = 10             # mehr alte Fills = alter Hase, keine Meldung
    min_concentration: float = 0.6        # Anteil der größten Position am Gesamt-Buch
    max_checks_per_scan: int = 5          # API-Budget je Scan (Rate-Limit-Hygiene)
    recheck_hours: float = 12
    throttle_s: float = 1.0

    def __post_init__(self):
        if self.coins is None:
            self.coins = ["BTC", "ETH", "SOL", "HYPE"]


@dataclass
class OrderBookConfig:
    """Orderbuch-Scout: Mikrostruktur (Imbalance, Walls, dünne Liquidität).

    Read-only - eine der echten strukturellen Edges. Ehrliche Grenze: nicht-
    kolokiertes Polling fängt nur langsame Signale, kein HFT/Spoofing.
    """
    enabled: bool = True
    notify: bool = False             # Telegram-Push (Default aus - sonst Spam)
    coins: list = None  # type: ignore[assignment]
    poll_seconds: int = 300
    band_pct: float = 0.005          # nur Levels innerhalb 0.5% um den Mid zählen
    imbalance_threshold: float = 0.6  # |Imbalance| ab hier melden
    wall_ratio: float = 5.0          # Level >= 5x mittlere Größe = Wall
    min_signal_gap_s: int = 900      # min. Abstand zwischen Signalen je Coin (gegen Pendeln)
    throttle_s: float = 0.5          # Pause zwischen Coin-Abfragen (Rate-Limit)

    def __post_init__(self):
        if self.coins is None:
            self.coins = ["BTC", "ETH", "SOL", "HYPE"]


@dataclass
class TwapConfig:
    """TWAP-Scout: laufende TWAP-Ausführungen großer Wallets aufspüren.

    Prüft eine Kandidatenmenge (Watchlist + Leader + Anomalie-Funde + extra)
    via userTwapSliceFills. Read-only - handelt nie.
    """
    enabled: bool = True
    poll_seconds: int = 120
    active_window_minutes: int = 20   # Slices in diesem Fenster = TWAP läuft
    min_slices: int = 4               # so viele jüngste Slices = aktiver TWAP
    max_checks_per_scan: int = 8      # API-Budget je Scan (Rate-Limit)
    recheck_hours: float = 6          # dieselbe laufende TWAP nicht erneut melden
    throttle_s: float = 0.5
    extra_addresses: list = None  # type: ignore[assignment]  # bekannte Whales (z.B. Tron/Sun)

    def __post_init__(self):
        if self.extra_addresses is None:
            self.extra_addresses = []


@dataclass
class PolymarketConfig:
    """Polymarket-Scout: erfahrenes Geld in Prediction Markets beobachten.

    Read-only, separates Tier - handelt nie. Datenquelle: Polymarket Data-API
    (öffentlich). Endpunkte überschreibbar, falls die API sich ändert.
    """
    enabled: bool = False           # opt-in - separate Domain
    poll_seconds: int = 600
    trades_url: str = "https://data-api.polymarket.com/trades"
    positions_url: str = "https://data-api.polymarket.com/positions"
    trade_limit: int = 100
    min_trade_notional: float = 5_000     # USDC - ab dieser Wettgröße prüfen
    min_wallet_profit: float = 10_000     # nur Wallets mit nachweisbarem Track-Record
    max_checks_per_scan: int = 5
    recheck_hours: float = 12
    throttle_s: float = 1.0


@dataclass
class ConvergenceConfig:
    enabled: bool = True
    sources: list = None  # type: ignore[assignment]  # binance | okx | bybit
    period: str = "1h"
    agree_boost: float = 1.25        # max. Verstärkung bei externer Übereinstimmung
    disagree_scale: float = 0.4      # Dämpfung bei klarem Widerspruch
    neutral_band: float = 0.1        # |externe Stimme| darunter = kein Einfluss
    cache_seconds: int = 300

    def __post_init__(self):
        if self.sources is None:
            self.sources = ["binance", "okx", "bybit"]


@dataclass
class ShockConfig:
    enabled: bool = True
    window_minutes: int = 5
    move_threshold: float = 0.025   # 2.5% Bewegung im Fenster -> RISK_OFF
    vol_spike_ratio: float = 4.0    # Kurzfrist-Vola vs. Stunden-Vola
    cooldown_minutes: int = 30


@dataclass
class CopytradeConfig:
    leaders_file: str = "leaders.json"
    max_leaders: int = 3
    copy_ratio: float = 0.5
    max_alloc_per_coin: float = 0.25
    rebalance_threshold: float = 0.02
    min_notional: float = 10
    poll_seconds: int = 15
    # Feed-Only (Quest-Bot-Umstellung): das Kopier-Buch handelt NICHT mehr selbst,
    # es liefert nur noch Preise + Leader-Snapshots für den Quest-Bot und die
    # Discovery. Der Copier snapshottet dann und kehrt sofort zurück, bevor er
    # Targets rechnet oder Orders ausführt - kein eigenes Paper-Trading mehr.
    feed_only: bool = False
    analysis: AnalysisConfig = None  # type: ignore[assignment]


@dataclass
class TrendLabConfig:
    """Eigene TA-Strategie im Paper-Schatten (EMA/RSI-Trend)."""
    enabled: bool = True
    coin: str = "BTC"
    interval: str = "1h"
    lookback: int = 300
    atr_stop_mult: float = 2.0
    take_profit_r: float = 2.0
    risk_frac: float = 0.01          # 1% Equity-Risiko pro Trade (über Stop-Distanz)
    max_notional_frac: float = 0.25  # Notional-Deckel pro Position
    min_notional: float = 10
    strategy: StrategyConfig = None  # type: ignore[assignment]  # geerbt aus cfg.strategy


@dataclass
class FundingLabConfig:
    """Funding-Capture im Paper-Schatten (kassierende Seite halten)."""
    enabled: bool = True
    coins: list = None  # type: ignore[assignment]
    entry_apr: float = 0.30          # ab |Funding| >= 30% p.a. einsteigen
    exit_apr: float = 0.10           # unter 10% p.a. wieder raus
    stop_frac: float = 0.03          # harter Kurs-Stop 3% gegen die Position
    max_hold_hours: float = 24
    max_positions: int = 3
    risk_frac: float = 0.02          # Notional pro Position (2% Equity)
    min_notional: float = 10
    cache_seconds: int = 300

    def __post_init__(self):
        if self.coins is None:
            self.coins = ["BTC", "ETH", "SOL", "HYPE"]


@dataclass
class LabsConfig:
    """Strategie-Labor: eigene Signale parallel im Paper-Modus messen."""
    enabled: bool = True
    candle_fetch_seconds: int = 60   # Drossel für TrendLab-Candle-Abfragen (API-Last)
    trend: TrendLabConfig = None    # type: ignore[assignment]
    funding: FundingLabConfig = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.trend is None:
            self.trend = TrendLabConfig()
        if self.funding is None:
            self.funding = FundingLabConfig()


@dataclass
class SprintConfig:
    """Sprint-Buch: kleines Konto mit vollem Hebel auf den BESTEN Leader.

    Ziel +target_profit je Zyklus, dann Reset; unter bust_frac gilt der Zyklus
    als liquidiert (10x-Realität). Reiner Paper-Track, eigenes Konto.
    """
    enabled: bool = True
    equity: float = 1000.0          # frisches Kapital je Zyklus
    leverage: float = 10.0          # Exposure-Multiplikator auf den besten Leader
    target_profit: float = 100.0    # Take-Profit je Zyklus (+10%)
    # EIN Ritt = EINE Position (Nutzer-Vorgabe). Live-Vorfall: ein Leader hat
    # einen 7-Coin-Aktien-Korb auf einmal eröffnet und der Bot hat ALLE 7
    # gefressen - die 10x-Kapazität auf 7 Coins verschmiert statt EIN starkes
    # Signal zu reiten. Bei Körben gewinnt das Signal mit der größten relativen
    # Überzeugung des Leaders (|exposure|), der Rest wird sichtbar verworfen.
    max_positions: int = 1
    # MESS-MODUS (Nutzer, Mess-Woche): Ritte laufen PARALLEL - jedes Signal
    # öffnet seinen eigenen Ritt auf equity-Basis (1000$) und bewertet seinen
    # Trader unabhängig (Strike/Heilung je Ritt). Kein Ritt blockiert mehr die
    # Messung anderer Signale. Das ENDZIEL bleibt False:
    # POOL -> EIN GUTER RITT -> +10% -> RAUS, eine Position.
    parallel_rides: bool = False
    max_rides: int = 8            # Deckel für gleichzeitige Mess-Ritte
    # 1 Position PRO TRADER (Nutzer): ohne diesen Deckel könnte EIN Leader über
    # mehrere Ticks alle Ritt-Slots füllen - 7 korrelierte Wetten eines Traders
    # sähen in der Messung wie 7 unabhängige Datenpunkte aus.
    max_rides_per_leader: int = 1
    # Krypto-only: Builder-DEX-Assets (Aktien/Gold, Coins mit ':' wie
    # 'xyz:INTC') sind außerhalb der Börsenzeiten reine Spekulation auf HL -
    # stören die Messlatte für halbwegs stabile Trader. HINWEIS: bei
    # stock_market_hours=true wird dieser Wert zur Laufzeit automatisch
    # umgeschaltet (zum Börsen-Gong), der statische Wert ist dann nur der
    # Anfangs-/Fallback-Zustand.
    crypto_only: bool = True
    # Worldclock (Nutzer): Aktien-Perps NUR während der echten US-Börsen-Sitzung
    # (9:30-16:00 ET, Mo-Fr, kein Feiertag) handeln - außerhalb ist der Kurs
    # stale/spekulativ. Ist das an, schaltet der Bot crypto_only zum Eröffnungs-
    # Gong auf false (Aktien-Basket aktiv) und zum Schluss-Gong auf true, macht
    # je ein /analyze und sichert profitable Aktien-Positionen beim Schluss.
    # false = statisches crypto_only oben gilt durchgehend.
    stock_market_hours: bool = True
    bust_frac: float = 0.05         # Liquidations-Modell: darunter ist der Zyklus geplatzt
    # Eigener, breiterer Leader-Pool als das Hauptbuch: Sprint hält immer nur EINE
    # Position und steigt beim bestbewerteten Leader mit frischem Signal ein. Mehr
    # Quellen = weniger Leerlauf, OHNE Qualitätsverlust bei den genommenen Einstiegen
    # (der Top-Leader gewinnt immer, wenn er aktiv ist). Das Hauptbuch bleibt
    # konzentriert bei copytrade.max_leaders - dieser Pool betrifft nur das Sprint-Buch.
    pool_size: int = 20
    # Mindest-RICHTUNGS-Score (TraderMetrics.sprint_score, 0..100) für den Pool.
    # Bewusst NIEDRIG (Anti-Müll-Boden, kein Vorab-Urteil): das Sprint-Buch ist
    # Papier mit Strike-Maschine - schwache Leader fliegen nach max. 2 Verlust-
    # Ritten gebannt raus, Über-Filterung dagegen kostet die ganze Messung
    # (Live-Befund: Pool schrumpfte auf 6, tagelange Signal-Dürre).
    # Hauptbuch bleibt strikt bei analysis.min_score.
    pool_min_score: float = 10.0
    rebalance_threshold: float = 0.02
    min_notional: float = 10.0
    # Teil-Exit-Folge: hat der Leader >= partial_exit_frac seiner Einstiegsgröße
    # abgebaut, steigen wir aus (Scalper skalieren gestaffelt raus).
    partial_exit_frac: float = 0.75
    # Aufstockungs-Signal (nur im FLACH-Scan): Positions-Trader eröffnen selten
    # neu - ihr Überzeugungs-Moment ist das AUFSTOCKEN. Vergrößert ein Leader
    # eine bestehende Position um >= add_signal_frac (0.5 = +50%), gilt das als
    # frisches Richtungs-Signal; ebenso ein Richtungs-FLIP (Long->Short) im
    # Bestand. 0 = aus (dann zählt nur der klassische 0->Position-Übergang).
    add_signal_frac: float = 0.5
    # Bestätigungsfenster gegen Flip-Flopper (Nutzer-Beobachtung: Leader
    # eröffnet, schießt sofort ins Minus, steigt Sekunden später wieder aus
    # und öffnet denselben Trade erneut - wir waren jedes Mal blind instant
    # im Minus dabei). Statt bei einem frischen Signal sofort einzusteigen,
    # wird ein Kandidat registriert; erst wenn die Position des LEADERS (!)
    # mindestens confirm_delay_s durchgehend nicht im Minus war, steigen wir
    # zum DANN aktuellen Preis ein. 0 = aus (altes Sofort-Verhalten). Gilt
    # NUR für frische Signale aus flachem Zustand + Star-Preemption - eine
    # Flip-getriebene Re-Entry innerhalb eines laufenden Ritts bleibt sofort
    # (sonst bricht das die "Flip = derselbe Zyklus läuft weiter"-Semantik).
    # Sicherer Default 0 (aus) wie bei parallel_rides - config.yaml schaltet
    # den Produktivwert (10s) explizit scharf.
    confirm_delay_s: float = 0.0
    # LARP-Strikes: Verlust-Ritt -> Strike +1, Gewinn-Ritt -> Strike -1 (min 0).
    # Bei strike_ban Strikes wird der Leader fürs Sprint-Buch gesperrt.
    strike_ban: int = 2
    # Zeit+negativ-Cut (Nutzer-Beobachtung: eine Position hing 4h im Minus und
    # blockierte den einzigen Ritt-Slot, bis der Leader rotierte - -230$). Ist
    # ein Ritt länger als max_ride_hours offen UND aktuell im Minus, wird er
    # gecuttet (Slot frei, Verlust begrenzt, Leader kriegt seinen Strike). Cuttet
    # NICHT bei schnellem Rauschen (nur der langsame Dauer-Bluter), und NICHT
    # solange der Ritt im Plus ist (dann läuft er weiter Richtung Ziel). Sicherer
    # Default 0 (aus) - config.yaml schaltet den Produktivwert scharf.
    max_ride_hours: float = 0.0
    # Schläfer-Filter beim POOL-AUFBAU (Nutzer-Befund: 20 flache Schläfer-Wallets,
    # kein Signal - "das ist doch sus"): eine Wallet kommt nur in den Pool, wenn
    # sie in den letzten max_idle_days getradet hat ODER gerade eine Position
    # hält. "Historisch gut, jetzt seit Wochen still" (das Gate zählt sonst
    # Alt-Aktivität über das ganze 21-Tage-Fenster) fliegt raus, bevor sie einen
    # Slot als toter Scan-Punkt belegt. Ergänzt die Laufzeit-Idle-Rotation
    # (rotate_idle_hours), die WÄHREND der Pool-Zugehörigkeit stumm gewordene
    # rausrotiert - dieser Filter verhindert das Reinkommen von Anfang an. 0 = aus.
    max_idle_days: float = 3.0
    # Idle-Rotation (Nutzer: "scannen scannen Daten"): eine Wallet, die seit dem
    # Pool-Eintritt länger als rotate_idle_hours KEIN einziges frisches Signal
    # gab, ist totes Gewicht - sie belegt einen Scan-Slot, ohne Daten zu liefern
    # (schlimmer als ein Verlierer, der wenigstens gemessen wird). Beim nächsten
    # Pool-Rebuild werden solche Stummen nach HINTEN sortiert, frische Kandidaten
    # bekommen Vorrang. Self-balancing: eine Stumme rutscht nur dann doch wieder
    # rein, wenn es nicht genug aktive/neue Kandidaten gibt (Pool verhungert nie).
    # Stars (bewiesene Verdiener) sind ausgenommen. 0 = aus. Sinnvoll ~ reanalyze
    # _hours (Rebuild-Kadenz) - kürzer bringt nichts, da erst beim Rebuild rotiert.
    rotate_idle_hours: float = 6.0
    # BTC ist praktisch "der Markt" (Beta statt Leader-Alpha) und volatilitätsarm -
    # bis das feste +10%-Ziel (bei 10x ~1% Kursbewegung) erreicht ist, stoppt ein
    # scalpender Leader oft mehrfach aus - jeder Ritt kostet echte Ein-/Ausstiegs-
    # fee auf ~10x Notional. Ausschlussliste statt starrer Whitelist, frei änderbar.
    exclude_coins: list = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.exclude_coins is None:
            self.exclude_coins = ["BTC"]


@dataclass
class LighterConfig:
    """Lighter (zkLighter) als zusätzliche Trader-Quelle (read-only, Copy auf HL).

    Kein Ranking-API -> automatische Discovery über den öffentlichen Trade-Strom
    (marktweise, da recentTrades einen market_id-Pflichtparameter braucht) +
    optionale manuelle Seeds. Erst per /lighter <ref> verifizieren, dann messen.
    """
    enabled: bool = False
    base_url: str = "https://mainnet.zklighter.elliot.ai"
    accounts: list = None  # type: ignore[assignment]  # optionale manuelle Seed-Indizes/0x
    coins: list = None     # type: ignore[assignment]  # HL-handelbare Whitelist, None=alle
    poll_seconds: int = 120
    copy_ratio: float = 0.5
    # Automatische Discovery: aktive Konten aus dem öffentlichen Trade-Strom ziehen,
    # bewerten (LARP/Score) und die Besten im Paper-Schatten messen. Kein manuelles
    # Wallet-Durchgehen nötig.
    auto_discover: bool = True
    scan_seconds: int = 300
    max_candidates: int = 40      # API-Budget je Scan (Snapshot je Kandidat)
    min_equity: float = 5_000     # Wegwerf-Konten aussortieren
    min_positions: int = 1        # muss aktuell handeln
    max_leaders: int = 5          # so viele Beste werden gemessen
    initial_equity: float = 10_000  # Paper-Konto der Lighter-Messung
    throttle_s: float = 0.25      # Pause zwischen Konto-Reads (Rate-Limit-Hygiene)

    def __post_init__(self):
        if self.accounts is None:
            self.accounts = []


@dataclass
class CoinMarketManConfig:
    """HyperTracker (CoinMarketMan) als reichhaltige Leader-Quelle: vorberechnetes
    Perp-PnL-Leaderboard (Tag/Woche/Monat/all-time) mit Equity, Exposure-Ratio,
    Directional Bias. Ersetzt langfristig den schmalen HL-Leaderboard-Trichter fürs
    Discovery/Ranking; Live-Positionen bleiben bei HLs kostenlosem user_state
    (Rate-Limit-Hygiene: teurer Hochfrequenz-Teil bei HL, reiches Discovery bei CMM).

    Token liegt NICHT hier - der JWT steht als COINMARKETMAN_TOKEN in der .env
    (Secret, gitignored; per Telegram /setcmm setzbar). Read-only.
    """
    enabled: bool = False   # erst nach Live-Probe (/cmm) scharf schalten
    base_url: str = "https://ht-api.coinmarketman.com/api/external"
    period: str = "pnlMonth"   # Standard-Fenster für die /cmm-Probe
    # Discovery zieht aus MEHREREN Fenstern: pnlWeek fördert die AKTIVEN
    # Richtungs-Trader zutage (wer diese Woche vorne ist, hat diese Woche
    # gehandelt), pnlMonth die bewährten. Ein reines Monats-Board ist von
    # Sitzern dominiert - aktive Trader mit vielen kleinen Treffern stehen
    # dort auf Rang 300+, wo wir nie hinschauen.
    periods: list = None       # default: ["pnlWeek", "pnlMonth"]
    limit: int = 100           # Leaderboard-Zeilen pro Abruf (25 | 50 | 100)
    timeout: float = 20.0
    min_equity: float = 10_000     # Wegwerf-/Mini-Konten aussortieren
    min_pnl: float = 0.0           # nur im gewählten Fenster profitable
    # Kopierbarkeits-Filter: die Spitze des PnL-Boards sind Market-Maker/HFT-
    # Whales (Live-Befund: 408x Monats-Umsatz zur Equity, >2000 Fills im
    # Analysefenster) - unkopierbar (Churn frisst Fees) und für die Tiefen-
    # analyse unlesbar (HL deckelt userFills auf ~2000). Also VOR der teuren
    # Analyse aussieben:
    max_turnover: float = 120.0    # Monatsvolumen / Equity (Day-Trader ~5-100x)
    max_exposure: float = 6.0      # |exposureRatio| (offenes Notional / Equity)
    max_equity: float = 5_000_000  # darüber fast nur Fonds/MMs (Slippage-Realität)
    pages: int = 3                 # Board-Seiten je FENSTER und Scan (je 1 Request),
                                   # tiefer blättern, weil die Spitze rausgefiltert wird

    def __post_init__(self):
        if self.periods is None:
            self.periods = ["pnlWeek", "pnlMonth"]


@dataclass
class Config:
    network: str
    dry_run: bool
    market: MarketConfig
    strategy: StrategyConfig
    risk: RiskConfig
    backtest: BacktestConfig
    copytrade: CopytradeConfig
    news: NewsConfig
    shock: ShockConfig
    autopilot: AutopilotConfig
    convergence: ConvergenceConfig
    validation: ValidationConfig
    investigator: InvestigatorConfig
    scalp: ScalpConfig
    execution: ExecutionConfig
    funding_tilt: FundingTiltConfig
    anomaly: AnomalyConfig
    orderbook: OrderBookConfig
    twap: TwapConfig
    polymarket: PolymarketConfig
    labs: LabsConfig
    sprint: SprintConfig
    lighter: LighterConfig
    coinmarketman: CoinMarketManConfig

    @property
    def is_testnet(self) -> bool:
        return self.network != "mainnet"


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    with open(path) as f:
        raw = yaml.safe_load(f)
    ct_raw = dict(raw.get("copytrade", {}))
    analysis = AnalysisConfig(**ct_raw.pop("analysis", {}))
    labs = _build_labs(raw.get("labs", {}), raw["strategy"])
    cfg = Config(
        network=raw.get("network", "testnet"),
        dry_run=bool(raw.get("dry_run", True)),
        market=MarketConfig(**raw["market"]),
        strategy=StrategyConfig(**raw["strategy"]),
        risk=RiskConfig(**raw["risk"]),
        backtest=BacktestConfig(**raw["backtest"]),
        copytrade=CopytradeConfig(analysis=analysis, **ct_raw),
        news=NewsConfig(**raw.get("news", {})),
        shock=ShockConfig(**raw.get("shock", {})),
        autopilot=AutopilotConfig(**raw.get("autopilot", {})),
        convergence=ConvergenceConfig(**raw.get("convergence", {})),
        validation=ValidationConfig(**raw.get("validation", {})),
        investigator=InvestigatorConfig(**raw.get("investigator", {})),
        scalp=ScalpConfig(**raw.get("scalp", {})),
        execution=ExecutionConfig(**raw.get("execution", {})),
        funding_tilt=FundingTiltConfig(**raw.get("funding_tilt", {})),
        anomaly=AnomalyConfig(**raw.get("anomaly", {})),
        orderbook=OrderBookConfig(**raw.get("orderbook", {})),
        twap=TwapConfig(**raw.get("twap", {})),
        polymarket=PolymarketConfig(**raw.get("polymarket", {})),
        labs=labs,
        sprint=SprintConfig(**raw.get("sprint", {})),
        lighter=LighterConfig(**raw.get("lighter", {})),
        coinmarketman=CoinMarketManConfig(**raw.get("coinmarketman", {})),
    )
    _validate(cfg)
    return cfg


def _build_labs(raw: dict, strategy_raw: dict) -> "LabsConfig":
    """Baut die Labs-Config; TrendLab erbt die Indikator-Parameter aus strategy."""
    raw = dict(raw or {})
    trend_raw = dict(raw.pop("trend", {}) or {})
    funding_raw = dict(raw.pop("funding", {}) or {})
    trend = TrendLabConfig(strategy=StrategyConfig(**strategy_raw), **trend_raw)
    funding = FundingLabConfig(**funding_raw)
    return LabsConfig(trend=trend, funding=funding, **raw)


def _validate(cfg: Config) -> None:
    r = cfg.risk
    if not 0 < r.risk_per_trade <= 0.05:
        raise ValueError("risk_per_trade muss zwischen 0 und 5% liegen")
    if r.max_leverage < 1 or r.max_leverage > 10:
        raise ValueError("max_leverage muss zwischen 1 und 10 liegen")
    if cfg.strategy.ema_fast >= cfg.strategy.ema_slow:
        raise ValueError("ema_fast muss kleiner als ema_slow sein")
    if cfg.execution.mode not in ("hyperliquid", "signals"):
        raise ValueError("execution.mode muss 'hyperliquid' oder 'signals' sein")
    if cfg.execution.mode == "signals" and not cfg.dry_run:
        raise ValueError("execution.mode 'signals' erfordert dry_run: true - "
                         "Ausführung passiert extern, der Bot trackt nur im Paper-Modus")
    if not 0 < cfg.risk.max_total_drawdown <= 0.5:
        raise ValueError("max_total_drawdown muss zwischen 0 und 50% liegen")
    ct = cfg.copytrade
    if not 0 < ct.copy_ratio <= 1:
        raise ValueError("copy_ratio muss zwischen 0 und 1 liegen")
    if not 0 < ct.max_alloc_per_coin <= 0.5:
        raise ValueError("max_alloc_per_coin muss zwischen 0 und 50% liegen")
    sp = cfg.sprint
    if not 1 <= sp.leverage <= 25:
        raise ValueError("sprint.leverage muss zwischen 1 und 25 liegen")
    if sp.target_profit <= 0 or sp.equity <= 0:
        raise ValueError("sprint.equity und sprint.target_profit müssen positiv sein")
    if not 0 < sp.bust_frac < 0.5:
        raise ValueError("sprint.bust_frac muss zwischen 0 und 0.5 liegen")


def load_credentials() -> tuple[str, str]:
    """Liest Private Key und Account-Adresse aus .env / Umgebung."""
    load_dotenv(ROOT / ".env")
    key = os.environ.get("HL_PRIVATE_KEY", "")
    addr = os.environ.get("HL_ACCOUNT_ADDRESS", "")
    if not key or not key.startswith("0x"):
        raise RuntimeError(
            "HL_PRIVATE_KEY fehlt. Kopiere .env.example nach .env und trage "
            "den Key eines API-Wallets ein (nicht den Haupt-Wallet-Key)."
        )
    if not addr:
        raise RuntimeError("HL_ACCOUNT_ADDRESS fehlt in .env")
    return key, addr
