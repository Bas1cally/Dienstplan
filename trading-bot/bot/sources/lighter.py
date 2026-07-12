"""Lighter (zkLighter) als Copy-Quelle: fremde Positionen lesen -> LeaderSnapshot.

Die Probe (probe_dexs.py) hat bestätigt: Lighter gibt die offenen Positionen
JEDES Kontos öffentlich her (GET /api/v1/account?by=index|l1_address). Es gibt
KEIN Ranking-API - deshalb kommen die zu kopierenden Trader aus einer Watchlist
(config lighter.accounts), die der Nutzer aus dem Lighter-Web-Leaderboard füllt.

Feldnamen aus dem offiziellen SDK (AccountPosition): symbol, sign, position
(Betrag), avg_entry_price, position_value; Account: collateral (= Equity-Proxy).
Trotzdem defensiv geparst - erst am echten Konto per /lighter verifizieren,
dann darauf messen. Ausführung bleibt IMMER Hyperliquid.
"""

import json
import logging
import time

from ..copytrade.tracker import LeaderPosition, LeaderSnapshot
from ..journal import RUNTIME

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://mainnet.zklighter.elliot.ai"


def _http_get(url: str, params: dict):
    import requests

    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _first_account(data) -> dict:
    """Toleriert {"accounts":[{...}]}, {"account":{...}} oder das Konto direkt."""
    if isinstance(data, dict):
        if isinstance(data.get("accounts"), list) and data["accounts"]:
            return data["accounts"][0]
        if isinstance(data.get("account"), dict):
            return data["account"]
        return data
    if isinstance(data, list) and data:
        return data[0]
    return {}


def _positions_of(acc: dict) -> list:
    for key in ("positions", "position", "openPositions"):
        v = acc.get(key)
        if isinstance(v, list):
            return v
    return []


def _is_long(pos: dict) -> bool:
    """sign-Semantik defensiv: >0/„long"/1 = long, <0/2/„short" = short.
    (Wird am echten Konto per /lighter verifiziert.)"""
    s = pos.get("sign")
    if isinstance(s, str):
        return s.lower().startswith("l") or s in ("1", "+1")
    try:
        return int(s) >= 0 and int(s) != 2
    except (TypeError, ValueError):
        return float(pos.get("position", 0) or 0) >= 0


class LighterClient:
    def __init__(self, base_url: str = DEFAULT_BASE, http=None):
        self.base = base_url.rstrip("/")
        self._http = http or _http_get

    def raw_account(self, ref: str) -> dict:
        by = "l1_address" if str(ref).lower().startswith("0x") else "index"
        return _first_account(self._http(f"{self.base}/api/v1/account",
                                         {"by": by, "value": str(ref)}))

    def markets(self) -> list[dict]:
        """Marktliste (GET /api/v1/orderBooks): liefert market_id <-> symbol.
        Nötig, weil recentTrades einen market_id-Pflichtparameter braucht -
        Lighter ist marktbasiert, es gibt keinen globalen Trade-Strom wie bei HL."""
        data = self._http(f"{self.base}/api/v1/orderBooks", {})
        if isinstance(data, dict):
            for k in ("order_books", "orderBooks", "markets", "data", "result"):
                if isinstance(data.get(k), list):
                    return data[k]
            return []
        return data if isinstance(data, list) else []

    def recent_trades(self, market_id: int, limit: int = 100) -> list:
        """Trade-Strom EINES Markts (market_id ist Pflicht laut API). Toleriert
        mehrere Response-Formen."""
        data = self._http(f"{self.base}/api/v1/recentTrades",
                          {"market_id": market_id, "limit": limit})
        if isinstance(data, dict):
            for k in ("trades", "recentTrades", "data", "result"):
                if isinstance(data.get(k), list):
                    return data[k]
            return []
        return data if isinstance(data, list) else []

    def snapshot(self, ref: str, map_coin=None) -> LeaderSnapshot:
        acc = self.raw_account(ref)
        equity = float(acc.get("collateral") or acc.get("available_balance") or 0)
        positions: dict[str, LeaderPosition] = {}
        for p in _positions_of(acc):
            size_abs = abs(float(p.get("position") or 0))
            if size_abs == 0:
                continue
            symbol = str(p.get("symbol") or "").upper()
            coin = map_coin(symbol) if map_coin else symbol
            if not coin:
                continue  # auf HL nicht handelbar -> überspringen
            long = _is_long(p)
            positions[coin] = LeaderPosition(
                coin=coin,
                size=size_abs if long else -size_abs,
                entry=float(p.get("avg_entry_price") or 0),
                position_value=abs(float(p.get("position_value") or 0)),
                leverage=1.0,
            )
        return LeaderSnapshot(address=str(ref), equity=equity, positions=positions)


class LighterSource:
    """CopySource-konform: Watchlist als Discovery, Snapshots von Lighter."""

    name = "lighter"

    def __init__(self, cfg, client: LighterClient | None = None):
        self.cfg = cfg                        # LighterConfig
        self.client = client or LighterClient(cfg.base_url)
        self._hl_coins = set(cfg.coins) if cfg.coins else None
        self._market_ids: list[int] | None = None   # Cache, Märkte wechseln selten
        self.last_scan_note = ""              # sichtbare Diagnose statt stillem Fehlschlag

    def discover(self, min_score: float = 0) -> list[str]:
        return [str(a) for a in (self.cfg.accounts or [])]

    def snapshot(self, ref: str) -> LeaderSnapshot:
        return self.client.snapshot(ref, map_coin=self.map_coin)

    def map_coin(self, venue_symbol: str) -> str | None:
        # Lighter-Symbole sind meist reine Coins (BTC, ETH, SOL); nur auf HL
        # handelbare zulassen (Whitelist, sonst alles durch).
        sym = venue_symbol.upper().replace("-USD", "").replace("USD", "").strip()
        if self._hl_coins is not None and sym not in self._hl_coins:
            return None
        return sym or None

    # ---------- Auto-Discovery: aktive Konten aus dem Trade-Strom ----------

    def _resolve_market_ids(self) -> list[int]:
        """symbol -> market_id, gefiltert auf die konfigurierte Coin-Liste. Gecacht,
        weil Märkte selten wechseln - recentTrades braucht market_id als Pflichtfeld."""
        if self._market_ids is not None:
            return self._market_ids
        try:
            raw = self.client.markets()
        except Exception as e:
            log.warning("Lighter orderBooks nicht abrufbar: %s", str(e)[:80])
            self._market_ids = []
            return []
        wanted = set(self.cfg.coins) if self.cfg.coins else None
        ids = []
        for m in raw:
            sym = str(m.get("symbol") or "").upper().replace("-USD", "").replace("USD", "")
            mid = m.get("market_id")
            if mid is None or (wanted is not None and sym not in wanted):
                continue
            try:
                ids.append(int(mid))
            except (TypeError, ValueError):
                continue
        self._market_ids = ids
        return ids

    def discover_active(self) -> list[str]:
        """Zieht Kandidaten-Konten aus dem öffentlichen Trade-Strom (je Markt,
        market_id ist Pflicht) + Seeds. Setzt last_scan_note bei Problemen -
        sonst verschwindet ein Fehlschlag sonst spurlos in 'sucht…'."""
        cands: list[str] = [str(a) for a in (self.cfg.accounts or [])]  # Seeds zuerst
        if not getattr(self.cfg, "auto_discover", False):
            return cands
        market_ids = self._resolve_market_ids()
        if not market_ids:
            self.last_scan_note = "keine Märkte aufgelöst (orderBooks nicht erreichbar/leer)"
            log.warning("Lighter: %s", self.last_scan_note)
            return cands
        seen = set(cands)
        found_any_trades = False
        for i, mid in enumerate(market_ids):
            if i and self.cfg.throttle_s:
                time.sleep(self.cfg.throttle_s)
            try:
                trades = self.client.recent_trades(mid, limit=100)
            except Exception as e:
                log.warning("Lighter recentTrades market_id=%s fehlgeschlagen: %s",
                           mid, str(e)[:80])
                continue
            if trades:
                found_any_trades = True
            for t in trades:
                for idx in _account_ids(t):
                    if idx not in seen:
                        seen.add(idx)
                        cands.append(idx)
        self.last_scan_note = ("" if found_any_trades else
                               f"{len(market_ids)} Märkte abgefragt, keine Trades erhalten")
        return cands

    def rank(self) -> list[LeaderSnapshot]:
        """Discovery -> Snapshot -> Filter -> Top-N nach Aktivität (Equity x Positionen).
        Lighter hat kein PnL-Ranking-API; das ist der pragmatische Aktivitäts-Proxy."""
        cands = self.discover_active()[: self.cfg.max_candidates]
        scored: list[tuple[float, LeaderSnapshot]] = []
        throttle = getattr(self.cfg, "throttle_s", 0.25)
        for i, ref in enumerate(cands):
            if i and throttle:
                time.sleep(throttle)   # Rate-Limit-Hygiene: nicht 40 Reads am Stück
            try:
                snap = self.snapshot(ref)
            except Exception:
                continue
            if snap.equity < self.cfg.min_equity or len(snap.positions) < self.cfg.min_positions:
                continue
            activity = snap.equity * len(snap.positions)
            scored.append((activity, snap))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [s for _, s in scored[: self.cfg.max_leaders]]
        if not top and not self.last_scan_note:
            self.last_scan_note = (f"{len(cands)} Kandidaten gescannt, keiner erfüllt "
                                   f"min_equity/min_positions")
        try:
            RUNTIME.mkdir(exist_ok=True)
            (RUNTIME / "lighter_leaders.json").write_text(json.dumps(
                {"t": int(time.time()), "leaders": [s.address for s in top],
                 "scanned": len(cands), "qualified": len(scored),
                 "note": self.last_scan_note}, indent=2))
        except OSError:
            pass
        return top


def _account_ids(trade: dict) -> list[str]:
    """Defensiv: alle int-artigen Werte unter Keys, die 'account' enthalten."""
    out = []
    for k, v in trade.items():
        if "account" in k.lower():
            try:
                out.append(str(int(v)))
            except (TypeError, ValueError):
                continue
    return out


class LighterShadow:
    """Misst 'Lighter-Signale, auf HL ausgeführt': kopiert die per rank() ermittelten
    Top-Lighter-Trader gleichgewichtet in ein eigenes Paper-Buch, gepreist mit
    HL-Preisen. Reine Mess-Spur (dry_run) - kein Einfluss aufs Haupt-Buch."""

    def __init__(self, cfg, fee_rate: float, source: LighterSource | None = None,
                 runtime_dir=None, clock=time.time):
        from ..config import AnalysisConfig, CopytradeConfig, RiskConfig
        from ..paper import PaperBroker

        runtime = runtime_dir or RUNTIME
        self.cfg = cfg
        self.clock = clock
        self.source = source or LighterSource(cfg)
        self.paper = PaperBroker(cfg.initial_equity, fee_rate, path=runtime / "lighter_shadow.json")
        self._ct = CopytradeConfig(analysis=AnalysisConfig(), copy_ratio=cfg.copy_ratio,
                                   max_alloc_per_coin=0.25, rebalance_threshold=0.03,
                                   min_notional=10)
        self._risk = RiskConfig(risk_per_trade=0.01, atr_stop_mult=2.0, take_profit_r=2.0,
                                max_leverage=4, max_daily_loss=1.0, slippage=0.005,
                                max_total_drawdown=0.5)
        self._last_scan = 0.0
        self._leaders: list = []

    def tick(self, hl_prices: dict[str, float]) -> None:
        if not hl_prices:
            return
        # Discovery/Ranking gedrosselt (teuer: Snapshot je Kandidat)
        if self.clock() - self._last_scan >= self.cfg.scan_seconds or not self._leaders:
            self._last_scan = self.clock()
            try:
                self._leaders = self.source.rank()
            except Exception as e:
                log.warning("Lighter-Ranking fehlgeschlagen: %s", str(e)[:80])
        if not self._leaders:
            return
        # Nur Coins mit HL-Preis behalten (auf HL handelbar + bepreisbar)
        snaps = []
        for snap in self._leaders:
            pos = {c: p for c, p in snap.positions.items() if hl_prices.get(c)}
            if pos:
                snaps.append(LeaderSnapshot(snap.address, snap.equity, pos))
        if not snaps:
            return
        from ..copytrade.copier import compute_targets, plan_rebalance

        weights = {s.address: 1.0 / len(snaps) for s in snaps}
        equity = self.paper.equity(hl_prices)
        targets = compute_targets(snaps, weights, equity, self._ct, self._risk)
        for o in plan_rebalance(targets, self.paper.sizes(), hl_prices, equity, self._ct):
            self.paper.execute(o.coin, o.delta_size, o.price)

    def stats(self, hl_prices: dict[str, float]) -> dict:
        eq = self.paper.equity(hl_prices) if hl_prices else self.cfg.initial_equity + self.paper.realized_pnl
        return {
            "equity": round(eq, 2),
            "return_pct": round((eq / self.cfg.initial_equity - 1) * 100, 2),
            "trades": self.paper.trades,
            "realized_pnl": round(self.paper.realized_pnl, 2),
            "leaders": [s.address for s in self._leaders],
            "open_positions": len(self.paper.positions),
            "note": self.source.last_scan_note,
        }
