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
> **ENDZIEL danach (Nutzer wörtlich): „POOL -> EIN GUTER RITT -> 10% -> RAUS,
> eine Position"** — nach der Mess-Woche `parallel_rides: false` setzen und
> mit den gesammelten Strike-/Winrate-Daten den EINEN guten Ritt fahren.
> Krypto-only bleibt in beiden Modi (Aktien-Perps stören die Messlatte).
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
> Bekannter Report-Anzeige-Fehler (noch NICHT gefixt, nur dokumentiert):
> `report.py`s Sprint-Block liest `sprint_book.json`s rohes
> `realized_pnl`/`trades` als „Zyklus N läuft: Equity X" — das war unter
> v3-Einzel-Ritt korrekt (Buch wird zwischen Zyklen resettet), ist aber unter
> `parallel_rides: true` FALSCH: `_settle_one()` ruft nie `paper.reset()`
> auf, die Zahl ist die kumulierte Realisierung seit dem letzten echten
> Reset, nicht „der aktuelle Zyklus". Die verlässliche Zahl bleibt
> `st['banked']` (ritt-scharf über `_book_cycle` berechnet, unabhängig vom
> Reset-Verhalten). Fix: `report.py`s Sprint-Sektion um einen Parallel-Pfad
> erweitern, der `sprint_cycles.json`s `won/busted/banked` zeigt statt der
> Einzel-Ritt-Equity-Annahme — aufheben bis die Mess-Woche vorbei ist, dann
> zusammen mit dem Confidence-Points/Star-System angehen.

---

## Confidence Points / interner LARP-Star-Rang (Sprint-basiert)

**Status:** geplant, NICHT bauen während der laufenden Mess-Woche
(Start So 12.07.2026 16:30, Ende So 19.07.2026 16:30). Ändert Sprint-/Scoring-
Logik → würde die laufende Messung verfälschen. Erst danach umsetzen.

**Auftrag des Nutzers (wörtlich sinngemäß):**
Wenn ein Leader einen Sprint erfolgreich beendet und banked, bekommt er
Confidence Points und steigt in einem internen LARP-Ranking auf — ein
*weiterer Filter für den gesamten Code*. Confidence Points müssen mindestens
**100** erreichen, bevor der Leader als **Star** markiert wird. **5 Punkte pro
erfolgreichem Sprint** (→ mind. 20 erfolgreiche Sprint-Ritte für den Stern).
Die **Strike-Regeln bleiben rigoros, selbst wenn er ein Star ist**. Ziel: keine
Zufälle und „lucky dudes die larpen" — Star = wiederholt bewiesener Erfolg,
nicht Glück.

### Kern-Spec
- **Trigger:** Ein Leader beendet einen Sprint-Ritt erfolgreich und banked
  (Gewinn-Ritt, PnL > 0, als Zyklus verbucht in `_settle_ride`).
- **Belohnung:** +5 Confidence Points für genau diesen Ride-Leader.
- **Schwelle:** ≥ 100 Punkte → Leader wird als **Star** markiert
  (= mind. 20 erfolgreiche Sprint-Ritte).
- **Zweck:** interner LARP-Rang als **zusätzlicher, codeweiter Qualitätsfilter**
  (nicht nur Sprint). Star-Status ist ein *verdientes*, bewährtes Signal.
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
  schließen (Gewinn sofort banked, zählt als normaler Gewinn-Ritt für den
  bisherigen Leader: +5 Confidence, Strike-Heilung) und auf das Star-Signal
  wechseln. Ist der laufende Ritt **nicht im Profit** → kein Wechsel (keinen
  Verlust realisieren, um einem Star hinterherzujagen; der Ritt endet normal
  über seine eigenen Exit-/Strike-Regeln).
  - **Interaktion beachten:** Das ist eine bewusste Ausnahme zur bisherigen
    „kein Mid-Ride-Leaderwechsel"-Regel (v2/v3). Beim Bau sauber mit
    `_tick_riding` versöhnen — bisher wird nur auf Leader-Exit/-Flip/-Scaleout/
    -Rotation, TP/Bust, RISK_OFF und manuellen Close geschlossen.

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

### Offene Fragen für die Umsetzung (in der Bau-Session klären)
1. **Persistenz:** Confidence Points neben den Strikes in
   `runtime/sprint_cycles.json` ablegen (dort liegen `strikes`/`banned` schon),
   oder eigenes Konto? Vermutlich dazu, Key `confidence: {addr: n}` + abgeleitete
   `stars: [addr]`.
2. ~~Punkt-Dynamik bei Verlust~~ **ENTSCHIEDEN:** Verlust-Ritt = nur Strike,
   keine Confidence-Reduktion (siehe „Star-Verhalten" oben). Offen bleibt nur:
   Was passiert mit Confidence bei einem Ban? (einfrieren vs. zurücksetzen)
3. **Wo greift der „weitere Filter für den gesamten Code"?** Sprint-Teil
   **ENTSCHIEDEN** (Vorrang bei gleichzeitigen Signalen + Preemption eines
   profitablen Ritts, siehe oben). Noch offen: der *breitere* codeweite Filter —
   Star-Bonus im HL-Leaderboard-Score (`rotate_leaders`)? Voraussetzung/Boost
   für den Sprung auf echtes Kapital? Kernidee klar (Star = codeweites
   Qualitätssignal), konkrete Einhängepunkte später festlegen.
4. **Sichtbarkeit:** `/sprint` bzw. `/leaders` um Confidence/Star-Marker (z.B.
   ⭐) erweitern; Journal-Kind `sprint_star` beim Erreichen der 100.
5. **Quellen-Reichweite:** Gilt Confidence nur für HL-Sprint-Ritte, oder auch
   für später in den Sprint-Pool promotete Lighter-Trader? (Konsequent wäre:
   jede Quelle kann Sterne verdienen, gleiche Regeln.)

### Betroffene Dateien (voraussichtlich)
- `bot/sprint.py` — Confidence-Vergabe in `_settle_ride` (Gewinn-Ritt), Star-
  Schwelle, Persistenz, `stats()`-Ausgabe.
- `runtime/sprint_cycles.json` — neues Feld `confidence` / `stars`.
- `bot/autopilot.py` — `/sprint`- bzw. `/leaders`-Anzeige; ggf. Star-Bonus in
  `rotate_leaders` / `_build_sprint_pool` (der codeweite Filter).
- `tests/test_sprint.py` — +5 pro Gewinn-Ritt, Star ab 100, Strikes treffen auch
  Stars, Persistenz über Neustart.
