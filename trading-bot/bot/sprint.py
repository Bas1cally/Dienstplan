"""Sprint-Buch v3: frisches Leader-Signal -> einsteigen -> HALTEN -> abrechnen.

Zustandsmaschine statt Dauer-Reconciliation (v1 hat mit plan_rebalance je Tick
139 Trades produziert - bei 10x Hebel frisst das Fees):

  FLACH   - scannt ALLE Leader der Rotation (LARP-gefiltert), nicht nur den
            besten - sonst passiert tagelang nichts, wenn genau der eine
            gerade pausiert. Bestehende Positionen zählen NICHT (nie in
            laufende Trades einsteigen) - jeder Leader bekommt beim ersten
            Blick eine eigene Baseline. Erst ein Übergang 0 -> Position ist
            ein frisches Signal; melden mehrere Leader gleichzeitig, gewinnt
            der mit dem höchsten Score. Einstieg mit leverage-fachem
            Exposure, dieser Leader ist für den Ritt fixiert.
  IM RITT - kein Rebalancing. Exits NUR wenn: der Leader den Coin komplett
            schließt oder flippt (mitgehen - sein Edge ist das Exit-Timing),
            der Leader aus der Rotation fällt (wir wären blind), RISK_OFF,
            oder das eigene Ziel/Bust greift. Größenänderungen: ignorieren.

ZYKLUS = RITT (v3, vorher: mehrere Ritte akkumulierten in einem Zyklus).
  Jeder beendete Ritt - egal ob durch Leader-Exit/-Flip/-Scaleout, Rotation,
  RISK_OFF, manuellen Close ODER das eigene +100$/5%-Ziel - wird SOFORT
  verbucht (banked += PnL, won/busted je nach Vorzeichen) und das Konto sofort
  wieder auf equity (1000$) zurückgesetzt. Der nächste Ritt ist ein neuer,
  unabhängiger Zyklus. Kein "Zwischenstand mitschleppen" mehr - jeder Trade
  steht für sich, einfacher für Dashboard/Historie nachzuvollziehen.

  LARP-Strikes bleiben ritt-scharf: Verlust-Ritt -> Strike +1, Gewinn-Ritt
  heilt einen Strike (min 0); 2 Strikes -> Leader fürs Sprint-Buch gesperrt.
  Ausnahme: manueller Close und RISK_OFF sind nicht die Entscheidung/Schuld
  des Leaders - kein Strike, PnL wird trotzdem sofort verbucht.

Nur Paper-Modus; Fees konservativ als Taker.
"""

import json
import logging
import time

from .journal import RUNTIME
from .paper import PaperBroker

log = logging.getLogger(__name__)

_REASON_TXT = {
    "tp": "Ziel erreicht", "bust": "geplatzt (10x-Liquidation)",
    "leader_exit": "Leader raus", "leader_flip": "Leader gedreht",
    "leader_scaleout": "Leader hat abgebaut", "leader_rotated": "Leader rotiert",
    "manual": "manuell geschlossen", "risk_off": "RISK_OFF",
    "mode_switch": "Moduswechsel (Mess-Modus beendet)",
    "star_preempt": "Star-Signal hat übernommen",
}
_STRIKE_EXEMPT = {"manual", "risk_off"}  # nicht die Entscheidung/Schuld des Leaders

# Confidence-Points/Star (BACKLOG.md, Nutzer-Entscheidung 15.07.): NUR das
# eigene, durchgehaltene +10%-Ziel (tp) und eine Star-Preemption (der Bot
# beendet aktiv einen profitablen Ritt zugunsten eines bestätigten Star-
# Signals - "star_preempt", NICHT "leader_exit") verdienen einen Punkt. Jeder
# leader-getriebene Exit (leader_exit/-flip/-scaleout/-rotated) bleibt
# Bilanz-Gewinn + Strike-Heilung, ist aber confidence-NEUTRAL - kurzes Grün
# beim Leader-Exit beweist keine Willenskraft, nur das durchgehaltene eigene
# Ziel tut das (konsistent mit dem Anti-Flip-Flopper-Bestätigungsfenster).
_CONFIDENCE_EARNING = {"tp", "star_preempt"}
CONFIDENCE_PER_WIN = 5
STAR_THRESHOLD = 100

# Baselines überleben Neustarts nur, wenn die Datei jünger ist: bei kurzen
# Deploys (~1 min) wollen wir das Blindfenster schließen (ein während des
# Neustarts eröffnetes Signal ist noch frisch genug zum Reiten). Nach langer
# Downtime wäre der Einstieg dagegen längst verpasst -> lieber re-baselinen.
_BASELINE_MAX_AGE_S = 600.0

# Sicherheitsnetz gegen einen Live-Befund: fehlt für einen pending Coin
# dauerhaft der Preis (z.B. Symbol nicht in all_mids(), Feed-Lücke für genau
# diesen Coin), wartete der Kandidat bisher UNBEGRENZT - weder Promotion noch
# Reject war möglich, "Bestätigung läuft" stand für immer bei X% des
# Fensters. Nach dieser Zeit OHNE JEMALS einen Preis gesehen zu haben, wird
# der Kandidat verworfen statt für immer zu hängen.
_PENDING_MAX_AGE_S = 120.0


class SprintBook:
    def __init__(self, cfg, fee_rate: float, notifier=None, journal=None,
                 runtime_dir=None, clock=time.time):
        runtime = runtime_dir or RUNTIME
        self.cfg = cfg
        self.notifier = notifier
        self.journal = journal
        self.clock = clock
        self.paper = PaperBroker(cfg.equity, fee_rate, path=runtime / "sprint_book.json")
        self.fee_rate = fee_rate
        self.state_path = runtime / "sprint_cycles.json"
        # Mess-Modus (parallel_rides): Coin -> Leader-Adresse des jeweiligen Ritts
        self.ride_leaders: dict[str, str] = {}
        self.banked = 0.0
        self.won = 0
        self.busted = 0
        self.total_trades = 0            # über alle abgeschlossenen Zyklen
        self.ride_leader = ""            # fixiert, solange Positionen offen sind
        # Mode-Switch-Bilanz-Reset (QOL-Runde): gesetzt in _migrate_v1_or_load,
        # wenn beim Laden noch offene Alt-Mess-Ritte drainiert werden müssen -
        # der eigentliche Reset wartet dann in tick() bis der Drain fertig ist.
        self._bilanz_reset_pending = False
        self.strikes: dict[str, int] = {}   # addr -> aktive Strikes (LARP-Enttarnung)
        self.banned: set[str] = set()       # fürs Sprint-Buch gesperrte Leader
        # addr -> Confidence-Punkte (verdiente Anerkennung, siehe
        # _CONFIDENCE_EARNING). Star-Status ist ABGELEITET (>= STAR_THRESHOLD),
        # keine eigene Liste - vermeidet zwei Wahrheiten, die auseinanderlaufen
        # können. Gebannte Leader werden vor _enter bereits abgewiesen, können
        # also nie mehr Punkte verdienen - kein Sonderfall-Code fürs Einfrieren
        # nötig, das ergibt sich von selbst aus dem bestehenden Bann-Check.
        self.confidence: dict[str, int] = {}
        # Bestätigungsfenster (Flip-Flopper-Schutz): coin -> {leader, since}.
        # Bewusst NICHT persistiert - ein Kandidat bindet kein Kapital, ist nur
        # Sekunden bis wenige Minuten unterwegs; geht er über einen Neustart
        # verloren, entdeckt der nächste Scan ihn im Zweifel neu (oder eben
        # nicht mehr, dann war er ohnehin kein anhaltendes Signal).
        self._pending: dict[str, dict] = {}
        self._ride_start_equity: float | None = None  # Equity bei Ritt-Beginn (PnL-Attribution)
        self._ride_entry_sizes: dict[str, float] = {}  # coin -> |Leader-Größe| beim Einstieg
        # Baseline je Leader (addr -> coin -> signierte Größe): nur Übergänge
        # 0 -> Position NACH der Baseline sind frische Signale. Läuft für ALLE
        # Rotations-Leader mit (auch während eines Ritts), damit nach dem Ritt
        # keine längst laufenden Positionen fälschlich als "frisch" gelten.
        self._baselines: dict[str, dict[str, float]] = {}
        # Scan-Telemetrie (seit Prozess-Start): ohne sie sind "kein Signal kam"
        # und "Signale kamen, wurden aber still verworfen" von außen identisch -
        # genau das Beobachtbarkeits-Loch, das der Nutzer als 'irgendwas fehlt'
        # gespürt hat. Verworfene Signale werden gezählt UND die letzten gezeigt.
        self._scan_rejected: dict[str, int] = {}
        self._scan_last: list[dict] = []       # letzte verworfene Signale (max 5)
        self._fresh_seen = 0                   # frische Signale gesamt (auch verworfene)
        self._last_fresh_t: float | None = None
        self._last_entry_t: float | None = None
        self.baselines_path = runtime / "sprint_baselines.json"
        self._baselines_saved_t = 0.0
        # Warm/kalt-Start dieses Prozess-Laufs (einmalig in _load_baselines
        # gesetzt, ändert sich danach nicht mehr) - beantwortet "ist '0 frische
        # Signale' echte Ruhe oder ein frischer Kaltstart ohne Vergleichsbasis"
        # direkt in /sprint, ohne Server-Log-Zugriff (Nutzer-Nachfrage).
        self._baseline_status = "kalt neu gesetzt (kein Vorstand)"
        self._migrate_v1_or_load()
        self._load_baselines()

    # ---------- Tick (Zustandsmaschine) ----------

    def tick(self, leaders: list[dict], snapshots: list, prices: dict[str, float],
             risk_off: bool = False) -> None:
        if not prices:
            return
        if self.cfg.parallel_rides:
            # MESS-MODUS: jedes Signal = eigener Ritt auf 1000$-Basis, parallel.
            # Endziel (parallel_rides: false) bleibt der Einzel-Ritt unten.
            self._tick_parallel(leaders, snapshots, prices, risk_off)
            if leaders and snapshots:
                self._refresh_baselines(leaders, snapshots)
            return
        # Sicherung gegen einen Moduswechsel bei noch offenen Mess-Ritten: ohne
        # das würde die Einzel-Ritt-Logik unten mehrere gleichzeitige Coins als
        # "Leader aus der Rotation gefallen" (ride_leader=="") fehldeuten -
        # ALLE Coins auf einmal zu EINEM falschen Zyklus verbuchen, ohne
        # Strike/Heilung (leader="" ist falsy), ride_leaders nie geleert
        # (State-Leak). Jeden Alt-Ritt einzeln über die bestehende Mess-Modus-
        # Logik sauber abschließen (eigene 1000$-Basis, echte Strikes/Heilung -
        # der Marktausgang war real, auch wenn WIR den Modus gewechselt haben),
        # BEVOR die Einzel-Ritt-Logik unten überhaupt greift.
        if self.ride_leaders:
            for coin in list(self.ride_leaders):
                if prices.get(coin):
                    self._settle_one(coin, prices, "mode_switch")
            if self.ride_leaders:
                return  # Rest ohne Preis diesen Tick - nächster Tick erneut
            # _settle_one resettet das Paper-Buch NIE (im Mess-Modus können
            # mehrere Ritte gleichzeitig offen sein) - die Einzel-Ritt-Logik
            # unten braucht aber ein sauberes 1000$-Buch für ihre eigene TP/
            # Bust-Rechnung, sonst würde der allererste Einzel-Ritt-Tick sofort
            # (fälschlich) auf Basis der Mess-Modus-Restsalden feuern.
            self.paper.reset()
            # Nutzer-Befund (echter Bug): der Bilanz-Reset lief bisher schon
            # beim LADEN, BEVOR dieser Drain überhaupt Chance hatte zu laufen -
            # der Forced-Close des Alt-Ritts sickerte dadurch als allererster
            # Eintrag in die frisch genullte Bilanz. Ist ein Reset noch
            # ausstehend, greift er deshalb erst HIER, NACHDEM der Alt-Ritt
            # bereits vollständig abgerechnet ist - wirklich blitzsauber.
            if self._bilanz_reset_pending:
                self.reset_bilanz()
        # 1. Ziel/Bust hat Vorrang - Zyklus=Ritt, also sofort abrechnen+resetten
        eq = self.paper.equity(prices)
        if eq >= self.cfg.equity + self.cfg.target_profit:
            self._settle_ride(prices, "tp")
            return
        if eq <= self.cfg.equity * self.cfg.bust_frac:
            self._settle_ride(prices, "bust")
            return
        # 2. Markt-Schutz: glattstellen, Ritt (=Zyklus) endet sofort, nicht weiter warten
        if risk_off:
            if self.paper.sizes():
                log.warning("Sprint: RISK_OFF - stelle glatt")
                for coin in list(self.paper.sizes()):
                    self._close_coin(coin, prices, "risk_off")
                self._settle_ride(prices, "risk_off")
            return
        if not leaders or not snapshots:
            return

        if self.paper.sizes():
            self._tick_riding(leaders, snapshots, prices)
        else:
            self._tick_waiting(leaders, snapshots, prices)
        # Läuft IMMER (auch während eines Ritts, für die später kommende
        # Star-Preemption) - registriert selbst nichts, verarbeitet nur
        # bereits pending Kandidaten (Bestätigungsfenster gegen Flip-Flopper).
        self._process_pending(leaders, snapshots, prices)
        # Baselines ALLER Rotations-Leader aktuell halten (auch während eines
        # Ritts) und Ausgeschiedene vergessen - sonst gelten deren während des
        # Ritts eröffnete Positionen später fälschlich als "frisch".
        self._refresh_baselines(leaders, snapshots)

    # ---------- FLACH: alle Leader auf frische Signale scannen ----------

    def _tick_waiting(self, leaders: list[dict], snapshots: list,
                      prices: dict[str, float]) -> None:
        by_addr = {s.address.lower(): s for s in snapshots}
        # Star zuerst, dann höchster Score: melden mehrere Leader gleichzeitig,
        # gewinnt der bewiesene Star vor dem reinen Score-Tie-Break (BACKLOG-
        # Entscheidung: "Star-Vorrang bei gleichzeitigen Signalen"). Bei
        # AKTIVEM Bestätigungsfenster kein frühzeitiges Return mehr nach dem
        # ersten Treffer - ein frisches Signal wird nur als KANDIDAT
        # registriert (bindet kein Kapital), mehrere Leader dürfen parallel
        # pending sein, _process_pending löst das zur Bestätigungszeit auf.
        # Ist das Fenster AUS (confirm_delay_s<=0), bleibt das alte Verhalten
        # exakt erhalten: sobald real eingestiegen, sofort zurück - sonst
        # könnte ein zweiter, niedriger priorisierter Leader im selben Tick
        # ebenfalls einsteigen (max_positions wird sonst hier nicht geprüft).
        for l in sorted(leaders, key=lambda l: (
                self.is_star(str(l.get("address", ""))), float(l.get("score", 0))),
                reverse=True):
            addr = str(l.get("address", "")).lower()
            snap = by_addr.get(addr)
            if snap is None:
                continue
            prev = self._baselines.get(snap.address.lower())
            if prev is None:
                continue  # erster Blick: _refresh_baselines legt die Baseline an
            book = self._book_of(snap)
            fresh = self._fresh_coins(book, prev)
            if not fresh:
                continue
            self._note_fresh(len(fresh))
            if addr in self.banned:
                # LARP-enttarnt: sein frisches Signal zählt nicht - aber SICHTBAR
                # verwerfen statt still (Telemetrie fürs 'warum passiert nichts')
                for coin in fresh:
                    self._reject(coin, snap.address, "leader_gesperrt")
                continue
            # EIN Ritt = EINE Position: bei Korb-Eröffnungen (Leader macht z.B.
            # 7 Aktien-Shorts auf einmal auf) wird nur das STÄRKSTE Signal
            # Kandidat (größte relative Überzeugung), statt die 10x-Kapazität
            # auf den ganzen Korb zu verschmieren. Ausgeschlossene Coins (z.B.
            # BTC) füllen den Slot NICHT - der nächststärkste rückt nach, wie
            # zuvor bei sofortigem _enter().
            fresh.sort(key=lambda c: abs(snap.exposure(c)), reverse=True)
            registered = False
            for coin in fresh:
                if registered:
                    self._reject(coin, snap.address, "korb_begrenzt")
                    continue
                ko = self._ineligible_reason(coin)
                if ko:
                    self._reject(coin, snap.address, ko)
                    continue
                if self.cfg.confirm_delay_s <= 0:
                    # Feature AUS: exakt das alte Sofort-Verhalten, kein
                    # Pending-Umweg (der würde sonst auch bei delay=0 noch
                    # die "Leader nicht im Minus"-Prüfung anwenden - das wäre
                    # kein "aus" mehr, sondern eine andere neue Regel).
                    if len(self.paper.sizes()) >= self.cfg.max_positions:
                        self._reject(coin, snap.address, "korb_begrenzt")
                        continue
                    self._enter(coin, snap, prices)
                    registered = coin in self.paper.sizes()
                else:
                    self._register_pending(coin, snap)
                    registered = coin in self._pending
            if self.cfg.confirm_delay_s <= 0 and self.paper.sizes():
                return  # altes Verhalten: real eingestiegen -> keine weiteren Leader

    def _register_pending(self, coin: str, snap) -> None:
        """Bestätigungs-Kandidat statt sofortigem Einstieg (Flip-Flopper-
        Schutz): der Coin-Slot gehört dem ERSTEN Kandidaten, egal welcher
        Leader - eine Kollision (auch desselben Leaders erneut, während er
        schon pending ist) wird schlicht ignoriert, kein Timer-Reset, kein
        Ersatz-Kandidat."""
        if coin in self._pending:
            return
        self._pending[coin] = {"leader": snap.address, "since": self.clock()}

    def _process_pending(self, leaders: list[dict], snapshots: list,
                         prices: dict[str, float]) -> None:
        """Jeden Tick: prüft alle Bestätigungs-Kandidaten. Negativ oder Leader
        selbst schon wieder raus -> SOFORT verwerfen (nicht bis zum
        Fensterende warten - genau der Flip-Flop-Fall, den wir verhindern
        wollen). Durchgehend nicht im Minus UND Fenster um -> promoten, zum
        AKTUELLEN Preis (nicht dem Preis beim ursprünglichen Signal)."""
        if not self._pending:
            return
        by_addr = {s.address.lower(): s for s in snapshots}
        scores = {str(l.get("address", "")).lower(): float(l.get("score", 0))
                 for l in leaders}
        ready = []
        for coin, info in list(self._pending.items()):
            leader = info["leader"]
            snap = by_addr.get(leader.lower())
            pos = snap.positions.get(coin) if snap else None
            if pos is None or float(pos.size) == 0:
                self._pending.pop(coin, None)
                self._reject(coin, leader, "unbestaetigt_leader_weg")
                continue
            price = prices.get(coin)
            if not price:
                # Kein Preis diesen Tick - weder disqualifizieren noch promoten,
                # ABER nicht unbegrenzt: fehlt der Preis dauerhaft (Live-Befund:
                # TAO blieb minutenlang "1 Signal(e) werden bestätigt", ohne
                # jemals zu promoten oder zu verwerfen), löst das Sicherheitsnetz
                # nach _PENDING_MAX_AGE_S aus, statt für immer zu hängen.
                if self.clock() - info["since"] > _PENDING_MAX_AGE_S:
                    self._pending.pop(coin, None)
                    self._reject(coin, leader, "unbestaetigt_kein_preis")
                continue
            if not pos.not_losing(price):
                self._pending.pop(coin, None)
                self._reject(coin, leader, "unbestaetigt_negativ")
                continue
            if self.clock() - info["since"] < self.cfg.confirm_delay_s:
                continue  # noch nicht lange genug bestätigt
            ready.append((coin, info, snap))
        if not ready:
            return
        # Mehrere gleichzeitig reif: Star zuerst, dann Score (wie beim
        # Discovery-Scan) - max_positions lässt ohnehin nur einen zu.
        ready.sort(key=lambda item: (self.is_star(item[1]["leader"]),
                                     scores.get(item[1]["leader"].lower(), 0.0)),
                  reverse=True)

        if self.paper.sizes():
            # Läuft noch ein Ritt: NUR eine Star-Preemption kann hier greifen
            # (die Exit-Folge in _tick_riding hätte sonst schon geschlossen).
            # Ein reifer Kandidat, dessen Leader kein Star ist, bleibt einfach
            # PENDING liegen - "wird zum normalen Fresh-Entry-Kandidaten", der
            # seine Chance bekommt, sobald wir wieder flach sind (BACKLOG).
            eligible = [r for r in ready if self.is_star(r[1]["leader"])]
            if not eligible:
                return
            win_coin, win_info, win_snap = eligible[0]
            # Profitabilität JETZT neu prüfen, nicht zum Entdeckungszeitpunkt -
            # der Kurs kann in den Sekunden der Bestätigung gekippt sein.
            if self.paper.equity(prices) - self.cfg.equity <= 0:
                return  # Preemption gegenstandslos - alle Kandidaten warten weiter
            for coin, info, _ in ready:
                if coin != win_coin:
                    self._pending.pop(coin, None)
                    self._reject(coin, info["leader"], "andere_bestaetigt")
            self._pending.pop(win_coin, None)
            for coin in list(self.paper.sizes()):
                self._close_coin(coin, prices, "star_preempt")
            self._settle_ride(prices, "star_preempt")
            self._enter(win_coin, win_snap, prices)
            return

        # FLACH: normaler Promotion-Pfad
        win_coin, win_info, win_snap = ready[0]
        for coin, info, _ in ready[1:]:
            self._pending.pop(coin, None)
            self._reject(coin, info["leader"], "andere_bestaetigt")
        # max_positions gilt erst HIER (Promotion kostet Kapital) - nicht
        # schon bei der Registrierung (die kostet nichts).
        if len(self.paper.sizes()) >= self.cfg.max_positions:
            self._pending.pop(win_coin, None)
            self._reject(win_coin, win_info["leader"], "max_ritte")
            return
        self._pending.pop(win_coin, None)
        self._enter(win_coin, win_snap, prices)

    def _fresh_coins(self, book: dict[str, float], prev: dict[str, float]) -> list[str]:
        """Frische Richtungs-Signale eines Leaders (nur im FLACH-Scan genutzt):
        - klassisch: Übergang 0 -> Position (neuer Trade)
        - Aufstockung >= add_signal_frac: Positions-Trader eröffnen selten neu,
          ihr Überzeugungs-Moment ist das Vergrößern (Live-Befund: 8/8 Feed-
          Abdeckung, aber 0 frische Signale an einem vollen Handelstag - der
          Pool hält und stockt auf, statt neu zu eröffnen)
        - Richtungs-Flip im Bestand (Long -> Short): stärkstes Signal überhaupt."""
        out = []
        add_frac = self.cfg.add_signal_frac
        for c, sz in book.items():
            if sz == 0:
                continue
            prev_sz = prev.get(c, 0.0)
            if prev_sz == 0.0:
                out.append(c)                                   # neuer Trade
            elif add_frac > 0 and (sz > 0) != (prev_sz > 0):
                out.append(c)                                   # Flip im Bestand
            elif add_frac > 0 and abs(sz) >= (1 + add_frac) * abs(prev_sz):
                out.append(c)                                   # deutliche Aufstockung
        return out

    # ---------- MESS-MODUS: parallele Ritte, jedes Signal = eigener Zyklus ----------

    def _tick_parallel(self, leaders: list[dict], snapshots: list,
                       prices: dict[str, float], risk_off: bool) -> None:
        # 1. Ziel/Bust JE RITT (eigene 1000$-Basis, nicht das Sammelbuch)
        for coin in list(self.paper.sizes()):
            pnl = self._ride_pnl(coin, prices)
            if pnl is None:
                continue
            if pnl >= self.cfg.target_profit:
                self._settle_one(coin, prices, "tp")
            elif self.cfg.equity + pnl <= self.cfg.equity * self.cfg.bust_frac:
                self._settle_one(coin, prices, "bust")
        # 2. Markt-Schutz: alle Mess-Ritte glattstellen, jeder = eigener Zyklus
        if risk_off:
            if self.paper.sizes():
                log.warning("Sprint: RISK_OFF - stelle alle Mess-Ritte glatt")
                for coin in list(self.paper.sizes()):
                    self._settle_one(coin, prices, "risk_off")
            return
        if not leaders or not snapshots:
            return
        by_addr = {s.address.lower(): s for s in snapshots}
        # 3. Exit-Folge je Ritt über SEINEN Leader
        for coin, our_size in list(self.paper.sizes().items()):
            leader = self.ride_leaders.get(coin, "")
            snap = by_addr.get(leader.lower()) if leader else None
            if snap is None:
                self._settle_one(coin, prices, "leader_rotated")
                continue
            book = self._book_of(snap)
            leader_sz = book.get(coin, 0.0)
            entry_sz = self._ride_entry_sizes.get(coin, abs(leader_sz))
            if leader_sz == 0.0:
                self._settle_one(coin, prices, "leader_exit")
            elif (leader_sz > 0) != (our_size > 0):
                # Flip = neue Richtung, neue Überzeugung -> neuer Mess-Ritt
                # (das Settle hat den Leader-Slot gerade freigegeben)
                self._settle_one(coin, prices, "leader_flip")
                if self._leader_rides(snap.address) < self.cfg.max_rides_per_leader:
                    self._enter(coin, snap, prices, parallel=True)
            elif abs(leader_sz) <= (1 - self.cfg.partial_exit_frac) * entry_sz:
                self._settle_one(coin, prices, "leader_scaleout")
        # 4. Frische Signale ALLER Leader - jeder darf (s)einen Ritt eröffnen,
        #    je Leader und Tick nur das stärkste Signal (Korb-Regel bleibt).
        #    Star zuerst, dann Score (wie im Einzel-Ritt-Scan) für Konsistenz.
        for l in sorted(leaders, key=lambda l: (
                self.is_star(str(l.get("address", ""))), float(l.get("score", 0))),
                reverse=True):
            addr = str(l.get("address", "")).lower()
            snap = by_addr.get(addr)
            if snap is None:
                continue
            prev = self._baselines.get(addr)
            if prev is None:
                continue
            fresh = self._fresh_coins(self._book_of(snap), prev)
            if not fresh:
                continue
            self._note_fresh(len(fresh))
            if addr in self.banned:
                for coin in fresh:
                    self._reject(coin, snap.address, "leader_gesperrt")
                continue
            fresh.sort(key=lambda c: abs(snap.exposure(c)), reverse=True)
            opened = False
            for coin in fresh:
                if opened:
                    self._reject(coin, snap.address, "korb_begrenzt")
                    continue
                if self._leader_rides(snap.address) >= self.cfg.max_rides_per_leader:
                    # 1 Position PRO TRADER: sonst füllt ein aktiver Leader über
                    # mehrere Ticks alle Slots und 7 korrelierte Wetten sähen
                    # aus wie 7 unabhängige Messpunkte (Nutzer-Befund)
                    self._reject(coin, snap.address, "leader_belegt")
                    continue
                if coin in self.paper.sizes():
                    self._reject(coin, snap.address, "coin_belegt")
                    continue
                if len(self.paper.sizes()) >= self.cfg.max_rides:
                    self._reject(coin, snap.address, "max_ritte")
                    continue
                before = len(self.paper.sizes())
                self._enter(coin, snap, prices, parallel=True)
                opened = len(self.paper.sizes()) > before

    def _leader_rides(self, addr: str) -> int:
        key = addr.lower()
        return sum(1 for a in self.ride_leaders.values() if a.lower() == key)

    def _ride_pnl(self, coin: str, prices: dict[str, float]) -> float | None:
        """PnL eines Mess-Ritts auf seiner eigenen 1000$-Basis, konservativ
        inkl. Eröffnungs- UND (hypothetischer) Schließungs-Fee."""
        row = next((r for r in self.paper.position_rows(prices)
                    if r["coin"] == coin), None)
        if row is None:
            return None
        px = prices.get(coin, row["entry"]) or row["entry"]
        fees = abs(row["size"]) * (row["entry"] + px) * self.fee_rate
        return row["size"] * (px - row["entry"]) - fees

    def _settle_one(self, coin: str, prices: dict[str, float], reason: str) -> None:
        """Mess-Ritt beenden = EIGENER Zyklus: Position im Sammelbuch schließen,
        PnL auf 1000$-Basis verbuchen, Strike/Heilung für DIESEN Leader."""
        pnl = self._ride_pnl(coin, prices)
        size = self.paper.sizes().get(coin, 0.0)
        if size:
            row = next((r for r in self.paper.position_rows(prices)
                        if r["coin"] == coin), None)
            px = prices.get(coin) or (row["entry"] if row else 0.0)
            if px:
                self.paper.execute(coin, -size, px)
        leader = self.ride_leaders.pop(coin, "")
        self._ride_entry_sizes.pop(coin, None)
        self._book_cycle(leader, pnl if pnl is not None else 0.0, reason, coin=coin)

    # ---------- IM RITT: halten, nur dem Ride-Leader folgen ----------

    def _tick_riding(self, leaders: list[dict], snapshots: list,
                     prices: dict[str, float]) -> None:
        snap = next((s for s in snapshots
                     if s.address.lower() == self.ride_leader.lower()), None)
        if snap is None:
            # Leader aus der Rotation gefallen: wir wären blind -> schließen + abrechnen
            log.warning("Sprint: Ride-Leader %s aus der Rotation - schließe Positionen",
                        self.ride_leader[:10])
            for coin in list(self.paper.sizes()):
                self._close_coin(coin, prices, "leader_rotated")
            self._settle_ride(prices, "leader_rotated")
            return
        book = self._book_of(snap)
        prev = self._baselines.get(snap.address.lower())

        # Exit-Folge: Leader komplett raus / geflippt / >= partial_exit_frac abgebaut
        last_reason = None
        for coin, our_size in list(self.paper.sizes().items()):
            leader_sz = book.get(coin, 0.0)
            entry_sz = self._ride_entry_sizes.get(coin, abs(leader_sz))
            if leader_sz == 0.0:
                self._close_coin(coin, prices, "leader_exit")
                last_reason = "leader_exit"
            elif (leader_sz > 0) != (our_size > 0):
                # Flip = mitgehen: alte Richtung schließen UND die neue eröffnen
                self._close_coin(coin, prices, "leader_flip")
                last_reason = "leader_flip"
                self._enter(coin, snap, prices)
            elif abs(leader_sz) <= (1 - self.cfg.partial_exit_frac) * entry_sz:
                # Scale-out: Leader hat den Großteil abgebaut -> wir gehen mit
                self._close_coin(coin, prices, "leader_scaleout")
                last_reason = "leader_scaleout"
        # Weitere frische Einstiege desselben Leaders mitnehmen (nicht ohne Baseline,
        # z.B. direkt nach Neustart - dann erst re-baselinen, kein Fehl-Einstieg)
        if prev is not None:
            fresh = [c for c, sz in book.items()
                     if sz != 0 and prev.get(c, 0.0) == 0.0
                     and c not in self.paper.sizes()]
            if fresh:
                self._note_fresh(len(fresh))
            for coin in fresh:
                if len(self.paper.sizes()) >= self.cfg.max_positions:
                    # Ein Ritt = eine Position: keine Zusatz-Coins mitten im Ritt
                    self._reject(coin, snap.address, "korb_begrenzt")
                    continue
                self._enter(coin, snap, prices)

        if not self.paper.sizes():
            self._settle_ride(prices, last_reason or "leader_exit")
            return
        # Star-Preemption (BACKLOG): auch während eines laufenden Ritts weiter
        # ALLE ANDEREN Leader auf frische Signale scannen - aber NUR Stars
        # dürfen einen Kandidaten registrieren. Läuft NUR, wenn der Ritt noch
        # steht (return oben deckt den "gerade beendet"-Fall ab).
        self._scan_star_preemption(leaders, snapshots, prices)

    def _scan_star_preemption(self, leaders: list[dict], snapshots: list,
                              prices: dict[str, float]) -> None:
        """Star-Preemption-Kandidaten registrieren (nicht selbst entscheiden -
        das macht _process_pending, der zum Bestätigungszeitpunkt neu prüft,
        ob der Ritt dann noch läuft UND noch profitabel ist)."""
        by_addr = {s.address.lower(): s for s in snapshots}
        for l in leaders:
            addr = str(l.get("address", "")).lower()
            if addr == self.ride_leader.lower() or not self.is_star(addr):
                continue  # eigener Ride-Leader läuft über die Exit-Folge oben;
                          # nur Stars dürfen preemten
            snap = by_addr.get(addr)
            if snap is None:
                continue
            prev = self._baselines.get(addr)
            if prev is None:
                continue
            fresh = self._fresh_coins(self._book_of(snap), prev)
            if not fresh:
                continue
            self._note_fresh(len(fresh))
            candidates = [c for c in fresh if c not in self.paper.sizes()]
            if not candidates:
                continue
            candidates.sort(key=lambda c: abs(snap.exposure(c)), reverse=True)
            for coin in candidates:
                ko = self._ineligible_reason(coin)
                if ko:
                    self._reject(coin, snap.address, ko)
                    continue
                self._register_pending(coin, snap)
                break   # nur das stärkste Signal dieses Stars pro Tick

    def _refresh_baselines(self, leaders: list[dict], snapshots: list) -> None:
        keep = {str(l.get("address", "")).lower() for l in leaders}
        keep.add(self.ride_leader.lower())
        changed = False
        for s in snapshots:
            key = s.address.lower()
            if key in keep:
                book = self._book_of(s)
                if self._baselines.get(key) != book:
                    self._baselines[key] = book
                    changed = True
        for addr in list(self._baselines):
            if addr not in keep:
                self._baselines.pop(addr, None)
                changed = True
        self._persist_baselines(changed)

    def _persist_baselines(self, changed: bool) -> None:
        """Baselines auf Platte, damit ein Neustart kein Blindfenster reißt
        (RAM-only hieß: jeder Deploy re-baselined alles, während der Downtime
        eröffnete Positionen galten für immer als 'alt'). Geschrieben wird bei
        Änderung sofort, sonst alle 60s (frischer Zeitstempel)."""
        now = self.clock()
        if not changed and now - self._baselines_saved_t < 60:
            return
        self._baselines_saved_t = now
        try:
            self.baselines_path.write_text(json.dumps(
                {"t": now, "baselines": self._baselines}))
        except OSError:
            log.debug("sprint_baselines.json nicht schreibbar", exc_info=True)

    def _load_baselines(self) -> None:
        try:
            raw = json.loads(self.baselines_path.read_text())
            age = self.clock() - float(raw.get("t", 0))
            if age > _BASELINE_MAX_AGE_S:
                log.info("Sprint: Baseline-Datei %.0fs alt (> %.0fs) - re-baseline "
                         "(zu lange down, verpasste Einstiege wären nicht mehr frisch)",
                         age, _BASELINE_MAX_AGE_S)
                self._baseline_status = f"kalt neu gesetzt (Datei war {age:.0f}s alt)"
                return
            self._baselines = {
                str(a).lower(): {str(c): float(s) for c, s in (b or {}).items()}
                for a, b in raw.get("baselines", {}).items()
            }
            log.info("Sprint: %d Baselines übernommen (%.0fs alt) - kein "
                     "Blindfenster nach Neustart", len(self._baselines), age)
            self._baseline_status = f"warm übernommen ({age:.0f}s alt beim Start)"
        except (OSError, ValueError, TypeError, AttributeError):
            pass   # keine/kaputte Datei -> normales Re-Baseline beim ersten Tick (Default bleibt "kein Vorstand")

    def _note_fresh(self, n: int) -> None:
        self._fresh_seen += n
        self._last_fresh_t = self.clock()

    def _reject(self, coin: str, addr: str, reason: str) -> None:
        """Verworfenes frisches Signal SICHTBAR machen (Zähler + letzte Fälle) -
        stille returns waren von 'es kam nie ein Signal' nicht unterscheidbar."""
        self._scan_rejected[reason] = self._scan_rejected.get(reason, 0) + 1
        self._scan_last.append({"t": int(self.clock()), "coin": coin,
                                "leader": addr[:10], "grund": reason})
        del self._scan_last[:-5]
        log.info("Sprint: frisches Signal %s von %s VERWORFEN (%s)",
                 coin, addr[:10], reason)

    # ---------- Ein-/Ausstieg ----------

    def _ineligible_reason(self, coin: str) -> str | None:
        """Statische Zulässigkeit (unabhängig von Preis/Timing) - None = ok.
        Genutzt von _enter() UND von der Korb-Registrierung im FLACH-Scan:
        die Registrierung muss VOR der Bestätigung wissen, ob ein Kandidat
        überhaupt je eintreten könnte, sonst würde der stärkste (aber
        ausgeschlossene) Coin den Slot blockieren, statt dem nächststärksten
        Platz zu machen."""
        if coin in self.cfg.exclude_coins:
            return "coin_ausgeschlossen"  # Beta statt Leader-Alpha (z.B. BTC)
        if self.cfg.crypto_only and ":" in coin:
            # Builder-DEX-Asset (Aktien/Gold, z.B. 'xyz:INTC'): außerhalb der
            # Börsenzeiten reine Spekulation - stört die Messlatte (Nutzer)
            return "kein_krypto"
        return None

    def _enter(self, coin: str, snap, prices: dict[str, float],
               parallel: bool = False) -> None:
        ko = self._ineligible_reason(coin)
        if ko:
            self._reject(coin, snap.address, ko)
            return
        price = prices.get(coin)
        if not price or price <= 0:
            self._reject(coin, snap.address, "kein_hl_preis")
            return
        if parallel:
            # Mess-Modus: JEDER Ritt startet auf frischer equity-Basis (1000$),
            # unabhängig vom Sammelbuch - 1 Signal = 1 Ritt = 1k (Nutzer)
            cap = self.cfg.leverage * self.cfg.equity
            target = snap.exposure(coin) * self.cfg.leverage * self.cfg.equity
            notional = max(-cap, min(cap, target))
        else:
            equity = self.paper.equity(prices)
            target = snap.exposure(coin) * self.cfg.leverage * equity
            # Gross-Cap: Gesamtbuch bleibt unter leverage x Equity
            gross = sum(abs(s) * prices.get(c, 0.0) for c, s in self.paper.sizes().items())
            headroom = max(0.0, self.cfg.leverage * equity - gross)
            notional = max(-headroom, min(headroom, target))
        if abs(notional) < self.cfg.min_notional:
            self._reject(coin, snap.address, "unter_min_notional")
            return
        self._last_entry_t = self.clock()
        if not parallel and self._ride_start_equity is None:
            self._ride_start_equity = self.paper.equity(prices)  # PnL-Basis des Ritts
        self.paper.execute(coin, notional / price, price)
        if parallel:
            self.ride_leaders[coin] = snap.address
        else:
            self.ride_leader = snap.address
        self._ride_entry_sizes[coin] = abs(self._book_of(snap).get(coin, 0.0))
        side = "LONG" if notional > 0 else "SHORT"
        cycle = self.won + self.busted + 1
        log.info("Sprint: %s %s $%.0f (frisches Signal von %s, Zyklus %d)",
                 side, coin, abs(notional), snap.address[:10], cycle)
        if self.journal:
            self.journal.record("sprint_entry", coin=coin, side=side,
                                notional=round(abs(notional), 0),
                                leader=snap.address, cycle=cycle)
        if self.notifier:
            self.notifier.send(f"🟢 <b>Sprint-Einstieg</b>: {side} {coin} "
                               f"${abs(notional):,.0f}\nLeader <code>{snap.address[:10]}…</code> "
                               f"| Zyklus {cycle}")
        self._save_state()

    def _close_coin(self, coin: str, prices: dict[str, float], reason: str) -> None:
        size = self.paper.sizes().get(coin, 0.0)
        price = prices.get(coin)
        if size == 0.0 or not price:
            return
        self.paper.execute(coin, -size, price)
        run_pnl = self.paper.equity(prices) - self.cfg.equity
        reason_txt = _REASON_TXT.get(reason, reason)
        log.info("Sprint: Exit %s (%s), Zyklus-PnL %+.2f", coin, reason_txt, run_pnl)
        if self.journal:
            self.journal.record("sprint_exit", coin=coin, reason=reason,
                                cycle_pnl=round(run_pnl, 2))
        if self.notifier:
            self.notifier.send(f"🔴 <b>Sprint-Exit</b> {coin}: {reason_txt}\n"
                               f"Zyklus-PnL {run_pnl:+,.2f} $")

    # ---------- Zyklus-Ende (= Ritt-Ende, v3) ----------

    def close(self, prices: dict[str, float]) -> int:
        """Manueller Not-Ausstieg (/sprint close): sofort verbuchen + resetten,
        KEIN Strike (war deine Entscheidung, nicht der Leader). Gibt Anzahl zu."""
        n = len(self.paper.sizes())
        if n == 0:
            return 0
        if self.cfg.parallel_rides:
            # Mess-Modus: jeder Ritt wird als EIGENER Zyklus verbucht
            for coin in list(self.paper.sizes()):
                self._settle_one(coin, prices, "manual")
            return n
        for coin in list(self.paper.sizes()):
            self._close_coin(coin, prices, "manual")
        self._settle_ride(prices, "manual")
        return n

    def _settle_ride(self, prices: dict[str, float], reason: str) -> None:
        """Ritt zu Ende = Zyklus zu Ende (v3): sofort verbuchen (banked/won/busted),
        Konto sofort auf equity zurücksetzen. flatten() ist idempotent - falls schon
        alles per _close_coin zu ist (Normalfall), passiert hier nichts mehr; beim
        TP/Bust-Pfad (noch offene Positionen) schließt es hier alles auf einmal.

        Strikes bleiben ritt-scharf: Verlust -> Strike, Gewinn heilt einen (min 0).
        Ausnahme 'manual'/'risk_off' - nicht die Entscheidung/Schuld des Leaders."""
        self.paper.flatten(prices)
        leader = self.ride_leader
        pnl = self.paper.equity(prices) - self.cfg.equity
        self.total_trades += self.paper.trades
        # Coin VOR dem .clear() ziehen - unter Einzel-Ritt hält ein Ritt nie
        # mehr als einen Coin gleichzeitig (Flip dreht nur die Richtung, nicht
        # den Coin), also ist das eindeutig "der" Ritt-Coin fürs Journal
        # (fehlte bisher hier, anders als bei _settle_one im Mess-Modus - ohne
        # das kann kein Auswertungs-Tool nach Coin/Asset-Klasse aufschlüsseln).
        coin = next(iter(self._ride_entry_sizes), None)
        self._book_cycle(leader, pnl, reason, coin=coin)
        self.paper.reset()
        self.ride_leader = ""
        self._ride_start_equity = None
        self._ride_entry_sizes.clear()
        self._save_state()

    def _book_cycle(self, leader: str, pnl: float, reason: str,
                    coin: str | None = None) -> None:
        """Gemeinsames Zyklus-Ende beider Modi: verbuchen (banked/won/busted),
        Strike/Heilung für den Ritt-Leader, Journal + Telegram."""
        cycle = self.won + self.busted + 1
        self.banked += pnl
        won = pnl > 0
        if won:
            self.won += 1
        else:
            self.busted += 1

        if leader and reason not in _STRIKE_EXEMPT:
            key = leader.lower()
            if pnl < 0:
                self.strikes[key] = self.strikes.get(key, 0) + 1
                n = self.strikes[key]
                log.warning("Sprint: Verlust-Ritt %+.2f -> Strike %d für %s",
                            pnl, n, leader[:10])
                if self.journal:
                    self.journal.record("sprint_strike", leader=leader, strikes=n,
                                        ride_pnl=round(pnl, 2))
                if n >= self.cfg.strike_ban and key not in self.banned:
                    self.banned.add(key)
                    if self.journal:
                        self.journal.record("sprint_ban", leader=leader)
                    if self.notifier:
                        self.notifier.send(
                            f"🚫 <b>Leader enttarnt</b> <code>{leader[:10]}…</code>\n"
                            f"{n} Verlust-Ritte in Folge - fürs Sprint-Buch gesperrt.")
                elif self.notifier:
                    self.notifier.send(f"⚠️ Strike {n}/{self.cfg.strike_ban} für "
                                       f"<code>{leader[:10]}…</code> (Verlust-Ritt {pnl:+,.2f}$)")
            elif pnl > 0 and self.strikes.get(key):
                self.strikes[key] = max(0, self.strikes[key] - 1)  # profitabel heilt

        if leader and reason in _CONFIDENCE_EARNING and pnl > 0:
            key = leader.lower()
            was_star = self.confidence.get(key, 0) >= STAR_THRESHOLD
            self.confidence[key] = self.confidence.get(key, 0) + CONFIDENCE_PER_WIN
            if self.confidence[key] >= STAR_THRESHOLD and not was_star:
                log.warning("Sprint: %s ist jetzt ein STAR (%d Confidence-Punkte)",
                            leader[:10], self.confidence[key])
                if self.journal:
                    self.journal.record("sprint_star", leader=leader,
                                        confidence=self.confidence[key])
                if self.notifier:
                    self.notifier.send(
                        f"⭐ <b>Star enttarnt</b> <code>{leader[:10]}…</code>\n"
                        f"{self.confidence[key]} Confidence-Punkte - wiederholt "
                        f"bewiesener Erfolg, kein Zufall.")

        icon = "🏁" if reason == "tp" else "💥" if reason == "bust" else ("✅" if won else "🔻")
        reason_txt = _REASON_TXT.get(reason, reason)
        kind = {"tp": "sprint_tp", "bust": "sprint_bust"}.get(reason, "sprint_cycle_end")
        what = f"Zyklus {cycle}" + (f" ({coin})" if coin else "")
        log.warning("Sprint-%s %s (%s): PnL %+.2f (banked gesamt %+.2f)",
                    what, "gewonnen" if won else "verloren", reason_txt, pnl, self.banked)
        if self.journal:
            self.journal.record(kind, cycle=cycle, pnl=round(pnl, 2), reason=reason,
                                banked=round(self.banked, 2), leader=leader,
                                **({"coin": coin} if coin else {}))
        if self.notifier:
            self.notifier.send(
                f"{icon} <b>{what} beendet</b> ({reason_txt}): {pnl:+,.2f} $\n"
                f"Bilanz: {self.won}✅ {self.busted}💥 | Schatztruhe {self.banked:+,.2f} $"
            )
        self._save_state()

    # ---------- Status & Persistenz ----------

    def stats(self, prices: dict[str, float]) -> dict:
        # KEIN Sonderfall für leere prices: equity()/position_rows() degradieren
        # selbst schon sauber (Preis fehlt -> Entry-Preis bzw. 0 als Fallback,
        # nie ein Crash). Ein "if prices else leer"-Sonderfall hier würde eine
        # ECHTE offene Position kurz nach einem Neustart (bevor frische Preise
        # da sind) fälschlich als 'wartet auf frisches Signal' zeigen - genau
        # der Bug, der hier gefunden wurde.
        # EINE Abfrage des Positionsbestands, held/state/positions leiten sich alle
        # daraus ab - der Hintergrund-Loop (eigener Thread) kann jederzeit einen
        # Ritt schließen; zwei getrennte self.paper-Aufrufe könnten sonst
        # auseinanderlaufen (state="hält" während positions bereits leer ist).
        positions = self.paper.position_rows(prices)
        if self.cfg.parallel_rides:
            # Mess-Modus: je Ritt eigene 1000$-Basis - Equity/PnL sind die SUMME
            # der Ritt-PnLs auf equity-Basis, nicht das (driftende) Sammelbuch.
            # Jede Zeile bekommt IHREN Leader - sonst ist nicht zuordenbar, ob
            # 7 Ritte von 7 Tradern oder von einem stammen (Nutzer-Befund).
            for p in positions:
                p["leader"] = self.ride_leaders.get(p["coin"], "")[:10]
            ride_pnls = sum(p["unrealized_pnl"] for p in positions)
            eq = self.cfg.equity + ride_pnls
        else:
            eq = self.paper.equity(prices)
        held = [f"{p['coin']} {'LONG' if p['size'] > 0 else 'SHORT'}" for p in positions]
        # Leader NUR zeigen, solange wir wirklich reiten (aus derselben
        # positions-Momentaufnahme abgeleitet wie held/state) - self.ride_leader
        # separat zu lesen könnte sonst denselben Cross-Thread-Race wie oben
        # zeigen: Hintergrund-Loop räumt zwischen dem Schließen der Positionen
        # und dem Leader-Reset in _settle_ride() kurz auf, ein zeitgleicher
        # /sprint-Aufruf sähe dann 'wartet auf frisches Signal' UND einen
        # (bereits stillgelegten) Leader gleichzeitig - genau der beobachtete Bug.
        leader = self.ride_leader if positions else ""
        cycles_done = self.won + self.busted
        # Zyklus = Ritt (v3): cycle_pnl IST die PnL des laufenden Ritts, es gibt
        # keine separate "Ritt-PnL" mehr (die beiden waren vorher unterschiedlich,
        # weil ein Zyklus mehrere Ritte akkumulieren konnte - das gibt's nicht mehr).
        return {
            "equity": round(eq, 2),
            "cycle": cycles_done + 1,
            "cycle_pnl": round(eq - self.cfg.equity, 2),
            "positions": positions,
            "target": round(self.cfg.equity + self.cfg.target_profit, 2),
            "progress_pct": round((eq - self.cfg.equity) / self.cfg.target_profit * 100, 1),
            "state": (f"{len(positions)} Mess-Ritte laufen"
                      if positions and self.cfg.parallel_rides
                      else "hält" if positions
                      else f"{len(self._pending)} Signal(e) werden bestätigt"
                      if self._pending
                      else "wartet auf frisches Signal"),
            "held": held,
            "parallel": self.cfg.parallel_rides,
            "banked": round(self.banked, 2),
            "won": self.won,
            "busted": self.busted,
            "trades": self.paper.trades,
            "avg_trades_per_cycle": round((self.total_trades + self.paper.trades)
                                          / max(1, cycles_done + 1), 1),
            "leader": leader,
            "leader_is_star": self.is_star(leader) if leader else False,
            "strikes": {a[:10]: n for a, n in self.strikes.items() if n > 0},
            "banned": [a[:10] for a in self.banned],
            # Confidence nur für Leader mit Punkten (0 sind uninteressant);
            # stars separat abgeleitet, damit die Anzeige nicht bei jedem
            # Leader neu >= STAR_THRESHOLD rechnen muss.
            "confidence": {a[:10]: n for a, n in self.confidence.items() if n > 0},
            "stars": [a[:10] for a, n in self.confidence.items() if n >= STAR_THRESHOLD],
            # Bestätigungs-Kandidaten (Flip-Flopper-Schutz): laufen gerade,
            # noch nicht promoted/verworfen - sonst wäre "wartet auf frisches
            # Signal" von "Signal wartet auf Bestätigung" ununterscheidbar.
            # 'hat_preis' macht sofort sichtbar, WARUM ein Kandidat trotz
            # abgelaufenem Fenster noch nicht promoted ist (Live-Befund: TAO
            # blieb bei 62s/10s hängen, weil kein Preis ankam) - ohne
            # 'hat_preis' sah das identisch zu 'wartet noch normal' aus.
            "pending": [{"coin": c, "leader": i["leader"][:10],
                        "wait_s": round(self.clock() - i["since"], 1),
                        "hat_preis": bool(prices.get(c))}
                       for c, i in self._pending.items()],
            # Warm/kalt-Start (Nutzer-Nachfrage): unterscheidet 'echte Ruhe seit
            # dem letzten Save' von 'frischer Kaltstart, noch keine
            # Vergleichsbasis' - sonst sieht 'scan.fresh_seen == 0' in beiden
            # Fällen identisch aus.
            "baseline_status": self._baseline_status,
            # Scan-Telemetrie (seit Prozess-Start): macht 'kein Signal kam' von
            # 'Signal kam, wurde verworfen' unterscheidbar
            "scan": {
                "fresh_seen": self._fresh_seen,
                "rejected": dict(self._scan_rejected),
                "last_rejected": list(self._scan_last),
                "last_fresh_min": (round((self.clock() - self._last_fresh_t) / 60, 1)
                                   if self._last_fresh_t else None),
                "last_entry_min": (round((self.clock() - self._last_entry_t) / 60, 1)
                                   if self._last_entry_t else None),
            },
        }

    def _migrate_v1_or_load(self) -> None:
        raw = None
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            pass
        if raw is not None:
            self.banked = float(raw.get("banked", 0.0))
            self.won = int(raw.get("won", 0))
            self.busted = int(raw.get("busted", 0))
            self.total_trades = int(raw.get("total_trades", 0))
            self.ride_leader = str(raw.get("ride_leader", ""))
            self.ride_leaders = {str(c): str(a) for c, a in
                                 (raw.get("ride_leaders") or {}).items()}
            self.strikes = {str(k): int(v) for k, v in (raw.get("strikes") or {}).items()}
            self.banned = {str(a).lower() for a in (raw.get("banned") or [])}
            self.confidence = {str(k): int(v) for k, v in
                               (raw.get("confidence") or {}).items()}
            # Mess-Modus -> Einzel-Ritt (QOL-Runde, Nutzer-Entscheidung): die
            # Bilanz (Zyklus-Zähler/Schatztruhe) der Mess-Woche wurde unter
            # anderen Regeln erzielt (parallele Ritte, kein Bestätigungsfenster)
            # und soll die künftige Einzel-Ritt-Messung nicht verfälschen - EIN
            # frischer Start bei Zyklus 1/0$, sobald wir das erste Mal unter
            # parallel_rides=false laden. Strikes/Bans/Confidence bleiben (echtes
            # LARP-Wissen, keine Mess-Modus-spezifische Zahl). Alte v2/v3-State-
            # Dateien ohne 'parallel_rides'-Feld (vor diesem Feature gespeichert)
            # gelten als 'war Mess-Modus' (Default True) - das trifft exakt den
            # Umstieg von der laufenden Mess-Woche. Echte v1-Altlasten (noch kein
            # 'ride_leader'-Feld) sind ein ANDERER, älterer Migrationsfall mit
            # eigenem "Bilanz bleibt"-Vertrag (siehe unten) - hier ausgenommen.
            was_v1 = "ride_leader" not in raw
            was_parallel = bool(raw.get("parallel_rides", True))
            if not was_v1 and was_parallel and not self.cfg.parallel_rides:
                if self.ride_leaders:
                    # Noch offene Alt-Mess-Ritte: der Reset muss WARTEN, bis
                    # tick() sie über die Mode-Switch-Sicherung sauber
                    # abgerechnet hat - sonst zählt deren Forced-Close als
                    # allererster Eintrag der eigentlich frischen Bilanz
                    # (genau der Bug, den ein Nutzer live beobachtet hat).
                    self._bilanz_reset_pending = True
                else:
                    self.reset_bilanz()
        # v1-Migration: altes Buch hat mit Dauer-Reconciliation gechurnt (139 Trades)
        # -> Buch einmalig sauber neu starten, Bilanz (banked/won/busted) behalten.
        v1_state = raw is not None and "ride_leader" not in raw
        v1_dirty_book = raw is None and self.paper.trades > 0
        if v1_state or v1_dirty_book:
            log.warning("Sprint v1 -> v2: setze verchurntes Buch zurück (%d Trades), "
                        "Bilanz bleibt", self.paper.trades)
            self.paper.reset()
            self.ride_leader = ""
            self._save_state()

    def reset_bilanz(self) -> None:
        log.warning("Sprint: Mess-Modus -> Einzel-Ritt - Bilanz (Zyklus/"
                    "Schatztruhe) zurückgesetzt, Strikes/Bans/Confidence "
                    "bleiben erhalten")
        self.won = 0
        self.busted = 0
        self.banked = 0.0
        self.total_trades = 0
        self._bilanz_reset_pending = False
        self._save_state()

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(exist_ok=True)
            self.state_path.write_text(json.dumps({
                "updated": int(self.clock()), "banked": round(self.banked, 2),
                "won": self.won, "busted": self.busted,
                "total_trades": self.total_trades, "ride_leader": self.ride_leader,
                "ride_leaders": self.ride_leaders,
                "strikes": self.strikes, "banned": sorted(self.banned),
                "confidence": self.confidence,
                "parallel_rides": self.cfg.parallel_rides,
            }))
        except OSError:
            log.exception("sprint_cycles.json nicht schreibbar")

    def is_star(self, addr: str) -> bool:
        return self.confidence.get(addr.lower(), 0) >= STAR_THRESHOLD

    @staticmethod
    def _book_of(snap) -> dict[str, float]:
        return {c: float(p.size) for c, p in snap.positions.items() if float(p.size) != 0.0}
