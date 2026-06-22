"""TWAP-Scout: laufende TWAP-Ausführungen großer Wallets aufspüren.

Ein TWAP (Time-Weighted Average Price) zerlegt eine große Order in viele
gleich große Slices in festem Takt - das mechanische Treppchen im Chart, mit
dem z.B. Justin Sun/Tron über Stunden akkumuliert. Der eigentliche Edge:
ein LAUFENDER TWAP erzeugt anhaltenden, vorhersehbaren Richtungsdruck, solange
er läuft - anders als ein einzelner Whale-Fill, der sofort eingepreist ist.

Hyperliquid taggt TWAP-Slices nativ (userTwapSliceFills je Adresse). Der Scout
prüft eine Kandidatenmenge (Watchlist inkl. bekannter Whales + Leader + frisch
geflaggte Anomalie-Wallets) auf aktive TWAPs: genug Slices im jüngsten
Zeitfenster = TWAP läuft.

Reiner Beobachter: meldet an Journal (kind: twap), runtime/twap.jsonl, Status,
Telegram. HANDELT NIE. Ehrliche Grenze: bis die Slice-Kadenz erkennbar ist,
ist ein Teil des Moves gelaufen (Lag wie bei jedem Follower).
"""

import json
import logging
import time
from collections import defaultdict

from .journal import RUNTIME

log = logging.getLogger(__name__)


def detect_active_twaps(slices: list, now_ms: int, window_min: int, min_slices: int) -> list[dict]:
    """Findet je Coin aktive TWAPs aus den Slice-Fills einer Adresse (reine Funktion).

    Aktiv = mindestens `min_slices` Slices innerhalb der letzten `window_min`
    Minuten. Richtung aus der Netto-Slice-Größe.
    """
    cutoff = now_ms - window_min * 60_000
    by_coin: dict[str, list] = defaultdict(list)
    for s in slices:
        f = s.get("fill", s)  # toleriert {"fill":..} und flaches Format
        try:
            t = int(f.get("time", 0))
        except (TypeError, ValueError):
            continue
        if t >= cutoff and f.get("coin"):
            by_coin[f["coin"]].append(f)

    out = []
    for coin, fs in by_coin.items():
        if len(fs) < min_slices:
            continue
        net = sum(float(f["sz"]) * (1 if f.get("side") == "B" else -1) for f in fs)
        notional = sum(float(f["sz"]) * float(f["px"]) for f in fs)
        span_min = (max(int(f["time"]) for f in fs) - min(int(f["time"]) for f in fs)) / 60_000
        out.append({
            "coin": coin,
            "side": "LONG" if net >= 0 else "SHORT",
            "slices": len(fs),
            "notional": round(notional, 0),
            "span_min": round(span_min, 1),
        })
    return out


class TwapScout:
    def __init__(self, market_info, cfg, address_source=None, notifier=None,
                 journal=None, clock=time.time, sleep=time.sleep):
        self.market = market_info       # Info (Mainnet): user_twap_slice_fills, all_mids
        self.cfg = cfg
        self.address_source = address_source or (lambda: [])
        self.notifier = notifier
        self.journal = journal
        self.clock = clock
        self.sleep = sleep
        self.path = RUNTIME / "twap.jsonl"
        self.flagged: list[dict] = []
        self._last_scan = 0.0
        self._state: dict[str, float] = {}   # "addr:coin:side" -> zuletzt gemeldet

    def tick(self) -> None:
        if self.clock() - self._last_scan < self.cfg.poll_seconds:
            return
        self._last_scan = self.clock()
        self.scan()

    def scan(self) -> list[dict]:
        findings = []
        addresses = self._candidates()
        try:
            mids = {k: float(v) for k, v in self.market.all_mids().items()}
        except Exception as e:
            log.warning("TWAP-Scout: all_mids nicht abrufbar: %s", str(e)[:80])
            mids = {}
        now_ms = int(self.clock() * 1000)
        checked = 0
        for addr in addresses:
            if checked >= self.cfg.max_checks_per_scan:
                break
            checked += 1
            if checked > 1 and self.cfg.throttle_s:
                self.sleep(self.cfg.throttle_s)
            try:
                slices = self.market.user_twap_slice_fills(addr) or []
            except Exception as e:
                log.warning("TWAP-Slices für %s nicht abrufbar: %s", addr[:10], str(e)[:80])
                continue
            for tw in detect_active_twaps(slices, now_ms, self.cfg.active_window_minutes,
                                          self.cfg.min_slices):
                key = f"{addr}:{tw['coin']}:{tw['side']}"
                if self.clock() - self._state.get(key, 0) < self.cfg.recheck_hours * 3600:
                    continue  # dieselbe laufende TWAP nicht im Minutentakt neu melden
                self._state[key] = self.clock()
                tw["address"] = addr
                tw["price"] = mids.get(tw["coin"], 0.0)
                self._report(tw)
                findings.append(tw)
        self._prune()
        return findings

    def _candidates(self) -> list[str]:
        seen, out = set(), []
        for a in list(self.cfg.extra_addresses) + list(self.address_source()):
            a = (a or "").lower()
            if a and a not in seen:
                seen.add(a)
                out.append(a)
        return out

    def _report(self, t: dict) -> None:
        log.info("TWAP %s: %s %s, %d Slices über %.0fmin, $%s",
                 t["address"][:10], t["side"], t["coin"], t["slices"],
                 t["span_min"], f"{t['notional']:,.0f}")
        if self.journal:
            self.journal.record("twap", **t)
        try:
            RUNTIME.mkdir(exist_ok=True)
            with open(self.path, "a") as fh:
                fh.write(json.dumps({"t": int(self.clock()), **t}, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("twap.jsonl nicht schreibbar")
        if self.notifier:
            self.notifier.send(
                f"🧊 <b>TWAP läuft</b>: {t['side']} {t['coin']}\n"
                f"<code>{t['address']}</code>\n"
                f"{t['slices']} Slices über {t['span_min']:.0f}min, ${t['notional']:,.0f}\n"
                f"= anhaltender {'Kauf' if t['side'] == 'LONG' else 'Verkauf'}druck, solange er läuft"
            )
        self.flagged.append({"t": int(self.clock()), **t})
        del self.flagged[:-20]

    def _prune(self) -> None:
        cutoff = self.clock() - 2 * self.cfg.recheck_hours * 3600
        self._state = {k: v for k, v in self._state.items() if v > cutoff}
