#!/usr/bin/env python3
"""
Dienstplan Excel Generator

Usage:
  python3 generate.py                    # Normaler Modus: liest .xlsm, erzeugt gefüllten Plan
  python3 generate.py --year 2027        # Für anderes Jahr
  python3 generate.py --leer             # Leerer Plan (nur Struktur + Dropdowns)
  python3 generate.py --leer --year 2027 # Leerer Plan für 2027
  python3 generate.py --archiv           # Backup des aktuellen Plans vor Überschreiben
"""

import argparse
import calendar
import datetime
import json
import os
import shutil

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.page import PageMargins

# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "mitarbeiter.json")
ARCHIV_DIR = os.path.join(BASE_DIR, "archiv")

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

SEKRETARIAT_MA = {"Schmidt", "Radimersky"}
CRITICAL_SHIFTS = ["FI", "SI", "NI"]
DEFAULT_URLAUB_TAGE = 30

# ---------------------------------------------------------------------------
# Dynamische Feiertage (BW) – funktioniert für jedes Jahr
# ---------------------------------------------------------------------------
def _ostersonntag(year):
    """Gaußsche Osterformel."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)

def feiertage_bw(year):
    """Alle Feiertage in Baden-Württemberg für ein gegebenes Jahr."""
    ostern = _ostersonntag(year)
    return {
        datetime.date(year, 1, 1): "Neujahr",
        datetime.date(year, 1, 6): "Hl. 3 Könige",
        ostern - datetime.timedelta(days=2): "Karfreitag",
        ostern + datetime.timedelta(days=1): "Ostermontag",
        datetime.date(year, 5, 1): "Tag d. Arbeit",
        ostern + datetime.timedelta(days=39): "Chr. Himmelf.",
        ostern + datetime.timedelta(days=50): "Pfingstmontag",
        ostern + datetime.timedelta(days=60): "Fronleichnam",
        datetime.date(year, 8, 15): "Mariä Himmelf.",
        datetime.date(year, 10, 3): "Tag d. Einheit",
        datetime.date(year, 11, 1): "Allerheiligen",
        datetime.date(year, 12, 25): "1. Weihnacht",
        datetime.date(year, 12, 26): "2. Weihnacht",
    }

def schulferien_bw(year):
    """Schulferien BW – Schätzung basierend auf typischen Terminen.
    Für exakte Termine: manuell in der Ausgabe-Datei anpassen."""
    ostern = _ostersonntag(year)
    pfingsten = ostern + datetime.timedelta(days=49)
    return [
        ("Faschingsferien",
         _mo_of_week(year, 2, 3),
         _fr_of_week(year, 2, 3)),
        ("Osterferien",
         ostern - datetime.timedelta(days=1),
         ostern + datetime.timedelta(days=6)),
        ("Pfingstferien",
         pfingsten + datetime.timedelta(days=1),
         pfingsten + datetime.timedelta(days=12)),
        ("Sommerferien",
         datetime.date(year, 7, 30),
         datetime.date(year, 9, 12)),
        ("Herbstferien",
         _mo_of_week(year, 10, 4),
         _fr_of_week(year, 10, 4)),
        ("Weihnachtsferien",
         datetime.date(year, 12, 23),
         datetime.date(year + 1, 1, 6)),
    ]

def _mo_of_week(year, month, week_num):
    """Montag der N-ten Woche im Monat."""
    first = datetime.date(year, month, 1)
    days_to_monday = (7 - first.weekday()) % 7
    first_monday = first + datetime.timedelta(days=days_to_monday)
    return first_monday + datetime.timedelta(weeks=week_num - 1)

def _fr_of_week(year, month, week_num):
    mo = _mo_of_week(year, month, week_num)
    return mo + datetime.timedelta(days=4)

# Globale Variablen – werden in main() gesetzt
YEAR = 2026
FEIERTAGE = {}
SCHULFERIEN = []

# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
C_URLAUB = "92D050"
C_EH = "66FFFF"
C_KU = "B8CCE4"
C_AUSGLEICH = "D6E3BC"
C_WEEKEND = "FFC000"
C_YELLOW = "FFFF00"
C_RED = "FF0000"
C_HDR_BG = "4472C4"
C_HDR_FG = "FFFFFF"
C_ZEBRA = "F2F2F2"
C_BORDER = "B4B4B4"
C_SUM_BG = "D9E2F3"
C_FERIEN = "C6EFCE"
C_WARN_BG = "FFC7CE"
C_WARN_FG = "9C0006"

THIN_SIDE = Side("thin", C_BORDER)
THIN = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)
MEDIUM_SIDE = Side("medium", "000000")

FILL_HDR = PatternFill("solid", fgColor=C_HDR_BG)
FILL_WE = PatternFill("solid", fgColor=C_WEEKEND)
FILL_ZEBRA = PatternFill("solid", fgColor=C_ZEBRA)
FILL_FERIEN = PatternFill("solid", fgColor=C_FERIEN)
FILL_SUM = PatternFill("solid", fgColor=C_SUM_BG)
FILL_WARN = PatternFill("solid", fgColor=C_WARN_BG)
FILL_URLAUB = PatternFill("solid", fgColor=C_URLAUB)
FILL_EH = PatternFill("solid", fgColor=C_EH)
FILL_KU = PatternFill("solid", fgColor=C_KU)
FILL_AUSGLEICH = PatternFill("solid", fgColor=C_AUSGLEICH)
FILL_YELLOW = PatternFill("solid", fgColor=C_YELLOW)

FONT_TITLE = Font(name="Calibri", size=14, bold=True)
FONT_HDR = Font(name="Calibri", size=9, bold=True, color=C_HDR_FG)
FONT_HDR10 = Font(name="Calibri", size=10, bold=True, color=C_HDR_FG)
FONT_MONTH = Font(name="Calibri", size=11, bold=True)
FONT_NAME = Font(name="Calibri", size=10, bold=True)
FONT_NAME9 = Font(name="Calibri", size=9, bold=True)
FONT_CELL = Font(name="Calibri", size=9)
FONT_CELL8 = Font(name="Calibri", size=8)
FONT_CELL10 = Font(name="Calibri", size=10)
FONT_BOLD10 = Font(name="Calibri", size=10, bold=True)
FONT_BOLD9 = Font(name="Calibri", size=9, bold=True)
FONT_KW = Font(name="Calibri", size=8, italic=True, color="666666")
FONT_KW_W = Font(name="Calibri", size=8, italic=True, color=C_HDR_FG)
FONT_TAG_SP = Font(name="Calibri", size=9, bold=True, color="000000")
FONT_TAG_NR = Font(name="Calibri", size=9, bold=True, color=C_HDR_FG)
FONT_WT_SP = Font(name="Calibri", size=8, bold=True, color="000000")
FONT_WT_NR = Font(name="Calibri", size=8, color=C_HDR_FG)
FONT_WARN = Font(name="Calibri", size=7, bold=True, color=C_WARN_FG)
FONT_GRAY8 = Font(name="Calibri", size=8, italic=True, color="999999")
FONT_GRAY_SM = Font(name="Calibri", size=9, color="666666")
FONT_FERIEN = Font(name="Calibri", size=7, italic=True, color="006100")
FONT_RED = Font(name="Calibri", size=8, color=C_RED)
FONT_LEG_CODE = Font(name="Calibri", size=8, bold=True)
FONT_LEG_DESC = Font(name="Calibri", size=8)
FONT_SIG_LABEL = Font(name="Calibri", size=9, bold=True)
FONT_SIG_HINT = Font(name="Calibri", size=8, italic=True, color="999999")
FONT_SEKR = Font(name="Calibri", size=9, bold=True, italic=True, color="999999")

ALIGN_C = Alignment(horizontal="center", vertical="center")
ALIGN_CS = Alignment(horizontal="center", vertical="center", shrinkToFit=True)
ALIGN_L = Alignment(horizontal="left", vertical="center")

SHIFT_COLORS = {
    "U": (FILL_URLAUB, None), "U    alt": (FILL_URLAUB, None),
    "EH": (FILL_EH, FONT_RED), "KU": (FILL_KU, None),
    "A": (FILL_AUSGLEICH, None),
    "GT": (None, FONT_RED), "NST": (None, FONT_RED), "Fobi": (None, FONT_RED),
    "T-ZUG": (FILL_URLAUB, None), "T-ZG": (FILL_URLAUB, None),
}

LEGEND_LEFT = [
    ("U", "Urlaub", "U"),
    ("EH 08:00-17:00", "Erste Hilfe", "EH"),
    ("KU 08:30-16:30", "Kuppenheim", "KU"),
    ("A", "AU", "A"),
    ("GT", "Gleittag", "GT"),
    ("NST", "Ausgleichstag", "NST"),
    ("Fobi", "Fortbildung", "Fobi"),
    ("FI 06:00-14:00", "Frühschicht I", "FI"),
]
LEGEND_RIGHT = [
    ("FII 06:00-14:00", "Frühschicht II", "FII"),
    ("SI 14:00-22:00", "Spätschicht I", "SI"),
    ("SII 14:00-21:00", "Spätschicht II", "SII"),
    ("NI 22:00-06:00", "Nachtschicht I", "NI"),
    ("DI 08:00-15:30", "Dienst I", "DI"),
    ("DII 08:30-16:00", "Dienst II", "DII"),
    ("KT IRTAZ", "Koordination", "KT"),
    ("SD Individuell", "Sonderdienst", "SD"),
]

DROPDOWN_SHIFTS = [
    "FI", "FII", "SI", "SII", "NI", "DI", "DII",
    "KU", "EH", "SD", "GT", "NST", "KT", "Fobi", "U", "A", "T-ZUG",
]

CF_RULES = [
    ("U", PatternFill("solid", fgColor=C_URLAUB), Font(color="000000")),
    ("U    alt", PatternFill("solid", fgColor=C_URLAUB), Font(color="000000")),
    ("EH", PatternFill("solid", fgColor=C_EH), Font(color=C_RED)),
    ("KU", PatternFill("solid", fgColor=C_KU), Font(color="000000")),
    ("A", PatternFill("solid", fgColor=C_AUSGLEICH), Font(color="000000")),
    ("T-ZUG", PatternFill("solid", fgColor=C_URLAUB), Font(color="000000")),
    ("T-ZG", PatternFill("solid", fgColor=C_URLAUB), Font(color="000000")),
    ("Fobi", PatternFill("solid", fgColor="FFFFFF"), Font(color=C_RED)),
    ("GT", PatternFill("solid", fgColor="FFFFFF"), Font(color=C_RED)),
    ("NST", PatternFill("solid", fgColor="FFFFFF"), Font(color=C_RED)),
]


# ---------------------------------------------------------------------------
# MA-Config
# ---------------------------------------------------------------------------
def load_config():
    """Lade MA-Config aus JSON. Erstellt Default-Config falls nicht vorhanden."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def save_default_config(emps):
    """Speichert aktuelle MA-Liste als JSON-Config."""
    config = {
        "mitarbeiter": [
            {
                "name": e.name,
                "irtaz": e.irtaz,
                "urlaub_tage": DEFAULT_URLAUB_TAGE,
                "sekretariat": e.name in SEKRETARIAT_MA,
            }
            for e in emps
        ],
        "sekretariat_namen": list(SEKRETARIAT_MA),
        "kritische_schichten": CRITICAL_SHIFTS,
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"  Config gespeichert: {CONFIG_FILE}")

def emps_from_config(config):
    """Erstellt Employee-Objekte aus Config."""
    emps = []
    for m in config["mitarbeiter"]:
        e = Employee(m["name"], m.get("irtaz", 8.0))
        emps.append(e)
    global SEKRETARIAT_MA
    SEKRETARIAT_MA = set(config.get("sekretariat_namen", []))
    return emps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class Employee:
    def __init__(self, name, irtaz=8.0):
        self.name = name
        self.irtaz = irtaz
        self.shifts = {m: {} for m in range(12)}


class LayoutMap:
    def __init__(self):
        self.emp_rows = {}


def _is_weekend(dt):
    return dt.weekday() >= 5

def _is_feiertag(dt):
    return dt in FEIERTAGE

def _is_ferien(dt):
    for name, s, e in SCHULFERIEN:
        if s <= dt <= e:
            return name
    return None

def _arbeitstage(year, month):
    return sum(1 for d in range(1, calendar.monthrange(year, month)[1] + 1)
               if datetime.date(year, month, d).weekday() < 5
               and not _is_feiertag(datetime.date(year, month, d)))

def _samstage(year, month):
    return sum(1 for d in range(1, calendar.monthrange(year, month)[1] + 1)
               if datetime.date(year, month, d).weekday() == 5)

def _kw(dt):
    return dt.isocalendar()[1]

def _shift_style(code):
    c = str(code).strip() if code else ""
    if c in SHIFT_COLORS:
        sf, fn = SHIFT_COLORS[c]
        return sf, fn or FONT_CELL8
    if c.startswith("(") and ")" in c:
        return FILL_YELLOW, FONT_RED
    return None, FONT_CELL8

def _set(cell, value=None, font=None, fill=None, align=None, border=THIN):
    if value is not None:
        cell.value = value
    if font:
        cell.font = font
    if fill:
        cell.fill = fill
    if align:
        cell.alignment = align
    if border:
        cell.border = border
    return cell

def _outer_border(ws, r1, c1, r2, c2):
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            left = MEDIUM_SIDE if c == c1 else THIN_SIDE
            right = MEDIUM_SIDE if c == c2 else THIN_SIDE
            top = MEDIUM_SIDE if r == r1 else THIN_SIDE
            bottom = MEDIUM_SIDE if r == r2 else THIN_SIDE
            ws.cell(r, c).border = Border(left=left, right=right,
                                          top=top, bottom=bottom)

def _force_border(ws, r, c, border):
    """Border auf eine Zelle setzen, auch wenn sie in einem Merge liegt."""
    key = (r, c)
    if key not in ws._cells:
        ws._cells[key] = Cell(ws, row=r, column=c)
    ws._cells[key].border = border

def _box_border(ws, r1, c1, r2, c2):
    """Nur Außenrahmen (medium) – keine inneren Linien."""
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            top = MEDIUM_SIDE if r == r1 else None
            bottom = MEDIUM_SIDE if r == r2 else None
            left = MEDIUM_SIDE if c == c1 else None
            right = MEDIUM_SIDE if c == c2 else None
            if top or bottom or left or right:
                _force_border(ws, r, c, Border(top=top, bottom=bottom,
                                               left=left, right=right))

def _add_cond_fmt(ws, cell_range):
    for code, fill, font in CF_RULES:
        ws.conditional_formatting.add(
            cell_range,
            CellIsRule(operator="equal", formula=[f'"{code}"'],
                       fill=fill, font=font, stopIfTrue=True))

def _missing_critical(emps, mi, day):
    dt = datetime.date(YEAR, mi + 1, day)
    # Freitags keine Nachtschicht (NI) nötig
    check = [s for s in CRITICAL_SHIFTS if not (s == "NI" and dt.weekday() == 4)]
    assigned = set()
    for e in emps:
        s = e.shifts[mi].get(day)
        if s and s in check:
            assigned.add(s)
    return [s for s in check if s not in assigned]

def _feiertag_comments(ws, row, year, month):
    dim = calendar.monthrange(year, month)[1]
    for d in range(1, dim + 1):
        dt = datetime.date(year, month, d)
        if dt in FEIERTAGE:
            ws.cell(row, 1 + d).comment = Comment(
                FEIERTAGE[dt], "Emanuel Siebert", width=140, height=30)

def _ferien_ranges(year, month):
    dim = calendar.monthrange(year, month)[1]
    ranges = {}
    for d in range(1, dim + 1):
        dt = datetime.date(year, month, d)
        fname = _is_ferien(dt)
        if fname:
            if fname not in ranges:
                ranges[fname] = [d, d]
            else:
                ranges[fname][1] = d
    return ranges

def _is_special(dt):
    return _is_weekend(dt) or _is_feiertag(dt)


# ---------------------------------------------------------------------------
# Archiv
# ---------------------------------------------------------------------------
def archiv_backup(dst_file):
    """Erstellt ein Backup der bestehenden Datei mit Timestamp."""
    if not os.path.exists(dst_file):
        print(f"  Keine bestehende Datei: {dst_file} – kein Backup nötig.")
        return
    os.makedirs(ARCHIV_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.splitext(os.path.basename(dst_file))[0]
    backup = os.path.join(ARCHIV_DIR, f"{base}_{ts}.xlsx")
    shutil.copy2(dst_file, backup)
    print(f"  Archiv-Backup: {backup}")
    return backup


# ---------------------------------------------------------------------------
# Daten extrahieren (aus .xlsm)
# ---------------------------------------------------------------------------
def extract_data(src):
    print(f"Lese {src} ...")
    wb = load_workbook(src, data_only=True)
    dp = wb["Dienstplan"]
    mo = wb["Monat"]

    employees = []
    for r in range(8, 20):
        n = dp.cell(r, 2).value
        if n:
            employees.append(str(n).strip())
    print(f"  {len(employees)} MA: {', '.join(employees)}")

    irtaz = {}
    for i, emp in enumerate(employees):
        v = mo.cell(3 + i, 26).value
        irtaz[emp] = float(v) if v else 8.0

    emps = [Employee(n, irtaz.get(n, 8.0)) for n in employees]

    for months, emp_row0 in [(range(1, 7), 8), (range(7, 13), 25)]:
        col = 3
        for month in months:
            dim = calendar.monthrange(YEAR, month)[1]
            for day in range(1, dim + 1):
                for ei in range(len(employees)):
                    v = dp.cell(emp_row0 + ei, col).value
                    if v is not None:
                        emps[ei].shifts[month - 1][day] = str(v).strip()
                col += 1

    shift_types = []
    for c in range(3, 20):
        v = mo.cell(3, c).value
        if v:
            shift_types.append(str(v).strip())

    shift_hours = {}
    for r in range(3, 20):
        code = mo.cell(r, 28).value
        hrs = mo.cell(r, 29).value
        if code:
            try:
                shift_hours[str(code).strip()] = float(hrs)
            except (ValueError, TypeError):
                shift_hours[str(code).strip()] = None

    wb.close()
    total = sum(len(e.shifts[m]) for e in emps for m in range(12))
    print(f"  {total} Schichteinträge, {len(shift_types)} Typen")
    return emps, shift_types, shift_hours


# ---------------------------------------------------------------------------
# Eingabe-Sheet
# ---------------------------------------------------------------------------
def create_eingabe(ws, emps):
    print("Erstelle Eingabe-Sheet ...")
    ws.column_dimensions["A"].width = 14
    _set(ws.cell(1, 1), f"Dienstplan {YEAR}", FONT_TITLE, border=None)

    dv = DataValidation(
        type="list", formula1=f'"{",".join(DROPDOWN_SHIFTS)}"',
        allow_blank=True, showDropDown=False, showErrorMessage=True,
        errorTitle="Ungültige Schicht",
        error="Bitte wähle eine gültige Schicht aus der Liste.",
        showInputMessage=True, promptTitle="Schicht",
        prompt="Schicht auswählen oder frei eingeben")
    dv.errorStyle = "warning"
    ws.add_data_validation(dv)

    layout = LayoutMap()
    dv_ranges = []
    row = 3

    for mi in range(12):
        mn = mi + 1
        dim = calendar.monthrange(YEAR, mn)[1]
        last_cl = get_column_letter(1 + dim)
        layout.emp_rows[mi] = {}

        # Ferien-Balken
        fr = _ferien_ranges(YEAR, mn)
        if fr:
            for fname, (sd, ed) in fr.items():
                sc, ec = 1 + sd, 1 + ed
                if ec > sc:
                    ws.merge_cells(start_row=row, start_column=sc,
                                   end_row=row, end_column=ec)
                c = ws.cell(row, sc)
                c.value = fname
                c.fill = FILL_FERIEN
                c.font = FONT_FERIEN
                c.alignment = ALIGN_C
            row += 1

        ws.row_dimensions[row].height = 20
        _set(ws.cell(row, 1), MONTHS_DE[mi], FONT_MONTH, border=None)
        row += 1

        # KW
        kw_row = row
        _set(ws.cell(row, 1), "KW", FONT_KW, align=ALIGN_C, border=THIN)
        last_kw = None
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            kw = _kw(dt)
            c = ws.cell(row, 1 + d)
            c.alignment = ALIGN_C
            c.border = THIN
            if _is_special(dt):
                c.fill = FILL_WE
            if kw != last_kw:
                c.value = kw
                c.font = FONT_KW
            last_kw = kw
        ws.row_dimensions[row].height = 15
        row += 1

        # Tag
        tag_row = row
        _set(ws.cell(row, 1), "Tag", FONT_HDR, FILL_HDR, ALIGN_C)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            sp = _is_special(dt)
            _set(ws.cell(row, 1 + d), d,
                 FONT_TAG_SP if sp else FONT_TAG_NR,
                 FILL_WE if sp else FILL_HDR, ALIGN_C)
        _feiertag_comments(ws, tag_row, YEAR, mn)
        ws.row_dimensions[row].height = 18
        row += 1

        # WT
        _set(ws.cell(row, 1), "WT", FONT_HDR, FILL_HDR, ALIGN_C)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            sp = _is_special(dt)
            _set(ws.cell(row, 1 + d), WEEKDAYS_DE[dt.weekday()],
                 FONT_WT_SP if sp else FONT_WT_NR,
                 FILL_WE if sp else FILL_HDR, ALIGN_C)
        ws.row_dimensions[row].height = 18
        row += 1

        # MA-Zeilen (nur Basis-Fills – Schichtfarben rein über CF)
        ges_col = 1 + dim + 1  # Gesamt-Spalte rechts neben letztem Tag
        ges_cl = get_column_letter(ges_col)
        first_ma_row = row
        for ei, ed in enumerate(emps):
            layout.emp_rows[mi][ed.name] = row
            _set(ws.cell(row, 1), ed.name, FONT_NAME, align=ALIGN_L)
            zebra = FILL_ZEBRA if ei % 2 == 1 else None
            for d in range(1, dim + 1):
                dt = datetime.date(YEAR, mn, d)
                shift = ed.shifts[mi].get(d)
                c = ws.cell(row, 1 + d)
                c.alignment = ALIGN_CS
                c.border = THIN
                c.font = FONT_CELL8
                if shift:
                    c.value = shift
                if _is_special(dt):
                    c.fill = FILL_WE
                elif zebra:
                    c.fill = zebra
            # Gesamt-Spalte: Anzahl eingetragener Schichten
            _set(ws.cell(row, ges_col),
                 f"=COUNTA(B{row}:{last_cl}{row})",
                 FONT_BOLD9, FILL_SUM, ALIGN_C)
            ws.row_dimensions[row].height = 16
            row += 1

        data_range = f"B{first_ma_row}:{last_cl}{row - 1}"
        dv_ranges.append(data_range)
        _add_cond_fmt(ws, data_range)

        # Gesamt-Header (in Tag-/WT-Zeile)
        _set(ws.cell(tag_row, ges_col), "Ges", FONT_HDR, FILL_HDR, ALIGN_C)
        _set(ws.cell(tag_row - 1, ges_col), "", FONT_HDR, FILL_HDR, ALIGN_C)

        # Unterbesetzungs-Warnung (dynamische COUNTIF-Formeln)
        last_ma_row = row - 1
        _set(ws.cell(row, 1), "Besetzung", FONT_GRAY8, align=ALIGN_L, border=THIN)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            c = ws.cell(row, 1 + d)
            c.alignment = ALIGN_C
            c.border = THIN
            if _is_special(dt):
                c.fill = FILL_WE
                continue
            cl = get_column_letter(1 + d)
            rng = f"{cl}{first_ma_row}:{cl}{last_ma_row}"
            # Freitags keine NI nötig
            if dt.weekday() == 4:
                checks = ['COUNTIF({r},"FI")>0', 'COUNTIF({r},"SI")>0']
            else:
                checks = ['COUNTIF({r},"FI")>0', 'COUNTIF({r},"SI")>0',
                           'COUNTIF({r},"NI")>0']
            cond = ",".join(ch.format(r=rng) for ch in checks)
            c.value = f'=IF(AND({cond}),"","!")'
        # Bedingte Formatierung für die Besetzungszeile
        bes_range = f"B{row}:{last_cl}{row}"
        ws.conditional_formatting.add(
            bes_range,
            CellIsRule(operator="equal", formula=['"!"'],
                       fill=FILL_WARN, font=FONT_WARN, stopIfTrue=True))
        ws.row_dimensions[row].height = 15
        row += 1

        # Doppelbelegung-Warnung (gleiche kritische Schicht doppelt vergeben)
        _set(ws.cell(row, 1), "Doppelt", FONT_GRAY8, align=ALIGN_L, border=THIN)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            c = ws.cell(row, 1 + d)
            c.alignment = ALIGN_C
            c.border = THIN
            if _is_special(dt):
                c.fill = FILL_WE
                continue
            cl = get_column_letter(1 + d)
            rng = f"{cl}{first_ma_row}:{cl}{last_ma_row}"
            dbl = ','.join(
                f'COUNTIF({rng},"{s}")>1' for s in CRITICAL_SHIFTS)
            c.value = f'=IF(OR({dbl}),"!!","")'
        dbl_range = f"B{row}:{last_cl}{row}"
        ws.conditional_formatting.add(
            dbl_range,
            CellIsRule(operator="equal", formula=['"!!"'],
                       fill=FILL_WARN, font=FONT_WARN, stopIfTrue=True))
        ws.row_dimensions[row].height = 15

        # Medium-Rahmen um den gesamten Monatsblock
        _outer_border(ws, kw_row, 1, row, 1 + dim)
        row += 2

    dv.sqref = " ".join(dv_ranges)

    for d in range(1, 32):
        ws.column_dimensions[get_column_letter(1 + d)].width = 5.5

    ws.freeze_panes = "B1"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    print(f"  {row} Zeilen")
    return layout


# ---------------------------------------------------------------------------
# Monats-Sheet
# ---------------------------------------------------------------------------
def create_month(wb, mi, emps, layout):
    mn = mi + 1
    name = MONTHS_DE[mi]
    ws = wb.create_sheet(title=name)
    dim = calendar.monthrange(YEAR, mn)[1]
    last_col = 1 + dim
    last_cl = get_column_letter(last_col)

    ws.column_dimensions["A"].width = 13
    day_w = max(4.0, min(5.2, 63.5 / dim))
    for d in range(1, dim + 1):
        ws.column_dimensions[get_column_letter(1 + d)].width = day_w

    _set(ws.cell(1, 1), f"Dienstplan {name} {YEAR}", FONT_TITLE, border=None)
    _set(ws.cell(1, last_col - 3),
         f"AT: {_arbeitstage(YEAR, mn)}   Sa: {_samstage(YEAR, mn)}",
         FONT_GRAY_SM, border=None)

    # Row 2: Ferien
    fr = _ferien_ranges(YEAR, mn)
    for fname, (sd, ed) in fr.items():
        sc, ec = 1 + sd, 1 + ed
        if ec > sc:
            ws.merge_cells(start_row=2, start_column=sc, end_row=2, end_column=ec)
        c = ws.cell(2, sc)
        c.value = fname
        c.fill = FILL_FERIEN
        c.font = FONT_FERIEN
        c.alignment = ALIGN_C

    tbl_start = 3
    row = tbl_start

    # Tag
    tag_row = row
    _set(ws.cell(row, 1), "Tag", FONT_HDR, FILL_HDR, ALIGN_C)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        sp = _is_special(dt)
        _set(ws.cell(row, 1 + d), d,
             FONT_TAG_SP if sp else FONT_TAG_NR,
             FILL_WE if sp else FILL_HDR, ALIGN_C)
    _feiertag_comments(ws, tag_row, YEAR, mn)
    ws.row_dimensions[row].height = 18
    row += 1

    # WT
    _set(ws.cell(row, 1), "WT", FONT_HDR, FILL_HDR, ALIGN_C)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        sp = _is_special(dt)
        _set(ws.cell(row, 1 + d), WEEKDAYS_DE[dt.weekday()],
             FONT_WT_SP if sp else FONT_WT_NR,
             FILL_WE if sp else FILL_HDR, ALIGN_C)
    ws.row_dimensions[row].height = 18
    row += 1

    # KW
    _set(ws.cell(row, 1), "KW", FONT_HDR, FILL_HDR, ALIGN_C)
    last_kw = None
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        kw = _kw(dt)
        c = ws.cell(row, 1 + d)
        c.alignment = ALIGN_C
        c.border = THIN
        sp = _is_special(dt)
        c.fill = FILL_WE if sp else FILL_HDR
        if kw != last_kw:
            c.value = kw
            c.font = FONT_KW_W
        last_kw = kw
    ws.row_dimensions[row].height = 15
    row += 1

    # MA-Zeilen
    regular = [e for e in emps if e.name not in SEKRETARIAT_MA]
    sekr = [e for e in emps if e.name in SEKRETARIAT_MA]
    all_ordered = regular + sekr
    first_sekr_row = None
    first_data_row = row

    for ei, ed in enumerate(all_ordered):
        is_s = ed.name in SEKRETARIAT_MA
        if is_s and first_sekr_row is None:
            first_sekr_row = row

        _set(ws.cell(row, 1), ed.name,
             FONT_SEKR if is_s else FONT_NAME9, align=ALIGN_L)
        zebra = FILL_ZEBRA if ei % 2 == 1 else None
        src_row = layout.emp_rows[mi].get(ed.name)

        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            c = ws.cell(row, 1 + d)
            c.alignment = ALIGN_CS
            c.border = THIN
            c.font = FONT_CELL8
            if src_row:
                cl = get_column_letter(1 + d)
                c.value = f'=IF(Dienstplan!{cl}{src_row}="","",Dienstplan!{cl}{src_row})'
            if _is_special(dt):
                c.fill = FILL_WE
            elif zebra:
                c.fill = zebra
        ws.row_dimensions[row].height = 16
        row += 1

    # Unterbesetzung (dynamische COUNTIF-Formeln)
    last_ma_row = row - 1
    _set(ws.cell(row, 1), "Besetzung", FONT_GRAY8, align=ALIGN_L, border=THIN)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        c = ws.cell(row, 1 + d)
        c.alignment = ALIGN_C
        c.border = THIN
        if _is_special(dt):
            c.fill = FILL_WE
            continue
        cl = get_column_letter(1 + d)
        rng = f"{cl}{first_data_row}:{cl}{last_ma_row}"
        if dt.weekday() == 4:
            checks = ['COUNTIF({r},"FI")>0', 'COUNTIF({r},"SI")>0']
        else:
            checks = ['COUNTIF({r},"FI")>0', 'COUNTIF({r},"SI")>0',
                       'COUNTIF({r},"NI")>0']
        cond = ",".join(ch.format(r=rng) for ch in checks)
        c.value = f'=IF(AND({cond}),"","!")'
    # Bedingte Formatierung für Besetzungszeile
    bes_range = f"B{row}:{last_cl}{row}"
    ws.conditional_formatting.add(
        bes_range,
        CellIsRule(operator="equal", formula=['"!"'],
                   fill=FILL_WARN, font=FONT_WARN, stopIfTrue=True))
    ws.row_dimensions[row].height = 15
    row += 1
    tbl_end = row - 1

    _outer_border(ws, tbl_start, 1, tbl_end, last_col)

    data_range = f"B{first_data_row}:{last_cl}{tbl_end - 1}"
    _add_cond_fmt(ws, data_range)

    if first_sekr_row:
        for r in range(first_sekr_row, first_sekr_row + len(sekr)):
            ws.row_dimensions[r].hidden = True

    # === LEGENDE + UNTERSCHRIFTEN (nur Außenrahmen) ===
    row += 1
    leg_start = row
    leg_end_col = 14
    sig_col = 16
    sig_end = min(sig_col + 8, last_col)
    NO_BDR = None  # kein innerer Rahmen

    # --- Legende Header ---
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=leg_end_col)
    _set(ws.cell(row, 1), "Legende", FONT_HDR, FILL_HDR, ALIGN_C, NO_BDR)
    ws.row_dimensions[row].height = 18

    # --- Unterschriften Header (gleiche Zeile) ---
    ws.merge_cells(start_row=row, start_column=sig_col,
                   end_row=row, end_column=sig_end)
    _set(ws.cell(row, sig_col), "Unterschriften",
         FONT_HDR, FILL_HDR, ALIGN_C, NO_BDR)
    row += 1

    def _leg_font(ckey):
        _, fn = SHIFT_COLORS.get(ckey, (None, None))
        if fn and fn.color:
            return Font(name="Calibri", size=8, bold=True, color=fn.color)
        return FONT_LEG_CODE

    n_leg = max(len(LEGEND_LEFT), len(LEGEND_RIGHT))
    for i in range(n_leg):
        r = row + i
        ws.row_dimensions[r].height = 16

        if i < len(LEGEND_LEFT):
            code, desc, ckey = LEGEND_LEFT[i]
            sf, _ = SHIFT_COLORS.get(ckey, (None, None))
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
            _set(ws.cell(r, 1), code, _leg_font(ckey), sf, ALIGN_L, NO_BDR)
            ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=7)
            _set(ws.cell(r, 4), desc, FONT_LEG_DESC, align=ALIGN_L, border=NO_BDR)
        else:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
            _set(ws.cell(r, 1), "", border=NO_BDR)

        if i < len(LEGEND_RIGHT):
            code, desc, ckey = LEGEND_RIGHT[i]
            sf, _ = SHIFT_COLORS.get(ckey, (None, None))
            ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=10)
            _set(ws.cell(r, 8), code, _leg_font(ckey), sf, ALIGN_L, NO_BDR)
            ws.merge_cells(start_row=r, start_column=11, end_row=r, end_column=leg_end_col)
            _set(ws.cell(r, 11), desc, FONT_LEG_DESC, align=ALIGN_L, border=NO_BDR)
        else:
            ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=leg_end_col)
            _set(ws.cell(r, 8), "", border=NO_BDR)

    # WE-Zeile am Ende der Legende
    we_row = row + n_leg
    ws.merge_cells(start_row=we_row, start_column=1, end_row=we_row, end_column=3)
    _set(ws.cell(we_row, 1), "", fill=FILL_WE, border=NO_BDR)
    ws.merge_cells(start_row=we_row, start_column=4,
                   end_row=we_row, end_column=leg_end_col)
    _set(ws.cell(we_row, 4), "Wochenende / Feiertag", FONT_LEG_DESC,
         align=ALIGN_L, border=NO_BDR)
    ws.row_dimensions[we_row].height = 16

    # Nur Außenrahmen Legende (medium, keine inneren Linien)
    _box_border(ws, leg_start, 1, we_row, leg_end_col)

    # --- Unterschriften-Block ---
    r = leg_start + 1
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "Erstellt von:", FONT_SIG_LABEL,
         align=ALIGN_L, border=NO_BDR)
    ws.row_dimensions[r].height = 18
    r += 1

    # Unterschrift-Linie 1 (leer, zum Unterschreiben)
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "", border=NO_BDR)
    ws.row_dimensions[r].height = 30
    r += 1

    # Datum/Unterschrift Hinweis
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "Datum / Unterschrift", FONT_SIG_HINT,
         align=ALIGN_C, border=NO_BDR)
    ws.row_dimensions[r].height = 14
    r += 1

    # Leerzeile
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "", border=NO_BDR)
    ws.row_dimensions[r].height = 8
    r += 1

    # Genehmigt von
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "Genehmigt von:", FONT_SIG_LABEL,
         align=ALIGN_L, border=NO_BDR)
    ws.row_dimensions[r].height = 18
    r += 1

    # Unterschrift-Linie 2
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "", border=NO_BDR)
    ws.row_dimensions[r].height = 30
    r += 1

    # Datum/Unterschrift Hinweis 2
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "Datum / Unterschrift", FONT_SIG_HINT,
         align=ALIGN_C, border=NO_BDR)
    ws.row_dimensions[r].height = 14

    sig_end_row = r

    # Nur Außenrahmen Unterschriften (medium, keine inneren Linien)
    _box_border(ws, leg_start, sig_col, sig_end_row, sig_end)

    last_row = max(we_row, sig_end_row)

    ws.freeze_panes = "B6"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.page_margins = PageMargins(left=0.35, right=0.25, top=0.3, bottom=0.25,
                                  header=0.15, footer=0.15)
    ws.print_area = f"A1:{last_cl}{last_row}"


# ---------------------------------------------------------------------------
# Jahresübersicht
# ---------------------------------------------------------------------------
def create_jahresuebersicht(ws, emps, stypes):
    print("Erstelle Jahresübersicht ...")
    ws.column_dimensions["A"].width = 14
    _set(ws.cell(1, 1), f"Jahresübersicht {YEAR}", FONT_TITLE, border=None)

    row = 3
    _set(ws.cell(row, 1), "Mitarbeiter", FONT_HDR10, FILL_HDR, ALIGN_L)
    for i, st in enumerate(stypes):
        _set(ws.cell(row, 2 + i), st, FONT_HDR10, FILL_HDR, ALIGN_C)
        ws.column_dimensions[get_column_letter(2 + i)].width = 7
    tc = 2 + len(stypes)
    _set(ws.cell(row, tc), "Gesamt", FONT_HDR10, FILL_HDR, ALIGN_C)
    ws.column_dimensions[get_column_letter(tc)].width = 8
    ws.row_dimensions[row].height = 20
    row += 1

    sums = {st: 0 for st in stypes}
    for ei, ed in enumerate(emps):
        _set(ws.cell(row, 1), ed.name, FONT_NAME, align=ALIGN_L)
        zebra = FILL_ZEBRA if ei % 2 == 1 else None
        total = 0
        for i, st in enumerate(stypes):
            cnt = sum(1 for m in range(12) for _, c in ed.shifts[m].items()
                      if c == st)
            _set(ws.cell(row, 2 + i), cnt or "", FONT_CELL10, zebra, ALIGN_C)
            sums[st] += cnt
            total += cnt
        _set(ws.cell(row, tc), total, FONT_BOLD10, zebra, ALIGN_C)
        ws.row_dimensions[row].height = 18
        row += 1

    _set(ws.cell(row, 1), "Summe", FONT_BOLD10, FILL_SUM, ALIGN_L)
    gt = 0
    for i, st in enumerate(stypes):
        _set(ws.cell(row, 2 + i), sums[st], FONT_BOLD10, FILL_SUM, ALIGN_C)
        gt += sums[st]
    _set(ws.cell(row, tc), gt, FONT_BOLD10, FILL_SUM, ALIGN_C)
    ws.row_dimensions[row].height = 18

    _outer_border(ws, 3, 1, row, tc)
    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


# ---------------------------------------------------------------------------
# Monatsdetails
# ---------------------------------------------------------------------------
def create_monatsdetails(ws, emps, stypes, shrs, layout):
    print("Erstelle Monatsdetails ...")
    ws.column_dimensions["A"].width = 14
    for i in range(len(stypes) + 5):
        ws.column_dimensions[get_column_letter(2 + i)].width = 6.5

    _set(ws.cell(1, 1), f"Monatsdetails {YEAR}", FONT_TITLE, border=None)

    # === KONFIGURATIONSTABELLE (rechts) ===
    DEFAULT_HRS = {
        "FI": 7.25, "FII": 7.5, "SI": 7.5, "SII": 7.0, "NI": 7.5,
        "DI": 8.0, "DII": 8.0, "SD": 7.0, "EH": 8.0, "KT": 8.0,
        "KU": 8.0, "Sek": 7.0,
    }
    cfg_col = len(stypes) + 7

    # --- Mitarbeiter + IRTAZ ---
    ma_col = cfg_col
    irtaz_col = cfg_col + 1
    ws.column_dimensions[get_column_letter(ma_col)].width = 14
    ws.column_dimensions[get_column_letter(irtaz_col)].width = 8

    _set(ws.cell(1, ma_col), "Konfiguration", FONT_TITLE, border=None)
    _set(ws.cell(2, ma_col), "Mitarbeiter", FONT_HDR, FILL_HDR, ALIGN_C)
    _set(ws.cell(2, irtaz_col), "IRTAZ", FONT_HDR, FILL_HDR, ALIGN_C)

    irtaz_cells = {}
    for ei, ed in enumerate(emps):
        r = 3 + ei
        _set(ws.cell(r, ma_col), ed.name, FONT_NAME9, align=ALIGN_L)
        _set(ws.cell(r, irtaz_col), ed.irtaz, FONT_CELL10, align=ALIGN_C)
        irtaz_cells[ed.name] = f"${get_column_letter(irtaz_col)}${r}"
    _outer_border(ws, 2, ma_col, 2 + len(emps), irtaz_col)

    # --- Schichten + Stunden ---
    shift_col = cfg_col + 3
    hrs_col = cfg_col + 4
    ws.column_dimensions[get_column_letter(shift_col)].width = 8
    ws.column_dimensions[get_column_letter(hrs_col)].width = 10

    _set(ws.cell(2, shift_col), "Schicht", FONT_HDR, FILL_HDR, ALIGN_C)
    _set(ws.cell(2, hrs_col), "Stunden", FONT_HDR, FILL_HDR, ALIGN_C)

    hrs_cells = {}
    for si, st in enumerate(stypes):
        r = 3 + si
        _set(ws.cell(r, shift_col), st, FONT_BOLD9, align=ALIGN_C)
        hrs_val = shrs.get(st)
        if hrs_val is None:
            hrs_val = DEFAULT_HRS.get(st)
        if hrs_val is not None:
            _set(ws.cell(r, hrs_col), hrs_val, FONT_CELL10, align=ALIGN_C)
        else:
            _set(ws.cell(r, hrs_col), "IRTAZ", FONT_GRAY_SM, align=ALIGN_C)
            ws.cell(r, hrs_col).comment = Comment(
                "Verwendet individuelle IRTAZ des Mitarbeiters",
                "Dienstplan", width=200, height=30)
        hrs_cells[st] = f"${get_column_letter(hrs_col)}${r}"
    _outer_border(ws, 2, shift_col, 2 + len(stypes), hrs_col)

    # Hinweise
    hint_r = 3 + max(len(stypes), len(emps)) + 1
    _set(ws.cell(hint_r, ma_col),
         "Soll AZ = IRTAZ \u00d7 Arbeitstage", FONT_GRAY8, border=None)
    _set(ws.cell(hint_r + 1, ma_col),
         '"IRTAZ" = individuelle Tagesarbeitszeit', FONT_GRAY8, border=None)
    _set(ws.cell(hint_r + 2, ma_col),
         "Stunden/IRTAZ hier \u00e4ndern \u2192 Formeln aktualisieren sich",
         FONT_GRAY8, border=None)

    # === MONATSTABELLEN (mit COUNTIF-Formeln) ===
    gc = 2 + len(stypes) + 3  # Gesamt-Spalte (nach Diff)
    gc_cl = get_column_letter(gc)
    ws.column_dimensions[gc_cl].width = 8
    prev_gesamt = {}  # emp_name -> vorherige Gesamt-Zellreferenz

    row = 3
    for mi in range(12):
        mn = mi + 1
        at = _arbeitstage(YEAR, mn)
        sa = _samstage(YEAR, mn)
        dim = calendar.monthrange(YEAR, mn)[1]

        _set(ws.cell(row, 1), MONTHS_DE[mi], FONT_MONTH, border=None)
        _set(ws.cell(row, 3), "AT:", border=None)
        _set(ws.cell(row, 4), at, FONT_BOLD9, border=None)
        _set(ws.cell(row, 6), "Sa:", border=None)
        _set(ws.cell(row, 7), sa, FONT_BOLD9, border=None)
        row += 1

        hdr_row = row
        headers = ["MA"] + stypes + ["Soll", "Ist", "Diff", "Gesamt"]
        for i, h in enumerate(headers):
            _set(ws.cell(row, 1 + i), h, FONT_HDR, FILL_HDR, ALIGN_C)
        ws.row_dimensions[row].height = 18
        row += 1

        sc = 2 + len(stypes)
        first_data = row

        for ei, ed in enumerate(emps):
            _set(ws.cell(row, 1), ed.name, FONT_NAME9, align=ALIGN_L)
            zebra = FILL_ZEBRA if ei % 2 == 1 else None

            emp_row = layout.emp_rows[mi].get(ed.name)
            last_cl = get_column_letter(1 + dim)

            # Schicht-Anzahlen per COUNTIF
            for i, st in enumerate(stypes):
                if emp_row:
                    rng = f"Dienstplan!B{emp_row}:{last_cl}{emp_row}"
                    _set(ws.cell(row, 2 + i),
                         f'=COUNTIF({rng},"{st}")', FONT_CELL, zebra, ALIGN_C)
                else:
                    _set(ws.cell(row, 2 + i), 0, FONT_CELL, zebra, ALIGN_C)

            # Soll = AT × IRTAZ (Referenz auf Konfig)
            irtaz_ref = irtaz_cells[ed.name]
            _set(ws.cell(row, sc),
                 f"={at}*{irtaz_ref}", FONT_CELL, zebra, ALIGN_C)

            # Ist = Summe(Anzahl × Stunden) pro Schichttyp
            # ISNUMBER prüft ob feste Stunden oder IRTAZ-Text
            terms = []
            for i, st in enumerate(stypes):
                cnt_cl = get_column_letter(2 + i)
                hrs_ref = hrs_cells[st]
                terms.append(
                    f"IF(ISNUMBER({hrs_ref}),"
                    f"{cnt_cl}{row}*{hrs_ref},"
                    f"{cnt_cl}{row}*{irtaz_ref})")
            _set(ws.cell(row, sc + 1),
                 "=" + "+".join(terms), FONT_CELL, zebra, ALIGN_C)

            # Diff = Ist - Soll
            diff_cl = get_column_letter(sc + 2)
            ist_cl = get_column_letter(sc + 1)
            soll_cl = get_column_letter(sc)
            _set(ws.cell(row, sc + 2),
                 f"={ist_cl}{row}-{soll_cl}{row}", FONT_CELL, zebra, ALIGN_C)

            # Gesamt = kumulative Diff über alle Monate
            if ed.name in prev_gesamt:
                _set(ws.cell(row, gc),
                     f"={prev_gesamt[ed.name]}+{diff_cl}{row}",
                     FONT_BOLD9, zebra, ALIGN_C)
            else:
                _set(ws.cell(row, gc),
                     f"={diff_cl}{row}", FONT_BOLD9, zebra, ALIGN_C)
            prev_gesamt[ed.name] = f"{gc_cl}{row}"

            ws.row_dimensions[row].height = 16
            row += 1

        last_data = row - 1

        # Summe-Zeile (SUM-Formeln)
        _set(ws.cell(row, 1), "Summe", FONT_BOLD9, FILL_SUM, ALIGN_L)
        for i in range(len(stypes)):
            cl = get_column_letter(2 + i)
            _set(ws.cell(row, 2 + i),
                 f"=SUM({cl}{first_data}:{cl}{last_data})",
                 FONT_BOLD9, FILL_SUM, ALIGN_C)
        for off in range(4):  # Soll, Ist, Diff, Gesamt
            cl = get_column_letter(sc + off)
            _set(ws.cell(row, sc + off),
                 f"=SUM({cl}{first_data}:{cl}{last_data})",
                 FONT_BOLD9, FILL_SUM, ALIGN_C)
        ws.row_dimensions[row].height = 18

        # Bedingte Formatierung Diff + Gesamt
        for col_off in [2, 3]:  # Diff und Gesamt
            fmt_cl = get_column_letter(sc + col_off)
            fmt_range = f"{fmt_cl}{first_data}:{fmt_cl}{last_data}"
            ws.conditional_formatting.add(
                fmt_range,
                CellIsRule(operator="lessThan", formula=["0"],
                           font=Font(name="Calibri", size=9, color=C_RED),
                           stopIfTrue=True))
            ws.conditional_formatting.add(
                fmt_range,
                CellIsRule(operator="greaterThanOrEqual", formula=["0"],
                           font=Font(name="Calibri", size=9, color="008000"),
                           stopIfTrue=True))

        _outer_border(ws, hdr_row, 1, row, gc)
        row += 2

    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


# ---------------------------------------------------------------------------
# Urlaubsübersicht
# ---------------------------------------------------------------------------
def create_urlaubsuebersicht(ws, emps, layout):
    print("Erstelle Urlaubsübersicht ...")
    ws.column_dimensions["A"].width = 14
    for i in range(14):
        ws.column_dimensions[get_column_letter(2 + i)].width = 8

    _set(ws.cell(1, 1), f"Urlaubsübersicht {YEAR}", FONT_TITLE, border=None)

    row = 3
    _set(ws.cell(row, 1), "Mitarbeiter", FONT_HDR10, FILL_HDR, ALIGN_L)
    for i in range(12):
        _set(ws.cell(row, 2 + i), MONTHS_DE[i][:3], FONT_HDR10, FILL_HDR, ALIGN_C)
    gc, rc, dc = 14, 15, 16
    _set(ws.cell(row, gc), "Genommen", FONT_HDR10, FILL_HDR, ALIGN_C)
    ws.column_dimensions[get_column_letter(gc)].width = 10
    _set(ws.cell(row, rc), "Anspruch", FONT_HDR10, FILL_HDR, ALIGN_C)
    ws.column_dimensions[get_column_letter(rc)].width = 10
    _set(ws.cell(row, dc), "Rest", FONT_HDR10, FILL_HDR, ALIGN_C)
    ws.column_dimensions[get_column_letter(dc)].width = 8
    ws.row_dimensions[row].height = 20
    row += 1

    first_data_row = row
    gc_cl = get_column_letter(gc)
    rc_cl = get_column_letter(rc)
    dc_cl = get_column_letter(dc)

    for ei, ed in enumerate(emps):
        _set(ws.cell(row, 1), ed.name, FONT_NAME, align=ALIGN_L)
        zebra = FILL_ZEBRA if ei % 2 == 1 else None

        for mi in range(12):
            mn = mi + 1
            dim = calendar.monthrange(YEAR, mn)[1]
            emp_row = layout.emp_rows[mi].get(ed.name)
            cl = get_column_letter(2 + mi)
            if emp_row:
                last_cl = get_column_letter(1 + dim)
                rng = f"Dienstplan!B{emp_row}:{last_cl}{emp_row}"
                formula = f'=COUNTIF({rng},"U")+COUNTIF({rng},"U    alt")'
                _set(ws.cell(row, 2 + mi), formula, FONT_CELL10, zebra, ALIGN_C)
            else:
                _set(ws.cell(row, 2 + mi), 0, FONT_CELL10, zebra, ALIGN_C)

        # Genommen = Summe aller Monate
        _set(ws.cell(row, gc), f"=SUM(B{row}:M{row})", FONT_BOLD10, zebra, ALIGN_C)
        # Anspruch
        _set(ws.cell(row, rc), DEFAULT_URLAUB_TAGE, FONT_CELL10, zebra, ALIGN_C)
        # Rest = Anspruch - Genommen (mit bedingter Farbformatierung)
        _set(ws.cell(row, dc), f"={rc_cl}{row}-{gc_cl}{row}",
             FONT_BOLD10, zebra, ALIGN_C)
        ws.row_dimensions[row].height = 18
        row += 1

    # Summe-Zeile
    last_data_row = row - 1
    _set(ws.cell(row, 1), "Summe", FONT_BOLD10, FILL_SUM, ALIGN_L)
    for mi in range(12):
        cl = get_column_letter(2 + mi)
        _set(ws.cell(row, 2 + mi),
             f"=SUM({cl}{first_data_row}:{cl}{last_data_row})",
             FONT_BOLD10, FILL_SUM, ALIGN_C)
    _set(ws.cell(row, gc),
         f"=SUM({gc_cl}{first_data_row}:{gc_cl}{last_data_row})",
         FONT_BOLD10, FILL_SUM, ALIGN_C)
    ws.row_dimensions[row].height = 18

    _outer_border(ws, 3, 1, row, dc)

    # CF für Rest-Spalte: Rot wenn < 0, Orange wenn <= 5, Grün wenn > 5
    rest_range = f"{dc_cl}{first_data_row}:{dc_cl}{last_data_row}"
    ws.conditional_formatting.add(
        rest_range,
        CellIsRule(operator="lessThan", formula=["0"],
                   fill=PatternFill("solid", fgColor=C_WARN_BG),
                   font=Font(color=C_RED, bold=True),
                   stopIfTrue=True))
    ws.conditional_formatting.add(
        rest_range,
        CellIsRule(operator="lessThanOrEqual", formula=["5"],
                   fill=PatternFill("solid", fgColor=C_YELLOW),
                   font=Font(color=C_WARN_FG, bold=True),
                   stopIfTrue=True))
    ws.conditional_formatting.add(
        rest_range,
        CellIsRule(operator="greaterThan", formula=["5"],
                   fill=PatternFill("solid", fgColor=C_URLAUB),
                   font=Font(color="008000", bold=True),
                   stopIfTrue=True))

    row += 2
    _set(ws.cell(row, 1),
         f"Anspruch: {DEFAULT_URLAUB_TAGE} Tage/Jahr (anpassbar in Spalte {rc_cl})",
         Font(name="Calibri", size=8, italic=True, color="666666"), border=None)

    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Dienstplan Excel Generator")
    parser.add_argument("--year", type=int, default=None,
                        help="Jahr (default: aktuelles Jahr wenn --leer, sonst 2026)")
    parser.add_argument("--leer", action="store_true",
                        help="Leeren Dienstplan erstellen (nur Struktur + Dropdowns)")
    parser.add_argument("--archiv", action="store_true",
                        help="Backup des bestehenden Plans erstellen")
    parser.add_argument("--config-export", action="store_true",
                        help="MA-Config aus .xlsm exportieren nach mitarbeiter.json")
    args = parser.parse_args()

    # Jahr bestimmen
    global YEAR, FEIERTAGE, SCHULFERIEN
    if args.year:
        YEAR = args.year
    elif args.leer:
        YEAR = datetime.date.today().year + 1
    else:
        YEAR = 2026

    # Feiertage + Ferien dynamisch berechnen
    FEIERTAGE = feiertage_bw(YEAR)
    SCHULFERIEN = schulferien_bw(YEAR)

    dst_file = os.path.join(BASE_DIR, f"Dienstplan_{YEAR}_neu.xlsx")
    src_file = os.path.join(BASE_DIR, "Dienstplanübersicht 2026.xlsm")

    print("=" * 60)
    print(f"Dienstplan Excel Generator – {YEAR}")
    print("=" * 60)

    # Feiertage anzeigen
    print(f"\nFeiertage {YEAR} (BW):")
    for dt in sorted(FEIERTAGE):
        print(f"  {dt.strftime('%d.%m.')} {FEIERTAGE[dt]}")

    # Archiv
    if args.archiv:
        print("\nArchiv-Backup ...")
        archiv_backup(dst_file)

    # --- LEERER PLAN ---
    if args.leer:
        print(f"\nErstelle LEEREN Dienstplan für {YEAR} ...")

        # MA aus Config oder aus .xlsm
        config = load_config()
        stypes_default = DROPDOWN_SHIFTS
        shrs_default = {}
        if config:
            print(f"  MA aus Config: {CONFIG_FILE}")
            emps = emps_from_config(config)
        elif os.path.exists(src_file):
            print(f"  MA aus .xlsm (keine Config gefunden)")
            emps_full, stypes_default, shrs_default = extract_data(src_file)
            emps = [Employee(e.name, e.irtaz) for e in emps_full]
        else:
            print("  FEHLER: Weder mitarbeiter.json noch .xlsm gefunden!")
            return

        # Leere Shifts
        for e in emps:
            e.shifts = {m: {} for m in range(12)}

        wb = Workbook()
        wb.properties.creator = "Emanuel Siebert"
        wb.properties.lastModifiedBy = "Emanuel Siebert"
        ws = wb.active
        ws.title = "Dienstplan"
        layout = create_eingabe(ws, emps)

        print("Erstelle 12 Monats-Sheets ...")
        for mi in range(12):
            create_month(wb, mi, emps, layout)
            print(f"  {MONTHS_DE[mi]}")

        create_jahresuebersicht(wb.create_sheet("Jahresübersicht"),
                                emps, stypes_default)
        create_monatsdetails(wb.create_sheet("Monatsdetails"),
                             emps, stypes_default, shrs_default, layout)
        create_urlaubsuebersicht(wb.create_sheet("Urlaubsübersicht"), emps, layout)

        print(f"\nSpeichere {dst_file} ...")
        wb.save(dst_file)
        print(f"Fertig! Leerer Dienstplan {YEAR}: {dst_file}")
        wb.close()
        return

    # --- NORMALER MODUS (aus .xlsm lesen) ---
    if not os.path.exists(src_file):
        print(f"FEHLER: {src_file} nicht gefunden!")
        return

    emps, stypes, shrs = extract_data(src_file)

    # Config exportieren wenn gewünscht oder noch nicht vorhanden
    if args.config_export or not os.path.exists(CONFIG_FILE):
        save_default_config(emps)

    wb = Workbook()
    wb.properties.creator = "Emanuel Siebert"
    wb.properties.lastModifiedBy = "Emanuel Siebert"
    ws = wb.active
    ws.title = "Dienstplan"
    layout = create_eingabe(ws, emps)

    print("Erstelle 12 Monats-Sheets ...")
    for mi in range(12):
        create_month(wb, mi, emps, layout)
        print(f"  {MONTHS_DE[mi]}")

    create_jahresuebersicht(wb.create_sheet("Jahresübersicht"), emps, stypes)
    create_monatsdetails(wb.create_sheet("Monatsdetails"), emps, stypes, shrs, layout)
    create_urlaubsuebersicht(wb.create_sheet("Urlaubsübersicht"), emps, layout)

    print(f"\nSpeichere {dst_file} ...")
    wb.save(dst_file)
    print(f"Fertig! Sheets: {wb.sheetnames}")
    wb.close()


if __name__ == "__main__":
    main()
