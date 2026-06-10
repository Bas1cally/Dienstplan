# Hyperliquid Trading Bot

Trendfolge-Bot für Hyperliquid Perpetuals mit striktem Risikomanagement,
Backtesting und mehrstufigem Sicherheitskonzept.

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
```

## Architektur

```
trading-bot/
├── config.yaml          # alle Parameter (Strategie, Risiko, Copy-Trading)
├── backtest.py          # CLI: Backtest der Trendfolge-Strategie
├── run_bot.py           # CLI: Trendfolge-Bot (Live/Dry-Run)
├── analyze_traders.py   # CLI: Trader-Discovery + Scoring -> leaders.json
├── copy_bot.py          # CLI: Copy-Trading-Bot (Live/Dry-Run)
├── tests/
│   └── test_copytrade.py
└── bot/
    ├── config.py        # Config + Credentials laden/validieren
    ├── indicators.py    # EMA, RSI (Wilder), ATR
    ├── strategy.py      # Signal-Logik (Crossover + RSI-Filter)
    ├── risk.py          # Positionsgröße, SL/TP, Circuit Breaker
    ├── backtester.py    # Event-basierter Backtest mit Fees/Slippage
    ├── exchange.py      # Hyperliquid-SDK-Wrapper (Candles, Orders, Account)
    ├── trader.py        # Trendfolge-Live-Loop
    └── copytrade/
        ├── leaderboard.py  # Kandidaten vom öffentlichen Leaderboard
        ├── analyzer.py     # Fill-Historie -> Metriken + Score
        ├── tracker.py      # Snapshots der Leader-Positionen
        └── copier.py       # Ziel-Portfolio + Rebalancing mit Risiko-Caps
```
