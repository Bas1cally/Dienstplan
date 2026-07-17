# Backlog — geplante Features (noch nicht gebaut)

Dieses Dokument hält Feature-Ideen fest, die über Session-Grenzen hinweg
überleben müssen. Die nächste Session klont das Repo frisch — was hier steht,
ist da; Chat-Verlauf ist es nicht.

> **AKTUELLER FOKUS (Nutzer, 13.07.2026): das Sprint-Buch.** Stand: 3/3
> Gewinn-Zyklen unter v3. Die Mess-Woche (bis So 19.07. 16:30) läuft mit
> CMM-Discovery + 12er-Pool. Danach hat der Sprint-Ausbau Vorrang — zuerst
> das Confidence-Points/Star-System (Spec unten), dann ggf. Lighter-Promotion
> in den Sprint-Pool, je nach Lighter-Schatten-Zahlen.
>
> **UPDATE 15.07. — MESS-MODUS aktiv:** `sprint.parallel_rides: true`
> (config.yaml) lässt für die Mess-Woche JEDES Signal als eigenen Ritt auf
> 1000$-Basis parallel laufen (Trader-Bewertung je Ritt, nichts blockiert).
> Krypto-only bleibt in beiden Modi (Aktien-Perps stören die Messlatte).
>
> **UPDATE 17.07. — ZURÜCK IN DEN MESS-MODUS (zweite Datenflut).**
> `sprint.parallel_rides: true`, `max_rides: 8 -> 20` (config.yaml). Nach den
> heutigen Änderungen (Gambler-/Drawdown-Filter fürs Sprint-Gate, eigene
> lockere `sprint_min_active_days`, gesenkte Größen-Böden gegen den Big-Dog-
> Bias/für Shrimps mit hoher Trefferquote, Hebel-Kappung auf HLs echtes
> Pro-Coin-Limit, manuelle Verlust-Closes striken jetzt) soll erstmal wieder
> BREIT gemessen werden - viele Wallets gleichzeitig sammeln Strikes/
> Confidence unter den NEUEN Regeln, statt seriell einen nach dem anderen
> durch den einzigen Einzel-Ritt-Slot zu schleusen. `max_rides_per_leader:
> 1` bleibt unverändert (verhindert weiter, dass ein korrelierter Mehrfach-
> Basket EINES Leaders wie mehrere unabhängige Messpunkte aussieht - keine
> Kapazitätsbremse, sondern Methodik-Schutz). Die Mode-Switch-Sicherung
> (unten, 15.07.) greift nur beim Umschalten AUF Einzel-Ritt mit noch
> offenen Mess-Ritten - beim Umschalten IN den Mess-Modus war das Buch flach
> (keine offene Position), kein Sonderfall nötig. **ENDZIEL bleibt weiter
> `parallel_rides: false`** (POOL -> EIN GUTER RITT -> +10% -> RAUS), aber
> erst nach dieser zweiten Mess-Runde wieder scharf schalten.
>
> **UPDATE 15.07. (QOL-Runde) — ENDZIEL umgesetzt: zurück auf Einzel-Ritt.**
> `sprint.parallel_rides: false` + `confirm_delay_s: 10` + `crypto_only: true`
> (config.yaml). „POOL -> EIN GUTER RITT -> +10% -> RAUS, eine Position" läuft
> wieder, jetzt mit den in der Mess-Woche gesammelten Strike-/Winrate-Daten
> UND drei neuen Sicherungen: (1) Bestätigungsfenster gegen Flip-Flopper (ein
> frisches Signal wird erst geritten, nachdem die Position des LEADERS 10s
> durchgehend nicht im Minus war — nicht mehr blind instant rein), (2) das
> komplette Confidence-Points/Star-System (siehe unten, jetzt UMGESETZT statt
> geplant), (3) eine Mode-Switch-Sicherung, die beim Umschalten zurück auf
> Einzel-Ritt noch offene Mess-Ritte sauber je einzeln abrechnet statt sie
> fälschlich zu einem Zyklus zu verschmelzen. `/sprint assets` beantwortet die
> Krypto-vs-Aktien-Frage der Mess-Woche direkt per Telegram. Die Bank heißt in
> der Anzeige jetzt „Schatztruhe" (intern weiter `banked` — reine Anzeigefrage).
>
> **UPDATE 15.07. — Bilanz-Reset beim Umstieg (Nutzer-Nachfrage):** Zyklus-
> Zähler (`won`/`busted`) und Schatztruhe (`banked`, `total_trades`) setzen
> sich beim ERSTEN Laden unter `parallel_rides: false` automatisch auf 0/1
> zurück — die Mess-Woche-Bilanz (+1.370,63 $ über 50 Zyklen unter anderen
> Regeln: parallele Ritte, kein Bestätigungsfenster) würde sonst die künftige
> Einzel-Ritt-Messung verfälschen. „Die echte Bot-Messwoche" beginnt damit
> sauber bei Zyklus 1. Strikes/Bans/Confidence bleiben unangetastet — das ist
> erprobtes LARP-Wissen, keine Mess-Modus-spezifische Zahl. Läuft automatisch
> beim Deploy, kein manueller Schritt nötig (`_migrate_v1_or_load` in
> `bot/sprint.py`, erkennt den Umstieg am persistierten `parallel_rides`-Feld
> des letzten Saves bzw. dessen Fehlen bei alten State-Dateien).
>
> **UPDATE 15.07. — `crypto_only: false` dauerhaft, nicht mehr nur für die
> Mess-Woche.** Ursprünglich war `crypto_only: false` (14.07., Commit
> `dee239e`) ausdrücklich befristet: "NACH der Mess-Woche wieder true". Beim
> Config-Flip auf Einzel-Ritt wurde entsprechend zurückgesetzt. Nutzer-
> Nachfrage + explizite Entscheidung: Aktien-Perps (Builder-DEX, `xyz:...`)
> bleiben DAUERHAFT erlaubt, kein Rückfall auf Krypto-only. `sprint.
> crypto_only: false` in config.yaml wieder gesetzt (permanent, nicht mehr
> Mess-Woche-befristet).
>
> **UPDATE 15.07. — Ban räumt jetzt den Pool-Slot frei (Nutzer-Kernbefund).**
> Symptom: `/sprint` zeigte kaum noch Signale, `verworfen: leader_gesperrt`.
> Ursache: ein Sprint-Ban hat den LARP bisher nur STUMM geschaltet (Signale
> verworfen), aber seinen Pool-Slot NIE freigegeben - `_build_sprint_pool`
> filterte gebannte Adressen nicht raus. Nach der Mess-Woche waren 15/21
> Pool-Leader belastet (5 gebannt, 10 mit Strikes, alle unter dem harten
> Parallel-Regime OHNE Bestätigungsfenster verdient), die aktivsten
> Signalquellen also tot im Pool, ohne Ersatz. Nutzer: „das ist der Sinn der
> Strikes - LARPs raus, neue Wallets rein". Fix (`bot/autopilot.py`):
> `_build_sprint_pool` schließt gebannte Adressen aus (auch Haupt-Leader -
> der Ban ist das speziellere Urteil), der nächstbeste frische Kandidat rückt
> nach; `_prune_banned_from_pool` wirft beim Start die Gebannten aus dem
> geladenen Pool und fordert eine frische Analyse an (neue Wallets füllen die
> Slots). Selbstlimitierend: forced nur, solange der geladene Pool noch
> Gebannte enthält - nach dem ersten sauberen Rebuild nicht mehr. Bans/
> Strikes bleiben (legitime LARP-Enttarnung), müssen NICHT gelöscht werden -
> der Fix lässt sie nur ihren zweiten Job (Ersetzen) endlich zu Ende machen.
>
> **UPDATE 15.07. — Voller Effort auf Sprint, Beobachter abgeschaltet.**
> 33-Tage-`/fullreport` zeigte: Haupt-Buch −4,94 % (Veto-Outcome nicht
> signifikant), Anomalie-/Orderbuch-Scout nicht signifikant, Strategie-Labor
> negativ/nicht signifikant, Lighter-Schatten −8,22 %, alle Shadow-Varianten
> schlechter als das rote Haupt-Buch. **Sprint-Buch: 26✅/23💥, banked
> +1.370,63 $ über 50 Zyklen — einzige Spur mit echtem realisiertem Gewinn.**
> Nutzer-Entscheidung: „nur Beobachter abschalten" (nicht das Haupt-Buch,
> weil Sprints eigener Pool von derselben `_reanalyze`-Discovery-Pipeline
> gebaut wird wie die Haupt-Leader). Per config.yaml deaktiviert:
> `anomaly/orderbook/twap/labs/lighter.enabled: false`,
> `autopilot.shadow_variants: false`. Bleiben an: Haupt-Buch + Copier/
> Validator/MarketGuard (Sprint braucht deren Preise/Snapshots/RISK_OFF),
> die Leader-Analyse (`_reanalyze`, füttert Haupt- UND Sprint-Pool), CMM-
> Discovery, Sprint selbst. Kein Code gelöscht — reversibel per Config,
> falls eine Spur später wieder Entwicklungsfokus verdient.
>
> Bekannter Report-Anzeige-Fehler (noch NICHT gefixt, nur dokumentiert; seit
> dem Umschalten auf `parallel_rides: false` nicht mehr im Normalbetrieb
> sichtbar, betrifft nur eine künftige erneute Mess-Phase):
> `report.py`s Sprint-Block liest `sprint_book.json`s rohes
> `realized_pnl`/`trades` als „Zyklus N läuft: Equity X" — das ist unter
> Einzel-Ritt korrekt (Buch wird zwischen Zyklen resettet), war aber unter
> `parallel_rides: true` FALSCH: `_settle_one()` ruft nie `paper.reset()`
> auf, die Zahl ist die kumulierte Realisierung seit dem letzten echten
> Reset, nicht „der aktuelle Zyklus". Die verlässliche Zahl bleibt
> `st['banked']` (ritt-scharf über `_book_cycle` berechnet, unabhängig vom
> Reset-Verhalten). Fix: `report.py`s Sprint-Sektion um einen Parallel-Pfad
> erweitern, der `sprint_cycles.json`s `won/busted/banked` zeigt statt der
> Einzel-Ritt-Equity-Annahme — niedrige Priorität, erst wenn `parallel_rides`
> wieder für eine Messung aktiviert wird.

---

## Confidence Points / interner LARP-Star-Rang (Sprint-basiert)

**Status:** UMGESETZT (QOL-Runde, 15.07.2026). Läuft im Sprint-Buch.

**Auftrag des Nutzers (wörtlich sinngemäß):**
Wenn ein Leader einen Sprint erfolgreich beendet und banked, bekommt er
Confidence Points und steigt in einem internen LARP-Ranking auf — ein
*weiterer Filter für den gesamten Code*. Confidence Points müssen mindestens
**100** erreichen, bevor der Leader als **Star** markiert wird. **5 Punkte pro
erfolgreichem Sprint** (→ mind. 20 erfolgreiche Sprint-Ritte für den Stern).
Die **Strike-Regeln bleiben rigoros, selbst wenn er ein Star ist**. Ziel: keine
Zufälle und „lucky dudes die larpen" — Star = wiederholt bewiesener Erfolg,
nicht Glück.

### Kern-Spec (wie tatsächlich gebaut)
- **Trigger — verschärft gegenüber dem ursprünglichen "PnL > 0" (Nutzer-
  Entscheidung, QOL-Runde):** NICHT jeder Gewinn-Ritt verdient Confidence,
  sondern nur `reason == "tp"` (das eigene, durchgehaltene +10%-Ziel) oder
  `reason == "star_preempt"` (Preemption-Close, siehe unten — das BACKLOG
  verspricht dem verdrängten Leader hier explizit +5). Ein leader-getriebener
  Gewinn-Exit (`leader_exit`/`leader_flip`/`leader_scaleout`/`leader_rotated`,
  Leader steigt zufällig im Plus aus) bleibt Bilanz-Gewinn und heilt weiter
  einen Strike, ist aber confidence-neutral — nur echter, durchgehaltener
  Richtungs-Skill beweist sich. Konstante `_CONFIDENCE_EARNING = {"tp",
  "star_preempt"}` in `bot/sprint.py`.
- **Belohnung:** +5 Confidence Points für genau diesen Ride-Leader.
- **Schwelle:** ≥ 100 Punkte → Leader wird als **Star** markiert
  (= mind. 20 erfolgreiche Sprint-Ritte).
- **Zweck:** interner LARP-Rang als **zusätzlicher, codeweiter Qualitätsfilter**
  (nicht nur Sprint). Star-Status ist ein *verdientes*, bewährtes Signal. Der
  codeweite Teil (Star-Bonus im Haupt-Buch-Leaderboard/`rotate_leaders`)
  bleibt bewusst draußen (Nutzer-Entscheidung, QOL-Runde: nur Sprint-intern
  in dieser Runde) — siehe „Offene Fragen" unten.
- **Strikes bleiben rigoros — auch für Stars:** Ein Star ist NICHT strike-immun.
  Verlust-Ritte striken weiter, Bans (2 Strikes) greifen weiter. Der Stern
  schützt nicht vor Konsequenzen; er ist reine Auszeichnung fürs bewiesene
  Können.

### Star-Verhalten (Nutzer-Entscheidungen, 12.07.2026)
- **Verlust-Ritt = einfach ein Strike.** Confidence Points werden dadurch NICHT
  reduziert — der Verlust lebt allein in den Strikes (kurzfristige Konsequenz),
  Confidence ist der langfristige Ruf und wächst nur durch Gewinn-Ritte (+5).
  Wir wollen gar keine Verlust-Ritte; passiert einer, ist er schlicht ein Strike.
- **Star-Vorrang bei gleichzeitigen Signalen:** Signalisieren zwei Leader zur
  selben Zeit, hat der Star Vorrang — vor dem reinen Score-Tie-Break in
  `_tick_waiting`. Top-Star-Trader wird bevorzugt genommen.
- **Star-Preemption eines laufenden Ritts:** Läuft gerade ein Ritt und ist er
  **im Profit**, und ein (Super-)Star gibt ein frisches Signal → aktuellen Ritt
  schließen (Gewinn sofort banked, reason `star_preempt` — zählt bewusst als
  eigener Reason-Code statt `leader_exit`, damit er trotz der „nur tp"-
  Verschärfung oben +5 Confidence für den bisherigen Leader auslöst, wie hier
  versprochen — Strike-Heilung sowieso) und auf das Star-Signal wechseln. Ist
  der laufende Ritt **nicht im Profit** → kein Wechsel (keinen Verlust
  realisieren, um einem Star hinterherzujagen; der Ritt endet normal über
  seine eigenen Exit-/Strike-Regeln).
  - **UMGESETZT (`_scan_star_preemption`/`_process_pending` in `bot/sprint.py`):**
    läuft über denselben Bestätigungsfenster-Mechanismus wie frische Flach-
    Scan-Signale (10s, `_pending`) — nur Stars dürfen während eines laufenden
    Ritts einen Kandidaten registrieren. Profitabilität UND „läuft der Ritt
    noch" werden zum Bestätigungszeitpunkt neu geprüft, nicht zur Entdeckung
    (der Kurs kann in den 10s kippen) — endet der alte Ritt währenddessen von
    selbst oder rutscht ins Minus, verfällt die Preemption stillschweigend und
    der Star-Kandidat wird zum normalen Fresh-Entry, sobald wieder flach.

Restliche Feinheiten: „fuchsen wir aus, wenn es soweit ist" (Nutzer).

---

## CMM/HyperTracker-Ausbau (nach der Discovery-Reform)

**Status:** Discovery über das perp-pnl-Board ist LIVE (13.07.2026). Die API
kann deutlich mehr — Spec: github.com/Coin-Market-Man/hypertracker-skills
(SKILL.md). Budget beachten: Free-Tier = 100 Requests/TAG.

Kandidaten für später, nach Nutzen sortiert:
1. **Tiefen-Scoring via `GET /closed-trades/summary?address=…`** — echte
   Winrate (wins/losses/avgDuration) je Wallet als Zusatz-Signal im Analyzer-
   Score. Budget: nur für die Top-K (~15) je Analyse = ~60 Req/Tag.
2. **Smart-Money-Kohorten via `GET /wallets?segmentIds=8,9,10&hasOpenPositions=true`**
   — CMMs eigene PnL-Kohorten (8=Money Printer $1M+, 9=Smart Money $100K-$1M,
   10=Consistent Grinder) als alternative/zusätzliche Kandidatenquelle; 1 Request.
3. **`GET /positions?address=…`** — Positions-Historie mit PnL je Trade (ab
   April 2025) — könnte langfristig unsere eigene Fills-Rekonstruktion ersetzen.
4. **Cohort-Bias / Heatmap** (`/positions/heatmap`, `/segments/{id}/bias-history`)
   — Markt-Sentiment nach Kohorte (was machen die Smart-Money-Wallets NETTO?)
   als MarketGuard-/Konvergenz-Input.

### Offene Fragen — Stand nach der QOL-Runde
1. ~~Persistenz~~ **ERLEDIGT:** `confidence: {addr: n}` liegt neben `strikes`/
   `banned` in `runtime/sprint_cycles.json`, `stars` wird abgeleitet
   (`>= STAR_THRESHOLD`), keine eigene Liste — vermeidet Sync-Bugs.
2. ~~Punkt-Dynamik bei Verlust~~ **ENTSCHIEDEN:** Verlust-Ritt = nur Strike,
   keine Confidence-Reduktion. Bann friert Confidence implizit ein (gebannte
   Leader werden vor `_enter` abgewiesen, können also nie mehr Punkte
   verdienen — kein Sonderfall-Code nötig).
3. **Noch offen — der *breitere* codeweite Filter:** Sprint-interner Teil
   **ERLEDIGT** (Star-Vorrang bei gleichzeitigen Signalen + Preemption eines
   profitablen Ritts). Weiterhin offen und bewusst NICHT Teil der QOL-Runde
   (Nutzer-Entscheidung: „nur Sprint-intern jetzt"): ein Star-Bonus im
   Haupt-Buch-Leaderboard-Score (`rotate_leaders`)? Voraussetzung/Boost für
   den Sprung auf echtes Kapital? Kernidee klar (Star = codeweites
   Qualitätssignal), konkrete Einhängepunkte für später offen.
4. ~~Sichtbarkeit~~ **ERLEDIGT:** `/sprint` und `/sprint pool` zeigen ⭐ bei
   `confidence >= 100`; Journal-Kind `sprint_star` + Telegram-Push beim
   Erreichen der 100.
5. **Weiterhin offen — Quellen-Reichweite:** Gilt Confidence nur für HL-
   Sprint-Ritte, oder auch für später in den Sprint-Pool promotete Lighter-
   Trader? (Konsequent wäre: jede Quelle kann Sterne verdienen, gleiche
   Regeln.) Aktuell nur HL-Sprint, da Lighter-Promotion selbst noch nicht
   gebaut ist.

### Betroffene Dateien (umgesetzt in der QOL-Runde, 15.07.2026)
- `bot/sprint.py` — Confidence-Vergabe in `_book_cycle` (`_CONFIDENCE_EARNING
  = {"tp", "star_preempt"}`), Star-Schwelle (`is_star`), Persistenz
  (`_save_state`/`_migrate_v1_or_load`), `_pending`/`_process_pending`/
  `_scan_star_preemption` fürs Bestätigungsfenster + die Preemption,
  `stats()`-Ausgabe (`confidence`, `stars`, `leader_is_star`, `pending`).
- `runtime/sprint_cycles.json` — neues Feld `confidence` (persistiert).
- `bot/copytrade/tracker.py` — `LeaderPosition.not_losing(price)`-Helfer
  fürs Bestätigungsfenster.
- `bot/config.py` — `SprintConfig.confirm_delay_s`.
- `bot/autopilot.py` — `/sprint`-Anzeige (⭐-Marker, Bestätigungs-Kandidaten,
  „Schatztruhe"), `/sprint assets` (Krypto-vs-Aktien-Auswertung).
- `tests/test_sprint.py` — +5 nur bei `tp`/`star_preempt`, Star ab 100,
  Strikes treffen auch Stars, Persistenz über Neustart, Bestätigungsfenster
  (inkl. Flip-Re-Entry-Regression), volle Preemption-Testreihe.
