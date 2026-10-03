"""
BiFlorica (broker for Tessa / Plazoleta) balance statement -> the books, by itself.

Rows:  Payment  -> money put on the exchange: $ credited (291) = $ sent (300) − 3 %
       Withdraw -> a purchase: «TESSA - Альстромерия - MixAlstromeria - 70см - $0.32 - 2.0HB», stems, invoice №,
                   amount + 7 % BiFlorica fee
- Every row has an ID -> importing next week's statement (overlapping period) never duplicates anything.
- Each deposit is taken from the latest top-up on or before its date (you can change it with a button).
- Purchases become invoices of Tessa / Plazoleta «paid from the broker balance»; an invoice you uploaded
  yourself with the same number is linked instead of duplicated.
- «Начальный остаток» of the very first statement = the broker balance before the bot.
"""
import io
import re
from datetime import datetime

from sqlmodel import select

from .models import BrokerDeposit, Farm, Invoice, Line, TopUp, session

CODES = {"TESSA": "Tessa", "POSITANO": "Tessa", "PLAZOL": "Plazoleta", "PLAZOLETA": "Plazoleta"}


def is_statement(data: bytes) -> bool:
    try:
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        vals = {str(v).strip().lower() for r in ws.iter_rows(max_row=8, values_only=True) for v in r if v}
    except Exception:
        return False
    return ("операция" in vals and "списание" in vals) or any("biflorica" in v for v in vals)


def parse(data: bytes) -> dict:
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(data), data_only=True).worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    hi = next(i for i, r in enumerate(rows) if any(str(v or "").strip().lower() == "операция" for v in r))
    h = [str(v or "").strip().lower() for v in rows[hi]]
    col = lambda name: h.index(name) if name in h else None
    c = {k: col(v) for k, v in {"id": "id", "date": "дата", "op": "операция", "desc": "описание", "stems": "число стеблей",
                                "hb": "hb", "doc": "номер документа", "start": "начальный остаток", "out": "списание",
                                "fee": "biflorica %", "pay": "платеж", "end": "исходящий остаток", "awb": "awb"}.items()}
    num = lambda r, k: float(r[c[k]]) if c[k] is not None and isinstance(r[c[k]], (int, float)) else 0.0
    ops, opening = [], None
    for r in rows[hi + 1:]:
        if c["op"] is None or not r[c["op"]]:
            continue
        op = str(r[c["op"]]).strip().lower()
        if opening is None:
            opening = num(r, "start")
        ext = str(int(r[c["id"]])) if isinstance(r[c["id"]], (int, float)) else str(r[c["id"]])
        date = str(r[c["date"]])[:10] if r[c["date"]] else ""
        if op == "payment":
            ops.append({"kind": "dep", "ext": ext, "date": date, "usd": num(r, "pay")})
        elif op == "withdraw":
            desc = str(r[c["desc"]] or "")
            inner = re.search(r'"(.+)"', desc)
            parts = [p.strip() for p in (inner.group(1) if inner else desc).split(" - ")]
            code = parts[0].upper() if parts else ""
            farm = CODES.get(code) or (code.title() if code else "?")
            price = next((float(p.replace("$", "")) for p in parts if p.startswith("$")), None)
            item = " ".join(p for p in parts[1:] if not p.startswith("$") and not p.upper().endswith("HB"))
            ops.append({"kind": "buy", "ext": ext, "date": date, "farm": farm, "item": item or desc,
                        "stems": num(r, "stems"), "hb": num(r, "hb"), "price": price,
                        "amount": abs(num(r, "out")), "fee": abs(num(r, "fee")),
                        "doc": str(r[c["doc"]] or "").replace("Инвойс №", "").strip(),
                        "awb": str(r[c["awb"]] or "").strip() if c["awb"] is not None else ""})
    return {"opening": opening or 0.0, "ops": ops, "end": None}


def _topup_for(date: str, tops) -> int | None:
    """Latest top-up on or before the deposit date."""
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return tops[-1].id if tops else None
    best = None
    for t in tops:
        try:
            td = datetime.strptime(t.date, "%d.%m.%Y")
        except ValueError:
            continue
        if td <= d and (best is None or td >= best[0]):
            best = (td, t.id)
    return best[1] if best else (tops[0].id if tops else None)


def import_statement(data: bytes) -> dict:
    from .api import SETTINGS, _settings, marking
    from .backup import mark_dirty
    st = parse(data)
    new_dep, new_buy, linked = [], [], []
    with session() as s:
        tops = sorted(s.exec(select(TopUp)).all(), key=lambda t: t.id)
        seen_dep = {d.ext_id for d in s.exec(select(BrokerDeposit)).all()}
        invs = s.exec(select(Invoice)).all()
        seen_buy = {i.ext_id for i in invs if i.ext_id}
        countries = {f.name: f.country for f in s.exec(select(Farm)).all()}
        for o in st["ops"]:
            if o["kind"] == "dep" and o["ext"] not in seen_dep:
                d = BrokerDeposit(ext_id=o["ext"], date=o["date"], usd_credited=o["usd"],
                                  usd_sent=round(o["usd"] / 0.97, 2), topup_id=_topup_for(o["date"], tops))
                s.add(d); s.flush()
                new_dep.append(d.id)
            elif o["kind"] == "buy" and o["ext"] not in seen_buy:
                dd = datetime.strptime(o["date"], "%Y-%m-%d").strftime("%d.%m.%Y") if o["date"] else ""
                # an invoice you uploaded yourself with this number -> link it
                from .calc import _d
                od = _d(o["date"])
                ph = [i for i in invs if i.farm == o["farm"] and i.via_broker and not i.ext_id
                      and (not od or not _d(i.invoice_date) or abs((_d(i.invoice_date) - od).days) <= 7)]
                ph.sort(key=lambda i: abs(((_d(i.invoice_date) or od) - od).days) if od else 0)
                same = ph[0] if ph else None
                if same:                       # farm invoice uploaded first: money now from the statement
                    same.ext_id, same.topup_id, same.usd_paid = o["ext"], 0, 0
                    same.invoice_total_usd, same.invoice_date = o["amount"], dd or same.invoice_date
                    for l in s.exec(select(Line).where(Line.invoice_id == same.id)).all():
                        s.delete(l)
                    s.add(Line(invoice_id=same.id, name=o["item"], boxes=o["hb"] or None, stems=o["stems"],
                               price_usd=o["price"] or (o["amount"] / o["stems"] if o["stems"] else 0)))
                    s.add(same); linked.append(same.id)
                    invs = [i for i in invs if i.id != same.id]
                    continue
                inv = Invoice(topup_id=0, farm=o["farm"], country=countries.get(o["farm"], ""), client_code=marking(),
                              invoice_no=o["doc"], invoice_date=dd, awb=o["awb"], usd_paid=0, paid=True,
                              paid_date=dd, invoice_total_usd=o["amount"], via_broker=True, ext_id=o["ext"],
                              note="из выписки брокера BiFlorica", alloc_mode="value")
                s.add(inv); s.flush()
                s.add(Line(invoice_id=inv.id, name=o["item"], boxes=o["hb"] or None, stems=o["stems"],
                           price_usd=o["price"] or (o["amount"] / o["stems"] if o["stems"] else 0)))
                new_buy.append(inv.id)
        s.commit()
    if _settings().get("broker_opening") is None:            # first statement: balance before the bot
        SETTINGS.write_text(__import__("json").dumps({**_settings(), "broker_opening": {"usd": st["opening"], "rate": None}}))
    mark_dirty()
    return {"new_dep": new_dep, "new_buy": new_buy, "linked": linked, "opening": st["opening"], "ops": st["ops"]}
