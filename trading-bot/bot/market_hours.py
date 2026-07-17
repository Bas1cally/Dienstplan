"""Worldclock für die US-Aktienbörse (NASDAQ/NYSE).

Der Quest-Bot soll Aktien-Perps (Builder-DEX, 'xyz:...') NUR handeln, während
der zugrundeliegende Aktienmarkt wirklich offen ist - außerhalb der Handels-
zeiten ist der Perp-Kurs stale/spekulativ ('Spekulanten-Trades', Nutzer). Diese
Uhr sagt, ob gerade reguläre Sitzung ist: 9:30-16:00 ET, Mo-Fr, kein Feiertag.

Alles über die echte Zeitzone America/New_York gerechnet, damit der 'Gong'
ganzjährig zur ECHTEN Glocke feuert (Sommer-/Winterzeit inklusive), nicht zu
einer festen UTC-Zeit. Reine Zeit-Logik, injizierbares `now` fürs Testen.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # Fallback, falls tzdata auf dem Host fehlt
    _ET = None
    # Deep-Dive-Fund: OHNE tzdata behandelte _to_et() bisher UTC unveraendert
    # als "ET" - der Boersen-Gong feuerte 4-5h verschoben (je nach Sommer-/
    # Winterzeit), und Aktien-Perps wurden zur FALSCHEN Zeit auf stale Kursen
    # gehandelt - genau das, was diese Uhr verhindern soll. Fail-SAFE statt
    # falsch: ohne verlaessliche Zeitzone gilt der Markt als geschlossen (der
    # Aktien-Basket bleibt einfach aus, statt zur falschen Zeit zu handeln).
    log.error("market_hours: tzdata/zoneinfo für America/New_York nicht "
              "verfügbar - US-Börse gilt bis zur Behebung als GESCHLOSSEN "
              "(Aktien-Basket bleibt aus, statt zur falschen Zeit zu handeln).")

_OPEN = time(9, 30)
_CLOSE = time(16, 0)


def _easter(year: int) -> date:
    """Oster-Sonntag (Anonymous Gregorian) - für Karfreitag (Börse zu)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-ter <weekday> (0=Mo) im Monat, z.B. 3. Montag im Januar (MLK)."""
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Letzter <weekday> im Monat, z.B. letzter Montag im Mai (Memorial Day)."""
    d = date(year, month, 28) + timedelta(days=4)  # sicher im Folgemonat
    d = d.replace(day=1) - timedelta(days=1)        # letzter Tag des Zielmonats
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Fällt ein Feiertag auf Sa -> Börse zu am Fr davor; auf So -> Mo danach
    (US-Standard-Regel; das Wochenende selbst ist ohnehin geschlossen)."""
    if d.weekday() == 5:      # Samstag
        return d - timedelta(days=1)
    if d.weekday() == 6:      # Sonntag
        return d + timedelta(days=1)
    return d


def market_holidays(year: int) -> set[date]:
    """Tage, an denen die US-Aktienbörse GESCHLOSSEN ist (beobachtete Daten).
    Ganztägige Schließungen; die seltenen Früh-Schluss-Tage (13:00, z.B. Tag
    nach Thanksgiving) sind bewusst NICHT enthalten - dort handeln wir dann
    ein paar Stunden länger, was harmlos ist (die 2h-Regel/Leader-Exit fangen
    eine offene Aktien-Position ohnehin ab)."""
    h: set[date] = set()
    h.add(_observed(date(year, 1, 1)))                  # New Year's Day
    h.add(_nth_weekday(year, 1, 0, 3))                  # MLK (3. Mo Jan)
    h.add(_nth_weekday(year, 2, 0, 3))                  # Presidents' (3. Mo Feb)
    h.add(_easter(year) - timedelta(days=2))           # Karfreitag
    h.add(_last_weekday(year, 5, 0))                    # Memorial (letzter Mo Mai)
    h.add(_observed(date(year, 6, 19)))                 # Juneteenth
    h.add(_observed(date(year, 7, 4)))                  # Independence Day
    h.add(_nth_weekday(year, 9, 0, 1))                  # Labor (1. Mo Sep)
    h.add(_nth_weekday(year, 11, 3, 4))                 # Thanksgiving (4. Do Nov)
    h.add(_observed(date(year, 12, 25)))                # Christmas
    return h


def _to_et(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_ET) if _ET is not None else now


def us_market_open(now: datetime | None = None) -> bool:
    """Ist die reguläre US-Aktien-Sitzung gerade offen? (9:30-16:00 ET, Mo-Fr,
    kein Feiertag). `now` als aware/naive UTC-datetime injizierbar fürs Testen."""
    if _ET is None:
        return False   # fail-safe ohne verlässliche Zeitzone, siehe Modul-Header
    et = _to_et(now)
    if et.weekday() >= 5:                       # Wochenende
        return False
    if et.date() in market_holidays(et.year):   # Feiertag
        return False
    return _OPEN <= et.time() < _CLOSE
