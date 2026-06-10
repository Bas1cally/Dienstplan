"""Signal-Bridge: macht aus Bot-Entscheidungen ausführbare Order-Tickets.

Für Plattformen ohne Trading-API (Prop-Firmen wie Breakout by Kraken):
Der Bot analysiert und trackt alles im Paper-Modus - jede Entscheidung wird
zusätzlich als fertiges Ticket emittiert, das du manuell (oder über einen
eigenen Webhook-Empfänger) auf der Zielplattform ausführst.

Kanäle je Signal:
  1. runtime/signals.jsonl        - maschinenlesbares Protokoll
  2. Telegram                     - sofort aufs Handy, ausführfertig formatiert
  3. SIGNAL_WEBHOOK_URL (.env)    - optionaler POST an einen eigenen Empfänger

WICHTIG (Prop-Compliance): Viele Prop-Firmen verbieten Copy-Trading und
Vollautomation. Die Signal-Bridge automatisiert bewusst NICHT die Ausführung -
ob und wie du die Signale umsetzt, liegt bei dir und den ToS der Plattform.
"""

import json
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


class SignalBridge:
    def __init__(self, notifier=None, path: Path | None = None):
        self.notifier = notifier
        self.path = path or RUNTIME / "signals.jsonl"
        self.webhook = os.environ.get("SIGNAL_WEBHOOK_URL", "")

    def emit(self, source: str, coin: str, side: str, size: float, price: float,
             stop: float | None = None, take_profit: float | None = None,
             reason: str = "") -> dict:
        """Emittiert ein Order-Ticket über alle konfigurierten Kanäle."""
        ticket = {
            "t": int(time.time()),
            "source": source,            # copy | scalp | flatten
            "coin": coin,
            "side": side,                # BUY | SELL
            "size": round(abs(size), 6),
            "price": round(price, 6),
            "stop": round(stop, 6) if stop else None,
            "take_profit": round(take_profit, 6) if take_profit else None,
            "reason": reason,
        }
        self._file(ticket)
        self._telegram(ticket)
        self._webhook(ticket)
        return ticket

    def _file(self, t: dict) -> None:
        try:
            self.path.parent.mkdir(exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("Signal-Datei nicht schreibbar")

    def _telegram(self, t: dict) -> None:
        if not self.notifier:
            return
        lines = [f"📋 <b>SIGNAL [{t['source']}] {t['side']} {t['coin']}</b>",
                 f"Größe: {t['size']}  @ ~{t['price']:,}"]
        if t["stop"]:
            lines.append(f"Stop: {t['stop']:,}")
        if t["take_profit"]:
            lines.append(f"TP: {t['take_profit']:,}")
        if t["reason"]:
            lines.append(f"Grund: {t['reason']}")
        self.notifier.send("\n".join(lines))

    def _webhook(self, t: dict) -> None:
        if not self.webhook:
            return
        try:
            requests.post(self.webhook, json=t, timeout=10).raise_for_status()
        except Exception:
            log.exception("Signal-Webhook fehlgeschlagen")
