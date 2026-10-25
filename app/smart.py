"""Unknown document -> strong AI explains it and proposes actions as buttons. Daily cap keeps the cost small."""
import json
import re
import uuid
from datetime import date, datetime, timedelta

from sqlmodel import select

from .models import Invoice, Logistics, session

DAILY_CAP = 15
PENDING: dict = {}      # token -> action dict (freight credit waiting for the button)


def _books() -> str:
    from .api import _dt
    since = datetime.now() - timedelta(days=75)
    rows = []
    with session() as s:
        for i in s.exec(select(Invoice)).all():
            d = _dt(i.invoice_date or "")
            if d and d < since:
                continue
            rows.append(f"инвойс | {i.farm} | №{i.invoice_no} | {i.invoice_date} | MAWB {i.awb or '?'} | "
                        f"${(i.usd_paid if i.topup_id else (i.est_usd or i.invoice_total_usd)) or 0:g} | "
                        + ("оплачен" if (i.topup_id or i.via_broker) else "НЕ оплачен"))
        for l in s.exec(select(Logistics)).all():
            rows.append(f"логистика | {l.provider} | MAWB {l.awb} | ${l.usd or 0:g} | "
                        + ("оплачен" if l.topup_id or (l.paid and l.rub) else "НЕ оплачен"))
    return "\n".join(rows[-220:])


def allowed() -> bool:
    from .api import _settings, SETTINGS
    st = _settings()
    day = st.get("smart_day") or {}
    today = date.today().isoformat()
    n = day.get("n", 0) if day.get("date") == today else 0
    if n >= DAILY_CAP:
        return False
    SETTINGS.write_text(json.dumps({**st, "smart_day": {"date": today, "n": n + 1}}, ensure_ascii=False))
    return True


async def analyze(data: bytes, mime: str, note: str = "") -> tuple[str, list]:
    """-> (text, buttons [(label, callback)])"""
    from . import ai
    from .api import resolve_farm, _norm_name, _farm_keys
    r = await ai.analyze_document(data, mime, _books(), note)
    txt = (f"🧠 Разобрал сильным ИИ: {r.get('what') or 'документ'}\n\n{r.get('summary') or ''}"
           + (("\n\n⚠️ Не сходится с учётом:\n" + "\n".join("• " + d for d in r.get("differences") or []))
              if r.get("differences") else ""))
    buttons = []
    for a in r.get("actions") or []:
        if a.get("type") == "mark_paid" and a.get("invoice_no"):
            nd = re.sub(r"\D", "", a["invoice_no"]).lstrip("0")
            with session() as s:
                f = resolve_farm(s, a.get("farm") or "")
                keys = _farm_keys(s, f.name) if f else set()
                hit = next((i for i in s.exec(select(Invoice)).all()
                            if (not keys or _norm_name(i.farm) in keys) and nd
                            and re.sub(r"\D", "", i.invoice_no or "").endswith(nd) and not i.topup_id), None)
            if hit:
                buttons.append((f"💳 Отметить оплаченным: {hit.farm} №{hit.invoice_no}", f"pay:i:{hit.id}"))
        elif a.get("type") == "freight_credit" and a.get("awb") and a.get("amount_usd"):
            tok = uuid.uuid4().hex[:8]
            PENDING[tok] = a
            buttons.append((f"➖ Кредит −${abs(a['amount_usd']):g} на MAWB {a['awb']}", f"sc:{tok}"))
    return txt, buttons


def apply_credit(tok: str) -> str:
    from .ai import find_mawb
    from .api import LogisticsIn, save_logistics, leg_of
    a = PENDING.pop(tok, None)
    if not a:
        raise ValueError("устарело — пришли документ ещё раз")
    awb = find_mawb(a["awb"]) or a["awb"]
    prov = a.get("provider") or "Expolanka"
    save_logistics(LogisticsIn(topup_id=None, paid=False, awb=awb, leg=leg_of(prov, None), provider=prov,
                               invoice_no="кредит", usd=-abs(float(a["amount_usd"])), rub=None,
                               note="кредит по разбору сильного ИИ: " + (a.get("reason") or "")[:200]), None, 0, 0)
    return (f"➖ Кредит −${abs(a['amount_usd']):g} записан на MAWB {awb} ({prov}) как неоплаченная логистика. "
            "Укажи пополнение, в котором его зачли: «В пути» → «Неоплаченная логистика» → «Оплачен».")
