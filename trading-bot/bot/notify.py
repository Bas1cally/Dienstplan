"""Telegram-Benachrichtigungen für die Ereignisse, die man sofort wissen will.

Setup (2 Minuten):
  1. Bei @BotFather einen Bot anlegen -> TELEGRAM_BOT_TOKEN in .env
  2. Dem Bot schreiben, dann https://api.telegram.org/bot<TOKEN>/getUpdates
     aufrufen -> chat.id als TELEGRAM_CHAT_ID in .env

Fehlende Variablen = Benachrichtigungen still deaktiviert; der Bot läuft normal.
"""

import logging
import os

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if self.enabled:
            log.info("Telegram-Benachrichtigungen aktiv")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, html: bool = True) -> None:
        """Sendet eine Nachricht; Fehler werden geloggt, nie geworfen.
        Lehnt Telegram das HTML-Parsing ab (400, z.B. rohes '<' in dynamischem
        Inhalt), wird als Klartext nachgesendet - Push darf nie stumm sterben.
        `html=False`: von vornherein OHNE parse_mode senden - für Freitext mit
        ungeprüftem Inhalt (z.B. der volle Report), der öfter mal ein '<'/'>'
        enthält und sonst bei jeder Nachricht den 400-Retry-Umweg bräuchte."""
        if not self.enabled:
            return
        try:
            payload = {"chat_id": self.chat_id, "text": text}
            if html:
                payload["parse_mode"] = "HTML"
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload, timeout=10,
            )
            if html and r.status_code == 400:
                log.warning("Telegram lehnt HTML ab (400) - sende als Klartext")
                r = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text},
                    timeout=10,
                )
            r.raise_for_status()
        except Exception:
            log.exception("Telegram-Versand fehlgeschlagen")
