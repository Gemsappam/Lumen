"""
Floratrack report (xlsx they send) -> logistics per AWB with the REAL ruble cost.

Rules (from Arman):
- Only two kinds of lines matter: "Импорт АМС-МСК" (Kenya: Amsterdam -> Moscow, continues the Kenyan MAWB)
  and "Консолидация Эквадор / Колумбия" (all-inclusive door to Moscow). Everything else (перелёт,
  хранение, претензии, other tabs) is ignored.
- Their "ИТОГО РУБ" is NOT used. Ruble cost = USD of the truck × the rate we actually paid:
  the "Баланс" tab lists every payment "пп 264 000,00 ₽ -4%" -> $2 910.91 credited. Payments close
  charges first-in-first-out, so each truck gets the effective ₽/$ of the payment(s) that covered it.
- Floratrack writes only the last 4 digits of the AWB. We match them to our MAWBs; kg must agree.
"""
from __future__ import annotations

import io
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from openpyxl import load_workbook

SHEET_RE = re.compile(r"^\s*(\d{1,2})[.,](\d{1,2})\s*[- ]\s*\S+")
PP_RE = re.compile(r"пп\s*((?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:,\d+)?)\s*₽", re.I)


@dataclass
class AwbCharge:
    sheet: str
    date: datetime | None
    kind: str            # import | ecuador | colombia
    last4: str
    kg: float
    boxes: float
    usd: float
    rub: float = 0.0
    rate: float = 0.0
    provisional: bool = False     # truck not (fully) covered by a payment yet
    rate_note: str = ""


@dataclass
class Report:
    charges: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    balance_usd: float = 0.0


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def is_floratrack(wb) -> bool:
    return "Баланс" in wb.sheetnames and sum(1 for n in wb.sheetnames if SHEET_RE.match(n)) >= 1


def _find_row(ws, label, start=1, end=120):
    lab = label.lower()
    for r in range(start, end):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and v.strip().lower().startswith(lab):
            return r
    return None


def _awb_block(ws, header_row):
    """Block like:  'Авианакладная' row with AWB last4 in E,F,G...; next row kg; next row boxes."""
    awb_r = _find_row(ws, "Авианакладная", header_row, header_row + 6)
    if not awb_r:
        return []
    out = []
    for c in range(5, 12):  # E..K
        a = ws.cell(awb_r, c).value
        if a in (None, "", 0):
            continue
        last4 = re.sub(r"\D", "", str(a))[-4:].zfill(4)
        kg = _num(ws.cell(awb_r + 1, c).value)
        boxes = _num(ws.cell(awb_r + 2, c).value)
        if kg:
            out.append((last4, kg, boxes))
    return out


def _truck(ws):
    """Per-AWB USD for one truck sheet + the sheet's own USD total for matching the Баланс row."""
    date = ws["B2"].value if isinstance(ws["B2"].value, datetime) else None
    rate_imp = _num(ws["B9"].value)            # Импорт $/kg
    rate_ec = _num(ws["B6"].value)             # Консолидация Эквадор $/kg
    rate_co = _num(ws["B7"].value)             # Консолидация Колумбия $/kg
    pre_rate = _num(ws["H11"].value) or _num(ws["B21"].value)   # preecooling $/kg
    awb_fee = _num(ws["H12"].value) or _num(ws["B22"].value)    # $ per AWB (IPH)
    total_usd = _num(ws["G27"].value)
    eur, usd = _num(ws["G1"].value), _num(ws["G2"].value)
    eur_to_usd = eur / usd if eur and usd else 1.0          # preecooling is billed in EUR
    out = []
    r = _find_row(ws, "ИМПОРТ АМС-МСК", 30)
    if r:
        for last4, kg, boxes in _awb_block(ws, r):
            out.append(AwbCharge(ws.title, date, "import", last4, kg, boxes, kg * rate_imp + (kg * pre_rate + awb_fee) * eur_to_usd))
    only_kenya = bool(out)
    r = _find_row(ws, "ЭКВАДОР КОНСОЛИДАЦИЯ", 30)
    if r:
        for last4, kg, boxes in _awb_block(ws, r):
            out.append(AwbCharge(ws.title, date, "ecuador", last4, kg, boxes, kg * rate_ec))
    r = _find_row(ws, "КОЛУМБИЯ КОНСОЛИДАЦИЯ", 30)
    if r:
        for last4, kg, boxes in _awb_block(ws, r):
            out.append(AwbCharge(ws.title, date, "colombia", last4, kg, boxes, kg * rate_co))
    if only_kenya and all(x.kind == "import" for x in out) and total_usd:
        # Kenya-only truck: the whole truck total is Kenya's (split by our calc if several MAWBs)
        calc = sum(x.usd for x in out) or 1
        for x in out:
            x.usd = total_usd * x.usd / calc
    # mixed truck: Kenya = import + preecooling (EUR × EUR rate ÷ USD rate), Ecuador/Colombia = consolidation only
    return date, total_usd, out


def _balance(ws):
    """Rows of the Баланс tab in order: charges (E) and payments (F, with ₽ in the note)."""
    rows = []
    for r in range(9, ws.max_row + 1):
        d, truck, _vol, kg, due, paid, _bal, note = (ws.cell(r, c).value for c in range(1, 9))
        if not isinstance(d, datetime):
            continue
        due, paid = _num(due), _num(paid)
        if due:
            rows.append({"row": r, "date": d, "type": "charge", "usd": due, "kg": _num(kg),
                         "truck": str(truck or "").strip(), "note": str(note or "")})
        if paid:
            m = PP_RE.search(str(note or ""))
            rub = float(m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")) if m else None
            rows.append({"row": r, "date": d, "type": "payment", "usd": paid, "rub": rub, "note": str(note or "")})
    return rows


def _fifo(rows, warnings):
    """Match charges to payments first-in-first-out. Returns {balance row: (₽/$, provisional, note)}."""
    pays = deque()
    last_rate = None
    for p in (x for x in rows if x["type"] == "payment"):
        if p["rub"]:
            last_rate = p["rub"] / p["usd"]
        rate = p["rub"] / p["usd"] if p["rub"] else last_rate     # credits without ₽ (compensations): nearest known rate
        pays.append({"usd": p["usd"], "rate": rate, "row": p["row"], "cash": bool(p["rub"])})
    out = {}
    for c in (x for x in rows if x["type"] == "charge"):
        need, rub, used, prov = c["usd"], 0.0, [], False
        while need > 1e-6 and pays:
            p = pays[0]
            take = min(need, p["usd"])
            rub += take * (p["rate"] or 0)
            used.append(f"стр.{p['row']}" + ("" if p["cash"] else " (не деньгами)"))
            p["usd"] -= take
            need -= take
            if p["usd"] <= 1e-6:
                pays.popleft()
        if need > 1e-6:              # not paid yet: estimate with the latest payment rate
            prov = True
            rub += need * (last_rate or 0)
        out[c["row"]] = (rub / c["usd"] if c["usd"] else 0, prov, ", ".join(used))
    return out


def parse(data: bytes) -> Report:
    wb = load_workbook(io.BytesIO(data), data_only=True)
    rep = Report()
    rows = _balance(wb["Баланс"])
    rates = _fifo(rows, rep.warnings)
    charges = [x for x in rows if x["type"] == "charge"]
    rep.balance_usd = sum(x["usd"] for x in rows if x["type"] == "payment") - sum(x["usd"] for x in charges)
    used_rows = set()
    for name in wb.sheetnames:
        if not SHEET_RE.match(name):
            continue
        date, total, awbs = _truck(wb[name])
        if not awbs:
            continue
        # the Баланс line of this truck: same USD total, closest date
        cands = [c for c in charges if c["row"] not in used_rows and abs(c["usd"] - total) < 0.1]
        if date:
            cands.sort(key=lambda c: abs((c["date"] - date).days))
        if not cands:
            rep.warnings.append(f"{name}: машина на ${total:.2f} не найдена во вкладке «Баланс» — курс не определён")
            continue
        c = cands[0]
        used_rows.add(c["row"])
        rate, prov, note = rates[c["row"]]
        s = sum(a.usd for a in awbs)
        if abs(s - total) > 1:
            rep.warnings.append(f"{name}: сумма по AWB ${s:.2f} ≠ итог листа ${total:.2f} (в машине есть что-то кроме консолидации/импорта)")
        for a in awbs:
            a.rate, a.provisional = rate, prov
            a.rub = round(a.usd * rate, 2)
            a.rate_note = f"машина {name}, курс {rate:.4f} ₽/$ по оплатам «Баланс» {note or '—'}" + (
                " — ещё не оплачено полностью, курс предварительный" if prov else "")
            rep.charges.append(a)
    return rep
