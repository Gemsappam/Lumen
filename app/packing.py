"""
Packing list per farm invoice: only farm, MAWB, item and quantity — no prices, no money.
Sent as .xlsx to the chats registered with /packing_here, but ONLY once the invoice has a MAWB.
"""
import io
import json
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from sqlmodel import select

from .models import Invoice, Line, session

F = "Arial"
thin = Side(style="thin", color="BFBFBF")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)


def write_sheet(ws, inv, lines):
    """One packing list on a worksheet: farm, MAWB, items and quantities — no prices."""
    ws["A1"] = "PACKING LIST"
    ws["A1"].font = Font(name=F, bold=True, size=14)
    ws["A3"], ws["B3"] = "Ферма", inv.farm
    ws["A4"], ws["B4"] = "MAWB", inv.awb
    for r in (3, 4):
        ws.cell(r, 1).font = Font(name=F, bold=True)
        ws.cell(r, 2).font = Font(name=F, bold=True, size=12)
    for c, h in enumerate(["Номенклатура", "Количество, шт"], 1):
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


def _sheet_title(name: str, used: set) -> str:
    t = re.sub(r"[\\/*?:\[\]]", " ", name)[:28] or "Ферма"
    base, n = t, 2
    while t in used:
        t, n = f"{base[:25]} {n}", n + 1
    used.add(t)
    return t


def build_xlsx(inv, lines) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Packing list"
    write_sheet(ws, inv, lines)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def bundle(inv_ids: list[int], breakdown_bytes: bytes | None = None) -> bytes:
    """ONE file for a MAWB: [Детализация (без ETD/ETA)] + a sheet per farm packing list."""
    from openpyxl import load_workbook
    if breakdown_bytes:
        wb = load_workbook(io.BytesIO(breakdown_bytes))
        wb.worksheets[0].title = "Детализация"
    else:
        wb = Workbook()
        wb.remove(wb.active)
    used = set(wb.sheetnames)
    with session() as s:
        for iid in inv_ids:
            inv = s.get(Invoice, iid)
            ls = s.exec(select(Line).where(Line.invoice_id == iid)).all()
            write_sheet(wb.create_sheet(_sheet_title(inv.farm, used)), inv, ls)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def targets() -> list[dict]:
    """Packing chats. If a chat has a topic registered, the same chat without a topic (= General) is dropped."""
    from .api import _settings
    ts = _settings().get("packing_targets") or []
    with_topic = {t["chat_id"] for t in ts if t.get("thread_id")}
    clean = [t for t in ts if t.get("thread_id") or t["chat_id"] not in with_topic]
    if len(clean) != len(ts):
        set_targets(clean)
    return clean


def set_targets(t: list[dict]):
    from .api import SETTINGS, _settings
    SETTINGS.write_text(json.dumps({**_settings(), "packing_targets": t}))
    from .backup import mark_dirty
    mark_dirty()


def _is_kenya(s, inv) -> bool:
    from .api import _country_of
    return _country_of(s, inv) == "Кения"


def pending() -> list:
    """Invoices that have a MAWB and whose packing list hasn't been sent yet.
    Kenyan ones wait until the box breakdown of their MAWB has been sent (kbreak)."""
    from .calc import norm_awb
    from . import kbreak
    with session() as s:
        out = []
        for i in s.exec(select(Invoice).where(Invoice.packing_sent == False)).all():  # noqa: E712
            if i.awb and i.awb.strip():
                k = norm_awb(i.awb)
                if not kbreak.has(k):
                    continue                       # hold: no consolidation list for this MAWB yet (any country)
                if s.exec(select(Line).where(Line.invoice_id == i.id)).first():
                    out.append((i.id, i.farm, i.awb, k))
        return out


def awbs_in_transit() -> list[dict]:
    """MAWBs with goods in transit — for «which shipment to send to the chats?»."""
    from .calc import norm_awb
    from . import kbreak
    by = {}
    with session() as s:
        for i in s.exec(select(Invoice)).all():
            if i.arrived_at or not (i.awb or "").strip():
                continue
            k = norm_awb(i.awb)
            d = by.setdefault(k, {"key": k, "awb": i.awb, "farms": [], "country": i.country or ""})
            d["farms"].append(i.farm)
    st = kbreak._state()
    for k, d in by.items():
        d["has_bd"] = kbreak.has(k)
        d["country"] = (st.get(k) or {}).get("country") or d["country"]
    return sorted(by.values(), key=lambda d: d["awb"])


def in_transit_all(awb_key: str | None = None) -> tuple[list, list]:
    """Every invoice still in transit (or only of one MAWB): (with MAWB -> items, without MAWB -> farms skipped)."""
    from .calc import norm_awb
    with session() as s:
        out, skipped = [], []
        for i in s.exec(select(Invoice)).all():
            if i.arrived_at:
                continue
            if awb_key and norm_awb(i.awb) != awb_key:
                continue
            if not (i.awb and i.awb.strip()):
                skipped.append(i.farm)
                continue
            if s.exec(select(Line).where(Line.invoice_id == i.id)).first():
                from .calc import norm_awb
                out.append((i.id, i.farm, i.awb, norm_awb(i.awb)))
        return out, skipped


def mark_sent(inv_id: int):
    with session() as s:
        i = s.get(Invoice, inv_id)
        if i:
            i.packing_sent = True
            s.add(i)
            s.commit()
