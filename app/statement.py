"""Farm «statement of account» (Nintanga, Zeeflora, …): which of OUR invoices the farm already counts as paid.

parse(data, mime)   -> {"farm", "rows": [{kind, invoice_no, date, awb, amount}], "balance_printed"}
reconcile(parsed)   -> report text + buttons: paid by the statement but not in our books -> «Отметить оплаченным»
"""
import re
from datetime import datetime

from sqlmodel import select

from .models import Invoice, session

TOOL = {
    "name": "farm_statement",
    "description": "Statement of account from a flower farm: invoices, payments, credits, balance.",
    "input_schema": {"type": "object", "properties": {
        "farm": {"type": "string", "description": "farm (seller) name as printed"},
        "rows": {"type": "array", "description": "EVERY row of the statement, in the order printed", "items": {
            "type": "object", "properties": {
                "kind": {"type": "string", "enum": ["invoice", "payment", "credit"]},
                "invoice_no": {"type": ["string", "null"]},
                "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                "awb": {"type": ["string", "null"], "description": "AWB digits"},
                "amount": {"type": "number", "description": "positive amount in USD"}},
            "required": ["kind", "amount"]}},
        "balance_printed": {"type": ["number", "null"],
                            "description": "final balance as printed (keep its sign: usually negative = customer owes)"}},
        "required": ["farm", "rows"]}}


async def parse(data: bytes, mime: str) -> dict:
    from . import ai
    content = [*ai._blocks(data, mime), {"type": "text", "text": (
        "Это стейтмент (statement of account) плантации для нашей компании. Перепиши ВСЕ строки: инвойсы (номер, дата, "
        "AWB, сумма), платежи (PAYMENT, дата, сумма), кредиты/скидки. Суммы — положительные. Мусор вроде «_x0000_» "
        "и хвосты пробелов из номеров убери.")}]
    return await ai._structured(ai.PARSE_MODEL, "Ты бухгалтер импортёра цветов.", content, TOOL, 16000)


def _digits(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def _d(s):
    try:
        return datetime.strptime(str(s or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def coverage(rows: list) -> tuple[list, float]:
    """FIFO: payments + credits cover invoices oldest first -> [(row, paid_usd, paid_on)], balance (+ = we owe)."""
    invs = [r for r in rows if r.get("kind") == "invoice" and (r.get("amount") or 0) > 0]
    pays = sorted([r for r in rows if r.get("kind") in ("payment", "credit") and (r.get("amount") or 0) > 0],
                  key=lambda r: _d(r.get("date")) or datetime.min)
    invs_sorted = sorted(enumerate(invs), key=lambda x: (_d(x[1].get("date")) or datetime.min, x[0]))
    total_pay = sum(p["amount"] for p in pays)
    cum_pays, run = [], 0.0
    for p in pays:
        run += p["amount"]
        cum_pays.append((run, p.get("date")))
    out, cum_inv = [], 0.0
    for _n, r in invs_sorted:
        before = cum_inv
        cum_inv += r["amount"]
        paid = max(0.0, min(r["amount"], total_pay - before))
        on = next((d for c, d in cum_pays if c >= cum_inv - 0.05), None) if paid >= r["amount"] - 0.05 else None
        out.append((r, round(paid, 2), on))
    owe = round(sum(r["amount"] for r in invs) - total_pay, 2)
    return out, owe


def reconcile(parsed: dict, farm_hint: str = "") -> tuple[str, list]:
    """-> (text, buttons [(label, callback)])"""
    from .api import resolve_farm, farm_balance_text, _norm_name, _farm_keys
    from .calc import norm_awb
    with session() as s:
        f = resolve_farm(s, parsed.get("farm") or "") or (resolve_farm(s, farm_hint) if farm_hint else None)
        farm = f.name if f else (parsed.get("farm") or farm_hint or "?")
        keys = _farm_keys(s, farm) if f else {_norm_name(farm)}
        ours = [i for i in s.exec(select(Invoice)).all() if _norm_name(i.farm) in keys]
        ours_d = [(i.id, _digits(i.invoice_no), norm_awb(i.awb or ""), bool(i.topup_id or i.via_broker), i.invoice_no,
                   i.est_usd or i.invoice_total_usd) for i in ours]
        from .api import _dt
        first = min((_dt(i.invoice_date or "") for i in ours if _dt(i.invoice_date or "")), default=None)
    cov, owe = coverage(parsed.get("rows") or [])
    used, lines, buttons = set(), [], []
    from datetime import timedelta
    since = (first - timedelta(days=10)) if first else datetime.min      # rows from before the bot: not interesting
    recent = [c for c in cov if (_d(c[0].get("date")) or datetime.min) >= since] or cov[-6:]
    for r, paid, on in recent[-15:]:
        nd = _digits(r.get("invoice_no")).lstrip("0")
        hit = next((o for o in ours_d if nd and o[1].endswith(nd) and o[0] not in used), None)
        if not hit and r.get("awb"):
            hit = next((o for o in ours_d if o[2] and o[2] == _digits(r["awb"])[-11:] and o[0] not in used), None)
        st = ("✅ оплачен" + (f" ({on[8:10]}.{on[5:7]})" if on else "")) if paid >= r["amount"] - 0.05 else \
            (f"🟠 частично ${paid:g} из ${r['amount']:g}" if paid > 0 else "🔴 не оплачен")
        if hit:
            used.add(hit[0])
            ourst = "в учёте оплачен" if hit[3] else "в учёте НЕ оплачен"
            flag = " ⚠️" if (paid >= r["amount"] - 0.05) != hit[3] else ""
            if paid >= r["amount"] - 0.05 and not hit[3]:
                buttons.append((f"💳 Отметить оплаченным № {r.get('invoice_no')}", f"pay:i:{hit[0]}"))
        else:
            ourst, flag = "нет в учёте", " ❔"
        awb = _digits(r.get("awb"))
        lines.append(f"• № {r.get('invoice_no') or '?'} · {str(r.get('date') or '')[8:10]}.{str(r.get('date') or '')[5:7]}"
                     + (f" · {awb[:3]}-{awb[3:11]}" if len(awb) >= 11 else "") + f" · ${r['amount']:g} — {st} · {ourst}{flag}")
    missing = [o for o in ours_d if o[0] not in used and not o[3]]
    head = (f"📄 Стейтмент {farm}: по их учёту "
            + (f"мы должны ${owe:,.2f}" if owe > 0.05 else (f"у нас аванс ${-owe:,.2f}" if owe < -0.05 else "расчёт ровный")))
    bp = parsed.get("balance_printed")
    if bp is not None and abs(abs(bp) - abs(owe)) > 0.5:
        head += f" (в самом файле итог напечатан ${bp:,.2f} — у них в таблице расхождение)"
    txt = (head + "\n" + farm_balance_text(farm) + "\n\nПоследние инвойсы в стейтменте:\n" + "\n".join(lines)
           + (("\n\nУ нас не оплачены, а в стейтменте их нет:\n" + "\n".join(f"• № {o[4]} · ≈${o[5] or 0:g}" for o in missing))
              if missing else "")
           + ("\n\n⚠️ Есть инвойсы, которые ферма считает оплаченными, а у нас они не оплачены — отметь кнопками ниже."
              if buttons else ""))
    return txt, buttons
