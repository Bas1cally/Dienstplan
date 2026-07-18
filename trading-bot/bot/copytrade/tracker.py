"""Beobachtet die offenen Positionen der Leader-Wallets.

Liefert pro Poll einen vollständigen Snapshot (kein Event-Diffing): Der Copier
gleicht daraus das Ziel-Portfolio ab. Vorteil: verpasste Polls oder Neustarts
korrigieren sich selbst, statt Positionen "zu verlieren".
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class LeaderPosition:
    coin: str
    size: float            # signiert: >0 long, <0 short
    entry: float
    position_value: float  # |Notional| in USD
    leverage: float

    def not_losing(self, price: float, tol_frac: float = 0.0) -> bool:
        """Für Sprints Bestätigungsfenster (Flip-Flopper-Schutz): ist die
        Position des LEADERS zum aktuellen Preis nicht im Minus? Bewusst
        >= statt > 0 - im Moment der Signal-Entdeckung ist price meist noch
        exakt gleich entry (PnL==0, weder Gewinn noch Verlust). Ein
        genau-0-PnL ist kein Warnsignal, nur eine ECHTE Bewegung ins Minus
        disqualifiziert den Kandidaten.

        tol_frac (Live-Fund 18.07.: die ersten 2 frischen Signale nach dem
        Frequenz-Fix wurden BEIDE als 'unbestaetigt_negativ' verworfen,
        xyz:BRENTOIL): der Leader füllt Long am ASK, unser Vergleichspreis
        ist der MID - der liegt konstruktionsbedingt ~einen halben Spread
        UNTER seinem Entry, die Position sieht also direkt nach JEDER
        Eröffnung minimal 'im Minus' aus, ohne dass sich der Markt bewegt
        hat. Ohne Toleranz ist das Fenster bei spread-breiten Assets
        (Builder-DEX-Perps wie Öl/Aktien) ein Fast-Immer-Verwerfer. Die
        Toleranz (Anteil vom Entry, z.B. 0.002 = 0.2%) deckt Spread-
        Rauschen ab; eine ECHTE Bewegung dagegen (der beobachtete
        Flip-Flopper schoss deutlich ins Minus) disqualifiziert weiter."""
        loss = (price - self.entry) * (1.0 if self.size > 0 else -1.0)
        return loss >= -tol_frac * abs(self.entry)


@dataclass
class LeaderSnapshot:
    address: str
    equity: float
    positions: dict[str, LeaderPosition]

    def exposure(self, coin: str) -> float:
        """Signierte Allokation in coin relativ zur Equity des Leaders (-x..+x)."""
        pos = self.positions.get(coin)
        if not pos or self.equity <= 0:
            return 0.0
        sign = 1.0 if pos.size > 0 else -1.0
        return sign * pos.position_value / self.equity


class LeaderTracker:
    def __init__(self, info, addresses: list[str], dexs: list[str] | None = None,
                 throttle_s: float = 0.05):
        self.info = info
        self.addresses = addresses
        self.dexs = dexs or [""]  # Haupt-DEX + Builder-DEXs (Aktien, Gold, Öl)
        self.throttle_s = throttle_s     # Burst-Glättung: 14+ Adressen x DEXs je Tick
        self._last_good: dict[str, LeaderSnapshot] = {}   # Cache statt Phantom-Daten
        self.last_fresh = 0              # Abdeckung der letzten Runde (Diagnose)
        self.last_stale = 0
        self.last_total = 0
        self._last_snapshot_partial = False   # Seitenkanal, siehe snapshot()

    def snapshot(self, address: str) -> LeaderSnapshot:
        equity = 0.0
        raw_positions: list = []
        # Seitenkanal für snapshot_all(): war DIESER Aufruf vollständig (alle
        # DEXs erreichbar) oder hat ein Builder-DEX (i>0) transient gefehlt?
        # Ein Direktaufruf von snapshot() (z.B. /leaders <addr>) bekommt trotzdem
        # das best-effort Ergebnis - nur snapshot_all() muss den Unterschied
        # kennen, um es NICHT als vollwertigen 'letzten guten Stand' zu cachen
        # (siehe dort: sonst verschwindet eine Position auf dem ausgefallenen
        # DEX fälschlich aus dem gecachten Buch -> falscher 'Leader raus'-Exit).
        self._last_snapshot_partial = False
        for i, dex in enumerate(self.dexs):
            try:
                state = self.info.user_state(address, dex=dex) if dex else self.info.user_state(address)
            except Exception:
                if i == 0:
                    # Haupt-DEX weg -> KEIN Phantom-Snapshot (Equity 0, leeres
                    # Buch) zurückgeben: der würde falsche 'Leader raus'-Exits
                    # auslösen und Baselines zerstören (alles sähe 'frisch' aus).
                    raise
                log.debug("Leader %s: DEX %r nicht abrufbar", address[:10], dex, exc_info=True)
                self._last_snapshot_partial = True
                continue
            equity += float(state["marginSummary"]["accountValue"])
            raw_positions.extend(state.get("assetPositions", []))
        positions: dict[str, LeaderPosition] = {}
        for p in raw_positions:
            pos = p["position"]
            size = float(pos["szi"])
            if size == 0:
                continue
            positions[pos["coin"]] = LeaderPosition(
                coin=pos["coin"],
                size=size,
                entry=float(pos.get("entryPx") or 0),
                position_value=abs(float(pos.get("positionValue") or 0)),
                leverage=float(pos.get("leverage", {}).get("value", 1)),
            )
        return LeaderSnapshot(address=address, equity=equity, positions=positions)

    def snapshot_all(self) -> list[LeaderSnapshot]:
        """Alle Adressen snapshotten. Schlägt eine fehl (429/Netz), liefert der
        Cache den LETZTEN GUTEN Stand statt sie still wegzulassen - Weglassen
        hieße: Copier plant Ziel 0 (falsches Flatten) und Sprint verliert die
        Baseline. Stale ist ehrlich besser als falsch-leer; die Abdeckung
        (fresh/stale/total) ist für /sprint sichtbar."""
        import time as _time

        # Eigene Listen-Kopie: der Analyse-Thread darf self.addresses jederzeit
        # atomar tauschen - die laufende Runde zählt gegen IHRE Liste, sonst
        # entstehen unmögliche Anzeigen wie 'Abdeckung 13/11' (Live-Befund).
        addrs = list(self.addresses)
        snaps: list[LeaderSnapshot] = []
        fresh = stale = 0
        for i, addr in enumerate(addrs):
            if i and self.throttle_s:
                _time.sleep(self.throttle_s)   # Bursts glätten (Rate-Limit-Hygiene)
            try:
                s = self.snapshot(addr)
                if self._last_snapshot_partial:
                    # Ein Builder-DEX (z.B. der Aktien-Perp-Basket) war diese
                    # Runde transient nicht erreichbar - der zurückgegebene
                    # Snapshot fehlt dessen Positionen KOMPLETT. Das NICHT als
                    # 'letzter guter Stand' cachen (sonst verschwindet die
                    # Position auf dem ausgefallenen DEX fälschlich aus dem
                    # gecachten Buch -> falscher 'Leader raus'-Exit/Baseline-
                    # Zerstörung). Stattdessen wie ein Totalausfall behandeln:
                    # letzten wirklich vollständigen Stand weiterreichen, falls
                    # vorhanden - sonst bleibt der unvollständige Snapshot die
                    # einzige verfügbare Information (besser als nichts).
                    cached = self._last_good.get(addr)
                    if cached is not None:
                        s = cached
                    stale += 1
                    log.warning("Snapshot %s teilweise fehlgeschlagen (Builder-DEX) - "
                                "nutze letzten VOLLSTÄNDIGEN Stand statt lückenhafter Daten",
                                addr[:10])
                else:
                    self._last_good[addr] = s
                    fresh += 1
            except Exception:
                s = self._last_good.get(addr)
                if s is None:
                    log.exception("Snapshot fehlgeschlagen für %s - überspringe diese Runde", addr)
                    continue
                stale += 1
                log.warning("Snapshot %s fehlgeschlagen - nutze letzten guten Stand",
                            addr[:10])
            snaps.append(s)
        self.last_fresh, self.last_stale = fresh, stale
        self.last_total = len(addrs)
        return snaps
