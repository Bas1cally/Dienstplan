"""Dünner Wrapper um das Hyperliquid-SDK (Info + Exchange)."""

import logging
import time

import pandas as pd
from hyperliquid.info import Info
from hyperliquid.utils import constants

log = logging.getLogger(__name__)

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def api_url(testnet: bool) -> str:
    return constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL


class HyperliquidClient:
    def __init__(self, testnet: bool, private_key: str | None = None, account_address: str | None = None):
        self.base_url = api_url(testnet)
        self.account_address = account_address
        self.info = Info(self.base_url, skip_ws=True)
        self.exchange = None
        if private_key:
            from eth_account import Account
            from hyperliquid.exchange import Exchange

            wallet = Account.from_key(private_key)
            self.exchange = Exchange(wallet, self.base_url, account_address=account_address)
        self._sz_decimals: dict[str, int] = {}

    # ---------- Marktdaten ----------

    def candles(self, coin: str, interval: str, lookback: int) -> pd.DataFrame:
        """Holt die letzten `lookback` Candles als OHLCV-DataFrame."""
        end = int(time.time() * 1000)
        start = end - lookback * INTERVAL_MS[interval]
        raw = self.info.candles_snapshot(coin, interval, start, end)
        df = pd.DataFrame(
            {
                "time": [int(c["t"]) for c in raw],
                "open": [float(c["o"]) for c in raw],
                "high": [float(c["h"]) for c in raw],
                "low": [float(c["l"]) for c in raw],
                "close": [float(c["c"]) for c in raw],
                "volume": [float(c["v"]) for c in raw],
            }
        )
        # Letzte Candle ist noch offen -> für Signale verwerfen
        if len(df) and df.iloc[-1]["time"] + INTERVAL_MS[interval] > end:
            df = df.iloc[:-1]
        return df.reset_index(drop=True)

    def mid_price(self, coin: str) -> float:
        return float(self.info.all_mids()[coin])

    def sz_decimals(self, coin: str) -> int:
        """Nachkommastellen für Ordergrößen laut Exchange-Metadaten."""
        if not self._sz_decimals:
            meta = self.info.meta()
            self._sz_decimals = {a["name"]: a["szDecimals"] for a in meta["universe"]}
        return self._sz_decimals[coin]

    # ---------- Account ----------

    def equity(self) -> float:
        state = self.info.user_state(self.account_address)
        return float(state["marginSummary"]["accountValue"])

    def position(self, coin: str) -> dict | None:
        """Offene Position für coin oder None. szi > 0 = long, < 0 = short."""
        state = self.info.user_state(self.account_address)
        for p in state.get("assetPositions", []):
            pos = p["position"]
            if pos["coin"] == coin and float(pos["szi"]) != 0:
                return {
                    "size": float(pos["szi"]),
                    "entry": float(pos["entryPx"]),
                    "unrealized_pnl": float(pos["unrealizedPnl"]),
                }
        return None

    # ---------- Orders ----------

    def set_leverage(self, coin: str, leverage: int) -> None:
        assert self.exchange, "Exchange nicht initialisiert (kein Private Key)"
        res = self.exchange.update_leverage(leverage, coin, is_cross=True)
        log.info("Leverage %sx für %s gesetzt: %s", leverage, coin, res.get("status"))

    def market_open(self, coin: str, is_buy: bool, size: float, slippage: float) -> dict:
        assert self.exchange, "Exchange nicht initialisiert (kein Private Key)"
        size = round(size, self.sz_decimals(coin))
        res = self.exchange.market_open(coin, is_buy, size, None, slippage)
        self._raise_on_error(res)
        return res

    def market_close(self, coin: str, slippage: float) -> dict:
        assert self.exchange, "Exchange nicht initialisiert (kein Private Key)"
        res = self.exchange.market_close(coin, None, None, slippage)
        self._raise_on_error(res)
        return res

    @staticmethod
    def _raise_on_error(res: dict) -> None:
        if res.get("status") != "ok":
            raise RuntimeError(f"Order fehlgeschlagen: {res}")
        statuses = res["response"]["data"]["statuses"]
        errors = [s["error"] for s in statuses if "error" in s]
        if errors:
            raise RuntimeError(f"Order abgelehnt: {errors}")
