"""Telegram-Fernsteuerung: den Bot vom Handy abfragen und notfalls stoppen.

Long-Polling auf getUpdates in einem eigenen Thread. Reagiert NUR auf die in
.env hinterlegte TELEGRAM_CHAT_ID - fremde Chats werden ignoriert. Befehle:

  /status     Zustand, Equity, Orders heute, Risiko-Level, Leader, WS
  /report     kompakte Auswertung (wie report.py --offline)
  /leaders    aktuelle Leader inkl. ROI seit Kopie
  /anomalies  letzte Funde des Anomalie-Scouts
  /stop /start  Autopilot anhalten / wieder starten
  /help       Befehlsübersicht

Ohne TELEGRAM_*-Variablen passiert nichts (Thread startet gar nicht erst).
"""

import logging
import os
import threading
import time

import requests

log = logging.getLogger(__name__)


class TelegramCommander:
    def __init__(self, handlers: dict, token: str = "", chat_id: str = "",
                 poll_timeout: int = 30):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID", ""))
        self.handlers = handlers          # "/cmd" -> callable() -> str
        self.poll_timeout = poll_timeout
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="telecmd")
        self._thread.start()
        log.info("Telegram-Fernsteuerung aktiv (%d Befehle)", len(self.handlers))

    def stop(self) -> None:
        self._stop.set()

    # ---------- reine Dispatch-Logik (offline testbar) ----------

    def dispatch(self, text: str) -> str | None:
        """Mappt eine Nachricht auf eine Antwort. None = ignorieren."""
        cmd = text.strip().split()[0].lower() if text.strip() else ""
        cmd = cmd.split("@")[0]  # /status@meinbot -> /status
        if cmd in self.handlers:
            try:
                return self.handlers[cmd]()
            except Exception as e:
                log.exception("Befehl %s fehlgeschlagen", cmd)
                return f"⚠️ {cmd} fehlgeschlagen: {str(e)[:120]}"
        if cmd.startswith("/"):
            return "Unbekannter Befehl. " + self.handlers.get("/help", lambda: "/help")()
        return None

    # ---------- Polling-Schleife ----------

    def _loop(self) -> None:
        # Beim Start nur künftige Updates abholen (alte Befehle nicht nachträglich ausführen)
        self._drain_backlog()
        while not self._stop.is_set():
            try:
                updates = self._get_updates()
            except Exception as e:
                log.warning("getUpdates fehlgeschlagen: %s", str(e)[:80])
                self._stop.wait(5)
                continue
            for u in updates:
                self._offset = max(self._offset, u["update_id"] + 1)
                msg = u.get("message") or u.get("edited_message") or {}
                if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                    continue  # nur der Besitzer darf steuern
                reply = self.dispatch(msg.get("text", ""))
                if reply:
                    self._send(reply)

    def _drain_backlog(self) -> None:
        try:
            updates = self._get_updates(timeout=0)
            for u in updates:
                self._offset = max(self._offset, u["update_id"] + 1)
        except Exception:
            pass

    def _get_updates(self, timeout: int | None = None) -> list:
        r = requests.get(
            f"https://api.telegram.org/bot{self.token}/getUpdates",
            params={"offset": self._offset,
                    "timeout": self.poll_timeout if timeout is None else timeout},
            timeout=self.poll_timeout + 10,
        )
        r.raise_for_status()
        return r.json().get("result", [])

    def _send(self, text: str) -> None:
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            ).raise_for_status()
        except Exception:
            log.exception("Antwort-Versand fehlgeschlagen")
