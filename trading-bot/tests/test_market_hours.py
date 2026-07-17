"""Unit-Tests für die US-Börsen-Worldclock (bot/market_hours.py)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.market_hours import market_holidays, us_market_open


def utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_open_within_regular_session_summer():
    # 2026-07-15 Mittwoch, EDT (UTC-4): 9:30 ET = 13:30 UTC, 16:00 ET = 20:00 UTC
    assert us_market_open(utc(2026, 7, 15, 13, 30)) is True    # 9:30 ET Eröffnung
    assert us_market_open(utc(2026, 7, 15, 17, 0)) is True     # 13:00 ET mitten drin
    assert us_market_open(utc(2026, 7, 15, 19, 59)) is True    # 15:59 ET


def test_closed_outside_session():
    assert us_market_open(utc(2026, 7, 15, 13, 29)) is False   # 9:29 ET vor Eröffnung
    assert us_market_open(utc(2026, 7, 15, 20, 0)) is False    # 16:00 ET Schluss (exklusiv)
    assert us_market_open(utc(2026, 7, 15, 23, 0)) is False    # 19:00 ET Feierabend


def test_weekend_closed():
    assert us_market_open(utc(2026, 7, 18, 17, 0)) is False    # Samstag
    assert us_market_open(utc(2026, 7, 19, 17, 0)) is False    # Sonntag


def test_dst_boundary_winter_uses_est():
    # Januar = EST (UTC-5): 9:30 ET = 14:30 UTC. 13:30 UTC wäre erst 8:30 ET.
    assert us_market_open(utc(2026, 1, 7, 14, 30)) is True     # 9:30 EST offen
    assert us_market_open(utc(2026, 1, 7, 13, 30)) is False    # 8:30 EST noch zu


def test_holiday_closed():
    # 2026: Independence Day (4.7. = Samstag) -> beobachtet Fr 3.7.
    assert us_market_open(utc(2026, 7, 3, 17, 0)) is False
    # Christmas 2026 = Freitag
    assert us_market_open(utc(2026, 12, 25, 17, 0)) is False


def test_fails_safe_closed_without_tzdata():
    """Deep-Dive-Fund: fehlt tzdata (_ET=None), behandelte _to_et() UTC bisher
    unverändert als 'ET' - der Gong feuerte 4-5h verschoben, Aktien-Perps
    wurden zur falschen Zeit auf stale Kursen gehandelt. Jetzt fail-safe: ohne
    verlässliche Zeitzone gilt der Markt als geschlossen, egal welche Uhrzeit."""
    import bot.market_hours as mh

    orig = mh._ET
    try:
        mh._ET = None
        # Eine Zeit, die bei korrekter ET-Umrechnung eindeutig 'offen' wäre
        # (Mi 14:00 UTC = 10:00 EDT im Juli) - mit _ET=None MUSS trotzdem False gelten.
        assert mh.us_market_open(utc(2026, 7, 15, 14, 0)) is False
    finally:
        mh._ET = orig


def test_holidays_2026_complete_and_correct():
    from datetime import date

    h = market_holidays(2026)
    assert date(2026, 1, 1) in h       # New Year
    assert date(2026, 1, 19) in h      # MLK (3. Mo)
    assert date(2026, 2, 16) in h      # Presidents' (3. Mo)
    assert date(2026, 4, 3) in h       # Karfreitag
    assert date(2026, 5, 25) in h      # Memorial (letzter Mo)
    assert date(2026, 6, 19) in h      # Juneteenth
    assert date(2026, 7, 3) in h       # Independence beobachtet
    assert date(2026, 9, 7) in h       # Labor (1. Mo)
    assert date(2026, 11, 26) in h     # Thanksgiving (4. Do)
    assert date(2026, 12, 25) in h     # Christmas
    assert len(h) == 10


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")
