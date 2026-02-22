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
- Legende mit Uhrzeiten
"""

import calendar
import datetime
import os
from collections import OrderedDict

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

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

# Mitarbeiter die nur zur Übersicht sind (Sekretariat) – beim Druck ausblenden
SEKRETARIAT_MA = {"Schmidt", "Radimersky"}

# Feiertage Baden-Württemberg 2026
FEIERTAGE = [
    datetime.date(2026, 1, 1),   datetime.date(2026, 1, 6),
    datetime.date(2026, 4, 3),   datetime.date(2026, 4, 6),
    datetime.date(2026, 5, 1),   datetime.date(2026, 5, 14),
    datetime.date(2026, 5, 25),  datetime.date(2026, 6, 4),
    datetime.date(2026, 8, 15),  datetime.date(2026, 10, 3),
    datetime.date(2026, 11, 1),  datetime.date(2026, 12, 25),
    datetime.date(2026, 12, 26),
]

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

# Schichtfarben: fill, font_color
SHIFT_COLORS = {
    "U":       (C_URLAUB, None),
    "U    alt": (C_URLAUB, None),
    "EH":      (C_EH, C_RED),
    "KU":      (C_KU, None),
    "A":       (C_AUSGLEICH, None),
    "GT":      (None, C_RED),
    "NST":     (None, C_RED),
    "Fobi":    (None, C_RED),
    "T-ZUG":   (C_URLAUB, None),
    "T-ZG":    (C_URLAUB, None),
}

# Schichtbeschreibungen mit Uhrzeiten (wie im Proto)
SHIFT_DESCRIPTIONS = OrderedDict([
    ("U", "Urlaub"),
    ("EH 08:00 -17:00", "Erste Hilfe"),
    ("KU 08:30 - 16:30", "Kuppenheim"),
    ("A", "AU"),
    ("GT", "Gleittag"),
    ("NST", "Ausgleichstag"),
    ("Fobi", "Fortbildung"),
    ("FI 06:00 - 14:00", "Frühschicht I"),
    ("FII 06:00-14:00", "Frühschicht II"),
    ("SI 14:00 - 22:00", "Spätschicht I"),
    ("SII 14:00 -21:00", "Spätschicht II"),
    ("NI 22:00 -06:00", "Nachtschicht I"),
    ("DI 08:00 - 15:30", "Dienst I"),
    ("DII 08:30 - 16:00", "Dienst II"),
    ("KT IRTAZ", "Koordinationstätigkeit"),
    ("SD Individuell", "Sonderdienst"),
])

# Dropdown-Liste: Alle Schichtcodes die der User auswählen kann
DROPDOWN_SHIFTS = [
    "FI", "FII", "SI", "SII", "NI",
    "DI", "DII", "KU", "EH", "SD",
    "GT", "NST", "KT", "Fobi",
    "U", "A", "T-ZUG",
]

THIN = Border(
    left=Side("thin", C_BORDER), right=Side("thin", C_BORDER),
    top=Side("thin", C_BORDER), bottom=Side("thin", C_BORDER),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class Employee:
    def __init__(self, name, irtaz=8.0):
        self.name = name
        self.irtaz = irtaz
        self.shifts = {m: {} for m in range(12)}  # month_idx -> {day: code}


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


# ---------------------------------------------------------------------------
# Layout-Mapping: Welche Zeile hat welcher Monat/MA im Eingabe-Sheet?
# Wird beim Erstellen des Eingabe-Sheets gespeichert und für Formeln benutzt.
# ---------------------------------------------------------------------------
class LayoutMap:
    """Speichert die Zeilen-Positionen der MA pro Monat im Eingabe-Sheet."""
    def __init__(self):
        # month_idx -> {emp_name: row_number_in_eingabe_sheet}
        self.emp_rows = {}
        # month_idx -> first_data_column (always 2, col B = day 1)
        self.data_col_start = 2  # col B


# ---------------------------------------------------------------------------
# Daten extrahieren
# ---------------------------------------------------------------------------
def extract_data(src):
    print(f"Lese {src} ...")
    wb = load_workbook(src, data_only=True)
    dp = wb["Dienstplan"]
    mo = wb["Monat"]

    # Mitarbeiter (Band 1: rows 8-19, col B)
    employees = []
    for r in range(8, 20):
        n = dp.cell(r, 2).value
        if n:
            employees.append(str(n).strip())
    print(f"  {len(employees)} MA: {', '.join(employees)}")

    # IRTAZ (Monat col Z rows 3-14)
    irtaz = {}
    for i, emp in enumerate(employees):
        v = mo.cell(3 + i, 26).value
        irtaz[emp] = float(v) if v else 8.0

    emps = [Employee(n, irtaz.get(n, 8.0)) for n in employees]

    # Schichtdaten: Band1 Jan-Jun rows 8-19, Band2 Jul-Dez rows 25-36
    for months, emp_row0 in [(range(1, 7), 8), (range(7, 13), 25)]:
        col = 3  # Start col C
        for month in months:
            dim = calendar.monthrange(YEAR, month)[1]
            for day in range(1, dim + 1):
                for ei in range(len(employees)):
                    v = dp.cell(emp_row0 + ei, col).value
                    if v is not None:
                        emps[ei].shifts[month - 1][day] = str(v).strip()
                col += 1

    # Schichttypen + Stunden aus Monat-Sheet
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
                shift_hours[str(code).strip()] = None  # IRTAZ

    wb.close()
    total = sum(len(e.shifts[m]) for e in emps for m in range(12))
    print(f"  {total} Schichteinträge, {len(shift_types)} Typen")
    return emps, shift_types, shift_hours


# ---------------------------------------------------------------------------
# Sheet-Generatoren
# ---------------------------------------------------------------------------
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


def create_eingabe(ws, emps):
    """Jahresplan-Sheet mit allen 12 Monaten – HAUPT-EINGABE mit Dropdowns."""
    print("Erstelle Eingabe-Sheet ...")
    hdr_fill = _fill(C_HDR_BG)
    we_fill = _fill(C_WEEKEND)
    hdr_font = Font(name="Calibri", size=9, bold=True, color=C_HDR_FG)
    ca = Alignment(horizontal="center", vertical="center")
    la = Alignment(horizontal="left", vertical="center")

    ws.column_dimensions["A"].width = 14
    _apply_cell(ws.cell(1, 1), f"Dienstplan {YEAR}",
                Font(name="Calibri", size=14, bold=True), border=None)

    # Dropdown-Validierung erstellen
    dropdown_list = ",".join(DROPDOWN_SHIFTS)
    dv = DataValidation(
        type="list",
        formula1=f'"{dropdown_list}"',
        allow_blank=True,
        showDropDown=False,  # In openpyxl ist False = Dropdown ANZEIGEN
        showErrorMessage=True,
        errorTitle="Ungültige Schicht",
        error="Bitte wähle eine gültige Schicht aus der Liste.",
        showInputMessage=True,
        promptTitle="Schicht",
        prompt="Schicht auswählen oder frei eingeben",
    )
    # Soft validation: allow manual entry too
    dv.errorStyle = "warning"
    ws.add_data_validation(dv)

    layout = LayoutMap()
    row = 3

    for mi in range(12):
        mn = mi + 1
        dim = calendar.monthrange(YEAR, mn)[1]
        layout.emp_rows[mi] = {}

        # Monatsname
        _apply_cell(ws.cell(row, 1), MONTHS_DE[mi],
                    Font(name="Calibri", size=11, bold=True), border=None)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            if _is_ferien(dt):
                ws.cell(row, 1 + d).fill = _fill(C_URLAUB)
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

        # Tag-Nummern
        _apply_cell(ws.cell(row, 1), "Tag", hdr_font, hdr_fill, ca)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            f = we_fill if (_is_weekend(dt) or _is_feiertag(dt)) else hdr_fill
            fn = Font(name="Calibri", size=9, bold=True,
                      color="000000" if f == we_fill else C_HDR_FG)
            _apply_cell(ws.cell(row, 1 + d), d, fn, f, ca)
        row += 1

        # Wochentag
        _apply_cell(ws.cell(row, 1), "WT", hdr_font, hdr_fill, ca)
        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            f = we_fill if (_is_weekend(dt) or _is_feiertag(dt)) else hdr_fill
            fn = Font(name="Calibri", size=8,
                      color="000000" if f == we_fill else C_HDR_FG)
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

                # Dropdown auf jede Datenzelle
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
        row += 1  # Leerzeile

    for d in range(1, 32):
        ws.column_dimensions[get_column_letter(1 + d)].width = 5.5
    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    print(f"  {row} Zeilen")
    return layout


def create_month(wb, mi, emps, layout):
    """Druckfertiges Monats-Sheet mit Formeln aus dem Eingabe-Sheet."""
    mn = mi + 1
    name = MONTHS_DE[mi]
    ws = wb.create_sheet(title=name)
    dim = calendar.monthrange(YEAR, mn)[1]

    hdr_fill = _fill(C_HDR_BG)
    we_fill = _fill(C_WEEKEND)
    hdr_font = Font(name="Calibri", size=9, bold=True, color=C_HDR_FG)
    ca = Alignment(horizontal="center", vertical="center", wrap_text=True)
    la = Alignment(horizontal="left", vertical="center")

    ws.column_dimensions["A"].width = 14

    # Titel
    _apply_cell(ws.cell(1, 1), f"Dienstplan {name} {YEAR}",
                Font(name="Calibri", size=14, bold=True), border=None)
    _apply_cell(ws.cell(2, 1),
                f"Arbeitstage: {_arbeitstage(YEAR, mn)}  |  Samstage: {_samstage(YEAR, mn)}",
                Font(name="Calibri", size=10, color="666666"), border=None)
    row = 4

    # Tag
    _apply_cell(ws.cell(row, 1), "Tag", hdr_font, hdr_fill, ca)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        f = we_fill if (_is_weekend(dt) or _is_feiertag(dt)) else hdr_fill
        fn = Font(name="Calibri", size=9, bold=True,
                  color="000000" if f == we_fill else C_HDR_FG)
        _apply_cell(ws.cell(row, 1 + d), d, fn, f, ca)
    row += 1

    # WT
    _apply_cell(ws.cell(row, 1), "WT", hdr_font, hdr_fill, ca)
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        f = we_fill if (_is_weekend(dt) or _is_feiertag(dt)) else hdr_fill
        fn = Font(name="Calibri", size=8,
                  color="000000" if f == we_fill else C_HDR_FG)
        _apply_cell(ws.cell(row, 1 + d), WEEKDAYS_DE[dt.weekday()], fn, f, ca)
    row += 1

    # KW
    _apply_cell(ws.cell(row, 1), "KW", hdr_font, hdr_fill, ca)
    last_kw = None
    for d in range(1, dim + 1):
        dt = datetime.date(YEAR, mn, d)
        kw = _kw(dt)
        c = ws.cell(row, 1 + d)
        c.alignment = ca
        c.border = THIN
        f = we_fill if (_is_weekend(dt) or _is_feiertag(dt)) else hdr_fill
        c.fill = f
        if kw != last_kw:
            c.value = kw
            c.font = Font(name="Calibri", size=8, italic=True, color=C_HDR_FG)
        last_kw = kw
    row += 1

    # --- MA-Zeilen mit Formeln aus dem Eingabe-Sheet ---
    # Trenne reguläre MA und Sekretariats-MA
    regular_emps = [e for e in emps if e.name not in SEKRETARIAT_MA]
    sekr_emps = [e for e in emps if e.name in SEKRETARIAT_MA]

    first_sekr_row = None

    for ei, ed in enumerate(regular_emps + sekr_emps):
        is_sekr = ed.name in SEKRETARIAT_MA
        if is_sekr and first_sekr_row is None:
            first_sekr_row = row

        _apply_cell(ws.cell(row, 1), ed.name,
                    Font(name="Calibri", size=9, bold=True,
                         italic=is_sekr, color="999999" if is_sekr else "000000"),
                    align=la)
        zebra = _fill(C_ZEBRA) if ei % 2 == 1 else None

        # Zeile im Eingabe-Sheet für diesen MA/Monat
        src_row = layout.emp_rows[mi].get(ed.name)

        for d in range(1, dim + 1):
            dt = datetime.date(YEAR, mn, d)
            c = ws.cell(row, 1 + d)
            c.alignment = ca
            c.border = THIN

            # Formel: Verweis auf Eingabe-Sheet
            src_col_letter = get_column_letter(1 + d)
            if src_row:
                c.value = f"=IF(Dienstplan!{src_col_letter}{src_row}=\"\",\"\",Dienstplan!{src_col_letter}{src_row})"

            # Formatierung: Wochenende/Feiertag oder Zebra
            if _is_weekend(dt) or _is_feiertag(dt):
                c.fill = we_fill
            elif zebra:
                c.fill = zebra

            c.font = Font(name="Calibri", size=8)

        row += 1

    last_data_row = row - 1
    row += 1

    # --- Legende (links) + Unterschriften (rechts) nebeneinander ---
    leg_row = row
    _apply_cell(ws.cell(row, 1), "Legende:",
                Font(name="Calibri", size=9, bold=True), border=None)

    items = list(SHIFT_DESCRIPTIONS.items())
    half = (len(items) + 1) // 2

    for i, (code, desc) in enumerate(items):
        col_off = 0 if i < half else 5
        r = leg_row + 1 + (i if i < half else i - half)

        # Farbe für den Code-Teil der Legende
        code_key = code.split()[0] if " " in code else code
        bg, fg = SHIFT_COLORS.get(code_key, (None, None))
        _apply_cell(ws.cell(r, 1 + col_off), code,
                    Font(name="Calibri", size=8, bold=True, color=fg or "000000"),
                    _fill(bg) if bg else None,
                    Alignment(horizontal="left", vertical="center"))
        _apply_cell(ws.cell(r, 2 + col_off), desc,
                    Font(name="Calibri", size=8), border=None, align=la)

    leg_end_row = leg_row + 1 + half

    # Wochenende-Legende
    _apply_cell(ws.cell(leg_end_row, 1), "", border=THIN, fill=we_fill)
    _apply_cell(ws.cell(leg_end_row, 2), "Wochenende / Feiertag",
                Font(name="Calibri", size=8), border=None)

    # --- Unterschriftenfeld RECHTS neben der Legende ---
    # Positioniere ab Spalte nach den Tagen (rechts vom Datenbereich)
    # Nutze die letzten Spalten des Monats
    sig_col = max(dim - 3, 12)  # Mindestens ab Spalte 12
    sig_col_letter = get_column_letter(sig_col)

    _apply_cell(ws.cell(leg_row + 1, sig_col), "Erstellt von:",
                Font(name="Calibri", size=10), border=None)
    ws.merge_cells(start_row=leg_row + 1, start_column=sig_col + 1,
                   end_row=leg_row + 1, end_column=sig_col + 4)
    _apply_cell(ws.cell(leg_row + 1, sig_col + 1), "",
                Font(name="Calibri", size=10), border=Border(
                    bottom=Side("thin", "000000")),
                align=Alignment(horizontal="center"))

    _apply_cell(ws.cell(leg_row + 3, sig_col), "Genehmigt von:",
                Font(name="Calibri", size=10), border=None)
    ws.merge_cells(start_row=leg_row + 3, start_column=sig_col + 1,
                   end_row=leg_row + 3, end_column=sig_col + 4)
    _apply_cell(ws.cell(leg_row + 3, sig_col + 1), "",
                Font(name="Calibri", size=10), border=Border(
                    bottom=Side("thin", "000000")),
                align=Alignment(horizontal="center"))

    _apply_cell(ws.cell(leg_row + 4, sig_col + 1), "Datum / Unterschrift",
                Font(name="Calibri", size=8, italic=True, color="999999"),
                border=None)

    row = max(leg_end_row + 1, leg_row + 6)

    # Spaltenbreiten
    for d in range(1, dim + 1):
        ws.column_dimensions[get_column_letter(1 + d)].width = 5.0

    # --- Druckeinstellungen ---
    ws.freeze_panes = "B7"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.print_area = f"A1:{get_column_letter(1+dim)}{row}"

    # Sekretariats-MA Zeilen beim Druck ausblenden
    if first_sekr_row is not None:
        for r in range(first_sekr_row, first_sekr_row + len(sekr_emps)):
            ws.row_dimensions[r].hidden = True


def create_jahresuebersicht(ws, emps, stypes):
    """Jahresübersicht: MA x Schichttyp-Summen."""
    print("Erstelle Jahresübersicht ...")
    hdr_fill = _fill(C_HDR_BG)
    hdr_font = Font(name="Calibri", size=10, bold=True, color=C_HDR_FG)
    ca = Alignment(horizontal="center", vertical="center")
    la = Alignment(horizontal="left", vertical="center")

    ws.column_dimensions["A"].width = 14
    _apply_cell(ws.cell(1, 1), f"Jahresübersicht {YEAR}",
                Font(name="Calibri", size=14, bold=True), border=None)

    # Header
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

    # Summe
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

    ws.freeze_panes = "B4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4


def create_monatsdetails(ws, emps, stypes, shrs):
    """Monatsdetails: Schichtzählung + Soll/Ist/Diff pro MA pro Monat."""
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

        # Monats-Header
        _apply_cell(ws.cell(row, 1), MONTHS_DE[mi],
                    Font(name="Calibri", size=11, bold=True), border=None)
        _apply_cell(ws.cell(row, 3), "AT:", border=None)
        _apply_cell(ws.cell(row, 4), at,
                    Font(name="Calibri", size=9, bold=True), border=None)
        _apply_cell(ws.cell(row, 6), "Sa:", border=None)
        _apply_cell(ws.cell(row, 7), sa,
                    Font(name="Calibri", size=9, bold=True), border=None)
        row += 1

        # Spalten-Header
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

            # Ist AZ
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

        # Summe
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
        row += 2

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

    print(f"\nSpeichere {DST_FILE} ...")
    wb.save(DST_FILE)
    print(f"Fertig! Sheets: {wb.sheetnames}")
    wb.close()


if __name__ == "__main__":
    main()
