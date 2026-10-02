"""
Kenya shipment box breakdown (the TK Kenya «Breakdown.xlsx»: AWB | Shipper | … | Packs | Weight | VW | ETD | ETA).

- You send the file to the bot -> per-farm kg go into the MAWB breakdown (logistics split by kg),
  the file itself is kept for that MAWB.
- Packing lists of KENYAN invoices on that MAWB wait for this file: in the packing chats the
  breakdown goes FIRST, then the packing lists. Ecuador / Colombia are not affected.
"""
import io
import json
import re

from .config import DATA_DIR

DIR = DATA_DIR / "breakdowns"
DIR.mkdir(exist_ok=True)


def is_breakdown(data: bytes) -> bool:
    try:
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        head = [str(c or "").strip().lower() for c in next(ws.iter_rows(max_row=1, values_only=True))]
    except Exception:
        return False
    return "awb" in head and ("packs" in head or "shipper full name" in head)


def parse(data: bytes) -> dict:
    """-> {"awb", "rows": [{farm_raw, packs, weight, vw}], "packs", "weight", "vw", "use": "weight"|"vw"}"""
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(data), data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(c or "").strip().lower() for c in rows[0]]
    col = lambda name: head.index(name) if name in head else None
    ia, ish, ip, iw, iv = col("awb"), col("shipper full name"), col("packs"), col("weight"), col("vw")
    ie = col("eta")
    out, awb, eta = [], None, None
    for r in rows[1:]:
        if ia is None or not r[ia] or not ish or not r[ish]:
            continue
        awb = awb or str(r[ia])
        num = lambda i: float(r[i]) if i is not None and isinstance(r[i], (int, float)) else 0.0
        out.append({"farm_raw": str(r[ish]).strip(), "packs": int(num(ip)), "weight": num(iw), "vw": num(iv)})
        if ie is not None and hasattr(r[ie], "strftime"):
            eta = max(eta, r[ie]) if eta else r[ie]
    tw, tv = sum(x["weight"] for x in out), sum(x["vw"] for x in out)
    # the AWB is charged by the larger of real and volumetric weight -> split by that column
    return {"awb": awb, "rows": out, "packs": sum(x["packs"] for x in out), "weight": tw, "vw": tv,
            "use": "weight" if tw >= tv else "vw", "eta": eta}


def clean(data: bytes, names: dict | None = None) -> bytes:
    """For the chats: AWB | Ферма | Коробки (+ «Общее»). No consignee, origin, dest, house bill, weights, dates."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    ws = wb.worksheets[0]
    head = [str(c.value or "").strip().lower() for c in ws[1]]
    drop = ("eta", "etd", "weight", "vw", "consignee full name", "origin", "dest.", "dest", "house bill")
    for name in drop:                                    # chats see: AWB | Ферма | Коробки
        if name in head:
            ws.delete_cols(head.index(name) + 1)
            head = [str(c.value or "").strip().lower() for c in ws[1]]
    if names:                                            # TK names -> ours: ZEEFLORA LTD -> Zeeflora
        hf = [str(c.value or "").strip().lower() for c in ws[1]]
        if "shipper full name" in hf:
            fc = hf.index("shipper full name") + 1
            for r in range(2, ws.max_row + 1):
                v = ws.cell(r, fc).value
                if isinstance(v, str) and v.strip() in names:
                    ws.cell(r, fc).value = names[v.strip()]
    rename = {"shipper full name": "Ферма", "packs": "Коробки"}
    for c in ws[1]:
        k = str(c.value or "").strip().lower()
        if k in rename:
            c.value = rename[k]
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.strip().upper() == "TOTAL":
                c.value = "Общее"
    # «TOTAL» sat under a deleted column -> put «Общее» next to the boxes total, left of it
    hb = [str(c.value or "") for c in ws[1]]
    if "Коробки" in hb:
        bc = hb.index("Коробки") + 1
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, 1).value in (None, "") and isinstance(ws.cell(r, bc).value, (int, float)):
                ws.cell(r, bc - 1, "Общее")
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 10
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _state() -> dict:
    from .api import _settings
    st = _settings().get("kenya_bd") or {}
    bad = [k for k, v in st.items() if v.get("packs") == "?"]        # built from kg by an older version: drop
    if bad:
        for k in bad:
            st.pop(k, None)
            (DIR / f"{k}.xlsx").unlink(missing_ok=True)
        _save_state(st)
    return st


def _save_state(st: dict):
    from .api import SETTINGS, _settings
    SETTINGS.write_text(json.dumps({**_settings(), "kenya_bd": st}, ensure_ascii=False))
    from .backup import mark_dirty
    mark_dirty()


def store(data: bytes, info: dict, awb_key: str, farms: list[dict]):
    """farms: [{"farm", "packs", "kg"}] with OUR farm names."""
    from datetime import timedelta
    store_rows(awb_key, info["awb"] or awb_key, "Кения", farms, info.get("eta"))


def store_rows(awb_key: str, awb_display: str, country: str, farms: list[dict], eta=None):
    """Any consolidation list (Kenya / Ecuador / Colombia) -> our sheet AWB | Ферма | Коробки (+ Общее)."""
    from datetime import timedelta
    from openpyxl import Workbook
    from openpyxl.styles import Border, Font, PatternFill, Side
    thin = Side(style="thin", color="BFBFBF")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()
    ws = wb.active
    ws.title = "Детализация"
    for c, h in enumerate(["AWB", "Ферма", "Коробки"], 1):
        x = ws.cell(1, c, h); x.font = Font(name="Arial", bold=True); x.border = box
        x.fill = PatternFill("solid", fgColor="D9E1F2")
    for r, f in enumerate(farms, 2):
        for c, v in enumerate([awb_display, f["farm"], f["packs"]], 1):
            x = ws.cell(r, c, v); x.font = Font(name="Arial"); x.border = box
    t = len(farms) + 2
    ws.cell(t, 2, "Общее").font = Font(name="Arial", bold=True)
    ws.cell(t, 3, sum(f["packs"] or 0 for f in farms)).font = Font(name="Arial", bold=True)
    for col, w in zip("ABC", (16, 30, 10)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO(); wb.save(buf)
    (DIR / f"{awb_key}.xlsx").write_bytes(buf.getvalue())
    st = _state()
    st[awb_key] = {"sent": False, "country": country, "packs": sum(f["packs"] or 0 for f in farms),
                   "kg": round(sum(f.get("kg") or 0 for f in farms), 1), "farms": farms,
                   "arrive": (f"{(eta + timedelta(days=5)):%d.%m}–{(eta + timedelta(days=7)):%d.%m}" if eta else None)}
    _save_state(st)


# ---- Ecuador: «prealerta» xlsx (UIO -> AMS) ---------------------------------------------------------
def is_prealert(data: bytes) -> bool:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            vals = {str(v).strip().upper() for r in ws.iter_rows(max_row=40, values_only=True) for v in r if v}
            if "AWB/BL" in vals and "EXPORTER" in vals:
                return True
    except Exception:
        pass
    return False


def parse_prealert(data: bytes) -> dict:
    from datetime import datetime
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(data), data_only=True).worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    awb, eta, origin, out = None, None, None, []
    for r in rows:
        cells = [str(v).strip() if v is not None else "" for v in r]
        up = [c.upper() for c in cells]
        if "AWB/BL" in up and not awb:
            awb = next((c for c in cells[up.index("AWB/BL") + 1:] if c), None)
        if "ORIGIN" in up:
            origin = next((c for c in cells[up.index("ORIGIN") + 1:] if c), origin)
        if "AMS" in up and up.index("AMS") + 1 < len(cells):                  # tracking leg ending in AMS
            i = up.index("AMS")
            for c in cells[i + 1:]:
                try:
                    eta = datetime.strptime(c, "%d/%m/%y"); break
                except ValueError:
                    continue
    hdr = next((i for i, r in enumerate(rows) if any(str(v or "").strip().upper() == "EXPORTER" for v in r)), None)
    if hdr is not None:
        h = [str(v or "").strip().upper() for v in rows[hdr]]
        ie = h.index("EXPORTER")
        ip = h.index("PCS RECEIVED") if "PCS RECEIVED" in h else (h.index("PCS COORD.") if "PCS COORD." in h else None)
        for r in rows[hdr + 1:]:
            name = str(r[ie] or "").strip()
            if not name or name.upper() == "TOTAL":
                continue
            out.append({"farm_raw": name, "packs": int(float(r[ip] or 0)) if ip is not None else 0, "kg": 0.0})
    return {"awb": awb, "rows": out, "eta": eta, "country": "Колумбия" if origin == "BOG" else "Эквадор"}


def has(awb_key: str) -> bool:
    """Kenyan packing lists go ONLY after the TK Kenya breakdown FILE for this MAWB."""
    return awb_key in _state() and (DIR / f"{awb_key}.xlsx").exists() and bool(_state()[awb_key].get("from_file", True))


def is_sent(awb_key: str) -> bool:
    return bool(_state().get(awb_key, {}).get("sent"))


def mark_sent(awb_key: str, sent=True):
    st = _state()
    if awb_key in st:
        st[awb_key]["sent"] = sent
        _save_state(st)


def caption(awb_key: str, packing_farms: list[str]) -> str:
    """Chat message: boxes only (no kg)."""
    st = _state().get(awb_key, {})
    awb = f"{awb_key[:3]}-{awb_key[3:]}" if len(awb_key) == 11 else awb_key
    lines = [f"📋 Поставка {st.get('country') or 'Кения'} · MAWB {awb}", f"{st.get('packs', '?')} кор."]
    lines += [f"• {f['farm']} — {f['packs']} кор." for f in st.get("farms", [])]
    if st.get("arrive"):
        lines.append(f"🛬 Ориентировочное прибытие: {st['arrive']}")
    if packing_farms:
        lines.append("📦 В файле: детализация + пакинг-листы (" + ", ".join(packing_farms) + ")")
    return "\n".join(lines)[:1024]


def breakdown_bytes(awb_key: str) -> bytes:
    return (DIR / f"{awb_key}.xlsx").read_bytes()
