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
    timeout: int = 30,
) -> list[Candidate]:
    """Holt das Leaderboard und filtert plausible Copy-Kandidaten.

    Filter-Rationale:
    - min_account_value: zu kleine Konten sind oft Lucky Punches / Wegwerf-Wallets
    - min_volume: ohne Volumen keine statistische Aussagekraft
    - Sortierung nach Monats-ROI statt absolutem PnL, damit nicht nur Wale oben stehen
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
        if not month:
            continue
        acct = float(row.get("accountValue", 0))
        vlm = float(month.get("vlm", 0))
        pnl = float(month.get("pnl", 0))
        roi = float(month.get("roi", 0))
        if acct < min_account_value or vlm < min_volume or pnl <= 0:
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
    log.info("%d Kandidaten nach Filterung, nehme Top %d", len(candidates), top_n)
    return candidates[:top_n]
