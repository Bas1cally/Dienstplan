# Backlog — geplante Features (noch nicht gebaut)

Dieses Dokument hält Feature-Ideen fest, die über Session-Grenzen hinweg
überleben müssen. Die nächste Session klont das Repo frisch — was hier steht,
ist da; Chat-Verlauf ist es nicht.

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
