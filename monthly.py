# monthly.py - the month summary: numbers for the screen, and a month xlsx with formulas
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def stats(rows):
    """rows: one dict per day with date, rooms, tours, bar, cash, card, aba, expenses, counted, diff."""
    if not rows:
        return None
    rev = [r["cash"] + r["card"] + r["aba"] for r in rows]
    best = max(range(len(rows)), key=lambda i: rev[i])
    worst = min(range(len(rows)), key=lambda i: rev[i])
    diffs = [r["diff"] for r in rows if r["diff"] is not None]
    expenses = sum(r["expenses"] for r in rows)
    total = sum(rev)
    return {
        "days": len(rows), "cash": sum(r["cash"] for r in rows), "card": sum(r["card"] for r in rows),
        "aba": sum(r["aba"] for r in rows), "rooms": sum(r["rooms"] for r in rows),
        "tours": sum(r["tours"] for r in rows), "bar": sum(r["bar"] for r in rows),
        "revenue": total, "expenses": expenses, "net": total - expenses, "avg": total / len(rows),
        "best": (rows[best]["date"], rev[best]), "worst": (rows[worst]["date"], rev[worst]),
        "counted_days": len(diffs), "short": sum(d for d in diffs if d < -0.004), "over": sum(d for d in diffs if d > 0.004),
    }


def build_xlsx(path, title, rows):
    """A workbook with one row per day (Days) and a summary made of formulas (Summary)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Days"
    font = "Arial"
    head_fill = PatternFill("solid", start_color="1F4E78")
    thin = Side(style="thin", color="BFBFBF")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    heads = ["Date", "Rooms", "Tours", "Bar", "Cash", "Card", "ABA", "Revenue", "Expenses", "Net",
             "Cash counted", "Cash difference"]
    ws.append(heads)
    first, last = 2, len(rows) + 1
    for i, r in enumerate(rows, start=2):
        ws.append([r["date"], r["rooms"], r["tours"], r["bar"], r["cash"], r["card"], r["aba"],
                   f"=SUM(E{i}:G{i})", r["expenses"], f"=H{i}-I{i}", r["counted"], r["diff"]])
    tot = last + 1
    ws.append(["TOTAL"] + [f"=SUM({get_column_letter(c)}{first}:{get_column_letter(c)}{last})" for c in range(2, 11)]
              + [None, f"=SUM(L{first}:L{last})"])
    for row in ws.iter_rows(min_row=1, max_row=tot, max_col=len(heads)):
        for c in row:
            c.border = box
            c.font = Font(name=font, bold=(c.row == 1 or c.row == tot), color="FFFFFF" if c.row == 1 else "000000")
            if c.row == 1:
                c.fill = head_fill
                c.alignment = Alignment(horizontal="center", wrap_text=True)
            elif c.column == 1 and c.row < tot:
                c.number_format = "dd/mm/yyyy"
            elif c.column > 1:
                c.number_format = '#,##0.00;[Red]-#,##0.00;"-"'
    for col, w in zip("ABCDEFGHIJKL", (12, 11, 11, 11, 11, 11, 11, 12, 12, 12, 13, 14)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B2"

    sm = wb.create_sheet("Summary", 0)
    rng = lambda col: f"Days!{col}{first}:{col}{last}"
    lines = [
        (title, None),
        (None, None),
        ("Days with a report", f"=COUNT({rng('A')})"),
        ("Total revenue", f"=Days!H{tot}"),
        ("   Cash", f"=Days!E{tot}"), ("   Card", f"=Days!F{tot}"), ("   ABA", f"=Days!G{tot}"),
        ("   Rooms", f"=Days!B{tot}"), ("   Tours", f"=Days!C{tot}"), ("   Bar", f"=Days!D{tot}"),
        ("Total expenses", f"=Days!I{tot}"),
        ("Net (revenue - expenses)", f"=Days!J{tot}"),
        ("Average revenue per day", f"=AVERAGE({rng('H')})"),
        ("Best day", f"=INDEX({rng('A')},MATCH(MAX({rng('H')}),{rng('H')},0))"),
        ("   revenue", f"=MAX({rng('H')})"),
        ("Worst day", f"=INDEX({rng('A')},MATCH(MIN({rng('H')}),{rng('H')},0))"),
        ("   revenue", f"=MIN({rng('H')})"),
        (None, None),
        ("Cash check: days counted", f"=COUNT({rng('K')})"),
        ("   total SHORT", f'=SUMIF({rng("L")},"<0")'),
        ("   total OVER", f'=SUMIF({rng("L")},">0")'),
    ]
    for a, b in lines:
        sm.append([a, b])
    sm["A1"].font = Font(name=font, bold=True, size=14)
    for row in sm.iter_rows(min_row=3, max_row=len(lines), max_col=2):
        label, value = row
        label.font = Font(name=font, bold=not str(label.value or "").startswith("   "))
        value.font = Font(name=font)
        value.number_format = '#,##0.00;[Red]-#,##0.00;"-"'
        value.alignment = Alignment(horizontal="right")
    for ref in ("B3", "B19"):
        sm[ref].number_format = "0"
    for ref in ("B14", "B16"):
        sm[ref].number_format = "dd/mm/yyyy"
    sm.column_dimensions["A"].width = 30
    sm.column_dimensions["B"].width = 16
    wb.save(path)