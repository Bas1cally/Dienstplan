"""Beobachtet die offenen Positionen der Leader-Wallets.

Liefert pro Poll einen vollständigen Snapshot (kein Event-Diffing): Der Copier
gleicht daraus das Ziel-Portfolio ab. Vorteil: verpasste Polls oder Neustarts
korrigieren sich selbst, statt Positionen "zu verlieren".
"""

import logging
import time
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
                 throttle_s: float = 0.05, dex_reprobe_s: float = 600.0,
                 clock=None):
        self.info = info
        self.addresses = addresses
        self.dexs = dexs or [""]  # Haupt-DEX + Builder-DEXs (Aktien, Gold, Öl)
        self.throttle_s = throttle_s     # Burst-Glättung: 14+ Adressen x DEXs je Tick
        self._last_good: dict[str, LeaderSnapshot] = {}   # Cache statt Phantom-Daten
        self.last_fresh = 0              # Abdeckung der letzten Runde (Diagnose)
        self.last_stale = 0
        self.last_total = 0
        self._last_snapshot_partial = False   # Seitenkanal, siehe snapshot()
        # DEX-Sparmodus (Spiegel-Fund 24.07.: 149 von 150 Log-Zeilen waren
        # Snapshot-Fehler, 7 von 15 Wallets dauerhaft stale, Tick-Dauer 86s
        # statt der konfigurierten 20s). Ursache: `dexs: auto` entdeckt gut
        # ein Dutzend Builder-DEXs, und snapshot() fragte JEDE Wallet auf
        # JEDEM davon ab - ~300 user_state-Calls pro Tick. In den echten
        # Daten liefern aber nur ZWEI DEXs je Positionen: der Haupt-DEX
        # (Krypto) und 'xyz' (Aktien/Rohstoffe).
        # Deshalb: je Wallet merken, auf welchen DEXs sie zuletzt wirklich
        # Positionen hatte, und nur die abfragen. Alle dex_reprobe_s wird
        # eine Wallet wieder voll durchgeprüft, damit ein erstmaliger
        # Ausflug auf einen neuen DEX gefunden wird.
        # SICHERHEIT (wichtig): übersprungen werden nur DEXs, die beim
        # letzten VOLLEN Probe-Lauf leer waren. Taucht dort zwischendurch
        # eine Position auf, fehlt sie zwar bis zum nächsten Probe-Lauf im
        # Buch - sie fehlt aber AUCH in der Baseline, kann also nie einen
        # falschen 'Leader raus'-Exit auslösen (nur ein verspätetes Signal).
        # DEXs, auf denen die Wallet Positionen HÄLT, werden immer weiter
        # abgefragt - deren Schließung sehen wir also punktgenau.
        self.dex_reprobe_s = dex_reprobe_s
        self._clock = clock or time.time
        self._wallet_dexs: dict[str, set[str]] = {}    # addr -> DEXs mit Positionen
        self._wallet_probed_t: dict[str, float] = {}   # addr -> letzter VOLLER Probe-Lauf
        self.last_dex_calls = 0            # Diagnose: user_state-Calls letzte Runde

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
        ask, full_probe = self._dexs_for(address)
        seen_with_positions: set[str] = set()
        answered: set[str] = set()
        for i, dex in enumerate(ask):
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
            self.last_dex_calls += 1
            answered.add(dex)
            equity += float(state["marginSummary"]["accountValue"])
            got = state.get("assetPositions", [])
            if any(float(p["position"]["szi"]) != 0 for p in got):
                seen_with_positions.add(dex)
            raw_positions.extend(got)
        self._remember_dexs(address, seen_with_positions, answered, full_probe)
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

    def _dexs_for(self, address: str) -> tuple[list[str], bool]:
        """Welche DEXs für DIESE Wallet abfragen? Gibt (Liste, war_voller_Probe)
        zurück. Der Haupt-DEX steht immer an Position 0 (sein Ausfall muss
        weiterhin hart durchschlagen, siehe snapshot()). Ohne Builder-DEXs
        oder mit abgeschaltetem Sparmodus (dex_reprobe_s <= 0) exakt das alte
        Verhalten: immer alle."""
        if len(self.dexs) <= 1 or self.dex_reprobe_s <= 0:
            return list(self.dexs), True
        known = self._wallet_dexs.get(address)
        due = self._clock() - self._wallet_probed_t.get(address, 0.0) >= self.dex_reprobe_s
        if known is None or due:
            return list(self.dexs), True        # voller Durchlauf (auch beim ersten Mal)
        # Sparlauf: Haupt-DEX + die DEXs, auf denen sie zuletzt wirklich lag
        return [d for d in self.dexs if d == "" or d in known], False

    def _remember_dexs(self, address: str, with_positions: set[str],
                       answered: set[str], full_probe: bool) -> None:
        """DEX-Gedächtnis fortschreiben. Nach einem VOLLEN Lauf wird die
        Menge neu gesetzt - aber nur aus DEXs, die auch geantwortet haben:
        ein transient ausgefallener DEX darf nicht so aussehen, als hätte
        die Wallet ihn verlassen (sonst würde er aus dem Sparlauf fallen und
        eine echte Position dort erst beim nächsten Probe-Lauf auffallen).
        Nach einem Sparlauf kann nur ERGÄNZT werden - über die nicht
        abgefragten DEXs wissen wir nichts Neues."""
        if len(self.dexs) <= 1 or self.dex_reprobe_s <= 0:
            return
        if full_probe:
            keep = {d for d in self._wallet_dexs.get(address, set())
                    if d not in answered}     # nicht erreicht = Wissen behalten
            self._wallet_dexs[address] = with_positions | keep
            self._wallet_probed_t[address] = self._clock()
        else:
            self._wallet_dexs.setdefault(address, set()).update(with_positions)

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
        self.last_dex_calls = 0
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
