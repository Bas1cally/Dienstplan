"""Trader-Discovery über das öffentliche Hyperliquid-Leaderboard.

Nutzt den Stats-Endpunkt, der auch das Web-Frontend speist. Er ist nicht Teil
der offiziellen API und kann sich ändern - deshalb ist alles dahinter optional:
Kandidaten können auch manuell als Adressliste übergeben werden.
"""

import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


@dataclass
class Candidate:
    address: str
    account_value: float
    month_pnl: float
    month_roi: float
    month_volume: float


def fetch_candidates(
    min_account_value: float,
    min_volume: float,
    top_n: int,
    top_percent: float = 1.0,
    timeout: int = 30,
) -> list[Candidate]:
    """Holt das Leaderboard und destilliert das Top-Perzentil als Kandidaten.

    Funnel:
      1. Basisfilter: Mindest-Kontogröße (gegen Wegwerf-Wallets), Mindest-Volumen
         (statistische Aussagekraft), Monat UND Woche profitabel (gegen Trader,
         die gerade implodieren)
      2. Perzentil-Schnitt: nur das Top-`top_percent`% nach Monats-ROI
      3. Deckel `top_n` für die teure Tiefenanalyse (Fill-Historie je Wallet)

    ROI statt absolutem PnL, damit nicht nur Wale oben stehen. Der eigentliche
    LARP-Check passiert danach in der Tiefenanalyse (larp.py).
    """
    log.info("Lade Leaderboard von %s ...", LEADERBOARD_URL)
    resp = requests.get(LEADERBOARD_URL, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json()["leaderboardRows"]
    log.info("%d Trader im Leaderboard", len(rows))

    candidates = []
    for row in rows:
        perf = dict(row.get("windowPerformances", []))
        month = perf.get("month")
        week = perf.get("week")
        if not month:
            continue
        acct = float(row.get("accountValue", 0))
        vlm = float(month.get("vlm", 0))
        pnl = float(month.get("pnl", 0))
        roi = float(month.get("roi", 0))
        week_pnl = float(week.get("pnl", 0)) if week else 0.0
        if acct < min_account_value or vlm < min_volume:
            continue
        if pnl <= 0 or week_pnl <= 0:
            continue
        candidates.append(
            Candidate(
                address=row["ethAddress"],
                account_value=acct,
                month_pnl=pnl,
                month_roi=roi,
                month_volume=vlm,
            )
        )

    candidates.sort(key=lambda c: c.month_roi, reverse=True)
    cut = max(1, int(len(candidates) * top_percent / 100))
    top = candidates[:cut][:top_n]
    log.info("%d Kandidaten nach Basisfiltern -> Top %.1f%% = %d -> Tiefenanalyse für %d",
             len(candidates), top_percent, cut, len(top))
    return top
