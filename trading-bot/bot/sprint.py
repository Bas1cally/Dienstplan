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
  Ausnahme: RISK_OFF/Börsen-Schluss sind erzwungene Schließungen, nicht die
  Entscheidung/Schuld des Leaders - kein Strike. Ein manueller Close (Nutzer-
  Entscheidung 17.07.) striked dagegen wie jeder andere Ritt - meist genau
  DESHALB manuell, weil er schon erkennbar schlecht läuft.

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
    "zeit_negativ": "zu lange im Minus (Zeit-Cut)",
    "markt_zu": "Börsen-Schluss (Gewinn gesichert)",
    "plus_lock": "Plus gesichert (war im Plus - kein Minus-Exit)",
    "kein_preis": "kein Preis verfügbar (zum Einstand abgerechnet)",
}
# Nutzer-Fund 22.07.: die Meldung oben behauptete "kein Minus-Exit", obwohl
# der gemeldete PnL negativ war (-7.24$, HYPE) - Plus-Lock kann trotz
# Fast-Path (siehe fast_exit_check) durch einen Kurssprung ZWISCHEN zwei
# Checks fallen (Gap-durch-den-Stop, Spiegel-Fund 22.07.: bis zu -33.57$
# vor dem Fast-Path). Der Text log darf das nicht weiterhin abstreiten,
# sonst wirkt die Meldung selbst-widersprüchlich (🔻 + "kein Minus-Exit").
_PLUS_LOCK_OVERSHOOT_TXT = ("Plus-Sicherung zu spät ausgelöst (Kurs fiel "
                           "zwischen zwei Checks durch den Floor)")
# Kein Strike: nicht die Entscheidung/Schuld des Leaders - erzwungene/externe
# Schließung, sein eigenes Verhalten war dabei irrelevant. 'markt_zu' = unsere
# Börsen-Öffnungszeiten-Regel (Gewinn sichern), 'risk_off' = Markt-weiter
# Schock, beides unabhängig vom Leader. 'manual' (Nutzer greift per /quest
# close ein) war früher auch exempt - Nutzer-Entscheidung 17.07.: ein Ritt,
# den man vorzeitig abbricht, ist meist genau DESHALB ein manueller Eingriff,
# weil er schon erkennbar schlecht läuft ("das ist ganz klar ein Gambler") -
# das soll wie jeder andere Verlust-Ritt einen Strike geben (pnl<0 in
# _book_cycle heilt/striked wie gehabt, ein GEWINN-Manual-Close striked
# also weiterhin nicht).
_STRIKE_EXEMPT = {"risk_off", "markt_zu", "plus_lock", "kein_preis"}
# 'kein_preis' (Spiegel-Fund 24.07.): derselbe Drain-Grundsatz wie beim
# Mode-Switch (_DRAIN_MAX_AGE_S) für laufende Mess-Ritte - fehlt einem Coin
# der Preis dauerhaft (> _DRAIN_MAX_AGE_S), zwangsabgerechnet zum Einstand
# statt für immer blind zu warten. Reine Datenlücke, keine Leader-Schuld.
# 'plus_lock' (Nutzer 19.07.: "sobald wir im Plus sind sollten wir nie mit
# Minus rausgehen") ist UNSERE Gewinn-Sicherungs-Regel, nicht die Entscheidung
# des Leaders - beabsichtigt landet der Exit um breakeven-positiv, kann aber
# durch einen Kurssprung ZWISCHEN zwei Checks auch negativ ausfallen (Spiegel-
# Fund 22.07., siehe _PLUS_LOCK_OVERSHOOT_TXT) - trotzdem UNSER Timing-Problem,
# nicht die Schuld des Leaders, ein Strike dafür wäre doppelt unfair.

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

# Spiegel-Anzeige-Kürzung (Nutzer-Fund 20.07.: "counter:0x12345678" kollidierte
# mit anderen Counter-Identitäten bei 10 Zeichen -> auf 18 angehoben). Spiegel-
# Fund 23.07. (beim Debuggen des "Signale schwächen ab"-Ticket): 18 reicht für
# 0x-Adressen (10 Präfix + 8 Hex), aber NICHT für Lighter - "counter:lighter:"
# ist bereits 16 Zeichen lang, ließ nur 2 Ziffern der eigentlichen ID übrig
# ("counter:lighter:72" für sowohl ...726314 als auch ...726722 - nicht mehr
# unterscheidbar). 24 zeigt bei typischen 6-stelligen Lighter-IDs die volle ID.
_DISPLAY_TRUNC = 24

# Baselines überleben Neustarts nur, wenn die Datei jünger ist: bei kurzen
# Deploys (~1 min) wollen wir das Blindfenster schließen (ein während des
# Neustarts eröffnetes Signal ist noch frisch genug zum Reiten). Nach langer
# Downtime wäre der Einstieg dagegen längst verpasst -> lieber re-baselinen.
_BASELINE_MAX_AGE_S = 600.0

# Idle-Rotation: _last_active-Einträge älter als das (7 Tage) werden gekappt,
# damit der Dict nicht unbegrenzt über alle je gesehenen Wallets wächst.
_ACTIVE_MAX_AGE_S = 7 * 86_400.0

# Mode-Switch-Drain-Sicherung: kann ein Alt-Ritt-Coin so lange nicht bepreist
# werden, wird er zwangsabgerechnet, damit der Bot nicht ewig blockiert
# (Audit-Fund: ein Aktien-Perp am Wochenende fehlt dauerhaft in all_mids()).
_DRAIN_MAX_AGE_S = 120.0

# Sicherheitsnetz gegen einen Live-Befund: fehlt für einen pending Coin
# dauerhaft der Preis (z.B. Symbol nicht in all_mids(), Feed-Lücke für genau
# diesen Coin), wartete der Kandidat bisher UNBEGRENZT - weder Promotion noch
# Reject war möglich, "Bestätigung läuft" stand für immer bei X% des
# Fensters. Nach dieser Zeit OHNE JEMALS einen Preis gesehen zu haben, wird
# der Kandidat verworfen statt für immer zu hängen.
_PENDING_MAX_AGE_S = 120.0


class SprintBook:
    def __init__(self, cfg, fee_rate: float, notifier=None, journal=None,
                 runtime_dir=None, clock=time.time, max_leverage_fn=None):
        runtime = runtime_dir or RUNTIME
        self.cfg = cfg
        self.notifier = notifier
        self.journal = journal
        self.clock = clock
        # Live-Fund (Nutzer 17.07.): Hyperliquid erlaubt nicht auf jedem Coin
        # denselben Hebel (z.B. Meme-Perps oft nur 3-5x statt 10-20x bei
        # Majors) - der Bot eröffnete PENGU trotzdem mit dem konfigurierten
        # Hebel, ein Trade, den die echte Exchange so gar nicht anbietet.
        # coin -> echtes Hebel-Limit (z.B. HyperliquidClient.max_leverage);
        # None = kein Live-Datenpunkt (Tests, kein Client verdrahtet) -
        # dann bleibt NUR der konfigurierte Wert maßgeblich, kein Kappen.
        self._max_leverage_fn = max_leverage_fn
        self._last_hl_max_leverage: float | None = None  # s. _leverage_for
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
        # Wann der Mode-Switch-Drain zum ersten Mal an unpreisbaren Alt-Ritt-
        # Coins hängenblieb (Freeze-Sicherung, siehe _DRAIN_MAX_AGE_S).
        self._drain_since: float | None = None
        # Dasselbe Prinzip für laufende Mess-Ritte (Spiegel-Fund 24.07.):
        # coin -> seit wann sein Preis im aktuellen prices-Dict fehlt. Nicht
        # persistiert (self-healing, ein Neustart startet den Timer neu -
        # kein Korrektheitsproblem, nur im schlimmsten Fall etwas später
        # erkannt) - siehe _check_ride_targets.
        self._price_missing_since: dict[str, float] = {}
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
        self._ride_start_t: float | None = None        # Ritt-Beginn (für Zeit+negativ-Cut)
        self._ride_entry_sizes: dict[str, float] = {}  # coin -> |Leader-Größe| beim Einstieg
        # Ritt-Beginn JE COIN für den Zeit+negativ-Cut im MESS-MODUS (Live-Fund
        # 18.07.: ein BTC-Mess-Ritt hing 3.3h im Minus, obwohl config.yaml
        # max_ride_hours: 2 scharf hatte - der Cut lief bisher NUR im Einzel-
        # Ritt-Zweig, _tick_parallel erfasste nicht einmal Startzeiten).
        # Persistiert wie _ride_entry_sizes, damit ein Deploy die Uhr laufender
        # Ritte nicht zurücksetzt.
        self._ride_start_ts: dict[str, float] = {}
        # Gewinn-/Verlust-Zyklen JE LEADER im EIGENEN Buch (Edge-Runde 19.07.):
        # Grundlage für Hot-Hand-Slots/-Sizing und Trust=Speed. Bewusst aus
        # ECHTEN Zyklen statt Analyse-Scores - ein Leader ist erst 'bewiesen',
        # wenn er UNS Geld verdient hat. Verluste aus _STRIKE_EXEMPT-Gründen
        # (risk_off/markt_zu = erzwungene Schließungen) zählen nicht gegen ihn,
        # konsistent zur Strike-Philosophie. Persistiert wie strikes/confidence.
        self.leader_record: dict[str, dict] = {}
        # Peak-PnL je laufendem Ritt (Trailing-TP): coin -> höchster gesehener
        # Ritt-PnL; Einzel-Ritt-Modus nutzt den Schlüssel "__single__".
        # Persistiert, damit ein Deploy den Peak eines laufenden Ritts nicht
        # vergisst (sonst würde der Trail nach Neustart vom aktuellen PnL aus
        # neu messen und zu spät/gar nicht ziehen).
        self._ride_peak: dict[str, float] = {}
        # Baseline je Leader (addr -> coin -> signierte Größe): nur Übergänge
        # 0 -> Position NACH der Baseline sind frische Signale. Läuft für ALLE
        # Rotations-Leader mit (auch während eines Ritts), damit nach dem Ritt
        # keine längst laufenden Positionen fälschlich als "frisch" gelten.
        self._baselines: dict[str, dict[str, float]] = {}
        # Idle-Rotation: addr -> Zeitpunkt des letzten FRISCHEN Signals dieses
        # Leaders (geseedet beim Pool-Eintritt). Wer zu lange stumm ist, wird
        # beim Pool-Rebuild nach hinten rotiert (idle_addrs). Persistiert neben
        # den Baselines, damit ein Neustart die Idle-Uhr nicht zurücksetzt.
        self._last_active: dict[str, float] = {}
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
                # Bestätigungsfenster (Wave-3-Audit-Fund 18.07.): lief bisher
                # NUR im Einzel-Ritt-Zweig unten - tick() kehrte im Mess-Modus
                # immer schon vorher zurück. config.yaml hatte confirm_delay_s
                # trotzdem scharf ("Flip-Flopper-Schutz"), der im aktiven
                # Mess-Modus also nie griff, jedes Signal wurde sofort geritten.
                # Star-Preemption gibt es im Mess-Modus bewusst NICHT (siehe
                # _promote_pending_parallel) - hier gibt es keinen EINEN Ritt,
                # den man verdrängen müsste, ein neues Signal nimmt einfach
                # den nächsten freien Ritt-Slot.
                self._process_pending(leaders, snapshots, prices)
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
                # Rest ohne Preis diesen Tick - normalerweise nächster Tick
                # erneut. ABER nicht unbegrenzt: hat ein Alt-Ritt-Coin über
                # _DRAIN_MAX_AGE_S hinweg NIE einen Preis bekommen (Audit-Fund:
                # ein Aktien-Perp 'xyz:...' am Wochenende fehlt dauerhaft in
                # all_mids() -> die Sicherung würde SONST jeden Tick für immer
                # zurückkehren und der ganze Bot handelte nie wieder), wird er
                # zum Einstandspreis (Breakeven, _settle_one fällt auf entry
                # zurück) zwangsabgerechnet, damit die Pipeline weiterläuft.
                if self._drain_since is None:
                    self._drain_since = self.clock()
                elif self.clock() - self._drain_since > _DRAIN_MAX_AGE_S:
                    log.warning("Sprint: Alt-Ritt-Coins seit %.0fs ohne Preis - "
                                "zwangsabrechnen zum Einstand, damit der Bot nicht "
                                "ewig blockiert: %s", _DRAIN_MAX_AGE_S,
                                list(self.ride_leaders))
                    for coin in list(self.ride_leaders):
                        self._settle_one(coin, prices, "mode_switch")
                    self._drain_since = None
                else:
                    return  # noch in der Karenz - nächster Tick erneut versuchen
                if self.ride_leaders:
                    return  # (Sollte nach dem Zwangs-Settle leer sein; Sicherheit)
            self._drain_since = None
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
        if self.paper.sizes() and self._take_profit_due("__single__", eq - self.cfg.equity):
            self._settle_ride(prices, "tp")
            return
        if self.paper.sizes() and self._plus_lock_due("__single__", eq - self.cfg.equity):
            for coin in list(self.paper.sizes()):
                self._close_coin(coin, prices, "plus_lock")
            self._settle_ride(prices, "plus_lock")
            return
        if eq <= self.cfg.equity * self.cfg.bust_frac:
            self._settle_ride(prices, "bust")
            return
        # 1b. Zeit+negativ-Cut (Nutzer): ein Ritt, der länger als max_ride_hours
        # offen UND aktuell im Minus ist, blockiert nur den einzigen Slot und
        # blutet (Live: 4h im Minus, dann -230$ beim Leader-Exit). Freigeben -
        # Verlust begrenzt, Leader kriegt seinen Strike (kein Exempt). Im Plus
        # NICHT cutten (dann läuft der Ritt weiter Richtung Ziel).
        if (self.cfg.max_ride_hours > 0 and self.paper.sizes()
                and self._ride_start_t is not None
                and eq < self.cfg.equity
                and self.clock() - self._ride_start_t > self.cfg.max_ride_hours * 3600):
            log.warning("Sprint: Ritt >%.1fh im Minus (Equity %.2f) - Zeit-Cut, Slot frei",
                        self.cfg.max_ride_hours, eq)
            for coin in list(self.paper.sizes()):
                self._close_coin(coin, prices, "zeit_negativ")
            self._settle_ride(prices, "zeit_negativ")
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
        # Läuft IMMER (auch während eines Ritts, für die Star-Preemption) -
        # registriert selbst nichts, verarbeitet nur bereits pending
        # Kandidaten (Bestätigungsfenster gegen Flip-Flopper). Derselbe
        # Aufruf läuft im Mess-Modus aus dem parallel_rides-Zweig oben,
        # dort OHNE Star-Preemption (siehe _promote_pending_parallel).
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
            self._note_fresh(len(fresh), snap.address)
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
                if self.cfg.confirm_delay_s <= 0 or self._skip_confirm(snap.address):
                    # Feature AUS - oder Trust=Speed (Edge-Runde): ein Leader
                    # mit bewiesenem Gewinn-Zyklus wartet nicht 10s wie ein
                    # No-Name, er hat den Flip-Flopper-Verdacht widerlegt.
                    if len(self.paper.sizes()) >= self.cfg.max_positions:
                        self._reject(coin, snap.address, "korb_begrenzt")
                        continue
                    self._enter(coin, snap, prices)
                    registered = coin in self.paper.sizes()
                else:
                    # registered NUR True, wenn DIESER Aufruf den Kandidaten
                    # platziert hat. Bei einer Kollision (Coin schon von einem
                    # ANDEREN Leader oder aus einem früheren Tick pending) gibt
                    # _register_pending False zurück -> dieser Leader probiert
                    # seinen NÄCHSTEN Coin, statt sich fälschlich als 'registriert'
                    # zu markieren und den Rest seines Korbs als korb_begrenzt zu
                    # verwerfen (Bug: der Coin verschwand dann lautlos, und der
                    # Rest wurde gegen einen fremden Slot begrenzt). Der kollidierte
                    # Coin wird NICHT verworfen - er ist ja bereits pending, wird
                    # also anderswo bearbeitet.
                    registered = self._register_pending(coin, snap)
            if self.cfg.confirm_delay_s <= 0 and self.paper.sizes():
                return  # altes Verhalten: real eingestiegen -> keine weiteren Leader

    def _register_pending(self, coin: str, snap) -> bool:
        """Bestätigungs-Kandidat statt sofortigem Einstieg (Flip-Flopper-
        Schutz): der Coin-Slot gehört dem ERSTEN Kandidaten, egal welcher
        Leader - eine Kollision (auch desselben Leaders erneut, während er
        schon pending ist) wird schlicht ignoriert, kein Timer-Reset, kein
        Ersatz-Kandidat.

        Rückgabe: True, wenn DIESER Aufruf den Kandidaten platziert hat; False
        bei Kollision (Coin-Slot schon vergeben). Der Aufrufer nutzt das, um
        den Korb dieses Leaders korrekt zu begrenzen - nur NACHDEM er wirklich
        seinen einen Kandidaten platziert hat, nicht schon bei einem fremden
        Slot (das war der lautlose Signal-Verlust-Bug)."""
        if coin in self._pending:
            return False
        self._pending[coin] = {"leader": snap.address, "since": self.clock()}
        return True

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
            if not pos.not_losing(price, self.cfg.confirm_tolerance):
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

        if self.cfg.parallel_rides:
            # Mess-Modus: KEIN "nur einer gewinnt" wie im Einzel-Ritt - hier
            # ist Platz für mehrere parallele Ritte, jeder bestätigte
            # Kandidat bekommt seinen eigenen Slot (falls noch frei).
            self._promote_pending_parallel(ready, prices)
            return

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

    def _promote_pending_parallel(self, ready: list, prices: dict[str, float]) -> None:
        """Mess-Modus-Pendant zur Promotion (Wave-3-Fund 18.07.): anders als im
        Einzel-Ritt gewinnt hier NICHT nur der eine beste Kandidat - es ist
        Platz für mehrere parallele Ritte, jeder bestätigte Kandidat bekommt
        einen eigenen Slot, solange noch einer frei ist. Keine Star-Preemption
        nötig: es gibt keinen EINEN laufenden Ritt, den man verdrängen müsste -
        ein neues Signal nimmt einfach den nächsten freien Platz. Alle
        Kapazitäts-Checks (Leader-Limit/Coin-Kollision/Gesamt-Slots) werden
        HIER erneut geprüft, nicht nur bei der Registrierung - der Zustand kann
        sich während der Wartezeit geändert haben (Leader schon anderweitig
        geritten, Coin belegt, Pool voll)."""
        for coin, info, snap in ready:
            self._pending.pop(coin, None)
            leader = info["leader"]
            if coin in self.paper.sizes():
                self._reject(coin, leader, "coin_belegt")
                continue
            if self._leader_rides(leader) >= self._leader_ride_limit(leader):
                self._reject(coin, leader, "leader_belegt")
                continue
            if len(self.paper.sizes()) >= self.cfg.max_rides:
                self._reject(coin, leader, "max_ritte")
                continue
            self._enter(coin, snap, prices, parallel=True)

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

    def _check_ride_targets(self, prices: dict[str, float]) -> None:
        """Ziel/Bust/Zeit-Cut JE RITT (eigene 1000$-Basis, nicht das
        Sammelbuch) - Tick-Schritt 1, aber ausgelagert, damit sowohl der
        volle _tick_parallel-Durchlauf als auch fast_exit_check() (Fast-
        Path zwischen den Autopilot-Ticks, siehe dort) dieselbe Logik ohne
        Duplizierung nutzen."""
        for coin in list(self.paper.sizes()):
            pnl = self._ride_pnl(coin, prices)
            if pnl is None:
                # Preis fehlt (Spiegel-Fund 24.07.) - nicht raten (siehe
                # _ride_pnl), aber auch nicht für immer blind warten (Audit-
                # Fund, gleiches Prinzip wie beim Mode-Switch-Drain: 'xyz:'-
                # Perps fehlen z.B. am Wochenende dauerhaft in all_mids()).
                # Nach _DRAIN_MAX_AGE_S ohne Preis zum Einstand zwangs-
                # abrechnen, statt den Ritt-Slot für immer zu blockieren.
                since = self._price_missing_since.setdefault(coin, self.clock())
                if self.clock() - since > _DRAIN_MAX_AGE_S:
                    log.warning("Sprint: %s seit %.0fs ohne Preis - "
                                "zwangsabrechnen zum Einstand, Slot frei",
                                coin, _DRAIN_MAX_AGE_S)
                    self._settle_one(coin, prices, "kein_preis")
                continue
            self._price_missing_since.pop(coin, None)
            if self._take_profit_due(coin, pnl):
                self._settle_one(coin, prices, "tp")
            elif self._plus_lock_due(coin, pnl):
                self._settle_one(coin, prices, "plus_lock")
            elif self.cfg.equity + pnl <= self.cfg.equity * self.cfg.bust_frac:
                self._settle_one(coin, prices, "bust")
            # Zeit+negativ-Cut JE RITT (Live-Fund 18.07.: lief bisher nur im
            # Einzel-Ritt-Zweig - ein BTC-Mess-Ritt blutete 3.3h, obwohl
            # max_ride_hours: 2 scharf war). setdefault seedet Alt-Ritte aus
            # der Zeit VOR diesem Fix beim ersten Tick (Uhr startet ab jetzt,
            # kein Sofort-Cut auf Basis einer nie erfassten Startzeit).
            elif (self.cfg.max_ride_hours > 0 and pnl < 0
                  and self.clock() - self._ride_start_ts.setdefault(coin, self.clock())
                      > self.cfg.max_ride_hours * 3600):
                log.warning("Sprint: Mess-Ritt %s >%.1fh im Minus (%.2f) - "
                            "Zeit-Cut, Slot frei", coin, self.cfg.max_ride_hours, pnl)
                self._settle_one(coin, prices, "zeit_negativ")

    def fast_exit_check(self, prices: dict[str, float]) -> None:
        """Schneller Zwischen-Check NUR für Ziel/Plus-Lock/Bust/Zeit-Cut,
        OHNE vollen Leader-Scan - für den Fast-Path zwischen den regulären
        Autopilot-Ticks (autopilot.py, gedrosselt von copytrade.poll_seconds,
        typ. 20s).

        Spiegel-Fund 22.07.: Plus-Lock (Floor 15$) schoss trotzdem bis zu
        -33.57$ ins Minus durch - ein 10x/10-15k$-Ritt kann sich in den 20s
        zwischen zwei vollen Ticks weiter bewegen, als der Floor Puffer
        gibt (klassisches Gap-durch-den-Stop, kein Logik-Fehler: der Peak
        wird korrekt geführt, aber zwischen zwei Preis-SAMPLES ist der Bot
        blind). Mit bereits vorhandenen (kostenlosen) WS-Mids alle paar
        Sekunden nachschauen fängt den Rückfall näher am Floor ab - reiner
        Exit-Reflex, rührt Entries/Sizing/Edge-Logik nicht an, kostet kein
        zusätzliches Netzwerk-Budget (keine eigenen API-Calls hier).

        Nur im Mess-Modus sinnvoll: Einzel-Ritt hat eigenes Timing/eigene
        Baseline-Logik, hier bewusst nicht dupliziert."""
        if not self.cfg.parallel_rides or not prices:
            return
        self._check_ride_targets(prices)

    def _tick_parallel(self, leaders: list[dict], snapshots: list,
                       prices: dict[str, float], risk_off: bool) -> None:
        # 1. Ziel/Bust JE RITT (eigene 1000$-Basis, nicht das Sammelbuch)
        self._check_ride_targets(prices)
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
        # 3. Exit-Folge je Ritt über SEINEN Leader. Counter-Ritte (Toxic Flow)
        #    werden hier KOMPLETT ÜBERSPRUNGEN (Nutzer 20.07., v2: "wenn wir
        #    counter traden macht es keinen Sinn wenn die Position zu macht
        #    weil der Leader beendet"). Journal-Beweis aus 90 min v1: ALLE 9
        #    Counter-Zyklen endeten durch Aktionen des TOXIC-Leaders (2x
        #    leader_exit inkl. -204$ Zwangsschluss mitten im Drawdown, 3x
        #    leader_flip = wir spiegelten den Churn eines Flip-Floppers
        #    invertiert mit, 1x scaleout, 3x rotated) - kein einziger über
        #    unsere eigenen Ziele. Die These 'der Leader liegt falsch' gilt
        #    konsequent: dann ist auch sein EXIT-Timing kein Signal. Eine
        #    Gegenwette ist UNSER Trade und endet NUR über die eigene
        #    Mechanik in Schritt 1: Trail-TP, Plus-Lock, Zeit-Cut, Bust.
        for coin, our_size in list(self.paper.sizes().items()):
            leader = self.ride_leaders.get(coin, "")
            if leader.startswith("counter:"):
                continue
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
                # (das Settle hat den Leader-Slot gerade freigegeben).
                # BAN-CHECK PFLICHT (Spiegel-Fund 20.07.: lighter:726722 wurde
                # bei 2 Strikes gebannt und machte über GENAU diesen Pfad
                # weitere 3 Verlust-Ritte, Strikes 3-5, ~-220$ NACH dem Bann -
                # das Settle hier kann den Bann gerade eben ausgelöst haben,
                # und der Wiedereinstieg lief daran vorbei; der Frisch-Signal-
                # Pfad in Schritt 4 prüft den Bann längst).
                self._settle_one(coin, prices, "leader_flip")
                if (not self._is_toxic(snap.address)
                        and self._leader_rides(snap.address) < self._leader_ride_limit(snap.address)):
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
            if self.cfg.counter_toxic and self._is_toxic(addr):
                continue   # Toxic-Flow-Pass unten übernimmt (Gegenwette statt Reject)
            snap = by_addr.get(addr)
            if snap is None:
                continue
            prev = self._baselines.get(addr)
            if prev is None:
                continue
            fresh = self._fresh_coins(self._book_of(snap), prev)
            if not fresh:
                continue
            self._note_fresh(len(fresh), snap.address)
            if addr in self.banned:
                for coin in fresh:
                    self._reject(coin, snap.address, "leader_gesperrt")
                continue
            if self._is_toxic(addr):
                # counter_toxic ist AUS (sonst wäre oben schon 'continue'
                # gelaufen) - Record-toxisch, aber nicht formell gebannt:
                # kein Gegenwette-Pass verfügbar, also sichtbar verwerfen
                # statt einen bekannt miesen Leader stillschweigend zu reiten.
                for coin in fresh:
                    self._reject(coin, snap.address, "leader_toxisch")
                continue
            fresh.sort(key=lambda c: abs(snap.exposure(c)), reverse=True)
            opened = False
            for coin in fresh:
                if opened:
                    self._reject(coin, snap.address, "korb_begrenzt")
                    continue
                if self._leader_rides(snap.address) >= self._leader_ride_limit(snap.address):
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
                if self.cfg.confirm_delay_s <= 0 or self._skip_confirm(snap.address):
                    # Trust=Speed (Edge-Runde): bewiesene Leader sofort rein -
                    # bei Momentum-Signalen ist früh die halbe Edge.
                    before = len(self.paper.sizes())
                    self._enter(coin, snap, prices, parallel=True)
                    opened = len(self.paper.sizes()) > before
                else:
                    # Bestätigungsfenster (Wave-3-Fund): auch im Mess-Modus
                    # erst registrieren statt sofort reiten - _process_pending
                    # (nach _tick_parallel, s. tick()) promotet zum dann
                    # aktuellen Preis, siehe _promote_pending_parallel.
                    opened = self._register_pending(coin, snap)
        # 5. Toxic Flow: frische Signale GEBANNTER Leader gegenhandeln
        if self.cfg.counter_toxic:
            self._tick_toxic(by_addr, prices)

    def _tick_toxic(self, by_addr: dict, prices: dict[str, float]) -> None:
        """Toxic Flow (Nutzer 20.07.: 'unsere gestrikten Leader werden ab
        sofort counter traded'): gebannte Leader sind BEWIESENE Falsch-Trader
        - ihre frischen Signale werden invertiert geritten statt verworfen
        (Spiegel-Befund: 119x leader_gesperrt = 119 verschenkte Datenpunkte).

        Jede Gegenwette läuft unter der Identität 'counter:<addr>' mit
        EIGENEM Strike-/Record-/Confidence-Konto: verliert die Gegenwette
        wiederholt (= der Leader hatte doch recht), bannt die Strike-Maschine
        die Counter-Identität und der Spuk endet von selbst - dieselbe
        Wahrheitsfindung wie bei jedem anderen Leader. Gewinnt sie, baut die
        Counter-Identität ganz normal Hot-Hand-Status auf.

        V1 bewusst OHNE Bestätigungsfenster (das prüft 'Leader nicht im
        Minus' - für eine Gegenwette wäre die Logik invertiert; bis dahin
        urteilen Strikes). Baselines für Gebannte/Record-Toxische hält
        _refresh_baselines am Leben, solange counter_toxic an ist.

        Toxic-by-Record (Spiegel-Fund 20.07.): scannt toxic_addrs(), nicht
        nur self.banned - eine Amnestie leert Bans, aber NICHT das
        Langzeitgedächtnis (leader_record). Ohne das hätte jede Amnestie
        den Toxic-Pool auf null gesetzt, bis die Strike-Maschine dieselben
        Leader mühsam neu enttarnt."""
        for addr in sorted(self.toxic_addrs()):
            # Gegenwette dauerhaft enttarnt (Spiegel-Fund 23.07., Nutzer:
            # "Signale schwächen mal wieder ab"): 162 von 179 fresh_seen
            # waren counter_gesperrt-Rauschen von GENAU 2 toxischen Lighter-
            # Adressen, deren Gegenwette längst gebannt ist und es (bis zur
            # nächsten Amnestie) bleibt - jeder weitere Scan kann NUR wieder
            # denselben Reject produzieren. Vorher wurde trotzdem jedes Mal
            # Baseline/Fresh-Signal-Arbeit gemacht UND fresh_seen hochgezählt,
            # was die Spiegel-Zahlen (und den Eindruck 'viele Signale, aber
            # keine Trades') komplett verzerrt hat. VOR der teuren Arbeit
            # prüfen und ohne Baseline/Fresh-Scan/Reject-Rauschen überspringen -
            # der Zustand ist in self.banned schon sichtbar, muss nicht bei
            # jedem Tick neu gemeldet werden. Absichtlich NICHT an
            # toxic_addrs()/_STRIKE_EXEMPT gekoppelt (die bleiben unverändert
            # für den Pool-Ausschluss) - reine Scan-Optimierung hier.
            ckey = f"counter:{addr}"
            if ckey.lower() in self.banned:
                continue
            snap = by_addr.get(addr)
            if snap is None:
                continue
            prev = self._baselines.get(addr)
            if prev is None:
                continue   # erster Blick nach dem Bann: erst Baseline legen
            fresh = self._fresh_coins(self._book_of(snap), prev)
            if not fresh:
                continue
            self._note_fresh(len(fresh), snap.address)
            fresh.sort(key=lambda c: abs(snap.exposure(c)), reverse=True)
            opened = False
            for coin in fresh:
                if opened:
                    self._reject(coin, snap.address, "korb_begrenzt")
                    continue
                if self._leader_rides(ckey) >= self._leader_ride_limit(ckey):
                    self._reject(coin, snap.address, "leader_belegt")
                    continue
                if coin in self.paper.sizes():
                    self._reject(coin, snap.address, "coin_belegt")
                    continue
                if len(self.paper.sizes()) >= self.cfg.max_rides:
                    self._reject(coin, snap.address, "max_ritte")
                    continue
                before = len(self.paper.sizes())
                self._enter(coin, snap, prices, parallel=True, counter=True)
                opened = len(self.paper.sizes()) > before

    def _leader_rides(self, addr: str) -> int:
        key = addr.lower()
        return sum(1 for a in self.ride_leaders.values() if a.lower() == key)

    def wins_of(self, addr: str) -> int:
        """Gewinn-Zyklen dieses Leaders im EIGENEN Buch (Edge-Runde)."""
        return self.leader_record.get(addr.lower(), {}).get("won", 0)

    def _is_toxic(self, addr: str) -> bool:
        """Toxic-by-Record (Spiegel-Fund 20.07.: '/quest amnestie' leerte
        `banned` komplett -> Toxic Flow hatte 78min kein Futter mehr, obwohl
        dieselben Leader ihre miese LANGZEIT-Bilanz nie verloren hatten).
        Ein Leader gilt auch dann als toxisch, wenn seine Lebenszeit-Bilanz
        im eigenen Buch (`leader_record`, überlebt Amnestien BEWUSST) um
        mindestens `toxic_record_deficit` negativ ist - unabhängig vom
        aktuellen (amnestierbaren) Strike-Stand. Strikes/Bans sind die
        kurzfristige Justiz, der Record das Langzeitgedächtnis."""
        key = addr.lower()
        if key in self.banned:
            return True
        if self.cfg.toxic_record_deficit <= 0:
            return False
        rec = self.leader_record.get(key)
        if not rec:
            return False
        return rec.get("lost", 0) - rec.get("won", 0) >= self.cfg.toxic_record_deficit

    def toxic_addrs(self) -> set[str]:
        """Alle aktuell toxischen HL-Adressen (Bann ODER Record-Defizit,
        siehe _is_toxic) - OHNE eigene counter:-Identitäten. Für den Pool-
        Aufbau (autopilot.py): ein toxischer Leader wird nie gleichzeitig
        gefolgt UND gekontert."""
        out = {a for a in self.banned if not a.startswith("counter:")}
        if self.cfg.toxic_record_deficit > 0:
            for addr, rec in self.leader_record.items():
                if addr.startswith("counter:"):
                    continue
                if rec.get("lost", 0) - rec.get("won", 0) >= self.cfg.toxic_record_deficit:
                    out.add(addr)
        return out

    def _proven(self, addr: str, min_wins: int) -> bool:
        """Hot-Hand-Kriterium (nachgeschärft, Spiegel-Fund 20.07.): Siege
        allein reichten NICHT - lighter:366058 bekam mit 2 Siegen bei 5+
        Pleiten die volle 1.5x-Size und verlor damit -164$ in EINEM Ritt.
        Heiße Hand heißt jetzt: genug Siege UND positive Sieg-Bilanz
        (mehr gewonnen als verloren), nicht nur 'hat mal gewonnen'."""
        rec = self.leader_record.get(addr.lower(), {})
        won, lost = rec.get("won", 0), rec.get("lost", 0)
        return won >= min_wins and won > lost

    def _leader_ride_limit(self, addr: str) -> int:
        """Slot-Limit je Leader: Basis + Hot-Hand-Bonus für BEWIESENE Leader
        (>= 1 Sieg UND positive Bilanz - Edge-Runde 19.07., nachgeschärft
        20.07.: Kapazität folgt der heißen Hand, nicht dem Ex-Glückstreffer)."""
        base = self.cfg.max_rides_per_leader
        if self._proven(addr, 1):
            return base + self.cfg.hot_hand_extra_rides
        return base

    def _size_mult(self, addr: str) -> float:
        """Notional-Faktor: ab 2 Gewinn-Zyklen UND positiver Bilanz greift
        hot_hand_size_mult - Größe folgt dem Beweisstand, nie umgekehrt
        (niemand wird KLEINER als Basis, Rookies fahren unverändert 1.0x)."""
        if self._proven(addr, 2):
            return self.cfg.hot_hand_size_mult
        return 1.0

    def _skip_confirm(self, addr: str) -> bool:
        """Trust = Speed (Edge-Runde): bewiesene Leader (>= 1 Sieg UND
        positive Bilanz) überspringen das Bestätigungsfenster - das Fenster
        bleibt Anti-Flip-Flopper-Schutz für Unbekannte und Netto-Verlierer."""
        return self.cfg.trusted_skip_confirm and self._proven(addr, 1)

    def _take_profit_due(self, key: str, pnl: float) -> bool:
        """Take-Profit-Entscheidung inkl. Trailing (Edge-Runde 19.07.):

        trail_frac == 0: altes Verhalten - schließen, sobald pnl >= Ziel.
        trail_frac > 0: das Ziel SCHARF-SCHALTET nur den Trail (Peak wird ab
        Einstieg mitgeführt) - geschlossen wird erst, wenn der PnL um
        trail_frac vom Peak zurückfällt. Datenbasis: alle großen Gewinner
        waren Überschießer (+225/+220/+112 bei +100-Ziel), das fixe Ziel
        kappte systematisch den rechten Tail. Bewusster Preis: ein Ritt, der
        das Ziel nur knapp erreicht, gibt bis zu trail_frac davon wieder her -
        der Trail tauscht garantierte +100 gegen die Chance auf +150/+225."""
        peak = max(self._ride_peak.get(key, pnl), pnl)
        self._ride_peak[key] = peak
        if self.cfg.trail_frac <= 0:
            return pnl >= self.cfg.target_profit
        if peak < self.cfg.target_profit:
            return False
        return pnl <= peak * (1 - self.cfg.trail_frac)

    def _plus_lock_due(self, key: str, pnl: float) -> bool:
        """Plus-Sicherung (Nutzer 19.07.: 'sobald wir im Plus sind sollten wir
        nie mit Minus rausgehen' - Live-Muster: Ritte standen ordentlich im
        Plus und wurden Stunden später vom Zeit-Cut oder Leader-Exit im MINUS
        beendet). Hat der Ritt-Peak einmal plus_lock_arm erreicht, wird beim
        Rückfall auf plus_lock_floor geschlossen - klein-grün statt rot.
        Greift nur UNTER dem Trail-Bereich (ab target_profit übernimmt der
        Trailing-TP, dessen Exit-Schwelle weit über dem Floor liegt). Der
        Peak wird von _take_profit_due mitgeführt (läuft im selben Tick davor).
        arm=0 heißt aus."""
        c = self.cfg
        if c.plus_lock_arm <= 0:
            return False
        peak = self._ride_peak.get(key, 0.0)
        return peak >= c.plus_lock_arm and pnl <= c.plus_lock_floor

    def _ride_pnl(self, coin: str, prices: dict[str, float]) -> float | None:
        """PnL eines Mess-Ritts auf seiner eigenen 1000$-Basis, konservativ
        inkl. Eröffnungs- UND (hypothetischer) Schließungs-Fee.

        Spiegel-Fund 24.07. (7 von 14 Plus-Lock-Exits im Tail exakt -9.00$ -
        auffällig identisch statt zufällig verteilt): fehlt der Coin-Preis
        im übergebenen `prices`-Dict, fiel dieser Helfer früher still auf
        den Entry-Preis zurück ('keine Bewegung' angenommen) - das ergibt
        IMMER exakt raw_pnl=0 minus Fee (für 10.000$ Notional exakt -9.00$),
        UNABHÄNGIG vom echten Kurs. Kombiniert mit Plus-Lock (pnl <= floor)
        triggerte das einen FALSCH-POSITIVEN Exit für jeden armed Ritt,
        sobald der Preis auch nur EINEN Tick fehlte - besonders der neue
        fast_exit_check-Pfad (1s-Fenster, WS-Mids können 'xyz:'-Coins/
        Lighter-Coins lückenhaft liefern) traf das viel häufiger als der
        alte volle Tick. None statt Rate-raten: alle Aufrufer behandeln
        None bereits als 'diesen Tick nicht beurteilbar, nächstes Mal mit
        echten Daten neu versuchen' (siehe _check_ride_targets) - derselbe
        'stale ist ehrlicher als falsch' Grundsatz wie beim HL-Tracker."""
        row = next((r for r in self.paper.position_rows(prices)
                    if r["coin"] == coin), None)
        if row is None:
            return None
        px = prices.get(coin)
        if not px:
            return None
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
        self._ride_start_ts.pop(coin, None)
        self._ride_peak.pop(coin, None)
        self._price_missing_since.pop(coin, None)
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
                self._note_fresh(len(fresh), snap.address)
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
            self._note_fresh(len(fresh), snap.address)
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
        # Toxic Flow: Toxische (gebannt ODER Record-Defizit) bleiben
        # BEOBACHTET (Baseline lebt weiter), sonst gäbe es keine frischen
        # Signale zum Gegenhandeln - toxisch nimmt nur den Pool-Slot, nicht
        # die Rolle als Kontra-Indikator.
        if self.cfg.counter_toxic:
            keep |= self.toxic_addrs()
        now = self.clock()
        changed = False
        for s in snapshots:
            key = s.address.lower()
            if key in keep:
                book = self._book_of(s)
                if self._baselines.get(key) != book:
                    self._baselines[key] = book
                    changed = True
                # Idle-Uhr beim POOL-EINTRITT starten (Karenz): ein neu
                # aufgenommener Leader hat ab jetzt rotate_idle_hours Zeit, ein
                # frisches Signal zu geben, bevor er als stumm gilt.
                if key not in self._last_active:
                    self._last_active[key] = now
        for addr in list(self._baselines):
            if addr not in keep:
                self._baselines.pop(addr, None)
                changed = True
        # _last_active NICHT beim Pool-Austritt löschen - eine rotierte Stumme
        # soll stumm bleiben (nicht bei jedem Rebuild frisch geseedet werden und
        # vordrängeln). Nur uralte Einträge kappen, damit der Dict nicht wächst.
        for addr in list(self._last_active):
            if now - self._last_active[addr] > _ACTIVE_MAX_AGE_S:
                self._last_active.pop(addr, None)
        self._persist_baselines(changed)

    def idle_addrs(self, max_idle_s: float) -> set[str]:
        """Pool-Leader, die seit max_idle_s KEIN frisches Signal gaben (Idle-
        Rotation). Ausgenommen: Stars (bewiesene Verdiener - das Confidence-
        System soll sie halten) UND der aktuelle Ride-Leader (den wir GERADE
        reiten - er hält eine Position, gibt aber definitionsgemäß kein frisches
        Signal; ihn rauszurotieren würde den laufenden Ritt blind schließen)."""
        if max_idle_s <= 0:
            return set()
        now = self.clock()
        riding = self.ride_leader.lower()
        return {a for a, t in self._last_active.items()
                if now - t > max_idle_s and a != riding and not self.is_star(a)}

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
                {"t": now, "baselines": self._baselines,
                 "last_active": self._last_active}))
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
            # Idle-Uhren mitladen, damit ein Neustart die Rotation nicht
            # zurücksetzt (sonst bekäme jede Stumme nach jedem Deploy wieder
            # die volle Karenz und würde nie rausrotiert).
            self._last_active = {str(a).lower(): float(t)
                                 for a, t in (raw.get("last_active") or {}).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            pass   # keine/kaputte Datei -> normales Re-Baseline beim ersten Tick (Default bleibt "kein Vorstand")

    def _note_fresh(self, n: int, addr: str | None = None) -> None:
        self._fresh_seen += n
        self._last_fresh_t = self.clock()
        if addr:
            # Dieser Leader hat gerade ein frisches Signal gegeben -> Idle-Uhr
            # zurücksetzen (er ist aktiv, bleibt vorn im Pool).
            self._last_active[addr.lower()] = self.clock()

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

    def _leverage_for(self, coin: str) -> float:
        """Hebel für DIESEN Coin: Override falls in leverage_overrides gelistet
        (Nutzer 17.07.: BTC/ETH liquider, vertragen mehr Hebel), sonst Basis-
        `leverage` - GEKAPPT auf das echte Hyperliquid-Limit für diesen Coin,
        falls bekannt (Live-Fund: PENGU wurde mit einem Hebel eröffnet, den
        die Exchange für diesen Coin gar nicht anbietet - Paper-Zahlen, die
        beim Live-Gang so nicht übernehmbar wären)."""
        lev = self.cfg.leverage_overrides.get(coin, self.cfg.leverage)
        # Für Diagnose (Journal/Status-Spiegel): IMMER festhalten, was der
        # HL-Lookup ergab - nicht nur beim Kappen. Sonst ist aus dem Log/
        # Journal nicht unterscheidbar, ob 'kein Kappen' heißt 'HL erlaubt
        # hier wirklich mehr' oder 'der Lookup ist fehlgeschlagen/None' -
        # genau die Frage, die beim ersten PENGU-Fund offen blieb.
        self._last_hl_max_leverage = (self._max_leverage_fn(coin)
                                      if self._max_leverage_fn is not None else None)
        real_max = self._last_hl_max_leverage
        if real_max is not None and real_max < lev:
            log.info("Sprint: Hebel für %s von x%.0f auf HL-Limit x%d gekappt",
                     coin, lev, real_max)
            return float(real_max)
        return lev

    def _enter(self, coin: str, snap, prices: dict[str, float],
               parallel: bool = False, counter: bool = False) -> None:
        ko = self._ineligible_reason(coin)
        if ko:
            self._reject(coin, snap.address, ko)
            return
        price = prices.get(coin)
        if not price or price <= 0:
            self._reject(coin, snap.address, "kein_hl_preis")
            return
        # FLAT 10x, nur RICHTUNG (Nutzer-Entscheidung): wir spiegeln NICHT die
        # anteilige Allokation des Leaders (die machte uns effektiv 1-5x, die
        # Zahlen bewegten sich schleppend), sondern nehmen nur seine Richtung
        # (Long/Short) und fahren die VOLLE leverage-Größe. Bei +10%-und-raus
        # zählt die Richtung, nicht wie viel Kapital der Leader selbst riskiert.
        # Toxic Flow: counter=True invertiert die Richtung (Gegenwette gegen
        # einen bewiesenen Falsch-Trader) und bucht den Ritt unter der
        # Identität 'counter:<addr>' - eigenes Strike-/Record-Konto.
        direction = 1.0 if snap.exposure(coin) >= 0 else -1.0
        if counter:
            direction = -direction
        ident = f"counter:{snap.address}" if counter else snap.address
        lev = self._leverage_for(coin)
        if parallel:
            # Mess-Modus: JEDER Ritt startet auf frischer equity-Basis (1000$),
            # unabhängig vom Sammelbuch - 1 Signal = 1 Ritt = 1k (Nutzer).
            # Hot-Hand-Sizing (Edge-Runde): ab 2 Gewinn-Zyklen im eigenen Buch
            # skaliert der Einsatz mit (_size_mult) - Größe folgt Beweisstand
            # (für Counter-Ritte: der Beweisstand der COUNTER-Identität).
            notional = direction * lev * self.cfg.equity * self._size_mult(ident)
        else:
            equity = self.paper.equity(prices)
            target = direction * lev * equity
            # Gross-Cap: Gesamtbuch bleibt unter (Coin-)leverage x Equity
            gross = sum(abs(s) * prices.get(c, 0.0) for c, s in self.paper.sizes().items())
            headroom = max(0.0, lev * equity - gross)
            notional = max(-headroom, min(headroom, target))
        if abs(notional) < self.cfg.min_notional:
            self._reject(coin, snap.address, "unter_min_notional")
            return
        self._last_entry_t = self.clock()
        if not parallel and self._ride_start_equity is None:
            self._ride_start_equity = self.paper.equity(prices)  # PnL-Basis des Ritts
            self._ride_start_t = self.clock()  # Ritt-Beginn (für den Zeit+negativ-Cut)
        self.paper.execute(coin, notional / price, price)
        if parallel:
            self.ride_leaders[coin] = ident
            self._ride_start_ts[coin] = self.clock()   # Zeit+negativ-Uhr je Ritt
        else:
            self.ride_leader = ident
        self._ride_entry_sizes[coin] = abs(self._book_of(snap).get(coin, 0.0))
        side = "LONG" if notional > 0 else "SHORT"
        cycle = self.won + self.busted + 1
        log.info("Sprint: %s%s %s $%.0f (frisches Signal von %s, Zyklus %d)",
                 "COUNTER " if counter else "", side, coin, abs(notional),
                 snap.address[:10], cycle)
        if self.journal:
            # hl_max_leverage im Journal sichtbar (Live-Fund PENGU): ohne das
            # ist aus dem Journal/Status-Spiegel nicht unterscheidbar, ob
            # 'voller konfigurierter Hebel gefahren' heißt 'HL erlaubt hier
            # wirklich mehr' oder 'der HL-Lookup lieferte None' (kein Client
            # verdrahtet, Coin unbekannt, Metadaten nicht ladbar).
            # leader=ident: Counter-Ritte laufen unter 'counter:<addr>', damit
            # Kohorten/Scorecard die Gegenwetten als eigene Spur auswerten.
            self.journal.record("sprint_entry", coin=coin, side=side,
                                notional=round(abs(notional), 0),
                                leader=ident, cycle=cycle,
                                leverage=lev, hl_max_leverage=self._last_hl_max_leverage,
                                **({"counter": True} if counter else {}))
        if self.notifier:
            if counter:
                self.notifier.send(
                    f"🔁 <b>Quest-COUNTER</b>: {side} {coin} ${abs(notional):,.0f}\n"
                    f"Gegenwette gegen <code>{snap.address[:10]}…</code> "
                    f"(gebannt) | Zyklus {cycle}")
            else:
                self.notifier.send(f"🟢 <b>Quest-Einstieg</b>: {side} {coin} "
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
            self.notifier.send(f"🔴 <b>Quest-Exit</b> {coin}: {reason_txt}\n"
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

    def close_stock_winners(self, prices: dict[str, float]) -> int:
        """Börsen-Schluss-Gong (Nutzer): profitable AKTIEN-Positionen ('xyz:...')
        zumachen - nach Feierabend ist der Perp-Kurs stale, also Gewinn sichern.
        Verlust-Positionen bleiben offen (die 2h-Regel fängt sie ab, kein Panik-
        Close am Gong). Krypto bleibt UNBERÜHRT (24/7). Reason 'markt_zu' ist
        strike-exempt (unsere Börsen-Regel, nicht Leader-Schuld). Gibt die Anzahl
        geschlossener Positionen zurück."""
        stocks = [c for c in self.paper.sizes() if ":" in c]
        if not stocks:
            return 0
        closed = 0
        if self.cfg.parallel_rides:
            for coin in stocks:
                pnl = self._ride_pnl(coin, prices)
                if pnl is not None and pnl > 0:
                    self._settle_one(coin, prices, "markt_zu")
                    closed += 1
            return closed
        # Einzel-Ritt: hält nur EINE Position - ist sie eine profitable Aktie, zu
        if self.paper.equity(prices) > self.cfg.equity:   # +PnL des Ritts
            for coin in stocks:
                self._close_coin(coin, prices, "markt_zu")
                closed += 1
            if not self.paper.sizes():
                self._settle_ride(prices, "markt_zu")
        return closed

    def _settle_ride(self, prices: dict[str, float], reason: str) -> None:
        """Ritt zu Ende = Zyklus zu Ende (v3): sofort verbuchen (banked/won/busted),
        Konto sofort auf equity zurücksetzen. flatten() ist idempotent - falls schon
        alles per _close_coin zu ist (Normalfall), passiert hier nichts mehr; beim
        TP/Bust-Pfad (noch offene Positionen) schließt es hier alles auf einmal.

        Strikes bleiben ritt-scharf: Verlust -> Strike, Gewinn heilt einen (min 0).
        Ausnahme 'risk_off'/'markt_zu' - erzwungene/externe Schließung, nicht die
        Entscheidung/Schuld des Leaders. 'manual' war früher auch exempt, striked
        seit 17.07. (Nutzer-Entscheidung) aber wie jeder andere Ritt - siehe
        _STRIKE_EXEMPT."""
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
        self._ride_start_t = None
        self._ride_entry_sizes.clear()
        self._ride_peak.pop("__single__", None)
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

        # Leader-Bilanz im eigenen Buch (Edge-Runde): Gewinne zählen immer,
        # Verluste nur, wenn der Grund nicht strike-exempt ist (erzwungene
        # Schließungen wie risk_off sind nicht die Schuld des Leaders -
        # dieselbe Logik wie bei den Strikes direkt darunter).
        if leader:
            rec = self.leader_record.setdefault(leader.lower(), {"won": 0, "lost": 0})
            if won:
                rec["won"] += 1
            elif reason not in _STRIKE_EXEMPT:
                rec["lost"] += 1

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
                            f"{n} Verlust-Ritte in Folge - vom Quest-Bot gesperrt.")
                    # Flip-Flopper-Bestätigung (Nutzer 21.07.: "umgekehrte
                    # Strikes bei einer gebannten Wallet machen keinen Sinn -
                    # 2 Strikes, er ist raus, 2 weitere trotz Counter heißt:
                    # Wallet ist Flip-Flopper"). Verliert die GEGENWETTE
                    # gegen eine bereits gebannte Wallet ebenfalls bis zum
                    # Bann, hat sich gezeigt: weder Folgen NOCH Kontern
                    # funktioniert - kein Richtungs-Trader (ob gut oder
                    # schlecht), sondern reines Rauschen/Churn. Aus dem
                    # Toxic-Watch nehmen (toxic_addrs() prüft dasselbe
                    # Kriterium) statt einen von max 12 Watch-Slots für immer
                    # auf einer erwiesen wertlosen Adresse zu verschwenden.
                    if key.startswith("counter:") and key[len("counter:"):] in self.banned:
                        original = leader[len("counter:"):]
                        log.warning("Sprint: %s bestätigter Flip-Flopper (Original "
                                    "UND Gegenwette gebannt) - nicht mehr beobachtet",
                                    original[:10])
                        if self.journal:
                            self.journal.record("sprint_flip_flopper_confirmed",
                                                leader=original)
                elif self.notifier:
                    self.notifier.send(f"⚠️ Strike {n}/{self.cfg.strike_ban} für "
                                       f"<code>{leader[:10]}…</code> (Verlust-Ritt {pnl:+,.2f}$)")
            elif pnl > 0 and self.strikes.get(key):
                self.strikes[key] = max(0, self.strikes[key] - 1)  # profitabel heilt

        # Nutzer-Fund (17.07.): '2 erfolgreiche Trader gestern, aber ich hab
        # NICHTS von Confidence-Punkten gesehen' - die Vergabe selbst war
        # korrekt (im Journal/confidence-dict nachweisbar), aber komplett
        # STUMM: weder Telegram-Push noch Journal-Eintrag für den normalen
        # +5-Fall, nur beim seltenen Star-Sprung (>=100) gab es überhaupt ein
        # Signal. confidence_line hängt sich jetzt an die ohnehin schon
        # gesendete Zyklus-Ende-Meldung an - kein Extra-Rauschen, aber
        # sichtbar bei GENAU dem Ereignis, das die Punkte auslöst.
        confidence_line = ""
        if leader and reason in _CONFIDENCE_EARNING and pnl > 0:
            key = leader.lower()
            was_star = self.confidence.get(key, 0) >= STAR_THRESHOLD
            self.confidence[key] = self.confidence.get(key, 0) + CONFIDENCE_PER_WIN
            confidence_line = (f"\n+{CONFIDENCE_PER_WIN} Confidence für "
                               f"<code>{leader[:10]}…</code> "
                               f"({self.confidence[key]}/{STAR_THRESHOLD})")
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
        if reason == "plus_lock" and not won:
            reason_txt = _PLUS_LOCK_OVERSHOOT_TXT
        else:
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
                f"{confidence_line}"
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
                # _DISPLAY_TRUNC statt kurz abgeschnitten: 'counter:0x12345678'
                # braucht Platz, sonst sähen alle Gegenwetten im Spiegel
                # identisch aus ('counter:0x')
                p["leader"] = self.ride_leaders.get(p["coin"], "")[:_DISPLAY_TRUNC]
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
            # _DISPLAY_TRUNC statt kurz abgeschnitten (Spiegel-Fund 20.07. +
            # 23.07.): Counter-Identitäten kollidierten in der Anzeige - erst
            # 0x-Adressen ("counter:0x8d7d49eb" vs "counter:0x786515c7" bei
            # 10 Zeichen), dann Lighter-IDs ("counter:lighter:72" für sowohl
            # ...726314 als auch ...726722 bei 18 Zeichen).
            "strikes": {a[:_DISPLAY_TRUNC]: n for a, n in self.strikes.items() if n > 0},
            "banned": sorted({a[:_DISPLAY_TRUNC] for a in self.banned}),
            # Confidence nur für Leader mit Punkten (0 sind uninteressant);
            # stars separat abgeleitet, damit die Anzeige nicht bei jedem
            # Leader neu >= STAR_THRESHOLD rechnen muss.
            "confidence": {a[:_DISPLAY_TRUNC]: n for a, n in self.confidence.items() if n > 0},
            "stars": [a[:_DISPLAY_TRUNC] for a, n in self.confidence.items() if n >= STAR_THRESHOLD],
            # Elite-Umschaltung-Audit (BACKLOG #2, Nutzer 22.07.: "audit ob
            # es Zeit wird für Reservat und Einzel-Trades"): leader_record
            # (won/lost je Identität, amnestie-fest) war bisher NUR intern -
            # ohne SSH-Zugriff war die Kriterien-Prüfung ("≥5 Leader mit ≥3
            # Zyklen bei ≥60% Winrate") aus dem Spiegel nicht möglich. Nur
            # Identitäten mit mindestens einem Zyklus (won+lost > 0) - leere
            # Einträge sind uninteressant und würden nur aufblähen.
            "leader_record": {a[:_DISPLAY_TRUNC]: {"won": r.get("won", 0), "lost": r.get("lost", 0)}
                              for a, r in self.leader_record.items()
                              if r.get("won", 0) + r.get("lost", 0) > 0},
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
            # Beobachtungs-Diagnose (Nutzer-Fund 18.07.: 'seit gestern Abend
            # keine Signale, egal wie der Pool aussieht' - Design-Frage statt
            # Vermutung: hält der Pool schon Positionen (Baseline != leer,
            # nur ein Close+Reopen/Flip/Aufstocken kann noch triggern) oder
            # ist er flach (nur ein frisches 0->Position-Signal triggert
            # überhaupt)? self._baselines IST der zuletzt bekannte Stand -
            # kein Extra-Datenpunkt nötig, nur bisher nicht nach außen sichtbar.
            "watch": {
                "tracked": len(self._baselines),
                "holding_now": sum(1 for b in self._baselines.values() if b),
                "flat_now": sum(1 for b in self._baselines.values() if not b),
                "sample": [{"addr": a[:10], "coins": sorted(b)}
                          for a, b in list(self._baselines.items())[:10] if b],
            },
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
            # Ritt-Startzeit über Neustarts halten, sonst würde ein Deploy die
            # Zeit+negativ-Uhr zurücksetzen und ein Dauer-Bluter nie gecuttet.
            rst = raw.get("ride_start_t")
            self._ride_start_t = float(rst) if rst is not None else None
            # Ritt-Einstiegsgrößen über Neustarts halten (Deep-Dive-Fund): ohne
            # das ist _ride_entry_sizes nach einem Deploy leer, und der Scale-out-
            # Exit (Leader baut >=partial_exit_frac ab) wird für den geerbten Ritt
            # deaktiviert (entry_sz fällt jeden Tick auf die AKTUELLE Leader-Größe
            # zurück -> Bedingung nie erfüllt), der Ritt blutet ggf. weiter.
            self._ride_entry_sizes = {str(c): float(s) for c, s in
                                      (raw.get("ride_entry_sizes") or {}).items()}
            # Zeit+negativ-Uhren der Mess-Ritte über Neustarts halten - sonst
            # bekäme jeder Dauer-Bluter nach jedem Deploy wieder die volle
            # max_ride_hours-Karenz (Alt-States ohne das Feld: leeres Dict,
            # der setdefault im Tick seedet dann ab jetzt).
            self._ride_start_ts = {str(c): float(s) for c, s in
                                   (raw.get("ride_start_ts") or {}).items()}
            self._ride_peak = {str(c): float(s) for c, s in
                               (raw.get("ride_peak") or {}).items()}
            self.leader_record = {str(a): {"won": int((r or {}).get("won", 0)),
                                           "lost": int((r or {}).get("lost", 0))}
                                  for a, r in (raw.get("leader_record") or {}).items()}
            # Ausstehender Bilanz-Reset über Neustarts halten (Deep-Dive-Fund):
            # ohne das geht der Reset verloren, wenn ein Neustart mitten in den
            # Mode-Switch-Drain fällt (der Drain hat parallel_rides=false schon
            # auf Platte geschrieben, aber das Flag lag nur im RAM -> das Gate
            # unten triggert nach dem Neustart nicht mehr, die Alt-Ritt-PnL
            # verseucht die frische Schatztruhe dauerhaft).
            self._bilanz_reset_pending = bool(raw.get("bilanz_reset_pending", False))
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
                # Erstmaliger Umstieg (State-Datei noch mit parallel_rides=True).
                self._bilanz_reset_pending = True
            # Ausstehender Reset (frisch erkannt ODER aus dem State geladen): ist
            # nichts (mehr) zu drainen, JETZT anwenden; sonst wartet tick() bis
            # der Drain durch ist. Deckt auch den Neustart-mitten-im-Drain-Fall ab
            # (parallel_rides schon false auf Platte, aber Flag persistiert True).
            if self._bilanz_reset_pending and not self.ride_leaders:
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

    def amnesty(self) -> tuple[int, int]:
        """Strike-Amnestie (Nutzer 19.07.: 'Resete alle strikes wir sammeln
        nochmal frisch Daten mit trail'): Strikes UND Bans (Bans sind nur die
        Konsequenz von 2 Strikes) auf null - die alten Urteile entstanden
        unter dem alten Exit-Regime (fixes TP, kein Trail, keine Plus-
        Sicherung): Ritte standen im Plus und wurden trotzdem im Minus
        beendet, der Strike traf den Leader für UNSER Exit-Timing.
        Confidence und leader_record bleiben - positive Beweise verfallen
        nicht durch einen Regelwechsel. Gibt (gelöschte Strikes, gelöschte
        Bans) zurück."""
        n_strikes = sum(1 for v in self.strikes.values() if v > 0)
        n_bans = len(self.banned)
        self.strikes.clear()
        self.banned.clear()
        log.warning("Sprint: AMNESTIE - %d Strike-Konten und %d Bans gelöscht "
                    "(frische Datensammlung unter Trail-Regeln)", n_strikes, n_bans)
        if self.journal:
            self.journal.record("sprint_amnesty", strikes=n_strikes, bans=n_bans)
        self._save_state()
        return n_strikes, n_bans

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
                "ride_leaders": self.ride_leaders, "ride_start_t": self._ride_start_t,
                "ride_entry_sizes": self._ride_entry_sizes,
                "ride_start_ts": self._ride_start_ts,
                "ride_peak": self._ride_peak,
                "leader_record": self.leader_record,
                "bilanz_reset_pending": self._bilanz_reset_pending,
                "strikes": self.strikes, "banned": sorted(self.banned),
                "confidence": self.confidence,
                "parallel_rides": self.cfg.parallel_rides,
            }))
        except OSError:
            log.exception("sprint_cycles.json nicht schreibbar")

    def is_star(self, addr: str) -> bool:
        return self.confidence.get(addr.lower(), 0) >= STAR_THRESHOLD

    def confidence_of(self, addr: str) -> int:
        """Rohe Confidence-Punktzahl (Nutzer 17.07.: bisher nur als binäres
        ⭐ ab STAR_THRESHOLD sichtbar, der Fortschritt davor komplett blind)."""
        return self.confidence.get(addr.lower(), 0)

    @staticmethod
    def _book_of(snap) -> dict[str, float]:
        return {c: float(p.size) for c, p in snap.positions.items() if float(p.size) != 0.0}
