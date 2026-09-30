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
- Not paid yet (no payment in «Баланс» covers it, or the truck isn't in «Баланс» yet):
  provisional ₽ = $ × (ЦБ today from cbr.ru + 3) / 0.96. (The rate on the truck sheet already has +3,
  so if cbr.ru is unreachable we fall back to sheet rate / 0.96.)
  The next report with the payment replaces it with the real rate.
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
    cb: float = 0.0               # USD rate on the truck sheet (ЦБ)


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
    return sum(1 for n in wb.sheetnames if SHEET_RE.match(n)) >= 1 and (
        "Баланс" in wb.sheetnames or any(_looks_like_truck(wb[n]) for n in wb.sheetnames if SHEET_RE.match(n)))


def _looks_like_truck(ws) -> bool:
    return bool(_find_row(ws, "ЭКВАДОР КОНСОЛИДАЦИЯ", 1) or _find_row(ws, "ИМПОРТ АМС-МСК", 1))


def provisional_rate(sheet_rate: float, today: tuple | None = None) -> tuple[float, str]:
    """Arman's rule while Floratrack hasn't been paid: (ЦБ today + 3) / 0.96.
    today = (rate, note) from cbr.floratrack_rate(); fallback: truck-sheet rate (already ЦБ+3) / 0.96."""
    if today and today[0]:
        return today[0], today[1]
    return (sheet_rate / 0.96 if sheet_rate else 0.0), f"курс листа машины {sheet_rate:.4f} (ЦБ+3) / 0.96 — сайт ЦБ недоступен"


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
    for x in out:
        x.cb = usd
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
        prov = need > 1e-6          # part (or all) of the truck not paid yet
        out[c["row"]] = (rub, need if prov else 0.0, ", ".join(used))
    return out


def parse(data: bytes, today: tuple | None = None) -> Report:
    wb = load_workbook(io.BytesIO(data), data_only=True)
    rep = Report()
    rows = _balance(wb["Баланс"]) if "Баланс" in wb.sheetnames else []
    if not rows:
        rep.warnings.append("В файле нет вкладки «Баланс» — всё посчитано по предварительному курсу (ЦБ + 3) / 0.96")
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
        s = sum(a.usd for a in awbs)
        if abs(s - total) > 1:
            rep.warnings.append(f"{name}: сумма по AWB ${s:.2f} ≠ итог листа ${total:.2f} (в машине есть что-то кроме консолидации/импорта)")
        cb = awbs[0].cb
        prate, pnote = provisional_rate(cb, today)
        if cands:
            c = cands[0]
            used_rows.add(c["row"])
            rub_cov, usd_left, note = rates[c["row"]]
            share_cov = (c["usd"] - usd_left) / c["usd"] if c["usd"] else 0
        else:
            rub_cov, usd_left, note, share_cov = 0.0, total, "", 0.0
        for a in awbs:
            # covered part at the payment rate, uncovered part at (ЦБ + 3) / 0.96
            part_cov = a.usd * share_cov
            rub = (rub_cov * (a.usd / total) if total else 0) + (a.usd - part_cov) * prate
            a.rub = round(rub, 2)
            a.rate = a.rub / a.usd if a.usd else 0
            a.provisional = share_cov < 0.9999
            if not a.provisional:
                a.rate_note = f"машина {name}, курс {a.rate:.4f} ₽/$ по оплатам «Баланс» {note or '—'}"
            elif share_cov > 0:
                a.rate_note = (f"машина {name}, курс {a.rate:.4f} — частично оплачено ({note}), остаток по "
                               f"{pnote} — предварительный")
            else:
                a.rate_note = (f"машина {name}, курс {a.rate:.4f} = {pnote} — предварительный, "
                               f"оплаты в «Балансе» ещё нет")
            rep.charges.append(a)
    return rep
