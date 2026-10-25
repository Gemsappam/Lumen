"""Journal of what the bot changes by itself + locks on what a person set by hand.

log(s, inv, field, new, reason)  -> sets inv.<field> = new AND writes a journal line (only if it really changes)
locked(inv_id, field)            -> a person set/undid this field: automatics must not touch it
lock(inv_id, field)              -> remember that
undo(log_id)                     -> put the old value back + lock the field
"""
import json
from datetime import datetime

from sqlmodel import select

from .models import AutoLog, Invoice, session

FIELDS = {"awb": "MAWB", "weight_kg": "кг", "arrived_at": "прибыл", "invoice_date": "дата инвойса",
          "truck": "машина", "merge": "слияние закупок", "line_kg": "вес строки", "opening": "баланс до бота", "boxes": "пакинг-лист"}


def _locks() -> dict:
    from .api import _settings
    return _settings().get("locks") or {}


def locked(inv_id, field: str) -> bool:
    return bool(inv_id) and f"{inv_id}:{field}" in _locks()


def lock(inv_id, field: str):
    from .api import _settings, SETTINGS
    if not inv_id:
        return
    st = _settings()
    lk = dict(st.get("locks") or {})
    lk[f"{inv_id}:{field}"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    SETTINGS.write_text(json.dumps({**st, "locks": lk}, ensure_ascii=False))


def _s(v):
    if v is None or v == "":
        return None
    return f"{v:g}" if isinstance(v, float) else str(v)


def log(s, inv, field: str, new, reason: str) -> bool:
    """Automatic change of one field of an invoice. Returns False (and changes nothing) if a person locked it."""
    if inv.id and locked(inv.id, field):
        return False
    old = getattr(inv, field, None)
    if _s(old) == _s(new):
        return True
    setattr(inv, field, new)
    s.add(inv)
    s.add(AutoLog(ts=datetime.now().strftime("%d.%m.%Y %H:%M"), inv_id=inv.id, farm=inv.farm or "",
                  awb=(new if field == "awb" else inv.awb) or "", field=field, old=_s(old), new=_s(new), reason=reason))
    return True


def note(s, inv_id, farm: str, awb: str, field: str, text: str, reason: str):
    """An event that can't be undone field-by-field (merge of two purchases)."""
    s.add(AutoLog(ts=datetime.now().strftime("%d.%m.%Y %H:%M"), inv_id=inv_id, farm=farm or "", awb=awb or "",
                  field=field, old=None, new=text, reason=reason))


def entries(limit: int = 300) -> list[dict]:
    with session() as s:
        rows = s.exec(select(AutoLog).order_by(AutoLog.id.desc()).limit(limit)).all()
        alive = {i.id for i in s.exec(select(Invoice)).all()}
        return [{**r.model_dump(), "label": FIELDS.get(r.field, r.field),
                 "can_undo": (not r.undone and r.field in FIELDS and r.field not in ("merge", "line_kg", "opening", "boxes") and r.inv_id in alive)}
                for r in rows]


def undo(log_id: int) -> dict:
    with session() as s:
        r = s.get(AutoLog, log_id)
        if not r or r.undone or r.field in ("merge", "line_kg", "opening"):
            raise ValueError("Эту запись нельзя отменить")
        inv = s.get(Invoice, r.inv_id) if r.inv_id else None
        if not inv:
            raise ValueError("Инвойса уже нет")
        v = r.old
        if r.field == "weight_kg":
            v = float(v) if v not in (None, "") else None
        setattr(inv, r.field, v if v is not None else (None if r.field != "awb" else ""))
        r.undone = True
        s.add(inv); s.add(r); s.commit()
        out = {"farm": inv.farm, "field": FIELDS.get(r.field, r.field), "back": r.old}
    lock(r.inv_id, r.field)
    from .backup import mark_dirty
    mark_dirty()
    return out
