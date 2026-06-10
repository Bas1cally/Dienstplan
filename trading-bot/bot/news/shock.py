"""Volatilitäts-Schock-Detektor: die schnelle Verteidigungslinie.

Wenn Trump etwas postet und der Markt crasht, sieht man das in den Preisdaten
SCHNELLER als in jeder bezahlbaren News-API - die Order-Flows der HFT-Firmen
reagieren in Millisekunden. Dieser Detektor schaut deshalb direkt auf die
1-Minuten-Candles:

  1. Absoluter Move: |Preisänderung| über das Fenster > Schwellwert (z.B. 2.5%/5min)
  2. Vola-Spike: Kurzfrist-Volatilität >> Stunden-Volatilität (auch ohne Richtungsmove)

Beides löst RISK_OFF mit Cooldown aus. False Positives kosten nur entgangene
Trades - False Negatives kosten das Konto. Im Zweifel: auslösen.
"""

import logging
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class ShockState:
    triggered: bool
    reason: str = ""
    move_pct: float = 0.0


class ShockDetector:
    def __init__(self, cfg, clock=time.time):
        self.cfg = cfg
        self._clock = clock  # injizierbar für Simulation/Backtest
        self._cooldown_until = 0.0

    def check(self, candles_1m: pd.DataFrame, now: float | None = None) -> ShockState:
        """Erwartet 1m-Candles (mind. 65 Stück) mit Spalte close."""
        now = now if now is not None else self._clock()
        if not self.cfg.enabled:
            return ShockState(False)
        if now < self._cooldown_until:
            return ShockState(True, reason="cooldown aktiv")
        if len(candles_1m) < self.cfg.window_minutes + 61:
            return ShockState(False)

        close = candles_1m["close"].to_numpy(dtype=float)
        w = self.cfg.window_minutes
        move = close[-1] / close[-1 - w] - 1.0

        if abs(move) >= self.cfg.move_threshold:
            self._trigger(now)
            return ShockState(True, reason=f"{move:+.2%} in {w}min", move_pct=move)

        rets = np.diff(np.log(close))
        recent_vol = float(np.std(rets[-w:]))
        base_vol = float(np.std(rets[-60 - w:-w]))
        if base_vol > 0 and recent_vol / base_vol >= self.cfg.vol_spike_ratio and abs(move) > 0.01:
            self._trigger(now)
            return ShockState(True, reason=f"Vola-Spike {recent_vol / base_vol:.1f}x, Move {move:+.2%}", move_pct=move)

        return ShockState(False, move_pct=move)

    def _trigger(self, now: float) -> None:
        self._cooldown_until = now + self.cfg.cooldown_minutes * 60
        log.warning("SCHOCK erkannt - RISK_OFF für %d Minuten", self.cfg.cooldown_minutes)
