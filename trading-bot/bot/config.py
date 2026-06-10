"""Lädt config.yaml und .env und stellt sie als typisierte Objekte bereit."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class MarketConfig:
    coin: str
    interval: str
    lookback_candles: int


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


@dataclass
class BacktestConfig:
    fee_rate: float
    slippage: float
    initial_equity: float


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
    server_host: str = "127.0.0.1"
    server_port: int = 8000


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
    analysis: AnalysisConfig = None  # type: ignore[assignment]


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

    @property
    def is_testnet(self) -> bool:
        return self.network != "mainnet"


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    with open(path) as f:
        raw = yaml.safe_load(f)
    ct_raw = dict(raw.get("copytrade", {}))
    analysis = AnalysisConfig(**ct_raw.pop("analysis", {}))
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
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    r = cfg.risk
    if not 0 < r.risk_per_trade <= 0.05:
        raise ValueError("risk_per_trade muss zwischen 0 und 5% liegen")
    if r.max_leverage < 1 or r.max_leverage > 10:
        raise ValueError("max_leverage muss zwischen 1 und 10 liegen")
    if cfg.strategy.ema_fast >= cfg.strategy.ema_slow:
        raise ValueError("ema_fast muss kleiner als ema_slow sein")
    ct = cfg.copytrade
    if not 0 < ct.copy_ratio <= 1:
        raise ValueError("copy_ratio muss zwischen 0 und 1 liegen")
    if not 0 < ct.max_alloc_per_coin <= 0.5:
        raise ValueError("max_alloc_per_coin muss zwischen 0 und 50% liegen")


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
