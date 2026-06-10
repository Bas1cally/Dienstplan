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
class Config:
    network: str
    dry_run: bool
    market: MarketConfig
    strategy: StrategyConfig
    risk: RiskConfig
    backtest: BacktestConfig

    @property
    def is_testnet(self) -> bool:
        return self.network != "mainnet"


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    with open(path) as f:
        raw = yaml.safe_load(f)
    cfg = Config(
        network=raw.get("network", "testnet"),
        dry_run=bool(raw.get("dry_run", True)),
        market=MarketConfig(**raw["market"]),
        strategy=StrategyConfig(**raw["strategy"]),
        risk=RiskConfig(**raw["risk"]),
        backtest=BacktestConfig(**raw["backtest"]),
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
