#!/usr/bin/env python3
"""
Dienstplan Excel Generator
"""

import calendar
import datetime
import os

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.page import PageMargins

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
SRC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "Dienstplanübersicht 2026.xlsm")
DST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "Dienstplan_2026_neu.xlsx")
YEAR = 2026

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

SEKRETARIAT_MA = {"Schmidt", "Radimersky"}
CRITICAL_SHIFTS = ["FI", "SI", "NI"]
DEFAULT_URLAUB_TAGE = 30

FEIERTAGE = {
    datetime.date(2026, 1, 1): "Neujahr",
    datetime.date(2026, 1, 6): "Hl. 3 Könige",
    datetime.date(2026, 4, 3): "Karfreitag",
    datetime.date(2026, 4, 6): "Ostermontag",
    datetime.date(2026, 5, 1): "Tag d. Arbeit",
    datetime.date(2026, 5, 14): "Chr. Himmelf.",
    datetime.date(2026, 5, 25): "Pfingstmontag",
    datetime.date(2026, 6, 4): "Fronleichnam",
    datetime.date(2026, 8, 15): "Mariä Himmelf.",
    datetime.date(2026, 10, 3): "Tag d. Einheit",
    datetime.date(2026, 11, 1): "Allerheiligen",
    datetime.date(2026, 12, 25): "1. Weihnacht",
    datetime.date(2026, 12, 26): "2. Weihnacht",
}

SCHULFERIEN = [
    ("Weihnachtsferien", datetime.date(2025, 12, 22), datetime.date(2026, 1, 5)),
    ("Faschingsferien", datetime.date(2026, 2, 16), datetime.date(2026, 2, 20)),
    ("Osterferien", datetime.date(2026, 4, 2), datetime.date(2026, 4, 11)),
    ("Pfingstferien", datetime.date(2026, 5, 26), datetime.date(2026, 6, 6)),
    ("Sommerferien", datetime.date(2026, 7, 30), datetime.date(2026, 9, 12)),
    ("Herbstferien", datetime.date(2026, 10, 26), datetime.date(2026, 10, 30)),
    ("Weihnachtsferien", datetime.date(2026, 12, 23), datetime.date(2027, 1, 9)),
]

# ---------------------------------------------------------------------------
# Vordefinierte Styles (wiederverwendbar, minimiert Style-Duplikate)
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

# Vorberechnete Fills
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
NO_FILL = PatternFill(fill_type=None)

# Vorberechnete Fonts
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

# Conditional-Formatting Regeln (code, fill, font) - IMMER beides angeben!
CF_RULES = [
    ("U", PatternFill("solid", fgColor=C_URLAUB), Font(color="000000")),
    ("EH", PatternFill("solid", fgColor=C_EH), Font(color=C_RED)),
    ("KU", PatternFill("solid", fgColor=C_KU), Font(color="000000")),
    ("A", PatternFill("solid", fgColor=C_AUSGLEICH), Font(color="000000")),
    ("T-ZUG", PatternFill("solid", fgColor=C_URLAUB), Font(color="000000")),
    ("Fobi", PatternFill("solid", fgColor="FFFFFF"), Font(color=C_RED)),
    ("GT", PatternFill("solid", fgColor="FFFFFF"), Font(color=C_RED)),
    ("NST", PatternFill("solid", fgColor="FFFFFF"), Font(color=C_RED)),
]


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
    """Gibt (fill, font) für einen Schichtcode zurück."""
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
    """Dicker Außenrand. NUR auf Bereiche OHNE merged cells verwenden!"""
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            left = MEDIUM_SIDE if c == c1 else THIN_SIDE
            right = MEDIUM_SIDE if c == c2 else THIN_SIDE
            top = MEDIUM_SIDE if r == r1 else THIN_SIDE
            bottom = MEDIUM_SIDE if r == r2 else THIN_SIDE
            ws.cell(r, c).border = Border(left=left, right=right,
                                          top=top, bottom=bottom)

def _add_cond_fmt(ws, cell_range):
    """Conditional Formatting – immer fill UND font (verhindert dxf-Korruption)."""
    for code, fill, font in CF_RULES:
        ws.conditional_formatting.add(
            cell_range,
            CellIsRule(operator="equal", formula=[f'"{code}"'],
                       fill=fill, font=font, stopIfTrue=True))

def _missing_critical(emps, mi, day):
    assigned = set()
    for e in emps:
        s = e.shifts[mi].get(day)
        if s and s in CRITICAL_SHIFTS:
            assigned.add(s)
    return [s for s in CRITICAL_SHIFTS if s not in assigned]

def _feiertag_comments(ws, row, year, month):
    dim = calendar.monthrange(year, month)[1]
    for d in range(1, dim + 1):
        dt = datetime.date(year, month, d)
        if dt in FEIERTAGE:
            ws.cell(row, 1 + d).comment = Comment(
                FEIERTAGE[dt], "Dienstplan", width=140, height=30)

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
# Daten extrahieren
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

    # Data Validation – wird am Ende als RANGE hinzugefügt (nicht pro Zelle!)
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
    dv_ranges = []  # Sammle Ranges für DV
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

        # Monatsname
        _set(ws.cell(row, 1), MONTHS_DE[mi], FONT_MONTH, border=None)
        row += 1

        # KW
        _set(ws.cell(row, 1), "KW", FONT_KW, align=ALIGN_C, border=None)
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
        row += 1

        # WT
        _set(ws.cell(row, 1), "WT", FONT_HDR, FILL_HDR, ALIGN_C)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            sp = _is_special(dt)
            _set(ws.cell(row, 1 + d), WEEKDAYS_DE[dt.weekday()],
                 FONT_WT_SP if sp else FONT_WT_NR,
                 FILL_WE if sp else FILL_HDR, ALIGN_C)
        row += 1

        # MA-Zeilen
        first_ma_row = row
        for ei, ed in enumerate(emps):
            layout.emp_rows[mi][ed.name] = row
            _set(ws.cell(row, 1), ed.name, FONT_NAME, align=ALIGN_L)
            zebra = FILL_ZEBRA if ei % 2 == 1 else None
            for d in range(1, dim + 1):
                dt = datetime.date(YEAR, mn, d)
                shift = ed.shifts[mi].get(d)
                c = ws.cell(row, 1 + d)
                c.alignment = ALIGN_C
                c.border = THIN
                if shift:
                    c.value = shift
                    sf, fn = _shift_style(shift)
                    c.font = fn
                    if sf:
                        c.fill = sf
                    elif _is_special(dt):
                        c.fill = FILL_WE
                    elif zebra:
                        c.fill = zebra
                else:
                    if _is_special(dt):
                        c.fill = FILL_WE
                    elif zebra:
                        c.fill = zebra
            row += 1

        # DV-Range für diesen Monat (alle MA-Zeilen, Spalte B bis letzte)
        dv_ranges.append(f"B{first_ma_row}:{last_cl}{row - 1}")

        # Unterbesetzungs-Warnung
        _set(ws.cell(row, 1), "Besetzung", FONT_GRAY8, align=ALIGN_L, border=THIN)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            c = ws.cell(row, 1 + d)
            c.alignment = ALIGN_C
            if _is_special(dt):
                c.fill = FILL_WE
                continue
            missing = _missing_critical(emps, mi, d)
            if missing:
                c.value = "!" + "/".join(missing)
                c.fill = FILL_WARN
                c.font = FONT_WARN
        row += 2

    # DV als Ranges statt Einzelzellen
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

    # Spaltenbreiten
    ws.column_dimensions["A"].width = 13
    day_w = max(4.0, min(5.2, 63.5 / dim))
    for d in range(1, dim + 1):
        ws.column_dimensions[get_column_letter(1 + d)].width = day_w

    # Row 1: Titel (KEIN merge – vermeidet Probleme)
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

    # === DATENTABELLE (ab Row 3, KEINE merges → _outer_border sicher) ===
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
    row += 1

    # WT
    _set(ws.cell(row, 1), "WT", FONT_HDR, FILL_HDR, ALIGN_C)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        sp = _is_special(dt)
        _set(ws.cell(row, 1 + d), WEEKDAYS_DE[dt.weekday()],
             FONT_WT_SP if sp else FONT_WT_NR,
             FILL_WE if sp else FILL_HDR, ALIGN_C)
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
            c.alignment = ALIGN_C
            c.border = THIN
            c.font = FONT_CELL8
            if src_row:
                cl = get_column_letter(1 + d)
                c.value = f'=IF(Dienstplan!{cl}{src_row}="","",Dienstplan!{cl}{src_row})'
            if _is_special(dt):
                c.fill = FILL_WE
            elif zebra:
                c.fill = zebra
        row += 1

    # Unterbesetzung
    _set(ws.cell(row, 1), "Besetzung", FONT_GRAY8, align=ALIGN_L, border=THIN)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        c = ws.cell(row, 1 + d)
        c.alignment = ALIGN_C
        c.border = THIN
        if _is_special(dt):
            c.fill = FILL_WE
            continue
        missing = _missing_critical(emps, mi, d)
        if missing:
            c.value = "!" + "/".join(missing)
            c.fill = FILL_WARN
            c.font = FONT_WARN
    row += 1
    tbl_end = row - 1

    # Dicker Außenrand (Datentabelle hat KEINE merges → sicher)
    _outer_border(ws, tbl_start, 1, tbl_end, last_col)

    # Conditional Formatting
    data_range = f"B{first_data_row}:{last_cl}{tbl_end - 1}"
    _add_cond_fmt(ws, data_range)

    # Sekretariat verstecken
    if first_sekr_row:
        for r in range(first_sekr_row, first_sekr_row + len(sekr)):
            ws.row_dimensions[r].hidden = True

    # =================================================================
    # LEGENDE + UNTERSCHRIFTEN (nur merges innerhalb, kein _outer_border)
    # =================================================================
    row += 1
    leg_start = row
    leg_end_col = 14

    def _lb(top=False, bottom=False, left=False, right=False):
        return Border(
            top=MEDIUM_SIDE if top else THIN_SIDE,
            bottom=MEDIUM_SIDE if bottom else THIN_SIDE,
            left=MEDIUM_SIDE if left else THIN_SIDE,
            right=MEDIUM_SIDE if right else THIN_SIDE)

    # Legende Header
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=leg_end_col)
    _set(ws.cell(row, 1), "Legende", FONT_HDR, FILL_HDR, ALIGN_C,
         _lb(top=True, left=True, right=True))
    row += 1

    def _leg_font(ckey):
        """Font für Legenden-Code: rot wenn Schicht rot ist, sonst schwarz."""
        _, fn = SHIFT_COLORS.get(ckey, (None, None))
        if fn and fn.color:
            return Font(name="Calibri", size=8, bold=True, color=fn.color)
        return FONT_LEG_CODE

    n_leg = max(len(LEGEND_LEFT), len(LEGEND_RIGHT))
    for i in range(n_leg):
        r = row + i
        last = (i == n_leg - 1)

        if i < len(LEGEND_LEFT):
            code, desc, ckey = LEGEND_LEFT[i]
            sf, _ = SHIFT_COLORS.get(ckey, (None, None))
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
            _set(ws.cell(r, 1), code, _leg_font(ckey),
                 sf, ALIGN_L, _lb(left=True))
            ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=7)
            _set(ws.cell(r, 4), desc, FONT_LEG_DESC, align=ALIGN_L, border=_lb())
        else:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
            _set(ws.cell(r, 1), "", border=_lb(left=True))

        if i < len(LEGEND_RIGHT):
            code, desc, ckey = LEGEND_RIGHT[i]
            sf, _ = SHIFT_COLORS.get(ckey, (None, None))
            ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=10)
            _set(ws.cell(r, 8), code, _leg_font(ckey),
                 sf, ALIGN_L, _lb())
            ws.merge_cells(start_row=r, start_column=11, end_row=r, end_column=leg_end_col)
            _set(ws.cell(r, 11), desc, FONT_LEG_DESC, align=ALIGN_L,
                 border=_lb(right=True))
        else:
            ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=leg_end_col)
            _set(ws.cell(r, 8), "", border=_lb(right=True))

    # WE/Feiertag
    we_row = row + n_leg
    ws.merge_cells(start_row=we_row, start_column=1, end_row=we_row, end_column=3)
    _set(ws.cell(we_row, 1), "", fill=FILL_WE,
         border=_lb(left=True, bottom=True))
    ws.merge_cells(start_row=we_row, start_column=4,
                   end_row=we_row, end_column=leg_end_col)
    _set(ws.cell(we_row, 4), "Wochenende / Feiertag", FONT_LEG_DESC,
         align=ALIGN_L, border=_lb(right=True, bottom=True))

    # === Unterschriften-Block ===
    sig_col = max(last_col - 7, 16)
    sig_end = last_col

    # Header
    ws.merge_cells(start_row=leg_start, start_column=sig_col,
                   end_row=leg_start, end_column=sig_end)
    _set(ws.cell(leg_start, sig_col), "Unterschriften",
         FONT_HDR, FILL_HDR, ALIGN_C, _lb(top=True, left=True, right=True))

    r = leg_start + 1
    # Erstellt von
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "Erstellt von:", FONT_SIG_LABEL,
         align=ALIGN_L, border=_lb(left=True, right=True))
    r += 1
    # Linie
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "",
         border=Border(left=MEDIUM_SIDE, right=MEDIUM_SIDE,
                       top=THIN_SIDE, bottom=Side("thin", "000000")))
    ws.row_dimensions[r].height = 22
    r += 1
    # Hinweis
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "Datum / Unterschrift", FONT_SIG_HINT,
         align=ALIGN_C, border=_lb(left=True, right=True))
    r += 1
    # Leer
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "", border=_lb(left=True, right=True))
    r += 1
    # Genehmigt von
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "Genehmigt von:", FONT_SIG_LABEL,
         align=ALIGN_L, border=_lb(left=True, right=True))
    r += 1
    # Linie
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "",
         border=Border(left=MEDIUM_SIDE, right=MEDIUM_SIDE,
                       top=THIN_SIDE, bottom=Side("thin", "000000")))
    ws.row_dimensions[r].height = 22
    r += 1
    # Hinweis
    ws.merge_cells(start_row=r, start_column=sig_col, end_row=r, end_column=sig_end)
    _set(ws.cell(r, sig_col), "Datum / Unterschrift", FONT_SIG_HINT,
         align=ALIGN_C, border=_lb(left=True, right=True, bottom=True))

    last_row = max(we_row, r)

    # Druckeinstellungen
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
        row += 1

    _set(ws.cell(row, 1), "Summe", FONT_BOLD10, FILL_SUM, ALIGN_L)
    gt = 0
    for i, st in enumerate(stypes):
        _set(ws.cell(row, 2 + i), sums[st], FONT_BOLD10, FILL_SUM, ALIGN_C)
        gt += sums[st]
    _set(ws.cell(row, tc), gt, FONT_BOLD10, FILL_SUM, ALIGN_C)

    _outer_border(ws, 3, 1, row, tc)
    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


# ---------------------------------------------------------------------------
# Monatsdetails
# ---------------------------------------------------------------------------
def create_monatsdetails(ws, emps, stypes, shrs):
    print("Erstelle Monatsdetails ...")
    ws.column_dimensions["A"].width = 14
    for i in range(len(stypes) + 5):
        ws.column_dimensions[get_column_letter(2 + i)].width = 6.5

    _set(ws.cell(1, 1), f"Monatsdetails {YEAR}", FONT_TITLE, border=None)
    row = 3

    for mi in range(12):
        mn = mi + 1
        at = _arbeitstage(YEAR, mn)
        sa = _samstage(YEAR, mn)

        _set(ws.cell(row, 1), MONTHS_DE[mi], FONT_MONTH, border=None)
        _set(ws.cell(row, 3), "AT:", border=None)
        _set(ws.cell(row, 4), at, FONT_BOLD9, border=None)
        _set(ws.cell(row, 6), "Sa:", border=None)
        _set(ws.cell(row, 7), sa, FONT_BOLD9, border=None)
        row += 1

        hdr_row = row
        headers = ["MA"] + stypes + ["Soll", "Ist", "Diff"]
        for i, h in enumerate(headers):
            _set(ws.cell(row, 1 + i), h, FONT_HDR, FILL_HDR, ALIGN_C)
        row += 1

        s_sums = {st: 0 for st in stypes}
        sum_soll = sum_ist = 0.0

        for ei, ed in enumerate(emps):
            _set(ws.cell(row, 1), ed.name, FONT_NAME9, align=ALIGN_L)
            zebra = FILL_ZEBRA if ei % 2 == 1 else None
            for i, st in enumerate(stypes):
                cnt = sum(1 for _, c in ed.shifts[mi].items() if c == st)
                _set(ws.cell(row, 2 + i), cnt or "", FONT_CELL, zebra, ALIGN_C)
                s_sums[st] += cnt

            sc = 2 + len(stypes)
            soll = at * ed.irtaz
            _set(ws.cell(row, sc), round(soll, 1), FONT_CELL, zebra, ALIGN_C)
            sum_soll += soll

            ist = 0.0
            for _, code in ed.shifts[mi].items():
                h = shrs.get(code)
                if h is not None:
                    ist += h
                else:
                    ist += ed.irtaz
            _set(ws.cell(row, sc + 1), round(ist, 2), FONT_CELL, zebra, ALIGN_C)
            sum_ist += ist

            diff = ist - soll
            _set(ws.cell(row, sc + 2), round(diff, 2),
                 Font(name="Calibri", size=9,
                      color=C_RED if diff < 0 else "008000"), zebra, ALIGN_C)
            row += 1

        _set(ws.cell(row, 1), "Summe", FONT_BOLD9, FILL_SUM, ALIGN_L)
        for i, st in enumerate(stypes):
            _set(ws.cell(row, 2 + i), s_sums[st], FONT_BOLD9, FILL_SUM, ALIGN_C)
        sc = 2 + len(stypes)
        for off, val in enumerate([round(sum_soll, 1), round(sum_ist, 2),
                                    round(sum_ist - sum_soll, 2)]):
            _set(ws.cell(row, sc + off), val, FONT_BOLD9, FILL_SUM, ALIGN_C)

        _outer_border(ws, hdr_row, 1, row, sc + 2)
        row += 2

    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


# ---------------------------------------------------------------------------
# Urlaubsübersicht
# ---------------------------------------------------------------------------
def create_urlaubsuebersicht(ws, emps):
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
    row += 1

    sum_per_month = [0] * 12
    sum_total = 0

    for ei, ed in enumerate(emps):
        _set(ws.cell(row, 1), ed.name, FONT_NAME, align=ALIGN_L)
        zebra = FILL_ZEBRA if ei % 2 == 1 else None
        total = 0
        for mi in range(12):
            cnt = sum(1 for _, c in ed.shifts[mi].items()
                      if c in ("U", "U    alt"))
            _set(ws.cell(row, 2 + mi), cnt or "", FONT_CELL10, zebra, ALIGN_C)
            total += cnt
            sum_per_month[mi] += cnt
        _set(ws.cell(row, gc), total, FONT_BOLD10, zebra, ALIGN_C)
        sum_total += total
        _set(ws.cell(row, rc), DEFAULT_URLAUB_TAGE, FONT_CELL10, zebra, ALIGN_C)
        rest = DEFAULT_URLAUB_TAGE - total
        color = C_RED if rest < 0 else ("008000" if rest > 5 else C_WARN_FG)
        _set(ws.cell(row, dc), rest,
             Font(name="Calibri", size=10, bold=True, color=color), zebra, ALIGN_C)
        row += 1

    _set(ws.cell(row, 1), "Summe", FONT_BOLD10, FILL_SUM, ALIGN_L)
    for mi in range(12):
        _set(ws.cell(row, 2 + mi), sum_per_month[mi], FONT_BOLD10, FILL_SUM, ALIGN_C)
    _set(ws.cell(row, gc), sum_total, FONT_BOLD10, FILL_SUM, ALIGN_C)

    _outer_border(ws, 3, 1, row, dc)

    row += 2
    _set(ws.cell(row, 1),
         f"Anspruch: {DEFAULT_URLAUB_TAGE} Tage/Jahr (anpassbar in Spalte {get_column_letter(rc)})",
         Font(name="Calibri", size=8, italic=True, color="666666"), border=None)

    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("Dienstplan Excel Generator")
    print("=" * 60)

    emps, stypes, shrs = extract_data(SRC_FILE)

    wb = Workbook()
    ws = wb.active
    ws.title = "Dienstplan"
    layout = create_eingabe(ws, emps)

    print("Erstelle 12 Monats-Sheets ...")
    for mi in range(12):
        create_month(wb, mi, emps, layout)
        print(f"  {MONTHS_DE[mi]}")

    create_jahresuebersicht(wb.create_sheet("Jahresübersicht"), emps, stypes)
    create_monatsdetails(wb.create_sheet("Monatsdetails"), emps, stypes, shrs)
    create_urlaubsuebersicht(wb.create_sheet("Urlaubsübersicht"), emps)

    print(f"\nSpeichere {DST_FILE} ...")
    wb.save(DST_FILE)
    print(f"Fertig! Sheets: {wb.sheetnames}")
    wb.close()


if __name__ == "__main__":
    main()
