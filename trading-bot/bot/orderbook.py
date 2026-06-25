"""Orderbuch-Scout: Mikrostruktur lesen, die der Trade-Strom nicht zeigt.

Der Anomalie-Scout sieht nur AUSGEFÜHRTE Trades. Das Orderbuch verrät mehr:
ruhende Liquidität, große Walls (Support/Resistance), Bid/Ask-Imbalance und
plötzliches Abziehen von Tiefe vor einem Move. Das ist eine der ECHTEN
strukturellen Edges - im Gegensatz zur commoditisierten TA.

Ehrliche Grenze: Als nicht-kolokierter Bot, der das Buch im Sekundentakt
pollt, fangen wir nur die LANGSAMEN Mikrostruktur-Signale (anhaltende
Imbalance, stehende Walls, ausdünnende Liquidität) - nicht das HFT-/Spoofing-
Spiel im Millisekundenbereich. Und Imbalance-Vorhersagekraft zerfällt in
Minuten; die Follow-through-Messung im Report (1h-Raster) ist nur ein grober
erster Indikator.

Reiner Beobachter: Treffer gehen an Journal (kind: orderbook),
runtime/orderbook.jsonl, Status und optional Telegram. HANDELT NIE.
"""

import json
import logging
import time

from .journal import RUNTIME

log = logging.getLogger(__name__)


def analyze_book(levels: list, mid: float, band_pct: float, wall_ratio: float) -> dict | None:
    """Mikrostruktur-Kennzahlen aus einem L2-Snapshot (reine Funktion).

    levels: [bids, asks], je Level {"px": .., "sz": ..}. Nur Levels innerhalb
    band_pct um den Mid zählen (der Rand des Buchs ist irrelevant fürs Signal).
    """
    if not mid or not levels or len(levels) < 2:
        return None
    bids, asks = levels[0] or [], levels[1] or []
    lo, hi = mid * (1 - band_pct), mid * (1 + band_pct)

    def near(side_levels, is_bid):
        out = []
        for l in side_levels:
            px, sz = float(l["px"]), float(l["sz"])
            if (is_bid and px >= lo) or (not is_bid and px <= hi):
                out.append((px, sz))
        return out

    near_bids, near_asks = near(bids, True), near(asks, False)
    bid_depth = sum(sz for _, sz in near_bids)
    ask_depth = sum(sz for _, sz in near_asks)
    total = bid_depth + ask_depth
    if total <= 0:
        return None
    imbalance = (bid_depth - ask_depth) / total

    all_near = near_bids + near_asks
    mean_sz = sum(sz for _, sz in all_near) / len(all_near) if all_near else 0.0
    wall_px, wall_sz, wall_side = 0.0, 0.0, ""
    if mean_sz > 0:
        for is_bid, lv in ((True, near_bids), (False, near_asks)):
            for px, sz in lv:
                if sz > wall_sz:
                    wall_px, wall_sz, wall_side = px, sz, "bid" if is_bid else "ask"
    wall_strength = wall_sz / mean_sz if mean_sz > 0 else 0.0

    return {
        "imbalance": round(imbalance, 3),
        "bid_depth": round(bid_depth, 4),
        "ask_depth": round(ask_depth, 4),
        "wall_side": wall_side,
        "wall_px": wall_px,
        "wall_strength": round(wall_strength, 1),
        "has_wall": wall_strength >= wall_ratio,
    }


class OrderBookScout:
    def __init__(self, market_info, cfg, notifier=None, journal=None,
                 clock=time.time, sleep=time.sleep):
        self.market = market_info       # Info (Mainnet) mit l2_snapshot/all_mids
        self.cfg = cfg
        self.notifier = notifier
        self.journal = journal
        self.clock = clock
        self.sleep = sleep
        self.path = RUNTIME / "orderbook.jsonl"
        self.flagged: list[dict] = []
        self._last_scan = 0.0
        self._state: dict[str, str] = {}   # coin -> letzte Signal-Signatur (Entprellung)

    def tick(self) -> None:
        if self.clock() - self._last_scan < self.cfg.poll_seconds:
            return
        self._last_scan = self.clock()
        self.scan()

    def scan(self) -> list[dict]:
        findings = []
        try:
            mids = {k: float(v) for k, v in self.market.all_mids().items()}
        except Exception as e:
            log.warning("Orderbuch-Scout: all_mids nicht abrufbar: %s", str(e)[:80])
            return findings
        for i, coin in enumerate(self.cfg.coins):
            mid = mids.get(coin)
            if not mid:
                continue
            if i and self.cfg.throttle_s:
                self.sleep(self.cfg.throttle_s)
            try:
                snap = self.market.l2_snapshot(coin)
            except Exception as e:
                log.warning("Orderbuch %s nicht abrufbar: %s", coin, str(e)[:80])
                continue
            metrics = analyze_book(snap.get("levels", []), mid,
                                   self.cfg.band_pct, self.cfg.wall_ratio)
            if not metrics:
                continue
            f = self._evaluate(coin, mid, metrics)
            if f:
                findings.append(f)
        return findings

    def _evaluate(self, coin: str, mid: float, m: dict) -> dict | None:
        strong = abs(m["imbalance"]) >= self.cfg.imbalance_threshold
        if not strong and not m["has_wall"]:
            self._state.pop(coin, None)
            return None
        # Richtung: bei starker Imbalance deren Vorzeichen; bei reinem Wall-Treffer
        # NICHT das Sub-Schwellen-Rauschen, sondern die Wall-Semantik (Bid-Wall =
        # Support = bullisch, Ask-Wall = Resistance = bärisch).
        if strong:
            side = "LONG" if m["imbalance"] > 0 else "SHORT"
        else:
            side = "LONG" if m["wall_side"] == "bid" else "SHORT"
        # Entprellung: nur bei Zustandswechsel melden (Imbalance-Vorzeichen / Wall-Seite)
        sig = f"{side if strong else ''}:{m['wall_side'] if m['has_wall'] else ''}"
        if self._state.get(coin) == sig:
            return None
        self._state[coin] = sig
        finding = {
            "coin": coin, "price": mid, "side": side,
            "imbalance": m["imbalance"], "wall": "",
        }
        if m["has_wall"]:
            dist = (m["wall_px"] / mid - 1) * 100
            finding["wall"] = f"{m['wall_side']} {m['wall_strength']:.0f}x @ {dist:+.2f}%"
        self._report(finding, strong)
        return finding

    def _report(self, f: dict, strong: bool) -> None:
        log.info("Orderbuch %s: imbalance %+.2f%s", f["coin"], f["imbalance"],
                 f" | Wall {f['wall']}" if f["wall"] else "")
        if self.journal:
            self.journal.record("orderbook", **f)
        try:
            RUNTIME.mkdir(exist_ok=True)
            with open(self.path, "a") as fh:
                fh.write(json.dumps({"t": int(self.clock()), **f}, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("orderbook.jsonl nicht schreibbar")
        # Telegram nur, wenn ausdrücklich gewünscht (cfg.notify) UND starke
        # Imbalance - Orderbuch-Signale sind häufig und würden sonst zuspammen.
        if self.notifier and strong and getattr(self.cfg, "notify", False):
            self.notifier.send(
                f"📖 <b>Orderbuch {f['coin']}</b>: Imbalance {f['imbalance']:+.2f} "
                f"({'Bid-Übergewicht' if f['imbalance'] > 0 else 'Ask-Übergewicht'})"
                + (f"\nWall: {f['wall']}" if f["wall"] else "")
            )
        self.flagged.append({"t": int(self.clock()), **f})
        del self.flagged[:-20]
