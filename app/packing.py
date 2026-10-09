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


class _PL:                                   # a packing line from the farm invoice (broker farms)
    def __init__(self, d):
        self.name, self.stems, self.boxes = d.get("name") or "", float(d.get("stems") or 0), d.get("boxes")


def write_sheet(ws, inv, lines):
    """One packing list on a worksheet: farm, MAWB, items and quantities — no prices.
    Broker farms (Tessa / Plazoleta): items come from the farm's own invoice, not from the broker statement."""
    if getattr(inv, "packing_lines_json", None):
        lines = [_PL(d) for d in json.loads(inv.packing_lines_json)]
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
    ws.cell(r, 2, sum(float(getattr(l, "stems", 0) or 0) for l in lines)).font = Font(name=F, bold=True)
    ws.cell(r, 2).number_format = "#,##0"
    for c in (1, 2):
        ws.cell(r, c).border = BOX
        ws.cell(r, c).fill = PatternFill("solid", fgColor="FCE4D6")
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 18
    boxes = json.loads(inv.boxes_json) if inv.boxes_json else _boxes_from_lines(lines)
    if boxes:
        _boxes_section(ws, r + 3, boxes)


def _boxes_from_lines(lines) -> list:
    """No box-by-box data from the invoice: a line with N boxes = N boxes of that variety;
    following lines without a box count ride in the previous box (mixed box)."""
    out = []
    for l in lines:
        n = int(float(getattr(l, "boxes", None) or 0))
        if n > 0:
            out.append({"qty": n, "pack": "", "content": [{"name": l.name, "stems_total": l.stems}]})
        elif out:
            out[-1]["content"].append({"name": l.name, "stems_total": l.stems})
    for b in out:
        q = b["qty"] or 1
        b["even"] = all(abs(c["stems_total"] / q - round(c["stems_total"] / q)) < 1e-6 for c in b["content"])
        if b["even"]:
            for c in b["content"]:
                c["stems_per_box"] = int(round(c.pop("stems_total") / q))
    return out


def _merge_box(content: list) -> list:
    out = {}
    for c in content or []:
        k = c.get("name")
        if k in out:
            for f in ("stems_per_box", "stems_total"):
                if f in c:
                    out[k][f] = (out[k].get(f) or 0) + (c.get(f) or 0)
        else:
            out[k] = dict(c)
    return list(out.values())


def _boxes_section(ws, r0: int, boxes: list):
    """«По коробкам»: every physical box numbered, its content one variety per row."""
    ws.cell(r0, 1, "ПО КОРОБКАМ").font = Font(name=F, bold=True, size=12)
    hdr = ["Коробка №", "Упаковка", "Сорт", "Стеблей"]
    for c, h in enumerate(hdr, 1):
        x = ws.cell(r0 + 1, c, h); x.font = Font(name=F, bold=True); x.border = BOX
        x.fill = PatternFill("solid", fgColor="D9E1F2")
    boxes = [{**b, "content": _merge_box(b.get("content"))} for b in boxes]
    r, n = r0 + 2, 0
    for b in boxes:
        q = int(b.get("qty") or 1)
        if b.get("even") is False or any("stems_per_box" not in c for c in b.get("content") or []):
            label = f"{n + 1}–{n + q}" if q > 1 else str(n + 1)
            first = True
            for item in b.get("content") or []:
                st = item.get("stems_total", item.get("stems_per_box"))
                vals = [label if first else "", (b.get("pack") or "") if first else "", item.get("name"),
                        f"{st:g} на {q} кор." if q > 1 else st]
                for c, v in enumerate(vals, 1):
                    x = ws.cell(r, c, v); x.font = Font(name=F, bold=(c == 1)); x.border = BOX
                first = False
                r += 1
            n += q
            continue
        for _ in range(q):
            n += 1
            first = True
            for item in b.get("content") or []:
                vals = [n if first else "", (b.get("pack") or "") if first else "", item.get("name"), item.get("stems_per_box")]
                for c, v in enumerate(vals, 1):
                    x = ws.cell(r, c, v); x.font = Font(name=F, bold=(c == 1)); x.border = BOX
                    if n % 2 == 0:
                        x.fill = PatternFill("solid", fgColor="F2F2F2")
                first = False
                r += 1
    ws.cell(r, 1, f"Всего коробок: {n}").font = Font(name=F, bold=True)
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["D"].width = 10


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
                if i.via_broker and not i.packing_lines_json:
                    continue                       # Tessa / Plazoleta: wait for the FARM invoice
                if unpacked_mix(s, i):
                    continue                       # «Mix Assorted» left in the lines: re-upload the invoice
                if s.exec(select(Line).where(Line.invoice_id == i.id)).first():
                    out.append((i.id, i.farm, i.awb, k))
        return out


MIX_RE = re.compile(r"\b(mix|assorted|select|surtido)\b", re.I)


def unpacked_mix(s, inv) -> bool:
    """Lines still say «Mix / Assorted»: the invoice was read before mixes were unpacked -> don't send it."""
    if getattr(inv, "packing_lines_json", None):
        return any(MIX_RE.search(d.get("name") or "") for d in json.loads(inv.packing_lines_json))
    return any(MIX_RE.search(l.name or "") for l in s.exec(select(Line).where(Line.invoice_id == inv.id)).all())


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
            if i.via_broker and not i.packing_lines_json:
                skipped.append(f"{i.farm} (нет инвойса фермы)")
                continue
            if unpacked_mix(s, i):
                skipped.append(f"{i.farm} (микс не разложен по сортам — удали инвойс и загрузи заново)")
                continue
            if s.exec(select(Line).where(Line.invoice_id == i.id)).first():
                from .calc import norm_awb
                out.append((i.id, i.farm, i.awb, norm_awb(i.awb)))
        return out, skipped


def signature(inv_id: int) -> tuple[str, str]:
    """(«MAWB|farm», hash of what the packing shows) — to never post the same packing twice automatically."""
    import hashlib
    from .calc import norm_awb
    with session() as s:
        i = s.get(Invoice, inv_id)
        if not i:
            return "", ""
        if i.packing_lines_json:
            body = i.packing_lines_json
        else:
            body = json.dumps(sorted((l.name, l.stems) for l in s.exec(select(Line).where(Line.invoice_id == inv_id)).all()))
        return f"{norm_awb(i.awb)}|{i.farm}", hashlib.md5((body + (i.boxes_json or "")).encode()).hexdigest()


def ever_posted(inv_id: int) -> bool:
    """Any packing of this farm for this MAWB was already posted (whatever the content)."""
    from .api import _settings
    k, _h = signature(inv_id)
    return bool(k) and k in (_settings().get("packing_sigs") or {})


def already_posted(inv_id: int) -> bool:
    from .api import _settings
    k, h = signature(inv_id)
    return bool(k) and (_settings().get("packing_sigs") or {}).get(k) == h


def remember_posted(inv_id: int):
    from .api import SETTINGS, _settings
    k, h = signature(inv_id)
    if k:
        st = _settings()
        SETTINGS.write_text(json.dumps({**st, "packing_sigs": {**(st.get("packing_sigs") or {}), k: h}}, ensure_ascii=False))


def mark_sent(inv_id: int):
    with session() as s:
        i = s.get(Invoice, inv_id)
        if i:
            i.packing_sent = True
            s.add(i)
            s.commit()
