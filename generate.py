#!/usr/bin/env python3
"""
Dienstplan Excel Generator
Liest die Original-Dienstplanübersicht 2026.xlsm ein und generiert eine
saubere, schön formatierte Excel-Datei mit:
- Eingabe-Sheet (Jahresübersicht aller Schichten) – HAUPT-EINGABE
- 12 Monats-Sheets (druckfertig, lesen per Formel aus dem Eingabe-Sheet)
- Jahresübersicht (Schichtzählung pro MA)
- Monatsdetails (Soll/Ist Arbeitszeit)

Features:
- Monats-Sheets werden automatisch aus dem Eingabe-Sheet befüllt (Formeln)
- Dropdown-Auswahl für Schichttypen in jeder Eingabezelle
- Schmidt & Radimersky (Sekretariat) beim Druck ausgeblendet
- Unterschriftenfeld rechts neben der Legende (A4-optimiert)
- Legende mit Uhrzeiten, merged Zellen
- Conditional Formatting für automatische Schichtfarben
- Dicke Außenränder um Datentabellen
- Ferien-Namen in Eingabe-Sheet
- Schmale Margins für maximalen Druckbereich
"""

import calendar
import datetime
import os
from collections import OrderedDict

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

# Schichten die besetzt sein MÜSSEN (Unterbesetzungs-Warnung)
CRITICAL_SHIFTS = ["FI", "SI", "NI"]

# Standard-Urlaubsanspruch pro Jahr
DEFAULT_URLAUB_TAGE = 30

# Feiertage BW 2026 mit Namen
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

# Farben
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
C_WARN_BG = "FFC7CE"   # Rot-Hintergrund für Unterbesetzung
C_WARN_FG = "9C0006"   # Dunkelrot-Text für Unterbesetzung

SHIFT_COLORS = {
    "U": (C_URLAUB, None), "U    alt": (C_URLAUB, None),
    "EH": (C_EH, C_RED), "KU": (C_KU, None), "A": (C_AUSGLEICH, None),
    "GT": (None, C_RED), "NST": (None, C_RED), "Fobi": (None, C_RED),
    "T-ZUG": (C_URLAUB, None), "T-ZG": (C_URLAUB, None),
}

# Legende: (Anzeige-Text, Beschreibung, Farb-Schlüssel)
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

THIN = Border(
    left=Side("thin", C_BORDER), right=Side("thin", C_BORDER),
    top=Side("thin", C_BORDER), bottom=Side("thin", C_BORDER),
)
MEDIUM_SIDE = Side("medium", "000000")


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

def _fill(color):
    return PatternFill(start_color=color, end_color=color, fill_type="solid")

def _shift_fill(code):
    if code and str(code).strip() in SHIFT_COLORS:
        bg = SHIFT_COLORS[str(code).strip()][0]
        return _fill(bg) if bg else None
    if code and str(code).startswith("(") and ")" in str(code):
        return _fill(C_YELLOW)
    return None

def _shift_font(code, size=8, bold=False):
    fg = None
    c = str(code).strip() if code else ""
    if c in SHIFT_COLORS:
        fg = SHIFT_COLORS[c][1]
    elif c.startswith("(") and ")" in c:
        fg = C_RED
    return Font(name="Calibri", size=size, bold=bold, color=fg or "000000")

def _apply_cell(cell, value=None, font=None, fill=None, align=None, border=THIN):
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

def _apply_outer_border(ws, min_row, min_col, max_row, max_col):
    """Dicken Außenrand um einen Bereich, innen dünne Ränder."""
    thin_s = Side("thin", C_BORDER)
    for r in range(min_row, max_row + 1):
        for c in range(min_col, max_col + 1):
            left = MEDIUM_SIDE if c == min_col else thin_s
            right = MEDIUM_SIDE if c == max_col else thin_s
            top = MEDIUM_SIDE if r == min_row else thin_s
            bottom = MEDIUM_SIDE if r == max_row else thin_s
            ws.cell(r, c).border = Border(left=left, right=right,
                                          top=top, bottom=bottom)

def _add_shift_cond_fmt(ws, cell_range):
    """Conditional Formatting für Schichtfarben (funktioniert auch mit Formeln)."""
    for code, bg, fg in [
        ("U", C_URLAUB, None), ("EH", C_EH, C_RED), ("KU", C_KU, None),
        ("A", C_AUSGLEICH, None), ("T-ZUG", C_URLAUB, None),
        ("Fobi", None, C_RED), ("GT", None, C_RED), ("NST", None, C_RED),
    ]:
        kwargs = {}
        if bg:
            kwargs["fill"] = _fill(bg)
        if fg:
            kwargs["font"] = Font(color=fg)
        if kwargs:
            ws.conditional_formatting.add(
                cell_range,
                CellIsRule(operator="equal", formula=[f'"{code}"'], **kwargs))

def _get_missing_critical(emps, mi, day):
    """Prüfe welche kritischen Schichten (FI/SI/NI) an einem Tag nicht besetzt sind."""
    assigned = set()
    for e in emps:
        shift = e.shifts[mi].get(day)
        if shift and shift in CRITICAL_SHIFTS:
            assigned.add(shift)
    return [s for s in CRITICAL_SHIFTS if s not in assigned]


def _add_feiertag_comments(ws, row, year, month):
    """Feiertag-Namen als Kommentare an den Tag-Zellen."""
    dim = calendar.monthrange(year, month)[1]
    for d in range(1, dim + 1):
        dt = datetime.date(year, month, d)
        if dt in FEIERTAGE:
            c = ws.cell(row, 1 + d)
            c.comment = Comment(FEIERTAGE[dt], "Dienstplan",
                                width=140, height=30)


def _get_ferien_ranges(year, month):
    """Ferien-Zeiträume innerhalb eines Monats → {name: (start_day, end_day)}."""
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
    """Jahresplan-Sheet – HAUPT-EINGABE mit Dropdowns + Ferien-Labels."""
    print("Erstelle Eingabe-Sheet ...")
    hdr_fill = _fill(C_HDR_BG)
    we_fill = _fill(C_WEEKEND)
    ferien_fill = _fill(C_FERIEN)
    hdr_font = Font(name="Calibri", size=9, bold=True, color=C_HDR_FG)
    ca = Alignment(horizontal="center", vertical="center")
    la = Alignment(horizontal="left", vertical="center")

    ws.column_dimensions["A"].width = 14
    _apply_cell(ws.cell(1, 1), f"Dienstplan {YEAR}",
                Font(name="Calibri", size=14, bold=True), border=None)

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
    row = 3

    for mi in range(12):
        mn = mi + 1
        dim = calendar.monthrange(YEAR, mn)[1]
        layout.emp_rows[mi] = {}

        # --- Ferien-Balken mit Name ---
        ferien_ranges = _get_ferien_ranges(YEAR, mn)
        if ferien_ranges:
            for fname, (sd, ed) in ferien_ranges.items():
                sc, ec = 1 + sd, 1 + ed
                if ec > sc:
                    ws.merge_cells(start_row=row, start_column=sc,
                                   end_row=row, end_column=ec)
                cell = ws.cell(row, sc)
                cell.value = fname
                cell.fill = ferien_fill
                cell.font = Font(name="Calibri", size=7, italic=True, color="006100")
                cell.alignment = ca
                cell.border = Border(
                    left=Side("thin", "006100"), right=Side("thin", "006100"),
                    top=Side("thin", "006100"), bottom=Side("thin", "006100"))
            row += 1

        # Monatsname
        _apply_cell(ws.cell(row, 1), MONTHS_DE[mi],
                    Font(name="Calibri", size=11, bold=True), border=None)
        row += 1

        # KW
        _apply_cell(ws.cell(row, 1), "KW",
                    Font(name="Calibri", size=8, italic=True, color="666666"),
                    align=ca)
        last_kw = None
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            kw = _kw(dt)
            c = ws.cell(row, 1 + d)
            c.alignment = ca
            c.border = THIN
            if _is_weekend(dt) or _is_feiertag(dt):
                c.fill = we_fill
            if kw != last_kw:
                c.value = kw
                c.font = Font(name="Calibri", size=8, italic=True, color="666666")
            last_kw = kw
        row += 1

        # Tag
        tag_row = row
        _apply_cell(ws.cell(row, 1), "Tag", hdr_font, hdr_fill, ca)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            is_sp = _is_weekend(dt) or _is_feiertag(dt)
            f = we_fill if is_sp else hdr_fill
            fn = Font(name="Calibri", size=9, bold=True,
                      color="000000" if is_sp else C_HDR_FG)
            _apply_cell(ws.cell(row, 1 + d), d, fn, f, ca)
        _add_feiertag_comments(ws, tag_row, YEAR, mn)
        row += 1

        # WT
        _apply_cell(ws.cell(row, 1), "WT", hdr_font, hdr_fill, ca)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            is_sp = _is_weekend(dt) or _is_feiertag(dt)
            f = we_fill if is_sp else hdr_fill
            fn = Font(name="Calibri", size=8,
                      color="000000" if is_sp else C_HDR_FG)
            _apply_cell(ws.cell(row, 1 + d), WEEKDAYS_DE[dt.weekday()], fn, f, ca)
        row += 1

        # MA-Zeilen
        for ei, ed in enumerate(emps):
            layout.emp_rows[mi][ed.name] = row
            _apply_cell(ws.cell(row, 1), ed.name,
                        Font(name="Calibri", size=10, bold=True), align=la)
            zebra = _fill(C_ZEBRA) if ei % 2 == 1 else None
            for d in range(1, dim + 1):
                dt = datetime.date(YEAR, mn, d)
                shift = ed.shifts[mi].get(d)
                c = ws.cell(row, 1 + d)
                c.alignment = ca
                c.border = THIN
                dv.add(c)
                if shift:
                    c.value = shift
                    sf = _shift_fill(shift)
                    c.font = _shift_font(shift, 8)
                    if sf:
                        c.fill = sf
                    elif _is_weekend(dt) or _is_feiertag(dt):
                        c.fill = we_fill
                    elif zebra:
                        c.fill = zebra
                else:
                    if _is_weekend(dt) or _is_feiertag(dt):
                        c.fill = we_fill
                    elif zebra:
                        c.fill = zebra
            row += 1

        # --- Unterbesetzungs-Warnung ---
        warn_fill = _fill(C_WARN_BG)
        warn_font = Font(name="Calibri", size=7, bold=True, color=C_WARN_FG)
        _apply_cell(ws.cell(row, 1), "Besetzung",
                    Font(name="Calibri", size=8, italic=True, color="999999"),
                    align=la, border=None)
        has_warning = False
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            c = ws.cell(row, 1 + d)
            c.alignment = ca
            if _is_weekend(dt) or _is_feiertag(dt):
                c.fill = we_fill
                continue
            missing = _get_missing_critical(emps, mi, d)
            if missing:
                c.value = "!" + "/".join(missing)
                c.fill = warn_fill
                c.font = warn_font
                has_warning = True
        row += 1
        row += 1

    for d in range(1, 32):
        ws.column_dimensions[get_column_letter(1 + d)].width = 5.5

    # Nur Spalte A einfrieren – kein Monat "angenagelt"
    ws.freeze_panes = "B1"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    print(f"  {row} Zeilen")
    return layout


# ---------------------------------------------------------------------------
# Monats-Sheet (druckfertig A4 Landscape)
# ---------------------------------------------------------------------------
def create_month(wb, mi, emps, layout):
    """Druckfertiges Monats-Sheet: sauber berahmt, A4-optimiert, Formeln."""
    mn = mi + 1
    name = MONTHS_DE[mi]
    ws = wb.create_sheet(title=name)
    dim = calendar.monthrange(YEAR, mn)[1]

    hdr_fill = _fill(C_HDR_BG)
    we_fill = _fill(C_WEEKEND)
    hdr_font = Font(name="Calibri", size=9, bold=True, color=C_HDR_FG)
    cell_font = Font(name="Calibri", size=9)
    ca = Alignment(horizontal="center", vertical="center")
    la = Alignment(horizontal="left", vertical="center")
    last_col = 1 + dim  # letzte Datenspalte

    # --- Spaltenbreiten: Name=13, Tage dynamisch für A4-Füllung ---
    ws.column_dimensions["A"].width = 13
    day_w = max(3.8, min(5.2, (25.0 * 2.54) / dim))  # ~25cm nutzbar
    for d in range(1, dim + 1):
        ws.column_dimensions[get_column_letter(1 + d)].width = day_w

    # === Row 1: Titel ===
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=8)
    _apply_cell(ws.cell(1, 1), f"Dienstplan {name} {YEAR}",
                Font(name="Calibri", size=14, bold=True), border=None)
    # Info rechts
    _apply_cell(ws.cell(1, last_col - 3),
                f"AT: {_arbeitstage(YEAR, mn)}   Sa: {_samstage(YEAR, mn)}",
                Font(name="Calibri", size=9, color="666666"), border=None)

    # === Row 2: Ferien-Balken ===
    ferien_ranges = _get_ferien_ranges(YEAR, mn)
    for fname, (sd, ed) in ferien_ranges.items():
        sc, ec = 1 + sd, 1 + ed
        if ec > sc:
            ws.merge_cells(start_row=2, start_column=sc, end_row=2, end_column=ec)
        cell = ws.cell(2, sc)
        cell.value = fname
        cell.fill = _fill(C_FERIEN)
        cell.font = Font(name="Calibri", size=7, italic=True, color="006100")
        cell.alignment = ca

    # === DATENTABELLE ab Row 3 ===
    tbl_start = 3
    row = tbl_start

    # Tag-Zeile
    tag_row = row
    _apply_cell(ws.cell(row, 1), "Tag", hdr_font, hdr_fill, ca)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        is_sp = _is_weekend(dt) or _is_feiertag(dt)
        f = we_fill if is_sp else hdr_fill
        fn = Font(name="Calibri", size=9, bold=True,
                  color="000000" if is_sp else C_HDR_FG)
        _apply_cell(ws.cell(row, 1 + d), d, fn, f, ca)
    _add_feiertag_comments(ws, tag_row, YEAR, mn)
    row += 1

    # Wochentag-Zeile
    _apply_cell(ws.cell(row, 1), "WT", hdr_font, hdr_fill, ca)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        is_sp = _is_weekend(dt) or _is_feiertag(dt)
        f = we_fill if is_sp else hdr_fill
        fn = Font(name="Calibri", size=8, bold=is_sp,
                  color="000000" if is_sp else C_HDR_FG)
        _apply_cell(ws.cell(row, 1 + d), WEEKDAYS_DE[dt.weekday()], fn, f, ca)
    row += 1

    # KW-Zeile
    _apply_cell(ws.cell(row, 1), "KW", hdr_font, hdr_fill, ca)
    last_kw = None
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        kw = _kw(dt)
        c = ws.cell(row, 1 + d)
        c.alignment = ca
        c.border = THIN
        is_sp = _is_weekend(dt) or _is_feiertag(dt)
        c.fill = we_fill if is_sp else hdr_fill
        if kw != last_kw:
            c.value = kw
            c.font = Font(name="Calibri", size=8, italic=True, color=C_HDR_FG)
        last_kw = kw
    row += 1

    # --- MA-Zeilen ---
    regular = [e for e in emps if e.name not in SEKRETARIAT_MA]
    sekr = [e for e in emps if e.name in SEKRETARIAT_MA]
    all_ordered = regular + sekr
    first_sekr_row = None

    for ei, ed in enumerate(all_ordered):
        is_s = ed.name in SEKRETARIAT_MA
        if is_s and first_sekr_row is None:
            first_sekr_row = row

        _apply_cell(ws.cell(row, 1), ed.name,
                    Font(name="Calibri", size=9, bold=True,
                         italic=is_s, color="999999" if is_s else "000000"),
                    align=la)
        zebra = _fill(C_ZEBRA) if ei % 2 == 1 else None
        src_row = layout.emp_rows[mi].get(ed.name)

        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            c = ws.cell(row, 1 + d)
            c.alignment = ca
            c.border = THIN
            if src_row:
                cl = get_column_letter(1 + d)
                c.value = f'=IF(Dienstplan!{cl}{src_row}="","",Dienstplan!{cl}{src_row})'
            if _is_weekend(dt) or _is_feiertag(dt):
                c.fill = we_fill
            elif zebra:
                c.fill = zebra
            c.font = Font(name="Calibri", size=8)
        row += 1

    # --- Unterbesetzungs-Warnung ---
    warn_fill = _fill(C_WARN_BG)
    warn_font = Font(name="Calibri", size=7, bold=True, color=C_WARN_FG)
    _apply_cell(ws.cell(row, 1), "Besetzung",
                Font(name="Calibri", size=8, italic=True, color="999999"),
                align=la)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        c = ws.cell(row, 1 + d)
        c.alignment = ca
        c.border = THIN
        if _is_weekend(dt) or _is_feiertag(dt):
            c.fill = we_fill
            continue
        missing = _get_missing_critical(emps, mi, d)
        if missing:
            c.value = "!" + "/".join(missing)
            c.fill = warn_fill
            c.font = warn_font
    row += 1

    tbl_end = row - 1

    # --- Dicker Außenrand um gesamte Datentabelle ---
    _apply_outer_border(ws, tbl_start, 1, tbl_end, last_col)

    # --- Conditional Formatting ---
    data_range = f"B{tbl_start + 3}:{get_column_letter(last_col)}{tbl_end - 1}"
    _add_shift_cond_fmt(ws, data_range)

    # --- Sekretariats-MA verstecken ---
    if first_sekr_row:
        for r in range(first_sekr_row, first_sekr_row + len(sekr)):
            ws.row_dimensions[r].hidden = True

    # =================================================================
    # LEGENDE (links, berahmt) + UNTERSCHRIFT (rechts, berahmt)
    # =================================================================
    row += 1
    leg_start = row
    leg_font_code = Font(name="Calibri", size=8, bold=True)
    leg_font_desc = Font(name="Calibri", size=8)
    leg_border = Border(
        left=Side("thin", C_BORDER), right=Side("thin", C_BORDER),
        top=Side("thin", C_BORDER), bottom=Side("thin", C_BORDER))

    # --- Legende-Header: merged über ganze Breite ---
    leg_end_col = 14  # Legende geht von Spalte 1 bis 14
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=leg_end_col)
    _apply_cell(ws.cell(row, 1), "Legende",
                Font(name="Calibri", size=9, bold=True, color=C_HDR_FG),
                hdr_fill, ca, leg_border)
    row += 1

    # Linke Spalte (col 1-3 Code, 4-7 Beschreibung)
    n_leg = max(len(LEGEND_LEFT), len(LEGEND_RIGHT))
    for i in range(n_leg):
        r = row + i
        # Linke Hälfte
        if i < len(LEGEND_LEFT):
            code, desc, ckey = LEGEND_LEFT[i]
            bg, fg = SHIFT_COLORS.get(ckey, (None, None))
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
            _apply_cell(ws.cell(r, 1), code,
                        Font(name="Calibri", size=8, bold=True,
                             color=fg or "000000"),
                        _fill(bg) if bg else None, la, leg_border)
            ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=7)
            _apply_cell(ws.cell(r, 4), desc, leg_font_desc, align=la,
                        border=leg_border)
        else:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
            _apply_cell(ws.cell(r, 1), "", border=leg_border)

        # Rechte Hälfte
        if i < len(LEGEND_RIGHT):
            code, desc, ckey = LEGEND_RIGHT[i]
            bg, fg = SHIFT_COLORS.get(ckey, (None, None))
            ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=10)
            _apply_cell(ws.cell(r, 8), code,
                        Font(name="Calibri", size=8, bold=True,
                             color=fg or "000000"),
                        _fill(bg) if bg else None, la, leg_border)
            ws.merge_cells(start_row=r, start_column=11, end_row=r, end_column=leg_end_col)
            _apply_cell(ws.cell(r, 11), desc, leg_font_desc, align=la,
                        border=leg_border)
        else:
            ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=leg_end_col)
            _apply_cell(ws.cell(r, 8), "", border=leg_border)

    # WE/Feiertag-Zeile
    we_row = row + n_leg
    ws.merge_cells(start_row=we_row, start_column=1, end_row=we_row, end_column=3)
    _apply_cell(ws.cell(we_row, 1), "", we_fill, border=leg_border)
    ws.merge_cells(start_row=we_row, start_column=4, end_row=we_row,
                   end_column=leg_end_col)
    _apply_cell(ws.cell(we_row, 4), "Wochenende / Feiertag",
                leg_font_desc, align=la, border=leg_border)

    leg_end_row = we_row
    # Dicker Außenrand um Legende
    _apply_outer_border(ws, leg_start, 1, leg_end_row, leg_end_col)

    # === UNTERSCHRIFTEN-BLOCK (rechts, berahmt) ===
    sig_col = max(last_col - 7, 16)   # 8 Spalten breit
    sig_end_col = last_col
    sig_font = Font(name="Calibri", size=10)
    sig_label = Font(name="Calibri", size=9, bold=True)
    sig_hint = Font(name="Calibri", size=8, italic=True, color="999999")

    # Header
    ws.merge_cells(start_row=leg_start, start_column=sig_col,
                   end_row=leg_start, end_column=sig_end_col)
    _apply_cell(ws.cell(leg_start, sig_col), "Unterschriften",
                Font(name="Calibri", size=9, bold=True, color=C_HDR_FG),
                hdr_fill, ca)

    # Erstellt von:
    r = leg_start + 1
    ws.merge_cells(start_row=r, start_column=sig_col,
                   end_row=r, end_column=sig_end_col)
    _apply_cell(ws.cell(r, sig_col), "Erstellt von:", sig_label, align=la)

    # Linie
    r += 1
    ws.merge_cells(start_row=r, start_column=sig_col,
                   end_row=r, end_column=sig_end_col)
    _apply_cell(ws.cell(r, sig_col), "", border=Border(
        bottom=Side("thin", "000000")))
    ws.row_dimensions[r].height = 22

    # Hinweis
    r += 1
    ws.merge_cells(start_row=r, start_column=sig_col,
                   end_row=r, end_column=sig_end_col)
    _apply_cell(ws.cell(r, sig_col), "Datum / Unterschrift", sig_hint, align=ca)

    # Leerzeile
    r += 1

    # Genehmigt von:
    r += 1
    ws.merge_cells(start_row=r, start_column=sig_col,
                   end_row=r, end_column=sig_end_col)
    _apply_cell(ws.cell(r, sig_col), "Genehmigt von:", sig_label, align=la)

    # Linie
    r += 1
    ws.merge_cells(start_row=r, start_column=sig_col,
                   end_row=r, end_column=sig_end_col)
    _apply_cell(ws.cell(r, sig_col), "", border=Border(
        bottom=Side("thin", "000000")))
    ws.row_dimensions[r].height = 22

    # Hinweis
    r += 1
    ws.merge_cells(start_row=r, start_column=sig_col,
                   end_row=r, end_column=sig_end_col)
    _apply_cell(ws.cell(r, sig_col), "Datum / Unterschrift", sig_hint, align=ca)

    sig_end_row = r
    # Dicker Außenrand um Unterschriften-Block
    _apply_outer_border(ws, leg_start, sig_col, sig_end_row, sig_end_col)

    last_row = max(leg_end_row, sig_end_row)

    # === Druckeinstellungen ===
    ws.freeze_panes = "B6"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.page_margins = PageMargins(left=0.35, right=0.25, top=0.3, bottom=0.25,
                                  header=0.15, footer=0.15)
    ws.print_area = f"A1:{get_column_letter(last_col)}{last_row}"


# ---------------------------------------------------------------------------
# Jahresübersicht
# ---------------------------------------------------------------------------
def create_jahresuebersicht(ws, emps, stypes):
    print("Erstelle Jahresübersicht ...")
    hdr_fill = _fill(C_HDR_BG)
    hdr_font = Font(name="Calibri", size=10, bold=True, color=C_HDR_FG)
    ca = Alignment(horizontal="center", vertical="center")
    la = Alignment(horizontal="left", vertical="center")

    ws.column_dimensions["A"].width = 14
    _apply_cell(ws.cell(1, 1), f"Jahresübersicht {YEAR}",
                Font(name="Calibri", size=14, bold=True), border=None)

    row = 3
    _apply_cell(ws.cell(row, 1), "Mitarbeiter", hdr_font, hdr_fill, la)
    for i, st in enumerate(stypes):
        _apply_cell(ws.cell(row, 2 + i), st, hdr_font, hdr_fill, ca)
        ws.column_dimensions[get_column_letter(2 + i)].width = 7
    tc = 2 + len(stypes)
    _apply_cell(ws.cell(row, tc), "Gesamt", hdr_font, hdr_fill, ca)
    ws.column_dimensions[get_column_letter(tc)].width = 8
    row += 1

    sums = {st: 0 for st in stypes}
    for ei, ed in enumerate(emps):
        _apply_cell(ws.cell(row, 1), ed.name,
                    Font(name="Calibri", size=10, bold=True), align=la)
        zebra = _fill(C_ZEBRA) if ei % 2 == 1 else None
        total = 0
        for i, st in enumerate(stypes):
            cnt = sum(1 for m in range(12) for _, c in ed.shifts[m].items()
                      if c == st)
            _apply_cell(ws.cell(row, 2 + i), cnt or "",
                        Font(name="Calibri", size=10), zebra, ca)
            sums[st] += cnt
            total += cnt
        _apply_cell(ws.cell(row, tc), total,
                    Font(name="Calibri", size=10, bold=True), zebra, ca)
        row += 1

    sf = _fill(C_SUM_BG)
    _apply_cell(ws.cell(row, 1), "Summe",
                Font(name="Calibri", size=10, bold=True), sf, la)
    gt = 0
    for i, st in enumerate(stypes):
        _apply_cell(ws.cell(row, 2 + i), sums[st],
                    Font(name="Calibri", size=10, bold=True), sf, ca)
        gt += sums[st]
    _apply_cell(ws.cell(row, tc), gt,
                Font(name="Calibri", size=10, bold=True), sf, ca)

    _apply_outer_border(ws, 3, 1, row, tc)
    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


# ---------------------------------------------------------------------------
# Monatsdetails
# ---------------------------------------------------------------------------
def create_monatsdetails(ws, emps, stypes, shrs):
    print("Erstelle Monatsdetails ...")
    hdr_fill = _fill(C_HDR_BG)
    hdr_font = Font(name="Calibri", size=9, bold=True, color=C_HDR_FG)
    ca = Alignment(horizontal="center", vertical="center")
    la = Alignment(horizontal="left", vertical="center")

    ws.column_dimensions["A"].width = 14
    for i in range(len(stypes) + 5):
        ws.column_dimensions[get_column_letter(2 + i)].width = 6.5

    _apply_cell(ws.cell(1, 1), f"Monatsdetails {YEAR}",
                Font(name="Calibri", size=14, bold=True), border=None)
    row = 3

    for mi in range(12):
        mn = mi + 1
        at = _arbeitstage(YEAR, mn)
        sa = _samstage(YEAR, mn)

        _apply_cell(ws.cell(row, 1), MONTHS_DE[mi],
                    Font(name="Calibri", size=11, bold=True), border=None)
        _apply_cell(ws.cell(row, 3), "AT:", border=None)
        _apply_cell(ws.cell(row, 4), at,
                    Font(name="Calibri", size=9, bold=True), border=None)
        _apply_cell(ws.cell(row, 6), "Sa:", border=None)
        _apply_cell(ws.cell(row, 7), sa,
                    Font(name="Calibri", size=9, bold=True), border=None)
        row += 1

        hdr_row = row
        headers = ["MA"] + stypes + ["Soll", "Ist", "Diff"]
        for i, h in enumerate(headers):
            _apply_cell(ws.cell(row, 1 + i), h, hdr_font, hdr_fill, ca)
        row += 1

        s_sums = {st: 0 for st in stypes}
        sum_soll = sum_ist = 0.0

        for ei, ed in enumerate(emps):
            _apply_cell(ws.cell(row, 1), ed.name,
                        Font(name="Calibri", size=9, bold=True), align=la)
            zebra = _fill(C_ZEBRA) if ei % 2 == 1 else None
            for i, st in enumerate(stypes):
                cnt = sum(1 for _, c in ed.shifts[mi].items() if c == st)
                _apply_cell(ws.cell(row, 2 + i), cnt or "",
                            Font(name="Calibri", size=9), zebra, ca)
                s_sums[st] += cnt

            sc = 2 + len(stypes)
            soll = at * ed.irtaz
            _apply_cell(ws.cell(row, sc), round(soll, 1),
                        Font(name="Calibri", size=9), zebra, ca)
            sum_soll += soll

            ist = 0.0
            for _, code in ed.shifts[mi].items():
                h = shrs.get(code)
                if h is not None:
                    ist += h
                else:
                    ist += ed.irtaz
            _apply_cell(ws.cell(row, sc + 1), round(ist, 2),
                        Font(name="Calibri", size=9), zebra, ca)
            sum_ist += ist

            diff = ist - soll
            _apply_cell(ws.cell(row, sc + 2), round(diff, 2),
                        Font(name="Calibri", size=9,
                             color=C_RED if diff < 0 else "008000"), zebra, ca)
            row += 1

        sf = _fill(C_SUM_BG)
        _apply_cell(ws.cell(row, 1), "Summe",
                    Font(name="Calibri", size=9, bold=True), sf, la)
        for i, st in enumerate(stypes):
            _apply_cell(ws.cell(row, 2 + i), s_sums[st],
                        Font(name="Calibri", size=9, bold=True), sf, ca)
        sc = 2 + len(stypes)
        for off, val in enumerate([round(sum_soll, 1), round(sum_ist, 2),
                                    round(sum_ist - sum_soll, 2)]):
            _apply_cell(ws.cell(row, sc + off), val,
                        Font(name="Calibri", size=9, bold=True), sf, ca)

        _apply_outer_border(ws, hdr_row, 1, row, sc + 2)
        row += 2

    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


# ---------------------------------------------------------------------------
# Urlaubsübersicht
# ---------------------------------------------------------------------------
def create_urlaubsuebersicht(ws, emps):
    """Jahres-Urlaubsübersicht: U-Tage pro Monat, Gesamt, Resturlaub."""
    print("Erstelle Urlaubsübersicht ...")
    hdr_fill = _fill(C_HDR_BG)
    hdr_font = Font(name="Calibri", size=10, bold=True, color=C_HDR_FG)
    ca = Alignment(horizontal="center", vertical="center")
    la = Alignment(horizontal="left", vertical="center")

    ws.column_dimensions["A"].width = 14
    for i in range(14):
        ws.column_dimensions[get_column_letter(2 + i)].width = 8

    _apply_cell(ws.cell(1, 1), f"Urlaubsübersicht {YEAR}",
                Font(name="Calibri", size=14, bold=True), border=None)

    row = 3
    # Header
    _apply_cell(ws.cell(row, 1), "Mitarbeiter", hdr_font, hdr_fill, la)
    for i in range(12):
        _apply_cell(ws.cell(row, 2 + i), MONTHS_DE[i][:3], hdr_font, hdr_fill, ca)
    gc = 14  # Gesamt-Spalte
    rc = 15  # Anspruch
    dc = 16  # Rest
    _apply_cell(ws.cell(row, gc), "Genommen", hdr_font, hdr_fill, ca)
    ws.column_dimensions[get_column_letter(gc)].width = 10
    _apply_cell(ws.cell(row, rc), "Anspruch", hdr_font, hdr_fill, ca)
    ws.column_dimensions[get_column_letter(rc)].width = 10
    _apply_cell(ws.cell(row, dc), "Rest", hdr_font, hdr_fill, ca)
    ws.column_dimensions[get_column_letter(dc)].width = 8
    row += 1

    sum_per_month = [0] * 12
    sum_total = 0

    for ei, ed in enumerate(emps):
        _apply_cell(ws.cell(row, 1), ed.name,
                    Font(name="Calibri", size=10, bold=True), align=la)
        zebra = _fill(C_ZEBRA) if ei % 2 == 1 else None
        total = 0
        for mi in range(12):
            cnt = sum(1 for _, c in ed.shifts[mi].items()
                      if c in ("U", "U    alt"))
            _apply_cell(ws.cell(row, 2 + mi), cnt or "",
                        Font(name="Calibri", size=10), zebra, ca)
            total += cnt
            sum_per_month[mi] += cnt

        # Gesamt genommen
        _apply_cell(ws.cell(row, gc), total,
                    Font(name="Calibri", size=10, bold=True), zebra, ca)
        sum_total += total

        # Anspruch
        _apply_cell(ws.cell(row, rc), DEFAULT_URLAUB_TAGE,
                    Font(name="Calibri", size=10), zebra, ca)

        # Rest
        rest = DEFAULT_URLAUB_TAGE - total
        color = C_RED if rest < 0 else ("008000" if rest > 5 else C_WARN_FG)
        _apply_cell(ws.cell(row, dc), rest,
                    Font(name="Calibri", size=10, bold=True, color=color),
                    zebra, ca)
        row += 1

    # Summenzeile
    sf = _fill(C_SUM_BG)
    _apply_cell(ws.cell(row, 1), "Summe",
                Font(name="Calibri", size=10, bold=True), sf, la)
    for mi in range(12):
        _apply_cell(ws.cell(row, 2 + mi), sum_per_month[mi],
                    Font(name="Calibri", size=10, bold=True), sf, ca)
    _apply_cell(ws.cell(row, gc), sum_total,
                Font(name="Calibri", size=10, bold=True), sf, ca)

    _apply_outer_border(ws, 3, 1, row, dc)

    # Hinweis
    row += 2
    _apply_cell(ws.cell(row, 1),
                f"Anspruch: {DEFAULT_URLAUB_TAGE} Tage/Jahr (anpassbar in Spalte {get_column_letter(rc)})",
                Font(name="Calibri", size=8, italic=True, color="666666"),
                border=None)

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
