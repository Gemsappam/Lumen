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


def clean(data: bytes) -> bytes:
    """The file as-is, minus ETD / ETA and Weight / VW columns (boxes only)."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    ws = wb.worksheets[0]
    head = [str(c.value or "").strip().lower() for c in ws[1]]
    for name in ("eta", "etd", "weight", "vw"):          # chats see boxes only: no weights, no dates
        if name in head:
            ws.delete_cols(head.index(name) + 1)
            head = [str(c.value or "").strip().lower() for c in ws[1]]
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
    (DIR / f"{awb_key}.xlsx").write_bytes(clean(data))
    st = _state()
    eta = info.get("eta")
    st[awb_key] = {"sent": False, "packs": info["packs"], "kg": info[info["use"]], "farms": farms,
                   "arrive": (f"{(eta + timedelta(days=5)):%d.%m}–{(eta + timedelta(days=7)):%d.%m}" if eta else None)}
    _save_state(st)


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
    lines = [f"📋 Поставка Кения · MAWB {awb}", f"{st.get('packs', '?')} кор."]
    lines += [f"• {f['farm']} — {f['packs']} кор." for f in st.get("farms", [])]
    if st.get("arrive"):
        lines.append(f"🛬 Ориентировочное прибытие: {st['arrive']}")
    if packing_farms:
        lines.append("📦 В файле: детализация + пакинг-листы (" + ", ".join(packing_farms) + ")")
    return "\n".join(lines)[:1024]


def breakdown_bytes(awb_key: str) -> bytes:
    return (DIR / f"{awb_key}.xlsx").read_bytes()
