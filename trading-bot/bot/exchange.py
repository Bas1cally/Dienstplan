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
    """Zwei Ebenen: Marktdaten IMMER vom Mainnet (alle Perp-DEXs - Krypto plus
    Builder-DEXs mit Aktien, Gold, Silber, Öl), Account/Orders auf dem
    konfigurierten Netz. So sieht der Bot echte Preise, auch im Testnet-Modus.
    """

    def __init__(self, testnet: bool, private_key: str | None = None,
                 account_address: str | None = None, dexs: str | list = "auto",
                 timeout: float | None = None):
        # timeout=None (Default) erhält das bisherige Verhalten überall im Bot -
        # explizit gesetzt (z.B. report.py für /fullreport) verhindert es, dass
        # ein degradiertes HL-API den aufrufenden Thread für immer hängen lässt
        # (requests wartet sonst UNBEGRENZT auf eine Antwort, kein Timeout-Fehler,
        # kein Crash - der Thread stirbt einfach nie, das try/except greift nie).
        self.base_url = api_url(testnet)
        self.testnet = testnet
        self.account_address = account_address
        self.timeout = timeout
        # Account-/Order-Ebene (testnet oder mainnet)
        self.info = Info(self.base_url, skip_ws=True, timeout=timeout)
        # Datenebene: Mainnet, alle gewünschten Perp-DEXs
        self.dexs = self._resolve_dexs(dexs, timeout=timeout)
        try:
            self.market = Info(api_url(False), skip_ws=True, perp_dexs=self.dexs, timeout=timeout)
        except Exception:
            log.exception("Multi-DEX-Marktdaten nicht ladbar - nur Haupt-DEX")
            self.dexs = [""]
            self.market = Info(api_url(False), skip_ws=True, timeout=timeout)
        # Builder-DEXs existieren nur auf Mainnet - Testnet-Orders nur Haupt-DEX
        self.exec_dexs = self.dexs if not testnet else [""]
        self._max_leverage_cache: dict[str, int] | None = None  # lazy, s. max_leverage()
        self.exchange = None
        if private_key:
            from eth_account import Account
            from hyperliquid.exchange import Exchange

            wallet = Account.from_key(private_key)
            self.exchange = Exchange(wallet, self.base_url, account_address=account_address,
                                     perp_dexs=self.exec_dexs if self.exec_dexs != [""] else None)

    @staticmethod
    def _resolve_dexs(dexs: str | list, timeout: float | None = None) -> list[str]:
        if isinstance(dexs, list):
            return dexs or [""]
        if dexs != "auto":
            return [""]
        try:
            probe = Info(api_url(False), skip_ws=True, timeout=timeout)
            names = [d["name"] for d in probe.perp_dexs()[1:] if d and d.get("name")]
            log.info("Perp-DEXs entdeckt: Haupt-DEX + %s", names or "keine Builder-DEXs")
            return [""] + names
        except Exception:
            log.warning("DEX-Discovery fehlgeschlagen - nur Haupt-DEX (Krypto)")
            return [""]

    # ---------- Marktdaten (immer Mainnet, alle DEXs) ----------

    def candles(self, coin: str, interval: str, lookback: int) -> pd.DataFrame:
        """Holt die letzten `lookback` Candles als OHLCV-DataFrame."""
        end = int(time.time() * 1000)
        start = end - lookback * INTERVAL_MS[interval]
        raw = self.market.candles_snapshot(coin, interval, start, end)
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

    def all_mids(self) -> dict[str, str]:
        """Mid-Preise über ALLE DEXs gemerged (Krypto + Aktien/Gold/Öl)."""
        merged: dict[str, str] = {}
        for dex in self.dexs:
            try:
                merged.update(self.market.all_mids(dex=dex))
            except Exception:
                log.debug("all_mids für DEX %r nicht abrufbar", dex, exc_info=True)
        return merged

    def mid_price(self, coin: str) -> float:
        return float(self.all_mids()[coin])

    def sz_decimals(self, coin: str) -> int:
        """Nachkommastellen für Ordergrößen laut Exchange-Metadaten (alle DEXs)."""
        return self.market.asset_to_sz_decimals[self.market.coin_to_asset[coin]]

    def max_leverage(self, coin: str) -> int | None:
        """Hyperliquids ECHTES Hebel-Limit für diesen Coin (Live-Fund, Nutzer
        17.07.: je Asset unterschiedlich - Meme-Perps wie PENGU oft nur 3-5x
        statt der 10-50x bei Majors; ein Paper-Bot, der das ignoriert, testet
        Trades, die die Exchange gar nicht anbietet). EINMAL je Prozess-Lauf
        über alle DEXs gecacht (Hebel-Limits ändern sich selten, Neustart/
        Deploy reicht als Refresh-Zyklus - wie asset_to_sz_decimals der SDK).
        None = kein Datenpunkt (unbekannter Coin oder Metadaten nicht ladbar) -
        Aufrufer soll dann NICHT kappen, kein Limit ist kein Freifahrtschein."""
        if self._max_leverage_cache is None:
            cache: dict[str, int] = {}
            for dex in self.dexs:
                try:
                    for a in self.market.meta(dex=dex)["universe"]:
                        lev = a.get("maxLeverage")
                        if lev:
                            cache[a["name"]] = int(lev)
                except Exception:
                    log.warning("max_leverage-Metadaten für DEX %r nicht ladbar",
                               dex, exc_info=True)
            # LEEREN Cache NICHT festschreiben (Spiegel-Fund 20.07.: der Bot
            # startete in einen HL-429-Sturm, meta() schlug für alle DEXs
            # fehl, das leere Dict wurde für die GESAMTE Prozess-Laufzeit
            # gecacht - jeder Entry stand mit hl_max_leverage=null im
            # Journal und die PENGU-Hebel-Kappung war de facto aus). Ohne
            # Daten beim nächsten Aufruf erneut versuchen; das ist billig,
            # weil der Lookup nur je Einstieg läuft, nicht je Tick.
            if cache:
                self._max_leverage_cache = cache
            else:
                return None
        return self._max_leverage_cache.get(coin)

    # ---------- Account ----------

    def merged_user_state(self, address: str) -> dict:
        """user_state über alle Ausführungs-DEXs: Equity summiert, Positionen vereint.

        Builder-DEXs haben separates Collateral - für das Gesamtbild zählt die
        Summe; Positions-Coins sind über DEXs hinweg eindeutig benannt.
        """
        equity = 0.0
        positions: list = []
        for dex in self.exec_dexs:
            try:
                state = self.info.user_state(address, dex=dex)
            except Exception:
                log.debug("user_state für DEX %r nicht abrufbar", dex, exc_info=True)
                continue
            equity += float(state["marginSummary"]["accountValue"])
            positions.extend(state.get("assetPositions", []))
        return {"marginSummary": {"accountValue": str(equity)}, "assetPositions": positions}

    def equity(self) -> float:
        return float(self.merged_user_state(self.account_address)["marginSummary"]["accountValue"])

    def position(self, coin: str) -> dict | None:
        """Offene Position für coin oder None. szi > 0 = long, < 0 = short."""
        state = self.merged_user_state(self.account_address)
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

    @staticmethod
    def round_px(px: float) -> float:
        """Preisrundung wie im SDK (_slippage_price): 5 signifikante Stellen,
        max. 6 Dezimalen - sonst lehnt Hyperliquid die Order ab."""
        return round(float(f"{px:.5g}"), 6)

    def smart_order(self, coin: str, is_buy: bool, size: float, slippage: float,
                    timeout_s: float = 20.0, poll_s: float = 1.0) -> str:
        """Maker-first-Ausführung: Post-Only-Limit zum Mid, bei Timeout/Reject
        Market-Fallback. Liefert 'maker' oder 'taker_fallback' (fürs Journal).

        Der Fee-Unterschied (Maker ~0.015% vs. Taker ~0.045% + Slippage) ist
        bei häufigem Rebalancing einer der größten messbaren Edges.
        """
        assert self.exchange, "Exchange nicht initialisiert (kein Private Key)"
        size = round(size, self.sz_decimals(coin))
        px = self.round_px(self.mid_price(coin))
        try:
            res = self.exchange.order(coin, is_buy, size, px, {"limit": {"tif": "Alo"}})
            status = res["response"]["data"]["statuses"][0]
        except Exception:
            log.exception("Maker-Order %s fehlgeschlagen - Market-Fallback", coin)
            self.market_open(coin, is_buy, size, slippage)
            return "taker_fallback"

        if "filled" in status:  # sofort als Maker gefüllt (selten, aber möglich)
            return "maker"
        if "error" in status:   # ALO abgelehnt (würde sofort matchen) -> Taker
            log.info("Maker-Order %s abgelehnt (%s) - Market-Fallback", coin, status["error"])
            self.market_open(coin, is_buy, size, slippage)
            return "taker_fallback"

        oid = status["resting"]["oid"]
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(poll_s)
            try:
                st = self.info.query_order_by_oid(self.account_address, oid)
                order_status = st.get("order", {}).get("status", "")
                if order_status == "filled":
                    return "maker"
                if order_status in ("canceled", "rejected"):
                    break
            except Exception:
                log.debug("Order-Status %s nicht abrufbar", oid, exc_info=True)

        # Timeout: Rest canceln und den noch offenen Teil als Market nachziehen
        remaining = size
        try:
            self.exchange.cancel(coin, oid)
            st = self.info.query_order_by_oid(self.account_address, oid)
            order = st.get("order", {}).get("order", {})
            if order:
                remaining = float(order.get("sz", size))  # sz = noch offener Rest
        except Exception:
            log.debug("Cancel/Status %s: nehme volle Restgröße an", oid, exc_info=True)
        if remaining > 0:
            self.market_open(coin, is_buy, round(remaining, self.sz_decimals(coin)), slippage)
        return "taker_fallback"

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
