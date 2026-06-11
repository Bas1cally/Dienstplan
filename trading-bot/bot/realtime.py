"""Echtzeit-Feed: Leader-Fills und Preise per WebSocket statt Polling.

Copy-Lag ist DER Edge-Killer beim Copy-Trading: Wer 10 Sekunden nach dem
Leader einsteigt, bekommt schlechtere Preise. Der Feed abonniert:

  userFills je Leader  -> jedes Fill-Event weckt den Autopilot-Loop SOFORT
                          (Copy-Lag: Millisekunden statt bis zu poll_seconds)
  allMids              -> frische Preise ohne REST-Roundtrip

Robustheit: Der Feed ist ein Beschleuniger, keine Abhängigkeit. Verbindungs-
probleme setzen nur `connected=False` - der Bot pollt dann wie bisher weiter.
Die Reconciliation bleibt unverändert die Wahrheit (Snapshots per REST);
der Feed bestimmt nur, WANN getickt wird und liefert frischere Preise.
"""

import logging
import threading
import time

from hyperliquid.info import Info

from .exchange import api_url

log = logging.getLogger(__name__)


class RealtimeFeed:
    MIDS_FRESH_S = 5.0

    def __init__(self, addresses: list[str] | None = None):
        self.wake = threading.Event()       # Autopilot wartet hierauf
        self.connected = False
        self.fills_seen = 0
        self.last_fill_t = 0.0
        self._mids: dict[str, float] = {}
        self._mids_t = 0.0
        self._lock = threading.Lock()
        self._info: Info | None = None
        self._subscribed: list[str] = []
        try:
            self._info = Info(api_url(False), skip_ws=False)
            self._info.subscribe({"type": "allMids"}, self._on_mids)
            self.connected = True
            log.info("Echtzeit-Feed verbunden (WebSocket)")
        except Exception:
            log.warning("WebSocket nicht verbindbar - Bot läuft im Polling-Modus", exc_info=True)
        if addresses:
            self.resubscribe(addresses)

    # ---------- Subscriptions ----------

    def resubscribe(self, addresses: list[str]) -> None:
        """Leader-Fills abonnieren (bei Rotation erneut aufrufen)."""
        if not self._info or not self.connected:
            return
        for addr in addresses:
            a = addr.lower()
            if a in self._subscribed:
                continue
            try:
                self._info.subscribe({"type": "userFills", "user": a}, self._on_fill)
                self._subscribed.append(a)
            except Exception:
                log.warning("userFills-Subscription für %s fehlgeschlagen", a[:10], exc_info=True)
        log.info("Echtzeit-Feed: %d Leader abonniert", len(self._subscribed))

    # ---------- Callbacks (WS-Thread) ----------

    def _on_fill(self, msg: dict) -> None:
        try:
            data = msg.get("data", {})
            if data.get("isSnapshot"):  # initialer Verlauf, kein neues Event
                return
            fills = data.get("fills") or []
            if not fills:
                return
            self.fills_seen += len(fills)
            self.last_fill_t = time.time()
            self.wake.set()  # Autopilot sofort wecken
        except Exception:
            log.debug("Fill-Event nicht verarbeitbar", exc_info=True)

    def _on_mids(self, msg: dict) -> None:
        try:
            mids = msg.get("data", {}).get("mids", {})
            with self._lock:
                for coin, px in mids.items():
                    self._mids[coin] = float(px)
                self._mids_t = time.time()
        except Exception:
            log.debug("Mids-Event nicht verarbeitbar", exc_info=True)

    # ---------- Abfragen (Bot-Thread) ----------

    def mids(self) -> dict[str, float] | None:
        """Frische WS-Preise oder None (dann REST nutzen)."""
        with self._lock:
            if self._mids and time.time() - self._mids_t < self.MIDS_FRESH_S:
                return dict(self._mids)
        return None

    def wait(self, timeout: float) -> bool:
        """Wartet auf Leader-Fill oder Timeout. True = durch Fill geweckt."""
        woken = self.wake.wait(timeout)
        self.wake.clear()
        return woken

    def close(self) -> None:
        try:
            if self._info:
                self._info.disconnect_websocket()
        except Exception:
            pass
