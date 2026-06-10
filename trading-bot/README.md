# Hyperliquid Trading Bot

Trendfolge-Bot für Hyperliquid Perpetuals mit striktem Risikomanagement,
Backtesting und mehrstufigem Sicherheitskonzept.

## Schnellstart: Autopilot mit Web-UI

```bash
pip install -r requirements.txt
python doctor.py          # Preflight: prüft Config, Tests, alle APIs
python server.py          # -> http://127.0.0.1:8000
```

Im Dashboard: **Wallet verbinden → Bot-Vollmacht signieren → Autopilot starten.**
Alles Weitere läuft im Hintergrund: Trader-Analyse, Leader-Rotation,
Copy-Trading, News-/Schock-Überwachung, Circuit Breaker.

- Die Vollmacht ist ein Hyperliquid **Agent-Wallet** (per EIP-712 in MetaMask
  signiert): Der Bot darf traden, aber **niemals auszahlen**. Der Haupt-Wallet-Key
  verlässt nie den Browser; der Agent-Key liegt nur lokal in `.env` (chmod 600).
- Headless ohne UI: `python autopilot.py`
- Der Server bindet auf `127.0.0.1` - niemals ungeschützt ins Internet stellen.

### Fernzugriff: Dashboard von unterwegs (nur für dich)

**Warum nicht GitHub Pages?** Pages hostet nur statische Dateien und ist
immer öffentlich (Zugriffsbeschränkung gibt es nur mit GitHub Enterprise).
Unser Dashboard braucht aber das lokale Backend, das den Agent-Key hält -
das gehört nie auf einen öffentlichen Host. Die saubere Lösung:

1. **Token setzen** (Pflicht bei Fernzugriff): in `.env`
   `DASHBOARD_TOKEN=$(openssl rand -hex 24)` - das UI fragt den Token einmal
   ab und merkt ihn sich; alle API-Calls sind sonst 401.
2. **Privaten Zugangsweg wählen** (Empfehlung: Tailscale):

   | Weg | Setup | Eigenschaften |
   |---|---|---|
   | **Tailscale** | App auf Rechner + Handy, gleiches Konto | privates Mesh-VPN, Dashboard unter `http://<rechnername>:8000`, nichts öffentlich exponiert |
   | **SSH-Tunnel** | `ssh -L 8000:127.0.0.1:8000 user@rechner` | klassisch, kein Zusatzdienst, Verbindung nur bei aktivem Tunnel |
   | Cloudflare Tunnel + Access | `cloudflared` + E-Mail-Gate | öffentliche URL mit Login davor - nur wenn Tailscale/SSH nicht gehen |

   In allen Fällen bleibt `server_host: 127.0.0.1` - der Tunnel/das VPN
   verbindet sich lokal, der Server selbst ist nie direkt im Internet.

> **Risikohinweis:** Kein Trading-Bot ist garantiert profitabel. Leverage
> verstärkt Verluste genauso wie Gewinne – bis hin zur Liquidation. Handle
> nur mit Geld, dessen Totalverlust du verkraften kannst, und erst nach
> ausgiebigem Backtest und Testnet-Betrieb.

## Strategie

- **Trend:** EMA(20)/EMA(50)-Crossover bestimmt die Richtung (long/short)
- **Filter:** RSI(14) verhindert Einstiege in überkaufte/überverkaufte Märkte
- **Stops:** Stop-Loss bei 2×ATR, Take-Profit bei 2R (Chance/Risiko 2:1)
- **Sizing:** Pro Trade wird max. 1 % des Kapitals riskiert; die Positionsgröße
  ergibt sich aus dem Stop-Abstand. Leverage ist hart auf 3× gedeckelt.
- **Circuit Breaker:** Bei −5 % Tagesverlust schließt der Bot alles und pausiert.

Alle Parameter in [`config.yaml`](config.yaml).

## Setup

```bash
cd trading-bot
pip install -r requirements.txt
```

## 1. Backtest (Pflicht vor allem anderen)

```bash
python backtest.py                # Coin/Intervall aus config.yaml, Mainnet-Daten
python backtest.py --candles 3000 # mehr Historie
python backtest.py --csv data.csv # eigene OHLCV-Daten
```

Liefert Win-Rate, Profit Factor, Max Drawdown, Sharpe usw. Konservative
Annahmen: Einstieg erst zur nächsten Candle, Stop-Loss wird vor Take-Profit
geprüft, Taker-Fees + Slippage auf jedem Fill.

## 2. Dry-Run (Standard)

```bash
python run_bot.py
```

Mit den Default-Einstellungen (`dry_run: true`, `network: testnet`) werden
Signale nur geloggt – keine Keys nötig, kein Risiko.

## 3. Testnet mit echten Orders

1. Testnet-Wallet auf <https://app.hyperliquid-testnet.xyz> anlegen, Spielgeld holen
2. `.env.example` → `.env` kopieren, API-Wallet-Key eintragen
3. In `config.yaml`: `dry_run: false` (Netzwerk bleibt `testnet`)
4. `python run_bot.py`

## 4. Mainnet (echtes Geld)

Erst wenn Backtest **und** mehrwöchiger Testnet-Lauf überzeugen:

- `network: mainnet`, `dry_run: false`
- Start nur mit explizitem Flag: `python run_bot.py --i-know-what-im-doing`
- **Empfehlung:** API-Wallet (Agent Wallet) statt Haupt-Wallet-Key verwenden,
  klein anfangen, `max_leverage` bei 2–3 lassen.

## Copy-Trading: profitable Trader finden und spiegeln

Hyperliquid ist vollständig transparent - jede Wallet, jede Position, jeder
Fill ist öffentlich. Das nutzt das Copy-Trading-Modul in drei Stufen:

### 1. Trader finden und bewerten

```bash
python analyze_traders.py                       # Leaderboard -> Tiefenanalyse
python analyze_traders.py --address 0xabc...    # bestimmte Wallets prüfen
```

Das Leaderboard liefert nur Kandidaten - die eigentliche Bewertung passiert
auf der **Fill-Historie** jedes Wallets (30 Tage, konfigurierbar):

| Dimension | Metrik | Gewicht |
|---|---|---|
| Profitabilität | Netto-ROI (nach Fees) | 30 % |
| Treffsicherheit | Profit Factor | 25 % |
| Risiko | Max Drawdown der PnL-Kurve | 25 % |
| Konsistenz | Anteil profitabler Tage | 20 % |

Zusätzlich skaliert die Stichprobengröße den Score: Ein Lucky Punch mit 7
Trades kann einen konsistenten Trader mit 60 Trades nie schlagen. Das
Ergebnis landet in `leaders.json` (prüfen und ggf. editieren!).

#### Top-1%-Funnel und LARP-Filter

Bevor überhaupt gescort wird, läuft ein mehrstufiger Funnel:

```
gesamtes Leaderboard (Tausende Wallets)
  -> Basisfilter: Mindest-Konto, Mindest-Volumen, Monat UND Woche profitabel
  -> Top 1% nach Monats-ROI (config: analysis.top_percent)
  -> Tiefenanalyse der Fill-Historie (max. top_n Wallets)
  -> LARP-Filter: harte K.O.-Gates
  -> Score-Ranking -> Top 3 nach leaders.json
```

Der **LARP-Filter** (`bot/copytrade/larp.py`) sortiert Blender aus, die auf
Leaderboards typischerweise oben stehen, aber keinen reproduzierbaren Edge
haben. Jedes Gate ist ein hartes K.O. mit Begründung im Log:

| Gate | Default | erkennt |
|---|---|---|
| `min_round_trips` | 30 | zu wenig Trades, Zufall nicht ausschließbar |
| `min_active_days` | 10 | Eintagsfliegen |
| `max_single_trade_share` | 40 % | Lucky Punch: ein Trade trägt den PnL |
| `min_profitable_week_share` | 60 % | eine gute Woche, drei schlechte |
| `max_drawdown` | 25 % | Überhebelung (irgendwann kommt die Null) |
| `min_median_holding_minutes` | 30 | Scalper - unser Copy-Lag frisst deren Edge |

Dafür rekonstruiert der Analyzer aus den Fills komplette **Round-Trips**
(Position 0 → offen → 0) inklusive Haltedauer; Positionen, die schon vor dem
Analysefenster offen waren, werden verworfen statt falsch gezählt.

### 2. Kopieren (Reconciliation statt Event-Kopie)

```bash
python copy_bot.py
```

Der Bot leitet aus den aktuellen Leader-Positionen ein **Ziel-Portfolio** ab
und gleicht das eigene Konto kontinuierlich dagegen ab:

```
Ziel je Coin = Σ (Leader-Exposure × Leader-Gewicht) × copy_ratio × eigene Equity
```

Vorteile gegenüber naivem "Order nachmachen": verpasste Polls und Neustarts
korrigieren sich selbst, mehrere Leader im selben Coin werden sauber
aggregiert (entgegengesetzte Positionen neutralisieren sich), und ein
Rebalance-Schwellwert verhindert, dass Fees das Konto auffressen.

### 3. Eigene Risiko-Caps (immer aktiv, egal was der Leader tut)

- `copy_ratio`: nur halb so aggressiv allokieren wie der Leader (Default 0.5)
- `max_alloc_per_coin`: max. 25 % der Equity pro Coin
- `max_leverage`: Gesamt-Exposure hart gedeckelt (Ziel-Portfolio wird skaliert)
- Tagesverlust-**Circuit-Breaker** schließt alles und pausiert den Bot

> Auch hier gilt: `dry_run: true` ist Default. Die Leader-Daten kommen immer
> vom Mainnet, die eigenen Orders gehen je nach `network` auf Testnet/Mainnet.

```bash
python tests/test_copytrade.py    # Unit-Tests der Copy-Logik
python tests/test_larp_news.py    # Unit-Tests LARP-Filter + News/Schock
python tests/test_autopilot.py    # Unit-Tests Leader-Rotation + KI-Merge
```

## Autopilot: Vollautomatik mit Leader-Rotation

`bot/autopilot.py` orchestriert alles in einem Daemon:

1. **Analyse beim Start** und danach alle `autopilot.reanalyze_hours` (24h):
   kompletter Funnel inkl. LARP-Filter
2. **Automatische Rotation**: Leader unter `min_keep_score` fliegen raus,
   die Plätze füllen die besten Neuen. Bestehende Leader bekommen einen
   kleinen Bestandsschutz-Bonus (+5), damit Gleichstand nicht zu sinnlosem
   Portfolio-Umbau führt. Leader, die aus dem Top-1% verschwinden, werden
   nicht blind weiterkopiert.
3. **Copy-Loop + MarketGuard** wie gehabt, plus `runtime/status.json` als
   Live-Zustand für das Web-Frontend.

## Paper-Trading-Modus (Default)

`dry_run: true` ist seit dem Paper-Broker (`bot/paper.py`) ein vollwertiger
Simulationsmodus statt reinem Signal-Logging:

- **Simulierte Fills** zum Mid-Preis inkl. Taker-Fee, Positions-Tracking mit
  Durchschnitts-Entry, realisiertem und unrealisiertem PnL
- **Persistenz:** `runtime/paper_state.json` übersteht Neustarts
- **Alles läuft wie live:** Equity-Kurve, Circuit Breaker, Leader-Tracking
  und Telegram-Alerts arbeiten auf dem simulierten Konto
- **Reset:** Button im Dashboard oder `POST /api/paper/reset`
  (löscht Paper-Konto, Equity-Verlauf und Journal)

Damit ist der Paper-Test aussagekräftig: Was der Bot im Paper-Modus macht,
würde er mit echtem Geld genauso machen - nur die Fills sind idealisiert
(kein Slippage über den Mid-Preis hinaus, keine Teilausführungen).

## Trade-Journal

Jede Entscheidung landet in `runtime/trades.jsonl` und im Aktivitäts-Feed
des Dashboards: ausgeführte Orders, **Validator-Vetos mit Begründung**,
Leader-Rotationen, Risk-Off-Glattstellungen und Circuit-Breaker-Events.
Nach dem Paper-Test lässt sich damit beantworten, ob die Filter PnL retten
oder nur Trades kosten.

## Preflight-Check

```bash
python doctor.py            # alle Checks
python doctor.py --notify   # zusätzlich Telegram-Testnachricht
```

Prüft Pflicht (Dependencies, Config, Unit-Tests, Hyperliquid-API,
Leaderboard) und Optionales (News-Feeds, Konvergenz-Quellen, Claude-Key,
Telegram, Paper-Konto-Zustand). Exit-Code 0 = startklar.

## Zwei-Bot-Prinzip: der Trade-Validator (Bot 2)

Jeder Einstieg braucht zwei unabhängige Ja-Stimmen:

```
Bot 1 (Copy-Engine)        Bot 2 (Validator)
"Leader sind long BTC"  →  prüft Markttechnik:        →  beide OK? → Order
                           · EMA(20/50)-Trend 15m + 1h
                           · Momentum (Preis vs EMA20)
                           · RSI-Extreme = hartes Veto
                           · optional: Claude-Zweitmeinung
```

Drei bewusste Design-Entscheidungen (`bot/validator.py`):

1. **Asymmetrie:** Der Validator blockt nur Exposure-*Erhöhungen*.
   Reduzieren und Schließen läuft immer durch - Risikoabbau braucht keine
   Genehmigung, sonst sitzt man im Crash auf einer Position fest.
2. **Fail-closed:** Ist der Validator nicht verfügbar (API-Ausfall), wird der
   Einstieg konservativ abgelehnt - lieber einen Trade verpassen als blind rein.
3. **Score statt Alles-oder-Nichts:** 2 von 3 Technik-Checks müssen bestehen
   (konfigurierbar); RSI-Extreme (>75 Long / <25 Short) sind ein hartes Veto.

Mit `validation.llm_enabled: true` wird **Claude zur dritten Stimme**: Er
bekommt die kompakten Marktdaten (EMAs, RSI, ATR%, Trend-Score) plus den
Trade-Vorschlag und darf nur vetoen, nicht selbst Trades vorschlagen.

> Ehrliche Einordnung: EMA/RSI sind bewährte Heuristiken, keine
> "wissenschaftlich bewiesenen" Edges. Der Wert des Validators liegt im
> Filtern offensichtlich schlechter Einstiege (Long ins fallende Messer) -
> der Preis dafür sind weniger Trades und etwas spätere Einstiege.

## Multi-Exchange-Konvergenz (Binance, OKX, Bybit)

**Realitätscheck:** Das alte Binance-Leaderboard mit einzelnen Trader-Positionen
wurde von Binance abgeschaltet - einzelne Fremd-Wallets lassen sich dort nicht
mehr verfolgen oder LARP-filtern. Der LARP-Filter funktioniert nur auf
Hyperliquid, wo jede Wallet on-chain transparent ist. Was es offiziell gibt:
**aggregierte Top-Trader-Positionierung** (Long/Short-Ratios) von Binance
(Top-Trader Position Ratio), OKX (Rubik) und Bybit (Account Ratio).

Genau das nutzt `bot/convergence.py`: Die LARP-gefilterten Hyperliquid-Leader
bleiben die einzige Trade-Quelle; die externen Ratios sind ein **Verstärker**:

| Externe Top-Trader vs. unsere Leader | Positionsgröße |
|---|---|
| Übereinstimmung (z.B. beide long) | bis **1.25×** (skaliert mit Stärke) |
| Neutral / keine Daten | 1.0× (kein Einfluss) |
| Klarer Widerspruch | **0.4×** gedämpft |

Konvergenz eröffnet **nie** eigene Positionen, und alle Caps (Coin-Limit,
Gesamt-Leverage) greifen auch nach dem Boost. Konfiguration: `convergence`-Block.

## Day-Trading-Profil

`config.yaml` ist auf Day-Trading kalibriert - **maximale Rewards bedeutet
maximales Risiko**, deshalb bleiben die Schutzmechanismen unangetastet:

- **Leader-Auswahl:** mediane Haltedauer 30 Min - 12 h (LARP-Gates
  `min/max_median_holding_minutes`) - Scalper raus (Copy-Lag), Swing-Trader
  raus (kein Day-Trading), Analysefenster 21 Tage
- **Tempo:** Polling 10 s, Leader-Rotation alle 6 h statt 24 h
- **Aggressivität:** `copy_ratio 0.6`, `max_leverage 4` (statt 0.5 / 3×)
- **Unverändert:** Circuit Breaker −5 %/Tag, Coin-Cap 25 %, RISK_OFF-Glattstellung

## Telegram-Alerts, Equity-Kurve, Leader-Tracking

- **Telegram** (`bot/notify.py`): `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in
  `.env` → Alerts bei Start/Stop, Risiko-Level-Wechsel, Leader-Rotation und
  Circuit Breaker. Ohne Variablen still deaktiviert.
- **Equity-Kurve:** der Autopilot schreibt minütlich nach
  `runtime/history.jsonl`; das Dashboard rendert den Verlauf als Chart.
- **Leader-Performance:** Spalte „seit Kopie" im Dashboard zeigt die gemessene
  ROI jedes Leaders ab dem Moment der Aufnahme (equity-basiert; Ein-/Aus-
  zahlungen des Leaders können den Wert verfälschen).

## Marktüberwachung: News-Analyse + Schock-Detektor

Beide Bots fragen pro Tick den `MarketGuard` ab, der zwei Verteidigungslinien
kombiniert:

| Level | Bedeutung | Trendfolge-Bot | Copy-Bot |
|---|---|---|---|
| `NORMAL` | alles ruhig | handelt normal | handelt normal |
| `CAUTION` | News-Lage angespannt | keine neuen Einstiege | nur Exposure reduzieren |
| `RISK_OFF` | Schock/kritische News | Position sofort schließen | Portfolio glattstellen |

### Schock-Detektor (die schnelle Linie)

**Wichtig zu verstehen:** Wenn Trump etwas postet und der Markt crasht, sieht
man das in den Preisdaten *schneller* als in jeder bezahlbaren News-API - die
HFT-Firmen haben die News längst gehandelt. Der Detektor (`bot/news/shock.py`)
überwacht deshalb die 1-Minuten-Candles direkt:

- **Absoluter Move:** ±2,5 % in 5 Minuten → `RISK_OFF` mit 30 Min. Cooldown
- **Vola-Spike:** Kurzfrist-Volatilität 4× über der Stunden-Basis → `RISK_OFF`

Das reagiert in Sekunden, unabhängig davon, *wer* den Crash ausgelöst hat.

### News-Sentiment (die Kontext-Linie)

`bot/news/sentiment.py` bewertet Schlagzeilen regelbasiert (0-10): Kriegs-,
Hack-, Insolvenz- und Depeg-Meldungen scoren kritisch; Zölle, Verbote,
SEC-Klagen und Zinsentscheide hoch. Aussagen von Trump/Fed/SEC/Whitehouse
bekommen einen 1,5×-Boost. Der aggregierte Score zerfällt mit konfigurierbarer
Halbwertszeit, damit alte Schlagzeilen den Bot nicht ewig blockieren.

### Claude-KI-Klassifizierung (allgemeine Protection, optional)

Keywords decken bekannte Muster ab - aber "Fed schedules unscheduled sunday
meeting" triggert kein Keyword und ist trotzdem hochriskant. Mit
`news.llm_enabled: true` (+ `ANTHROPIC_API_KEY` in `.env`) bewertet **Claude
jede neue Schlagzeile** zusätzlich auf einer 0-10-Risikoskala; der finale
Score ist das **Maximum** beider Bewertungen - die wachsamere Stimme gewinnt.
Fällt die API aus, läuft die Keyword-Engine unverändert weiter (kein Single
Point of Failure). Modell konfigurierbar (`news.llm_model`), Schlagzeilen
werden gebatcht (25/Request), um Kosten zu minimieren.

```bash
python news_monitor.py            # News-Pipeline isoliert testen
python news_monitor.py --watch    # Dauerbetrieb
```

### Zur Twitter/X-Frage

Die X-API ist als Schock-Frühwarnsystem leider die schlechteste Option:
Der Echtzeit-**Filtered Stream existiert erst im Pro-Tier (~5.000 USD/Monat)**;
der Basic-Tier (~200 USD/Monat) erlaubt nur Polling der Search-API - damit ist
man genauso langsam wie mit kostenlosen RSS-Feeds. Dazu postet Trump primär
auf Truth Social, nicht auf X. Der Adapter ist trotzdem eingebaut
(`news.twitter: true` + `TWITTER_BEARER_TOKEN` in `.env`), falls du den
Zugang ohnehin hast. Empfohlene Quellen-Kombi: **Schock-Detektor (Sekunden) +
RSS/CryptoPanic (Kontext, kostenlos)**.

## Architektur

```
trading-bot/
├── config.yaml          # alle Parameter (Strategie, Risiko, Copy-Trading)
├── doctor.py            # Preflight-Check vor dem Start
├── server.py            # Web-UI: Wallet-Connect + Autopilot-Steuerung
├── autopilot.py         # CLI: Autopilot headless
├── static/index.html    # Dashboard (MetaMask, Status, Leader, Positionen)
├── backtest.py          # CLI: Backtest der Trendfolge-Strategie
├── run_bot.py           # CLI: Trendfolge-Bot (Live/Dry-Run)
├── analyze_traders.py   # CLI: Trader-Discovery + LARP-Filter + Scoring
├── copy_bot.py          # CLI: Copy-Trading-Bot (Live/Dry-Run)
├── news_monitor.py      # CLI: News-Pipeline isoliert testen
├── tests/
│   ├── test_copytrade.py
│   ├── test_larp_news.py
│   └── test_autopilot.py
└── bot/
    ├── config.py        # Config + Credentials laden/validieren
    ├── indicators.py    # EMA, RSI (Wilder), ATR
    ├── strategy.py      # Signal-Logik (Crossover + RSI-Filter)
    ├── risk.py          # Positionsgröße, SL/TP, Circuit Breaker
    ├── backtester.py    # Event-basierter Backtest mit Fees/Slippage
    ├── exchange.py      # Hyperliquid-SDK-Wrapper (Candles, Orders, Account)
    ├── trader.py        # Trendfolge-Live-Loop
    ├── copytrade/
    │   ├── leaderboard.py  # Top-1%-Funnel über das öffentliche Leaderboard
    │   ├── analyzer.py     # Fill-Historie -> Round-Trips, Metriken, Score
    │   ├── larp.py         # harte K.O.-Gates gegen Blender
    │   ├── tracker.py      # Snapshots der Leader-Positionen
    │   └── copier.py       # Ziel-Portfolio + Rebalancing mit Risiko-Caps
    ├── paper.py            # Paper-Broker: simuliertes Konto mit Persistenz
    ├── journal.py          # Trade-Journal (Orders, Vetos, Rotationen)
    ├── validator.py        # Bot 2: prüft jeden Einstieg (EMA/RSI + Claude-Option)
    ├── convergence.py      # Top-Trader-Ratios Binance/OKX/Bybit als Verstärker
    ├── notify.py           # Telegram-Alerts
    ├── news/
    │   ├── sources.py      # RSS, CryptoPanic, X/Twitter (optional)
    │   ├── sentiment.py    # regelbasiertes Risiko-Scoring mit Zeit-Zerfall
    │   ├── llm.py          # Claude-KI-Klassifizierung (optional)
    │   ├── shock.py        # Preis-Schock-Detektor auf 1m-Candles
    │   └── guard.py        # kombiniert alles -> NORMAL/CAUTION/RISK_OFF
    └── autopilot.py        # Vollautomatik: Analyse, Rotation, Copy, Status
```
