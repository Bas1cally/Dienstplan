"""Anomalie-Scout: spürt "verdächtig gut informierte" Wallets im Trade-Strom auf.

Rechtliche Einordnung: Wir lesen ausschließlich ÖFFENTLICHE On-Chain-Daten -
auf Hyperliquid ist jeder Fill und jede Position jeder Adresse einsehbar.
Das ist On-Chain-Analyse, kein Insiderhandel (wir besitzen die nicht-öffentliche
Information ja nicht, wir sehen nur ihre Spuren im Markt).

Das gesuchte Muster ist der Klassiker für "jemand weiß was":
  frische Wallet (kaum Handelshistorie) + sofort eine große, konzentrierte
  Position in einem Coin.

Der Scout BEOBACHTET nur: Treffer gehen als Alert an Telegram, ins Journal
(kind: anomaly) und nach runtime/anomalies.jsonl. Er handelt NIE und hat
keinerlei Einfluss auf Order-Entscheidungen. Ehrliche Einordnung: viele
Treffer sind schlicht Whales mit neuer Wallet - erst ein paar Tage Treffer
ansehen (investigate.py <adresse>), bevor man daraus mehr macht.
"""

import json
import logging
import time

from .journal import RUNTIME

log = logging.getLogger(__name__)


class AnomalyScout:
    def __init__(self, info, cfg, notifier=None, journal=None,
                 clock=time.time, sleep=time.sleep):
        self.info = info              # Info-REST (Mainnet)
        self.cfg = cfg
        self.notifier = notifier
        self.journal = journal
        self.clock = clock
        self.sleep = sleep
        self.path = RUNTIME / "anomalies.jsonl"
        self.flagged: list[dict] = []     # letzte Funde (Status/Dashboard)
        self._seen: dict[str, float] = {}  # Adresse -> zuletzt geprüft
        self._last_scan = 0.0

    # ---------- Lebenszyklus ----------

    def tick(self) -> None:
        if self.clock() - self._last_scan < self.cfg.poll_seconds:
            return
        self._last_scan = self.clock()
        self.scan()

    def scan(self) -> list[dict]:
        """Ein Durchlauf: Trade-Strom sichten, neue Adressen prüfen, Funde melden."""
        findings = []
        checked = 0
        for addr, trigger in self._discover().items():
            if checked >= self.cfg.max_checks_per_scan:
                break
            if self.clock() - self._seen.get(addr, 0) < self.cfg.recheck_hours * 3600:
                continue
            self._seen[addr] = self.clock()
            checked += 1
            if checked > 1 and self.cfg.throttle_s:
                self.sleep(self.cfg.throttle_s)
            try:
                finding = self._check(addr)
            except Exception as e:
                log.warning("Anomalie-Check für %s fehlgeschlagen: %s", addr[:10], str(e)[:80])
                continue
            if finding:
                finding["trigger_coin"] = trigger["coin"]
                self._report(finding)
                findings.append(finding)
        self._prune_seen()
        return findings

    # ---------- Discovery: große Trades im öffentlichen Strom ----------

    def _discover(self) -> dict[str, dict]:
        """Adressen hinter auffällig großen Trades der beobachteten Coins."""
        out: dict[str, dict] = {}
        for coin in self.cfg.coins:
            try:
                trades = self.info.post("/info", {"type": "recentTrades", "coin": coin}) or []
            except Exception as e:
                log.warning("recentTrades %s fehlgeschlagen: %s", coin, str(e)[:80])
                continue
            for t in trades:
                notional = float(t.get("px", 0)) * float(t.get("sz", 0))
                if notional < self.cfg.min_trade_notional:
                    continue
                for user in t.get("users", []):
                    out.setdefault(user, {"coin": coin, "notional": notional})
        return out

    # ---------- Prüfung: frisch + groß + konzentriert? ----------

    def _check(self, addr: str) -> dict | None:
        now_ms = int(self.clock() * 1000)
        fills = self.info.user_fills_by_time(
            addr, now_ms - self.cfg.lookback_days * 86_400_000, now_ms) or []
        # "frisch" = kaum Aktivität VOR den letzten 24h (der heutige Burst zählt nicht)
        prior = [f for f in fills if int(f.get("time", 0)) < now_ms - 86_400_000]
        if len(prior) > self.cfg.max_prior_fills:
            return None

        state = self.info.user_state(addr)
        account_value = float(state["marginSummary"]["accountValue"])
        positions = []
        for ap in state.get("assetPositions", []):
            p = ap.get("position", {})
            value = abs(float(p.get("positionValue", 0)))
            if value > 0:
                positions.append((p.get("coin", "?"), float(p.get("szi", 0)), value))
        if not positions:
            return None
        total = sum(v for _, _, v in positions)
        coin, szi, largest = max(positions, key=lambda x: x[2])
        if largest < self.cfg.min_position_notional:
            return None
        concentration = largest / total
        if concentration < self.cfg.min_concentration:
            return None

        return {
            "address": addr,
            "coin": coin,
            "side": "LONG" if szi > 0 else "SHORT",
            "notional": round(largest, 0),
            "concentration": round(concentration, 2),
            "account_value": round(account_value, 0),
            "prior_fills": len(prior),
        }

    # ---------- Meldung (beobachten, nie handeln) ----------

    def _report(self, f: dict) -> None:
        log.info("Anomalie: %s %s %s $%s (%.0f%% des Buchs, %d Fills davor)",
                 f["address"][:10], f["side"], f["coin"], f"{f['notional']:,.0f}",
                 f["concentration"] * 100, f["prior_fills"])
        if self.journal:
            self.journal.record("anomaly", **f)
        try:
            RUNTIME.mkdir(exist_ok=True)
            with open(self.path, "a") as fh:
                fh.write(json.dumps({"t": int(self.clock()), **f}, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("anomalies.jsonl nicht schreibbar")
        if self.notifier:
            self.notifier.send(
                f"🕵️ <b>Anomalie</b>: frische Wallet wettet groß\n"
                f"<code>{f['address']}</code>\n"
                f"{f['side']} {f['coin']} ${f['notional']:,.0f} "
                f"({f['concentration'] * 100:.0f}% des Buchs)\n"
                f"Konto ${f['account_value']:,.0f} | {f['prior_fills']} Fills in "
                f"{self.cfg.lookback_days} Tagen davor\n"
                f"Dossier: <code>python investigate.py {f['address']}</code>"
            )
        self.flagged.append({"t": int(self.clock()), **f})
        del self.flagged[:-20]

    def _prune_seen(self) -> None:
        cutoff = self.clock() - 2 * self.cfg.recheck_hours * 3600
        self._seen = {a: t for a, t in self._seen.items() if t > cutoff}
