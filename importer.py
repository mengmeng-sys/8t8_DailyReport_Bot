# importer.py - read an uploaded xlsx report back into the bot's data.
# Pure function: read_report(path, original_name) -> (date, data, warnings).
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from fill import ROOM_ROWS, TOUR_ROWS, BAR_ROW, EXPENSE_FIRST, EXPENSE_LAST, DATE_CELL

MAX_UPLOAD = 2_000_000        # 2 MB, checked before downloading
MAX_INSIDE = 20_000_000       # 20 MB uncompressed, zip bomb guard
MAX_TEXT = 40


class BadFile(Exception):
    """A friendly message for the user."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def empty():
    return {"rooms": {}, "tours": {}, "bar": [], "discount": {}, "expenses": [], "cash_check": {}}


def clean_text(v):
    """Remove control characters and limit the length."""
    if v is None:
        return ""
    s = "".join(ch for ch in str(v) if ch == "\n" or ord(ch) >= 32)
    return s.strip()[:MAX_TEXT]


def _num(ws, ref, label):
    """Read a number cell. Only numbers are allowed, never negative."""
    v = ws[ref].value
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadFile(f"{label}: '{v}' is not a number.")
    if v < 0:
        raise BadFile(f"{label}: a negative number ({v}) is not allowed.")
    return float(v)


def _read_date(ws, filename):
    """The report date: cell B23 (a date or text dd/mm/yyyy), else the file name."""
    v = ws[DATE_CELL].value
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", v)
        if m:
            try:
                return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date().isoformat()
            except ValueError:
                pass
    m = re.match(r"(\d{2})[-.](\d{2})[-.](\d{4})", filename or "")
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date().isoformat()
        except ValueError:
            pass
    return None


def _cached(ws, ref):
    v = ws[ref].value
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def read_report(path, original_name=None):
    """Read an uploaded xlsx. Returns (date, data, warnings). Raises BadFile."""
    warnings = []
    path = Path(path)
    # 1. zip bomb and external links
    try:
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_INSIDE:
                raise BadFile("That file is too big inside (zip bomb).")
            if any(n.startswith("xl/externalLinks/") for n in z.namelist()):
                warnings.append("External links were ignored.")
    except zipfile.BadZipFile:
        raise BadFile("That is not a readable Excel file.")
    # 2. open the workbook
    try:
        wb = load_workbook(path, data_only=True, read_only=False)
    except Exception:
        raise BadFile("I cannot read that file. It may be broken or protected by a password.")
    ws = wb.worksheets[0]
    # 3. the input cells
    data = empty()
    for group, rows in (("rooms", ROOM_ROWS), ("tours", TOUR_ROWS)):
        for key, row in rows.items():
            vals = [_num(ws, f"{col}{row}", f"{group} {key}") for col in "DEF"]
            if any(vals):
                data[group][key] = vals
    bar = [_num(ws, f"{col}{BAR_ROW}", "bar") for col in "DEF"]
    if any(bar):
        data["bar"] = bar
    amount = _num(ws, "B10", "discount")
    if amount:
        data["discount"] = {"amount": amount, "reason": clean_text(ws["B11"].value)}
    for row in range(EXPENSE_FIRST, EXPENSE_LAST + 1):
        name = clean_text(ws[f"A{row}"].value)
        amt = _num(ws, f"B{row}", f"expense row {row}")
        if name:
            data["expenses"].append({"name": name, "amount": amt})
    # 4. the date
    d = _read_date(ws, original_name or path.name)
    # 5. compare the recomputed totals with the cached ones in the file
    rev = sum(sum(v) for g in ("rooms", "tours") for v in data[g].values()) + sum(data["bar"])
    cached_rev = _cached(ws, "C27")
    if cached_rev is not None and abs(cached_rev - rev) > 0.01:
        warnings.append(f"The file says revenue {cached_rev:g} but the cells add up to {rev:g}. I used the cells.")
    exp = sum(e["amount"] for e in data["expenses"])
    cached_exp = _cached(ws, "B44")
    if cached_exp is not None and abs(cached_exp - exp) > 0.01:
        warnings.append(f"The file says expenses {cached_exp:g} but the cells add up to {exp:g}. I used the cells.")
    return d, data, warnings
