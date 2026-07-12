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

### Offene Fragen für die Umsetzung (in der Bau-Session klären)
1. **Persistenz:** Confidence Points neben den Strikes in
   `runtime/sprint_cycles.json` ablegen (dort liegen `strikes`/`banned` schon),
   oder eigenes Konto? Vermutlich dazu, Key `confidence: {addr: n}` + abgeleitete
   `stars: [addr]`.
2. **Punkt-Dynamik bei Verlust:** Der Nutzer nennt nur den Zuwachs (+5 bei
   Gewinn). Offen: Reduziert ein Verlust-Ritt die Confidence Points, oder bleibt
   der Verlust allein in den Strikes? (Vorschlag: Strikes = kurzfristige
   Konsequenz, Confidence = langfristiger Ruf; Verlust strikt, aber Confidence
   sinkt evtl. leicht — mit Nutzer abstimmen.) Ein Ban könnte Confidence
   einfrieren/zurücksetzen — klären.
3. **Wo genau greift der „weitere Filter für den gesamten Code"?** Das ist der
   breiteste Punkt. Kandidaten: Star-Bonus im HL-Leaderboard-Score
   (`rotate_leaders`), Bevorzugung im Sprint-Pool, Voraussetzung/Boost für den
   Sprung auf echtes Kapital. Kernidee klar (Star = codeweites Qualitätssignal),
   konkrete Einhängepunkte mit Nutzer festlegen.
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
