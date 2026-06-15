"""Polymarket-Scout: erfahrenes Geld in Prediction Markets beobachten (read-only).

Polymarket läuft on-chain (Polygon) - jede Position und jeder Trade hängt an
einer sichtbaren Wallet. Das macht dasselbe möglich wie unser Anomalie-Scout
auf Hyperliquid: große, frische Wetten von Wallets mit nachweislich
profitabler Historie aufspüren ("informed money").

WICHTIG - das ist ein SEPARATES Tier, kein Handelsmodul:
  - Prediction Markets sind binäre Outcomes (lösen zu 0/1 auf), kein Perp-Preis.
  - Unser Risiko-/Sizing-/Validator-Stack greift hier NICHT.
  - Dieser Scout HANDELT NIE. Er beobachtet, prüft, meldet - mehr nicht.
  Erst wenn die Meldungen über Wochen zeigen, dass da ein folgbares Signal ist,
  lohnt sich der Gedanke an einen eigenen Execution-Stack.

Datenquelle: Polymarket Data-API (öffentlich, kein Key). Endpunkte sind in der
Config überschreibbar, falls die API sich ändert. Ausfall = still, nie Crash.
"""

import json
import logging
import time

from .journal import RUNTIME

log = logging.getLogger(__name__)


def _http_get(url: str, params: dict) -> list | dict:
    import requests

    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


class PolymarketScout:
    def __init__(self, cfg, notifier=None, journal=None, fetch=None,
                 clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.notifier = notifier
        self.journal = journal
        self.fetch = fetch or _http_get
        self.clock = clock
        self.sleep = sleep
        self.path = RUNTIME / "polymarket.jsonl"
        self.flagged: list[dict] = []
        self._seen: dict[str, float] = {}   # wallet -> zuletzt geprüft
        self._last_scan = 0.0

    # ---------- Lebenszyklus ----------

    def tick(self) -> None:
        if self.clock() - self._last_scan < self.cfg.poll_seconds:
            return
        self._last_scan = self.clock()
        self.scan()

    def scan(self) -> list[dict]:
        findings, checked = [], 0
        for addr, trig in self._discover().items():
            if checked >= self.cfg.max_checks_per_scan:
                break
            if self.clock() - self._seen.get(addr, 0) < self.cfg.recheck_hours * 3600:
                continue
            self._seen[addr] = self.clock()
            checked += 1
            if checked > 1 and self.cfg.throttle_s:
                self.sleep(self.cfg.throttle_s)
            try:
                finding = self._check(addr, trig)
            except Exception as e:
                log.warning("Polymarket-Check %s fehlgeschlagen: %s", addr[:10], str(e)[:80])
                continue
            if finding:
                self._report(finding)
                findings.append(finding)
        self._prune()
        return findings

    # ---------- Discovery: große Trades im öffentlichen Strom ----------

    def _discover(self) -> dict[str, dict]:
        try:
            trades = self.fetch(self.cfg.trades_url, {"limit": self.cfg.trade_limit}) or []
        except Exception as e:
            log.warning("Polymarket recent trades nicht abrufbar: %s", str(e)[:80])
            return {}
        out: dict[str, dict] = {}
        for t in trades:
            addr = t.get("proxyWallet") or t.get("wallet") or t.get("user") or ""
            if not addr:
                continue
            notional = self._notional(t)
            if notional < self.cfg.min_trade_notional:
                continue
            # größten Trade je Wallet behalten
            if addr not in out or notional > out[addr]["notional"]:
                out[addr] = {
                    "notional": notional,
                    "market": t.get("title") or t.get("market") or "?",
                    "outcome": t.get("outcome") or t.get("outcomeIndex", ""),
                    "side": t.get("side", ""),
                }
        return out

    @staticmethod
    def _notional(t: dict) -> float:
        if t.get("usdcSize") is not None:
            return abs(float(t["usdcSize"]))
        return abs(float(t.get("size", 0)) * float(t.get("price", 0)))

    # ---------- Prüfung: profitable Historie? ----------

    def _check(self, addr: str, trig: dict) -> dict | None:
        positions = self.fetch(self.cfg.positions_url, {"user": addr}) or []
        realized = sum(float(p.get("realizedPnl", 0) or 0) for p in positions)
        current_value = sum(abs(float(p.get("currentValue", p.get("value", 0)) or 0)) for p in positions)
        if realized < self.cfg.min_wallet_profit:
            return None  # keine nachweisbar profitable Historie -> kein "smart money"
        return {
            "address": addr,
            "market": trig["market"],
            "outcome": str(trig["outcome"]),
            "side": str(trig["side"]),
            "bet_usdc": round(trig["notional"], 0),
            "realized_pnl": round(realized, 0),
            "portfolio_usdc": round(current_value, 0),
        }

    # ---------- Meldung (beobachten, nie handeln) ----------

    def _report(self, f: dict) -> None:
        log.info("Polymarket: %s wettet $%s auf '%s' (%s) | Track-Record PnL $%s",
                 f["address"][:10], f"{f['bet_usdc']:,.0f}", f["market"][:40],
                 f["outcome"], f"{f['realized_pnl']:,.0f}")
        if self.journal:
            self.journal.record("polymarket", **f)
        try:
            RUNTIME.mkdir(exist_ok=True)
            with open(self.path, "a") as fh:
                fh.write(json.dumps({"t": int(self.clock()), **f}, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("polymarket.jsonl nicht schreibbar")
        if self.notifier:
            self.notifier.send(
                f"🎲 <b>Polymarket</b>: erfahrenes Geld wettet groß\n"
                f"<code>{f['address']}</code>\n"
                f"${f['bet_usdc']:,.0f} auf <b>{f['outcome']}</b>\n"
                f"„{f['market'][:80]}\"\n"
                f"Track-Record: PnL ${f['realized_pnl']:,.0f} | Portfolio ${f['portfolio_usdc']:,.0f}"
            )
        self.flagged.append({"t": int(self.clock()), **f})
        del self.flagged[:-20]

    def _prune(self) -> None:
        cutoff = self.clock() - 2 * self.cfg.recheck_hours * 3600
        self._seen = {a: t for a, t in self._seen.items() if t > cutoff}
