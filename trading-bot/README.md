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

## Architektur

```
trading-bot/
├── config.yaml        # alle Parameter (Strategie, Risiko, Netzwerk)
├── backtest.py        # CLI: Backtest auf historischen Daten
├── run_bot.py         # CLI: Live-/Dry-Run-Betrieb
└── bot/
    ├── config.py      # Config + Credentials laden/validieren
    ├── indicators.py  # EMA, RSI (Wilder), ATR
    ├── strategy.py    # Signal-Logik (Crossover + RSI-Filter)
    ├── risk.py        # Positionsgröße, SL/TP, Circuit Breaker
    ├── backtester.py  # Event-basierter Backtest mit Fees/Slippage
    ├── exchange.py    # Hyperliquid-SDK-Wrapper (Candles, Orders, Account)
    └── trader.py      # Live-Loop: pollt, prüft Stops, führt Signale aus
```
