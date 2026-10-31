"""Last mile: the truck that takes goods from the ТК warehouse to «Опт Склад Бауманская».

One trip = one sum in ₽ for one or several MAWBs picked up together. It is spread EQUALLY PER STEM over every
invoice of those MAWBs and becomes part of the cost (calc.LASTMILE: MAWB key -> ₽/stem).

settings.json:
  lastmile_trips: [{"id", "rub", "keys": [MAWB keys], "ts", "by"}]
  lastmile_since: date the feature went live — MAWBs picked up earlier are not asked about
"""
import json
import time
from datetime import datetime, timedelta

WAREHOUSE = "Опт Склад Бауманская"


def _st():
    from .api import _settings
    return _settings()


def _write(**kw):
    from .api import SETTINGS
    SETTINGS.write_text(json.dumps({**_st(), **kw}, ensure_ascii=False))
    from .backup import mark_dirty
    mark_dirty()


def since() -> datetime:
    st = _st()
    if not st.get("lastmile_since"):
        _write(lastmile_since=datetime.now().strftime("%Y-%m-%d"))
        st = _st()
    return datetime.strptime(st["lastmile_since"], "%Y-%m-%d")


def trips() -> list:
    return list(_st().get("lastmile_trips") or [])


def trip_of(key: str):
    return next((t for t in trips() if key in t.get("keys", [])), None)


def save_trip(keys: list, rub: float, uid: int = 0) -> dict:
    """A new trip replaces any earlier trip that shared a MAWB with it (re-entering = correcting)."""
    from .calc import norm_awb
    keys = sorted({norm_awb(k) for k in keys if k})
    old = trips()
    keep = [t for t in old if not set(t.get("keys", [])) & set(keys)]
    t = {"id": int(time.time() * 1000), "rub": round(float(rub or 0), 2), "keys": keys,
         "ts": datetime.now().strftime("%d.%m.%Y %H:%M"), "by": uid}
    _write(lastmile_trips=keep + [t])
    return {**t, "replaced": len(old) - len(keep)}


def delete_trip(tid: int) -> bool:
    old = trips()
    keep = [t for t in old if t.get("id") != tid]
    _write(lastmile_trips=keep)
    return len(keep) != len(old)


def per_stem(invs, lines) -> dict:
    """{MAWB key: ₽ per stem} — trip ₽ ÷ all stems of all invoices on the trip's MAWBs."""
    from .calc import norm_awb
    stems_inv = {}
    for l in lines:
        stems_inv[l.invoice_id] = stems_inv.get(l.invoice_id, 0) + (l.stems or 0)
    by_key = {}
    for i in invs:
        if (i.awb or "").strip():
            by_key[norm_awb(i.awb)] = by_key.get(norm_awb(i.awb), 0) + stems_inv.get(i.id, 0)
    out = {}
    for t in trips():
        st = sum(by_key.get(k, 0) for k in t.get("keys", []))
        if st and t.get("rub"):
            for k in t["keys"]:
                out[k] = t["rub"] / st
    return out


def _picked_dt(v):
    try:
        return datetime.strptime(str(v or ""), "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def status(key: str) -> str:
    """full — trip entered (0 ₽ counts); missing — picked up, no trip; not_picked — still at the ТК / on the way."""
    from .api import _picked
    if trip_of(key):
        return "full"
    pk = _picked()
    if key not in pk:
        return "not_picked"
    d = _picked_dt(pk[key])
    return "missing" if d and d >= since() - timedelta(days=7) else "old"


def label(key: str) -> str:
    s = status(key)
    if s == "full":
        t = trip_of(key)
        return (f"полная — вкл. доставку до склада {WAREHOUSE}"
                + (" (забрали сами, 0 ₽)" if not t.get("rub") else ""))
    if s == "missing":
        return f"неполная — нет расхода на доставку до склада {WAREHOUSE}"
    if s == "old":
        return f"без доставки до склада {WAREHOUSE} (груз забран до того, как её начали вносить)"
    return f"неполная — груз ещё не забран со склада ТК (доставка до {WAREHOUSE} не внесена)"


def pending(days: int = 45) -> dict:
    """missing: picked up (after the feature went live) and no trip yet; at_tk: arrived, still at the ТК warehouse.
    Both are offered as «same truck» choices."""
    from .api import _picked, awaiting_pickup
    from .calc import norm_awb
    from .models import Invoice, Line, session
    from sqlmodel import select
    pk, start = _picked(), since() - timedelta(days=7)
    with session() as s:
        invs = [i for i in s.exec(select(Invoice)).all() if (i.awb or "").strip()]
        stems = {}
        for l in s.exec(select(Line)).all():
            stems[l.invoice_id] = stems.get(l.invoice_id, 0) + (l.stems or 0)
    by = {}
    for i in invs:
        k = norm_awb(i.awb)
        r = by.setdefault(k, {"key": k, "awb": i.awb, "farms": set(), "stems": 0})
        r["farms"].add(i.farm)
        r["stems"] += stems.get(i.id, 0)
    missing = []
    for k, v in pk.items():
        d = _picked_dt(v)
        if not d or d < start or datetime.now() - d > timedelta(days=days) or trip_of(k) or k not in by:
            continue
        missing.append({**by[k], "farms": sorted(by[k]["farms"]), "picked": v})
    at_tk = [{**by[r["key"]], "farms": sorted(by[r["key"]]["farms"]), "picked": None}
             for r in awaiting_pickup() if r["key"] in by and not trip_of(r["key"])]
    return {"missing": sorted(missing, key=lambda x: x["picked"], reverse=True), "at_tk": at_tk}
