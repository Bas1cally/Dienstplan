# Backlog — Quest-Bot

Dieses Dokument hält fest, was über Session-Grenzen hinweg überleben muss.
Die nächste Session klont das Repo frisch — was hier steht, ist da;
Chat-Verlauf ist es nicht.

**Lage (19.07.):** Der Quest-Bot (Sprint-Buch) ist das EINZIGE aktive Buch.
Das Haupt-Copy-Buch handelt seit 15.07. nicht mehr (`feed_only: true` — es
liefert nur noch Preise/Snapshots/Discovery für Quest), alle Beobachter
(Anomaly/Orderbook/TWAP/Labs) sind aus. Alles unter OFFEN ist Quest.

---

## OFFEN — nächste Bauplätze (Quest)

### 1. ~~Kohorten-Analytik~~ UMGESETZT 20.07. (`/quest cohorts`)
Gebaut: Ära-scoped Auswertung je Quelle (HL vs Lighter), Exit-Grund,
Asset-Klasse + Top/Flop-Leader (>= 2 Zyklen), direkt aus dem vollen
Journal. Noch offen davon: tägliche Digest-Zeile (klein) und Hebel-
Klassen-Gruppierung (braucht Entry-Join, erst wenn wirklich vermisst).

### 1b. LIGHTER-ENTSCHEIDUNG STEHT AN (Daten liegen vor, Nutzer-Call)
Spiegel-Analyse 20.07. (30h nach Amnestie): 14✅/52💥, Schatztruhe
-1.450$ - dominiert von Lighter-Leadern (Journal-Tail: 14 von 16
sichtbaren Zyklen Lighter-geführt, Summe ~-646$; das isolierte Lighter-
Messbuch bestätigt -9,4%). Struktur-Problem: Lighter-Discovery liefert
ENDLOS neue IDs, jede kostet ~2 Strikes Lehrgeld (~-100 bis -300$) bis
zum Bann - ein Lehrgeld-Laufband ohne Vorfilter (keine Fill-Historie
verfügbar). Zwei der Verlust-Treiber waren allerdings BUGS (Ban-Bypass
im Flip-Re-Entry, Hot-Hand ohne Bilanz-Check - beide 20.07. gefixt).
**NUTZER-ENTSCHEIDUNG 20.07.: "24h fair neu messen"** - kein Vorab-
Urteil auf verbuggten Daten; Lighter läuft unverändert weiter, morgen
fällt die Entscheidung (aus / Rookie-Drosselung / weiter) per
`/quest cohorts` aus den POST-Fix-Zahlen.

**NACHTRAG 20.07. — TOXIC FLOW GEBAUT (Nutzer: "unsere gestrikten
Leader werden ab sofort counter traded"):** frische Signale GEBANNTER
Leader werden invertiert geritten statt verworfen (`counter_toxic:
true`). Gegenwetten laufen unter der Identität `counter:<addr>` mit
eigenem Strike-/Record-Konto - verliert die Gegenwette wiederholt
(Leader hatte doch recht), bannt sie sich selbst (`counter_gesperrt`).
Gebannte HL-Wallets bleiben dafür beobachtet (Toxic-Watch in
`_tracked_addresses`, max 12). `/quest cohorts` weist die Counter-Spur
als eigene Quelle aus. V1 ohne (invertiertes) Bestätigungsfenster.

**v2 (gleicher Tag, Nutzer-Korrektur nach 90 min Live-Daten): Gegenwetten
folgen dem Toxic-Leader NICHT mehr beim Exit.** Journal-Beweis: ALLE 9
v1-Counter-Zyklen endeten durch Leader-Aktionen (2x leader_exit inkl.
-204$ Zwangsschluss mitten im Drawdown, 3x leader_flip = invertiertes
Mitspiegeln eines Flip-Flopper-Churns, 1x scaleout, 3x rotated), KEINER
über eigene Ziele. Konsequente These: liegt der Leader falsch, ist auch
sein Exit-Timing kein Signal. Eine Gegenwette ist UNSER Trade und endet
nur noch über die eigene Mechanik (Trail-TP, Plus-Lock, Zeit-Cut, Bust).
Die counter:-Bans aus v1 (counter:0x8d7d49eb, counter:lighter:702386)
sind Artefakte des falschen Exit-Regimes - nach dem v2-Deploy einmal
`/quest amnestie` fahren. Nebenfixe: Spiegel-Anzeige von strikes/banned/
confidence auf 18 Zeichen (Counter-Identitäten kollidierten als
'counter:0x'), und max_leverage() schreibt einen LEEREN Lookup-Cache
nicht mehr fest (Bot startete im 429-Sturm -> hl_max_leverage war die
ganze Prozess-Laufzeit null, PENGU-Kappung de facto aus).

**NACHTRAG 2 (gleicher Tag, Spiegel-Fund nach der v2/Amnestie-Kombo):
Toxic-by-Record - Toxic Flow ist jetzt amnestie-fest.** Symptom (Nutzer:
"wo sind alle Signale plötzlich?"): nach `/quest amnestie` war `banned`
komplett leer, Toxic Flow hatte 78 Minuten kein einziges Ziel mehr - die
miese LANGZEIT-Bilanz derselben Leader (`leader_record`, überlebt
Amnestien BEWUSST) ging dabei verloren, weil nur `self.banned` gescannt
wurde. Fix: `SprintBook._is_toxic()`/`toxic_addrs()` - ein Leader gilt
zusätzlich als toxisch, wenn seine Lebenszeit-Bilanz im eigenen Buch um
mind. `toxic_record_deficit` (3) negativ ist, unabhängig vom (amnestier-
baren) Strike-Stand. Verdrahtet in: `_tick_toxic` (scannt jetzt
`toxic_addrs()`), Flip-Re-Entry-Check, Fresh-Signal-Pass (neuer Reject-
Grund `leader_toxisch`, falls `counter_toxic` aus ist), `_refresh_
baselines` (Baseline bleibt am Leben), `_tracked_addresses`/Toxic-Watch,
und `_build_sprint_pool` (ein record-toxischer Leader sitzt nie
gleichzeitig FOLGEND im Pool UND wird gekontert). Strikes/Bans bleiben
die kurzfristige Justiz, `leader_record` das Langzeitgedächtnis.

**NACHTRAG 3 (21.07., Spiegel-Audit-QOL) — Flip-Flopper-Erkennung.**
Nutzer: "umgekehrte Strikes bei einer gebannten Wallet machen keinen
Sinn — 2 Strikes, er ist raus, 2 weitere trotz Counter heißt: Wallet ist
gebannt Flip-Flopper." Bannt sich die GEGENWETTE (`counter:<addr>`)
selbst, während die Original-Adresse bereits gebannt ist, beweist das:
weder Folgen NOCH Kontern funktioniert bei dieser Wallet — reines
Rauschen/Churn, kein Richtungs-Skill in irgendeine Richtung. `_book_cycle`
erkennt den Fall (`key.startswith("counter:") and Original in banned`),
loggt eine Warnung und journalisiert `sprint_flip_flopper_confirmed`.
Konsequenz: `_tracked_addresses()`s Toxic-Watch (autopilot.py) überspringt
bestätigte Flip-Flopper bei der Vergabe der knappen 12 Watch-Slots — ein
noch nicht doppelt widerlegter toxischer Leader bekommt den Slot
stattdessen. `toxic_addrs()` selbst bleibt bewusst unverändert (steuert
weiter den Pool-Ausschluss über `_sprint_banned()` — ein Flip-Flopper
bleibt aus dem Follow-Pool draußen, nur die aktive Beobachtung wird
eingespart). Spiegel-Stand beim Bau: Toxic Flow lief gut (+212.67$
gebankt, 15✅/10💥), der konkrete Fall (Original + Gegenwette beide
gebannt) trat live noch nicht auf — vorausschauende Härtung, kein
akuter Bugfix.

**NACHTRAG 4 (21.07., "Noch Ideen?" — Spiegel-getriebener Fund) —
Lighter-`_known`-Cache übersteht jetzt Neustarts.** Der 19.07.-Fix
(`_known`-Cache hält geritten werdende Lighter-Leader im Feed, egal was
die Top-Liste gerade tut) lebte nur im RAM. Bei diesem Projekt ist ein
Neustart Alltag (jeder Deploy) - fiel der GERADE gerittene Leader beim
ERSTEN Scan nach dem Neustart nicht in die frische Top-Liste, war er
auch noch nicht in `_known` (leer nach Neustart) -> `keep` griff nicht
-> exakt die `leader_rotated`-Zwangsschließung, die der Cache eigentlich
verhindern soll. Live im Spiegel beobachtet: `lighter:513030` (ETH)
wurde 1 Minute nach einem Deploy so zwangsgeschlossen (+110.49$ - diesmal
Glück gehabt, hätte genauso ein Verlust sein können). Fix:
`LighterShadow._known` wird bei jedem erfolgreichen Scan nach
`runtime/lighter_known.json` gespiegelt und beim Start geladen (nur
Einträge <= 3x `scan_seconds` alt - dieselbe Staleness-Grenze wie beim
Top-Listen-Schutz, uralte Momentaufnahmen nach langer Downtime werden
verworfen statt geglaubt). 2 neue Tests (`test_lighter.py`): Restart
mit frischer Instanz + leerem RAM-`_known` versorgt einen gerittenen
Leader trotzdem sofort; eine zu alte Momentaufnahme wird beim Laden
verworfen.

**NACHTRAG 5 (22.07., "Es gibt neue Daten mach was draus") — Fast-Path
zwischen den Ticks (Plus-Lock schoss trotz Floor durch).** Spiegel-Zahlen:
Schatztruhe fiel von +198,61$ (21.07.) auf -365,28$ (22.07., ~19h später),
39✅/40💥 bei 361 Trades. Ursache im Journal sichtbar: fast die Hälfte
aller `plus_lock`-Exits im Tail waren NEGATIV (-9.0, -24.45, -0.51, -9.0,
-33.57$) - ein direkter Bruch des eigenen Versprechens ("sobald wir im
Plus sind sollten wir nie mit Minus rausgehen"). Kein Logik-Fehler (Peak/
Floor-Vergleich ist korrekt, siehe `_plus_lock_due`) - ein 10x/10-15k$-Ritt
kann sich in den `poll_seconds` (20s) zwischen zwei vollen Autopilot-Ticks
weiter bewegen, als der Floor-Puffer (15$, schon einmal 5→15 erhöht,
20.07.) abfängt: klassisches Gap-durch-den-Stop, kein Datenfehler.
Fix: neuer Fast-Path zwischen den vollen Ticks. `SprintBook.
fast_exit_check(prices)` prüft NUR Ziel/Plus-Lock/Bust/Zeit-Cut (kein
Leader-Scan) - `_tick_parallel`s Schritt 1 wurde dafür in
`_check_ride_targets()` ausgelagert, keine doppelte Logik.
`Autopilot._wait_for_next_tick()` zerlegt die Wartezeit zwischen zwei
vollen Ticks in `sprint.fast_check_seconds`-Stücke (config.yaml: 5s) und
ruft dazwischen `fast_exit_check` mit bereits vorhandenen (kostenlosen)
WS-Mids auf - kein zusätzliches Netzwerk-Budget, reiner Exit-Reflex,
rührt Entries/Sizing/Edge-Logik nicht an. Ohne offene Mess-Ritte oder mit
`fast_check_seconds: 0` exakt das alte Verhalten (Default aus in
config.py, config.yaml schaltet scharf). 8 neue Tests (3x `test_sprint.py`
für `fast_exit_check`, 5x `test_autopilot.py` für `_wait_for_next_tick`
inkl. Leader-Fill-Interrupt und Alt-Verhalten ohne offene Ritte/deaktiviert).
Reduziert das Gap-Risiko (20s → 5s Fenster), eliminiert es aber nicht
vollständig - echte Kurs-Sprünge kann kein Paper-Floor der Welt abfangen
(auch live slippen Stops).

**NACHTRAG 6 (22.07., Nutzer-Frage zu einer konkreten Meldung) —
Plus-Lock-Text widersprach sich selbst bei Verlust.** Nutzer zitierte
live: "🔻 Zyklus 101 (HYPE) beendet (Plus gesichert (war im Plus - kein
Minus-Exit)): -7.24$" - der Text behauptete "kein Minus-Exit", obwohl der
PnL direkt daneben negativ war. Kein neuer Bug (genau der Restrisiko-Fall
aus NACHTRAG 5 - Fast-Path reduziert das Gap-Fenster, eliminiert es aber
nicht), sondern eine seit dem 19.07.-Bau nie an die Realität angepasste
Text-Annahme (`_STRIKE_EXEMPT`-Kommentar ging von "landet per Definition
breakeven-positiv" aus). Fix: `_book_cycle` nutzt bei `plus_lock` mit
negativem PnL jetzt `_PLUS_LOCK_OVERSHOOT_TXT` ("Plus-Sicherung zu spät
ausgelöst - Kurs fiel zwischen zwei Checks durch den Floor") statt der
pauschalen "kein Minus-Exit"-Zeile; der positive Normalfall behält den
bisherigen Text. Kein Verhalten geändert (weiterhin strike-exempt, keine
neue Strafe) - nur ehrliche Kommunikation. 2 neue Tests (Overshoot- und
Normalfall-Text via Notifier-Nachricht geprüft).

**NACHTRAG 7 (22.07., Nutzer: "wie wär's mit ner Lösung statt beheben von
Rechtschreibfehlern") — Fast-Path von 5s auf 1s verschärft.** Berechtigter
Einwand: der Text-Fix (NACHTRAG 6) behob nur die Kommunikation, nicht die
Ursache. Die eigentliche Lösung (Fast-Path zwischen den Ticks) war schon
in NACHTRAG 5 gebaut, aber mit 5s Intervall noch nicht eng genug (-7.24$
Overshoot war genau dieses Restrisiko, kein neuer Bug). `fast_check_
seconds` in config.yaml auf 1s verschärft (5 -> 1): praktisch kostenlos
(reine In-Memory-Dict-Lookups über die paar offenen Ritte, keine neuen
Netz-Calls - WS-Mids werden ohnehin kontinuierlich im Hintergrund
aktualisiert), senkt das Gap-Fenster nochmal 5x. Ehrliche Grenze bleibt
bestehen (im BACKLOG dokumentiert, nicht schöngeredet): ein tick-basiertes
Paper-System kann einen Preis-Sprung INNERHALB eines einzelnen Checks
(jetzt 1s statt 20s) nicht abfangen - dieselbe Physik gilt für echte
Stop-Orders an einer Börse. Zwei ECHTE weitere Hebel, bisher nicht
gezogen (Trade-off gegen "Edge", daher Nutzer-Entscheidung statt
Alleingang): (a) `plus_lock_floor` weiter anheben (mehr Puffer, aber
sperrt Gewinne früher statt laufen zu lassen), (b) `plus_lock_arm`
mit anheben (mehr Vorlauf bevor die Sicherung überhaupt scharf wird).

**NACHTRAG 8 (22.07., Audit auf Nutzer-Anfrage) — Elite-Umschaltung-
Kriterien technisch berührt, aber NICHT umgeschaltet.** Stand bei 108
Zyklen: ≥100 Zyklen ✓, und genau 5 Identitäten mit ≥3 Zyklen bei ≥60%
Winrate (`lighter:513030` 4/0, `0x985f02b19dbc062e` 3/1, `counter:
0x28473085` 7/3, `0xa7405ff2687cb83b` 2/1, `0x7f20c6eaa0adc427` 3/2).
Formal erfüllt, aber bewusst NICHT umgeschaltet: (a) eine der 5 ist eine
synthetische `counter:`-Identität, keine echte folgbare Wallet - real
nur 4; (b) winzige Samples (meist n=3-4, außer der Counter-Identität);
(c) Gesamtbilanz weiterhin tief rot (-515,66$ bei 56✅/52💥). Empfehlung:
weiter Daten sammeln, bis Kandidaten ≥5-8 Zyklen UND die Gesamtbilanz
positiv zeigen - sonst wird auf Rauschen konzentriert. `leader_record`
ist jetzt dauerhaft im Status-Spiegel sichtbar (`SprintBook.stats()`),
diese Prüfung lässt sich künftig jederzeit ohne neuen Code wiederholen.

**NACHTRAG 9 (23.07., Nutzer: "Signale schwächen mal wieder ab") —
Toxic-Flow-Scan verschwendete sich an dauerhaft toten Gegenwetten.**
Spiegel-Zahlen: 162 von 179 `fresh_seen` (~90%!) waren `counter_
gesperrt`-Rauschen von genau 2 Lighter-Adressen (`lighter:702386`,
`lighter:726314`/`726722`), deren Gegenwette längst gebannt ist und es
bis zur nächsten Amnestie bleibt. `_tick_toxic` machte trotzdem bei
JEDEM Tick erneut volle Baseline-/Fresh-Signal-Arbeit UND zählte
`fresh_seen` hoch - verzerrte die Spiegel-Zahlen massiv (sah nach viel
Aktivität aus, aber `last_entry_min: 178.5` zeigte: seit 3h kein echter
Einstieg). Fix: `_tick_toxic` prüft `counter:<addr>` gebannt JETZT ganz
am Anfang der Schleife, VOR jeder Baseline-/Fresh-Arbeit - überspringt
dauerhaft tote Adressen komplett (kein fresh_seen, kein wiederholter
Reject-Spam, der Zustand steht eh in `banned`). Bewusst NICHT an
`_STRIKE_EXEMPT`/bestätigte-Flip-Flopper gekoppelt (die bleiben
unverändert für den Pool-Ausschluss über `toxic_addrs()`) - reine
Scan-Optimierung, greift auch für record-toxische (nicht formell
gebannte) Originale mit gebannter Gegenwette wie `lighter:726722`.
Nebenfund beim Debuggen: Spiegel-Anzeige-Kürzung auf 18 Zeichen (20.07.
für 0x-Kollisionen gedacht) reichte für Lighter-IDs nicht - "counter:
lighter:72" zeigte für sowohl ...726314 als auch ...726722 identisch.
Auf 24 Zeichen angehoben (`_DISPLAY_TRUNC`). 3 neue Tests.

**NACHTRAG 10 (24.07., "Massig Daten, schau dir den Stand an und
verbessere") — Fehlender Preis löste falsche Plus-Lock-Exits aus (der
eigentliche Grund für den Bilanz-Rutsch).** Spiegel-Zahlen: Schatztruhe
weiter auf -860,43$ gefallen (von -515,66$ am 22.07.), 65✅/77💥, avg
Trades/Zyklus auf 3.4 gesunken. Im 50er-Journal-Tail: 10 von 14 `plus_
lock`-Exits negativ, davon 7x EXAKT -9.00$ (kein Zufall - für 10.000$
Notional ist das exakt Open+Close-Fee bei 0 Kursbewegung). Root Cause:
`_ride_pnl()` fiel bei fehlendem Coin-Preis im `prices`-Dict still auf
den Entry-Preis zurück ("keine Bewegung" angenommen) - das ergibt IMMER
exakt `raw_pnl=0 - Fee`, unabhängig vom echten Kurs. Kombiniert mit
Plus-Lock (`pnl <= floor`) triggerte das einen FALSCH-POSITIVEN Exit für
jeden armed Ritt, sobald der Preis auch nur einmal fehlte - der neue
`fast_exit_check`-Pfad (1s-Fenster, WS-Mids liefern 'xyz:'/Lighter-Coins
lückenhaft) traf das viel häufiger als der alte volle Tick, der über
`all_mids()` fast immer vollständig war. Fix: `_ride_pnl()` gibt jetzt
`None` zurück statt zu raten, wenn der Preis fehlt oder 0 ist - alle
Aufrufer behandelten `None` bereits korrekt als "diesen Tick nicht
beurteilbar". Gegenstück (Audit-Präzedenzfall vom Mode-Switch-Drain:
'xyz:'-Perp fehlt am Wochenende DAUERHAFT in `all_mids()`): neuer
`_price_missing_since`-Tracker pro Coin, nach `_DRAIN_MAX_AGE_S` (120s)
ohne Preis wird zum Einstand zwangsabgerechnet (neuer Reason `kein_
preis`, strike-exempt) statt den Ritt-Slot für immer zu blockieren.
4 neue Tests. Erwartung: sollte einen spürbaren Teil der Plus-Lock-
Verluste beheben, da echte, funktionierende Mechanik jetzt nicht mehr
durch Datenlücken sabotiert wird - nächster Spiegel-Check zeigt, ob die
Bilanz sich stabilisiert.

**NACHTRAG 11 (24.07., Nutzer: "Wie weit sind wir vom Elite-Modus" →
"Baue") — Elite-Kriterium um Profitabilität ergänzt + `/quest elite`.**
Der Audit bei 149 Zyklen zeigte beide BACKLOG-Kriterien formal erfüllt
(149 ≥ 100 Zyklen; 7 Qualifizierte, davon 5 echte Wallets ≥ 5). ABER
zwei Befunde sprachen gegen die Umschaltung:
(a) **Blinder Fleck im Kriterium:** Winrate misst Häufigkeit, nicht
Größe. `0xc30c7ea9` erfüllte mit 2W/1L die 60%-Schwelle, stand über
dieselben 3 Zyklen aber bei **-16,98$** (viele kleine Plus-Lock-Siege,
ein großer Verlust) - im Elite-Modus hätte genau der die ganze Bank
bekommen. Umgekehrt war `counter:0x28473085` (+26,20$, 10W/4L) der
stärkste Verdiener, aber als synthetische Gegenwette gar nicht folgbar.
(b) **Datenbasis war vom -9$-Bug verunreinigt** (NACHTRAG 10): Plus-Lock
ist strike-exempt, die Falsch-Exits wurden also NICHT als Niederlage in
`leader_record` verbucht (Winrates blieben sauber) - aber die abgewürgten
Ritte konnten auch nie zum SIEG werden, die Siege sind also zu niedrig
gezählt.
Gebaut: neues `SprintBook.leader_pnl` (Netto-PnL je Identität, persistiert
und amnestie-fest wie `leader_record`; zählt anders als dieses JEDEN
Zyklus inkl. `_STRIKE_EXEMPT` - fürs Geld ist egal, wer schuld war), im
Spiegel sichtbar, plus `elite_audit()` + Telegram-Befehl `/quest elite`.
Neue Schwelle `ELITE_MIN_PNL` (netto positiv). `counter:`-Identitäten
werden separat ausgewiesen und zählen NICHT gegen die Mindestzahl echter
Wallets. Wichtig für die Übergangszeit: 'PnL noch unbekannt' (leader_pnl
startet leer, leader_record ist viel älter) wird strikt von 'PnL bekannt
und nicht positiv' getrennt - beides blockiert die Zulassung, liest sich
in `/quest elite` aber unterschiedlich, damit fehlende Daten nicht wie
Verluste aussehen. 7 neue Tests. **Nächster Schritt:** ~24h saubere
Post-Fix-Daten sammeln, dann `/quest elite` erneut - erst wenn beide
Haken auf ECHTEN Post-Bug-Zahlen stehen, ist die Umschaltung fällig.

**NACHTRAG 12 (24.07., Nutzer: "haben wir schon ewig keine Signale mehr")
— Snapshot-Sturm: der Tracker fragte jede Wallet auf JEDEM Builder-DEX ab.**
Zuerst die Entwarnung zur Wahrnehmung: der Absturz `fresh_seen` 179 → 6 ist
zu ~90% mein eigener Toxic-Scan-Fix (NACHTRAG 9), der Phantom-Signale nicht
mehr mitzählt. Die ECHTE Signalrate ist praktisch unverändert: 1,8/h vor dem
Fix (17 echte von 179 in 9,2h) gegen 2,2/h danach (6 in 2,7h). Es sind also
nicht plötzlich Signale verschwunden — sie waren vorher nur aufgebläht.
DAHINTER lag aber ein echter, schwerer Fund: **149 von 150 Log-Zeilen waren
Snapshot-Fehler**, ALLE 15 HL-Wallets betroffen, 7 von 15 dauerhaft stale,
Tick-Dauer 86s statt der konfigurierten 20s. Ursache: `market.dexs: auto`
entdeckt gut ein Dutzend Builder-DEXs, und `LeaderTracker.snapshot()` fragte
JEDE Wallet auf JEDEM davon ab — bei 25 beobachteten Wallets ~300
`user_state`-Calls pro Tick, die reihenweise ins Rate-Limit liefen. In den
echten Daten liefern aber nur ZWEI DEXs je Positionen: der Haupt-DEX
(Krypto, 282 Vorkommen) und `xyz` (Aktien/Rohstoffe, 167) — der Rest war
reine Verschwendung. Folge fürs Signal-Problem: eine stale Wallet liefert
den GECACHTEN Stand, ihre Positionsänderungen werden also erst beim
nächsten erfolgreichen Snapshot sichtbar (verspätete Signale; kurze
Leader-Trades innerhalb des Blindfensters sieht man nie).
Fix: DEX-Sparmodus im Tracker. Je Wallet wird gemerkt, auf welchen DEXs sie
zuletzt wirklich Positionen hatte; nur die werden abgefragt. Alle
`copytrade.dex_reprobe_s` (600s) läuft je Wallet wieder ein VOLLER
Durchlauf, damit ein erstmaliger Ausflug auf einen neuen DEX gefunden wird.
Sicherheits-Design: übersprungen werden nur DEXs, die beim letzten vollen
Lauf LEER waren — taucht dort zwischendurch etwas auf, fehlt es zwar bis
zum nächsten Probe-Lauf im Buch, aber eben AUCH in der Baseline, kann also
nie einen falschen `leader_exit` auslösen (nur ein verspätetes Signal).
DEXs mit gehaltener Position werden immer weiter abgefragt, deren
Schließung sehen wir punktgenau. Ein im Probe-Lauf transient
ausgefallener DEX bleibt im Gedächtnis (wird nicht als "verlassen"
missverstanden). `feed.dex_calls` im Spiegel macht die Wirkung messbar.
5 neue Tests. `dex_reprobe_s: 0` = altes Verhalten.

**NACHTRAG 13 (25.07., Tages-Check nach 13h Post-Fix-Laufzeit) — beide
Fixes bestätigt, neuer Haupt-Verlusttreiber gefunden + Falschrichtungs-Stop.**

*Snapshot-Storm-Fix (NACHTRAG 12) wirkt voll:* stale 7/15 → **0/16**,
`dex_calls` ~300 → **21**, Tick-Dauer 86s → **29s**, Signalrate 2,2/h →
**4,3/h**. *Plus-Lock-Fix (NACHTRAG 10) bestätigt:* vorher 10 von 14
plus_lock-Exits negativ (7x exakt -9,00$), jetzt **3 von 3 positiv**
(+40,23$). Der neue `kein_preis`-Drain hat 1x gefeuert, bei 0,00$ - genau
wie designt.

*Neuer Haupt-Verlusttreiber:* `zeit_negativ` (8 von 16 Zyklen, -406,38$,
avg -50,80$). Darin steckte ein einzelner Ausreißer, der ALLES dominierte:
`0xf224d1b2` auf **STX** LONG (5x, 5000$ Notional), Entry 23:49:01 → Exit
01:49:15 = **genau 2h**, der Zeit-Cut feuerte also korrekt - aber bei
**-270,78$**, das sind **97% der gesamten Tagesbilanz**. Ohne diesen einen
Ritt: -8,00$ statt -278,78$, praktisch flat.
*Strukturelle Lücke:* nach oben war alles austariert (Trail lässt laufen,
Plus-Lock sichert ab +30$ bei +15$), nach unten klaffte zwischen
`max_ride_hours` (2h) und `bust_frac` (-95%) **keine Verlustgrenze**.
Größter Gewinn +69,80$ gegen größten Verlust -270,78$ = Faktor 3,9 - diese
Asymmetrie verhindert Profitabilität mathematisch, unabhängig von der
Trefferquote.

*Gebaut (Nutzer-Entscheidung per AskUserQuestion: "Nur Falschrichtung
kappen"):* neuer `sprint.wrong_way_stop` (config.yaml: 50$) +
`SprintBook._wrong_way_due()`, verdrahtet in `_check_ride_targets` (greift
damit auch im 1s-Fast-Path) UND im Einzel-Ritt-Zweig (für den späteren
Elite-Modus). **Bewusst schmal geschnitten, um die Edge nicht zu dämpfen**
(Nutzer 19.07.: "ich brauch die Edge und nicht diesen lieber dämpfen
bullshit"): der Stop greift NUR für Ritte, deren Peak nie `plus_lock_arm`
erreicht hat - die also von Anfang an in die falsche Richtung liefen. Wer
schon meaningful im Plus war, behält volle Freiheit; dort übernimmt
Plus-Lock, dessen Bedingung (`peak >= arm`) das exakte Gegenstück ist -
beide können sich nie in die Quere kommen. Anders als `plus_lock`/
`kein_preis` NICHT strike-exempt: hier lag der Leader wirklich falsch.
Neuer Reason `falschrichtung`. 4 neue Tests (kappt Falschrichtung, verschont
gelaufene Ritte, aus per Default, greift im Fast-Path). `wrong_way_stop: 0`
= altes Verhalten.

### 2. Elite-Umschaltung (Masterplan Phase D — das dokumentierte ENDZIEL)
Kriterien sind seit 24.07. CODIFIZIERT und jederzeit per `/quest elite`
abrufbar (`SprintBook.elite_audit()`, Schwellen als `ELITE_*`-Konstanten
in `bot/sprint.py`): >= 100 abgeschlossene Mess-Zyklen UND >= 5 ECHTE
Wallets mit >= 3 Zyklen bei >= 60% Winrate UND netto positiver PnL im
eigenen Buch. Der Moduswechsel selbst (`parallel_rides: false`) bleibt
bewusst manuell — der Audit wertet nur aus, schaltet nie um. Dazu Confidence-
Kalibrierung: STAR_THRESHOLD 100 ist mit +5/Gewinn praktisch unerreichbar
(20 TP-Zyklen EINES Leaders) - runter auf ~25-30 ODER Vergabe anheben,
sonst bleibt das Star-System toter Code. Absorbiert auch die alte offene
Frage „Star-Bonus im Haupt-Buch-Score": das Haupt-Buch handelt nicht mehr
(feed_only) - der „codeweite Star-Filter" von damals heißt heute Elite-
Pool-Zulassung, es gibt keinen zweiten Einhängepunkt mehr.

### 3. Live-Readiness (Masterplan Phase E — bewusst zuletzt)
Sprint-Live-Executor hinter eigenem Flag (echte Orders statt Paper), mit
den vorhandenen Sicherungen (Hebel-Kappung ist schon live-treu);
Slippage-/Fee-Validierung gegen echte Fills; Kill-Switch + Tages-
Verlustlimit auf Sprint-Ebene. Erst nach 2+ Wochen positivem Elite-Paper.

### 4. ~~Report-Fix für den Mess-Modus~~ ERLEDIGT (geprüft 20.07.)
Beim Nachprüfen stellte sich heraus: der monierte Codepfad existiert
nicht mehr - `_cmd_report` wurde längst auf `quest_scorecard`/`banked`
umgebaut (ritt-scharf, parallel-sicher), niemand liest mehr
`sprint_book.json`s rohes `realized_pnl` als „aktueller Zyklus".

### 5. CMM/HyperTracker-Ausbau (Discovery füttert den Quest-Pool)
Details im Archiv-Abschnitt unten („CMM/HyperTracker-Ausbau") - kurz:
(a) Tiefen-Scoring via closed-trades/summary für die Top-K je Analyse,
(b) Smart-Money-Kohorten (segmentIds 8/9/10) als zusätzliche Quelle,
(c) Positions-API statt eigener Fills-Rekonstruktion (langfristig),
(d) Kohorten-Bias als MarketGuard-Input. Budget: 100 Req/Tag je Token.

### 6. Hygiene
- Telegram `getUpdates`-Timeouts häufen sich (Server->Telegram zäh) -
  Long-Polling-Timeout/Retry im Notifier prüfen.
- Audit-Kadenz: nach jeder größeren Bau-Phase ein Read-only-Deep-Dive
  (Wave-Muster) vor dem nächsten Umbau.

---

## ARCHIV — Chronik & erledigte Specs (Kontext für neue Sessions)

### Masterplan 19.07. „Vom Flow zur Profitabilität" (Kern erledigt/revidiert)
Phasen A (Stop-Loss je Ritt) + B (Rookie-Dämpfung) wurden am selben Tag
per Nutzer-Veto VERWORFEN ("ich brauch die Edge und nicht diesen
lieber-dämpfen-Bullshit") und durch die EDGE-RUNDE ersetzt (Commit
f0b4423): Trailing-TP (`trail_frac 0.3`), Hot-Hand-Konzentration
(`leader_record`: >=1 Gewinn -> 4 Slots, >=2 -> 1.5x Notional),
Trust=Speed (`trusted_skip_confirm`), BTC-Sperre + Majors-Overrides raus.
Direkt danach (gleicher Tag, Commit f90f00b): Plus-Sicherung
(`plus_lock_arm 30`/`plus_lock_floor 5` - „sobald wir im Plus sind nie
mit Minus rausgehen", strike-exempt) + `/quest amnestie` (Strikes+Bans
auf null, Confidence/leader_record bleiben - die alten Urteile entstanden
unterm alten Exit-Regime). Phasen C/D/E leben oben unter OFFEN weiter.

> **UPDATE 18.07. (abends) — FLOW-RUNDE (Nutzer: "ein Trade pro Stunde ist
> das Ziel, konstanter Flow"). Nach dem ersten funktionierenden Live-Tag
> (9 frische Signale, 3 Einstiege, +57.93$-MELANIA-Zyklus) vier Hebel per
> AskUserQuestion abgesegnet und aktiviert (alle config.yaml):**
> 1. `sprint.max_rides_per_leader: 1 -> 2` - das 1er-Limit war der größte
>    Frequenz-Deckel (5 von 9 Signalen "leader_belegt", alle von der besten
>    Signal-Maschine). Methodik-Schutz bleibt: 2 statt unbegrenzt, Journal
>    attribuiert weiter jeden Ritt seinem Leader.
> 2. `sprint.add_signal_frac: 0.5 -> 0.25` - Halter-Pool (watch 15/15
>    holding), Überzeugungs-Moment ist das Aufstocken.
> 3. `lighter.enabled + sprint_promote: true` - zweite Börse als Signal-
>    Quelle, seit heute mit Staleness-Schutz.
> 4. `autopilot.reanalyze_hours: 6 -> 3`, `sprint.pool_size: 20 -> 25`,
>    `analysis.top_n: 90 -> 110` - schnellere Rotation, mehr Breite.
>
> **UPDATE 18.07. (später) — ZWEITER STRUKTUR-FUND: die Gates filterten
> genau die Frequenz-Trader weg, die das '1 Trade pro Stunde'-Ziel liefern
> könnten.** Beweis aus einem einzigen Analyse-Lauf: 0x12203316 (197 Trips,
> 88% Trefferquote, Sprint-Score 100) raus wegen "nur 2 aktive Tage (< 3)";
> 0xcd87ea21 (53 Trips, 75%) raus als "Scalper" am abgeleiteten 15min-Boden;
> die EINZIGE Wallet, die je Signale feuerte (0x8d7d49eb), hatte 11-14min
> Haltedauer - exakt dieses Profil. Übrig blieben Portfolio-Halter: watch
> zeigte 9/9 "holding", 23 Minuten lang keine einzige Positions-Änderung.
> Fixes: (1) eigener `sprint_min_hold_minutes: 5` statt Hauptbuch-Hälfte
> (15min) - nur noch Sekunden-Scalper raus, Strikes urteilen den Rest;
> (2) `sprint_min_active_days: 3 -> 2` (config.yaml); (3) Analyzer-`_call`
> wiederholt jetzt auch 5xx-Fehler mit Backoff, nicht nur 429 (Live: 39 von
> 90 Kandidaten in EINEM Lauf kommentarlos an '502 Bad Gateway' verloren,
> Pool schrumpfte dadurch zufallsverzerrt auf 9).
>
> **UPDATE 18.07. — WICHTIGER FUND: Spot-Fills verfälschten die Analyse
> (Nutzer-Frage: "tracken wir Wallets nicht, die x2 oder Spot handeln?").**
> Symptom: seit Vorabend 20 Uhr komplette Signal-Stille, egal wie breit/
> vielfältig der Pool (16+ Wallets, alle Aktien-Baskets offen, gesenkte
> Größen-Böden für Shrimps) - mehrere vorherige Fixes (Pfad-A/B-Pool-
> Ranking, Bestätigungsfenster im Mess-Modus) hatten das NICHT behoben.
> Root Cause: `userFillsByTime` (HL-API) liefert PERP- UND SPOT-Fills
> gemischt zurück, Spot-Paare per HL-Konvention mit `@<Index>` statt einem
> Ticker (z.B. `@107`). `analyze_fills()` (`bot/copytrade/analyzer.py`) hat
> bisher NICHT unterschieden - ein Wallet, dessen "Aktivität" überwiegend
> oder komplett aus Spot-Trades bestand, sah in unseren Metriken (Trips/
> Trefferquote/aktive Tage) wie ein aktiver Trader aus und bestand alle
> LARP-/Sprint-Gates. Unser LIVE-Tracking (`LeaderTracker.snapshot`, HLs
> `user_state`) sieht aber AUSSCHLIESSLICH Perp-Positionen - Spot ist dort
> strukturell unsichtbar. Ergebnis: der Pool konnte mit Wallets vollgestopft
> sein, die auf dem Papier super aktiv wirkten, aber STRUKTURELL NIE ein
> Live-Signal geben konnten, egal wie oft die Pool-Zusammensetzung geändert
> wurde. Fix: `analyze_fills()` filtert `@`-Spot-Fills jetzt komplett raus,
> BEVOR irgendeine Metrik berechnet wird - Trips/PnL/Trefferquote/aktive
> Tage spiegeln jetzt nur noch das, was wir auch tatsächlich live erkennen
> können. Nächster Schritt: nach dem Deploy beobachten, ob sich die
> Kandidaten-/Pool-Zahlen sichtbar verschieben (weniger "Aktiv"-Wallets, die
> vorher nur durch Spot-Volumen aktiv aussahen) und ob endlich echte
> Live-Signale kommen.

> **AKTUELLER FOKUS (Nutzer, 13.07.2026, historische Momentaufnahme - beide
> hier genannten Punkte sind längst umgesetzt, siehe UPDATEs unten): das
> Sprint-Buch.** Stand: 3/3 Gewinn-Zyklen unter v3. Die Mess-Woche (bis So
> 19.07. 16:30) läuft mit CMM-Discovery + 12er-Pool. Danach hat der Sprint-
> Ausbau Vorrang — zuerst das Confidence-Points/Star-System (Spec unten,
> UMGESETZT 15.07.), dann ggf. Lighter-Promotion in den Sprint-Pool
> (UMGESETZT 17.07., `sprint_promote` in config.yaml).
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
> Bekannter Report-Anzeige-Fehler (INZWISCHEN WIEDER AKUT, siehe OFFEN #4
> oben - parallel_rides ist seit 17.07. wieder aktiv; Original-Vermerk:
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
>
> **UPDATE 18.07. — Wave-3-Audit-Funde (Nutzer: "Agenten ohne Fix laufen
> lassen, wir behalten es für Wave 3"). Deep-Dive-Workflow, 2 Finder-Lenses +
> 1 adversarialer Verifier je Fund, 8/8 bestätigt. NOCH NICHT GEFIXT,
> absichtlich für die nächste Bau-Runde vorgemerkt:**
>
> 1. **[Hoch, betrifft den AKTUELLEN Modus]** `bot/sprint.py` `tick()`
>    (~Zeile 187-197): bei `parallel_rides: true` (aktiv seit 17.07.)
>    verzweigt sofort in `_tick_parallel()` und `return`t davor - das
>    Bestätigungsfenster (`confirm_delay_s`, `_process_pending`) UND die
>    Star-Preemption (`_scan_star_preemption`) werden NIE erreicht.
>    `_tick_parallel()` reitet frische Signale weiter sofort (0s), obwohl
>    config.yaml `confirm_delay_s: 10` explizit mit dem Kommentar
>    "Flip-Flopper-Schutz" scharf gestellt hat. Der Schutz existiert nur im
>    Einzel-Ritt-Zweig, der im Mess-Modus nie läuft.
> 2. **[Mittel]** `bot/autopilot.py` `_build_sprint_pool` (~Zeile 1369):
>    `forced_main` hängt JEDEN aktuellen Haupt-Leader unbedingt an den Pool
>    an - auch wenn `cap_path_b_admission` ihn zuvor als überzähligen
>    Pfad-B-Sitzer explizit ausgeschlossen hat. Ein Haupt-Leader, der die
>    Haupt-LARP-Kriterien (kein Trefferquote-Check!) erfüllt, aber Sprint-
>    Pfad A verfehlt, rutscht so am Deckel vorbei zurück in den Pool.
> 3. **[Mittel]** `bot/sources/lighter.py` `sprint_snapshots()` (~Zeile 308):
>    kein Staleness-Schutz. Schlägt der periodische `rank()`-Scan wiederholt
>    fehl, bleibt `_leaders` beliebig lange auf dem letzten guten Stand -
>    ohne Zeitstempel-Check. Ein `lighter:`-Ritt sieht die (real längst
>    geschlossene) Leader-Position ewig als offen, `leader_exit` feuert nie.
>    Für HL gibt's mit `_maybe_warn_stale_feed` ein Pendant, für Lighter
>    fehlt es komplett. (Aktuell entschärft durch `sprint_promote: false`.)
> 4. **[Niedrig]** `bot/sources/coinmarketman.py` `_active_token_idx`
>    (~Zeile 55): rotiert bei 429 permanent weiter, wird nie zeitbasiert
>    zurückgesetzt - CMMs Limit ist aber ein TAGES-Kontingent, kein
>    Prozess-Lifetime-Fenster. Der Bot läuft als Dauer-systemd-Service ohne
>    geplante Neustarts; über Wochen wandert der Zeiger unwiederbringlich
>    durch alle 5 Token-Slots, obwohl auf früheren Slots täglich frisches
>    Kontingent liegen bliebe.
> 5. **[Mittel]** `bot/config.py` `SprintConfig.rebalance_threshold`
>    (Zeile 409, auch `config.yaml sprint.rebalance_threshold: 0.02`): totes
>    Config-Feld, copy-paste aus `CopytradeConfig` - `bot/sprint.py` liest es
>    nirgends (Sprint kennt keine proportionale Rebalancierung, nur volle
>    Enter/Exit-Positionen über `min_notional`).
> 6. **[Mittel, reine Doku]** `bot/copytrade/larp.py` `check_sprint()`-
>    Docstring (~Zeile 119): nennt veraltete Schwellen - "Trefferquote
>    >=52%" statt tatsächlich `SPRINT_MIN_WIN_RATE=0.45`, "Buch >=60% im
>    Plus" statt `SPRINT_MIN_GREEN_SHARE=0.55`, "halbierte" statt tatsächlich
>    geviertelte (mit Boden 8) Round-Trip-Hürde für Pfad A.
> 7. **[Niedrig, reine Doku]** `bot/copytrade/larp.py` `LarpVerdict.path`-
>    Kommentar (~Zeile 51): beschreibt nur die alte, wirkungslose
>    Nachrang-Sortierung in `_build_sprint_pool`, nicht den seit `ac0a4fa`
>    tatsächlich scharfen `cap_path_b_admission`-Deckel.
> 8. **[Niedrig, reine Doku]** Dieses Dokument selbst, Zeilen 10-11 + ~230:
>    behauptet weiterhin "Lighter-Promotion ... noch nicht gebaut" - ist
>    seit `cdbb8af`/`e5dfc3f` (17.07.) live, nur per `sprint_promote: false`
>    (config.yaml) abgeschaltet. Wird beim Wave-3-Fix mitkorrigiert.
>
> **UPDATE 18.07. — Wave 3 GEBAUT (Nutzer: "Kannst alles fixen sollte genug
> Limit da sein"). Alle 8 Funde oben behoben:**
> 1. Bestätigungsfenster (`confirm_delay_s`) läuft jetzt auch im Mess-Modus
>    (`_process_pending`/`_promote_pending_parallel` in `bot/sprint.py`) -
>    JEDER bestätigte Kandidat bekommt einen eigenen Ritt-Slot (kein "nur
>    einer gewinnt" wie im Einzel-Ritt), Flip-Re-Entries INNERHALB eines
>    laufenden Mess-Ritts bleiben bewusst sofort (Design-Prinzip unverändert).
>    Star-Preemption gilt weiterhin NUR im Einzel-Ritt (im Mess-Modus gibt es
>    keinen EINEN Ritt, den man verdrängen müsste).
> 2. `cap_path_b_admission`/`forced_main` waren beim genaueren Hinsehen KEIN
>    Leck, sondern zwei unterschiedliche, beide beabsichtigte Garantien
>    (Deckel für neu entdeckte Kandidaten in `sprint_ok` vs. "Haupt-Leader nie
>    aus den Augen verlieren", ein Haupt-Leader hat ohnehin die strengeren
>    Hauptbuch-Kriterien schon bestanden) - beide Docstrings jetzt präzise
>    gegeneinander abgegrenzt, kein Verhaltensfix nötig.
> 3. `LighterShadow.sprint_snapshots()` hat jetzt einen Staleness-Schutz
>    (`_last_scan_ok`, `bot/sources/lighter.py`) - ab dem 3-fachen
>    Scan-Intervall ohne erfolgreichen Scan liefert die Funktion `[], []`
>    statt eingefrorene Positionen als frisch auszugeben.
> 4. CMM-Token-Rotationszeiger (`bot/sources/coinmarketman.py`) setzt sich
>    jetzt bei jedem UTC-Tageswechsel automatisch zurück (`_maybe_reset_daily`)
>    - unabhängig vom Prozess-Neustart, passend zu CMMs Tages-Kontingent.
> 5. `SprintConfig.rebalance_threshold` (totes Copy-Paste-Feld) entfernt, aus
>    config.yaml + Tests mitentfernt.
> 6. `check_sprint()`-Docstring korrigiert (45%/55%/geviertelte Hürde statt
>    veralteter 52%/60%/"halbiert").
> 7. `LarpVerdict.path`-Kommentar nennt jetzt beide Konsumenten (Nachrang-
>    Sortierung UND den eigentlich scharfen `cap_path_b_admission`-Deckel).
> 8. Dieses Dokument: "AKTUELLER FOKUS" als historische Momentaufnahme
>    markiert, "Offene Frage 5" oben als geklärt aktualisiert.
>
> Neue/erweiterte Tests: `test_sprint.py` (+5, Mess-Modus-Bestätigungsfenster),
> `test_lighter.py` (+1, Staleness), `test_coinmarketman.py` (+1,
> Tages-Reset). Volle Suite + `simulate.py` grün.

---

## Confidence Points / interner LARP-Star-Rang (ARCHIV/Referenz-Spec)

**Status:** UMGESETZT (QOL-Runde, 15.07.2026). Läuft im Sprint-Buch.
Offen daran ist nur noch die Schwellen-Kalibrierung — siehe OFFEN #2.

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

## CMM/HyperTracker-Ausbau (OFFEN — Detail zu OFFEN #5 oben)

**Status:** Discovery über das perp-pnl-Board ist LIVE (13.07.2026). Die API
kann deutlich mehr — Spec: github.com/Coin-Market-Man/hypertracker-skills
(SKILL.md). Budget beachten: Free-Tier = 100 Requests/TAG (je Token,
Multi-Token-Rotation seit 17.07. mit Tages-Reset seit 18.07.).

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
3. ~~Der *breitere* codeweite Filter~~ **ÜBERHOLT (19.07., BACKLOG-
   Aufräumen):** Sprint-interner Teil war ERLEDIGT (Star-Vorrang +
   Preemption). Der damals offene Rest — „Star-Bonus im Haupt-Buch-
   Leaderboard-Score" — ist gegenstandslos: das Haupt-Buch handelt seit
   15.07. nicht mehr (`feed_only`), es GIBT keinen zweiten Einhängepunkt.
   Die lebende Nachfolge-Idee (Star/Record als Zulassung zum Elite-
   Einzel-Ritt und später zum echten Kapital) steckt in OFFEN #2.
4. ~~Sichtbarkeit~~ **ERLEDIGT:** `/sprint` und `/sprint pool` zeigen ⭐ bei
   `confidence >= 100`; Journal-Kind `sprint_star` + Telegram-Push beim
   Erreichen der 100.
5. ~~Quellen-Reichweite~~ **GEKLÄRT (Wave-3-Audit-Fund 18.07., Dokument war
   hier veraltet):** Lighter-Promotion in den Sprint-Pool ist seit `cdbb8af`/
   `e5dfc3f` (17.07.) gebaut (`LighterShadow.sprint_snapshots()`,
   `LighterConfig.sprint_promote`, aktuell per `sprint_promote: false` in
   config.yaml abgeschaltet, nicht unbebaut). `_book_cycle`/Confidence
   unterscheiden nicht nach Adress-Präfix - ein `lighter:`-Leader verdient
   also automatisch unter denselben Regeln Sterne wie ein HL-Leader, sobald
   `sprint_promote` scharf gestellt wird. Konsequent, kein Sonderfall nötig.

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
