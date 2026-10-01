"""
Packing list per farm invoice: only farm, MAWB, item and quantity — no prices, no money.
Sent as .xlsx to the chats registered with /packing_here, but ONLY once the invoice has a MAWB.
"""
import io
import json

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from sqlmodel import select

from .models import Invoice, Line, session

F = "Arial"
thin = Side(style="thin", color="BFBFBF")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)


def build_xlsx(inv, lines) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Packing list"
    ws["A1"] = "PACKING LIST"
    ws["A1"].font = Font(name=F, bold=True, size=14)
    ws["A3"], ws["B3"] = "Ферма", inv.farm
    ws["A4"], ws["B4"] = "MAWB", inv.awb
    for r in (3, 4):
        ws.cell(r, 1).font = Font(name=F, bold=True)
        ws.cell(r, 2).font = Font(name=F, bold=True, size=12)
    head = ["Номенклатура", "Количество, шт"]
    for c, h in enumerate(head, 1):
        x = ws.cell(6, c, h)
        x.font = Font(name=F, bold=True)
        x.fill = PatternFill("solid", fgColor="D9E1F2")
        x.border = BOX
        x.alignment = Alignment(horizontal="center")
    r = 7
    for l in lines:
        ws.cell(r, 1, l.name).font = Font(name=F)
        ws.cell(r, 2, int(l.stems) if float(l.stems).is_integer() else l.stems).font = Font(name=F)
        ws.cell(r, 2).number_format = "#,##0"
        for c in (1, 2):
            ws.cell(r, c).border = BOX
        r += 1
    ws.cell(r, 1, "ИТОГО").font = Font(name=F, bold=True)
    ws.cell(r, 2, f"=SUM(B7:B{r - 1})").font = Font(name=F, bold=True)
    ws.cell(r, 2).number_format = "#,##0"
    for c in (1, 2):
        ws.cell(r, c).border = BOX
        ws.cell(r, c).fill = PatternFill("solid", fgColor="FCE4D6")
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 18
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def targets() -> list[dict]:
    from .api import _settings
    return _settings().get("packing_targets") or []


def set_targets(t: list[dict]):
    from .api import SETTINGS, _settings
    SETTINGS.write_text(json.dumps({**_settings(), "packing_targets": t}))
    from .backup import mark_dirty
    mark_dirty()


def pending() -> list:
    """Invoices that have a MAWB and whose packing list hasn't been sent yet."""
    with session() as s:
        out = []
        for i in s.exec(select(Invoice).where(Invoice.packing_sent == False)).all():  # noqa: E712
            if i.awb and i.awb.strip():
                ls = s.exec(select(Line).where(Line.invoice_id == i.id)).all()
                if ls:
                    out.append((i.id, build_xlsx(i, ls), i.farm, i.awb))
        return out


def mark_sent(inv_id: int):
    with session() as s:
        i = s.get(Invoice, inv_id)
        if i:
            i.packing_sent = True
            s.add(i)
            s.commit()
