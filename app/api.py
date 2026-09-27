import hashlib
import re
import hmac
import json
import shutil
import time
import uuid
from collections import defaultdict
from statistics import median
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import select

from . import ai, excel
from .calc import compute, norm_awb, rate_of
from .config import ALLOWED_IDS, BOT_TOKEN, DATA_DIR, DEV_NO_AUTH, MASTER_XLSX
from .models import AwbWeights, Farm, Invoice, Line, Logistics, TopUp, session

router = APIRouter(prefix="/api")
BOT = None  # set by main.py so export can send the file into the chat


# ---------- auth: Telegram Mini App initData --------------------------------------------
def user_id(x_init_data: str = Header(default="")) -> int:
    if DEV_NO_AUTH:
        return next(iter(ALLOWED_IDS), 0)
    if not x_init_data:
        print("[lumen] auth: пустой initData", flush=True)
        raise HTTPException(401, "Нет данных входа от Telegram. Открой приложение кнопкой «📒 Открыть учёт» "
                                 "под сообщением /start или кнопкой «Учёт» у поля ввода — не старой кнопкой вместо клавиатуры и не в браузере.")
    pairs = dict(parse_qsl(x_init_data, keep_blank_values=True))
    h = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.strip().encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not h or not hmac.compare_digest(calc, h):
        print(f"[lumen] auth: подпись не совпала. поля={sorted(pairs)} токен=...{BOT_TOKEN.strip()[-4:]}", flush=True)
        raise HTTPException(401, f"Подпись Telegram не совпала: BOT_TOKEN на сервере (…{BOT_TOKEN.strip()[-4:]}) "
                                 "не от этого бота. Проверь BOT_TOKEN в .env и в переменных Bothost.")
    if time.time() - int(pairs.get("auth_date", 0)) > 7 * 86400:
        raise HTTPException(401, "Вход устарел — закрой и открой приложение заново")
    uid = json.loads(pairs.get("user", "{}")).get("id")
    if uid not in ALLOWED_IDS:
        raise HTTPException(403, f"Твой ID {uid} не в ALLOWED_IDS — добавь его в .env и перезапусти")
    return uid


# ---------- schemas -------------------------------------------------------------------
class TopUpIn(BaseModel):
    date: str
    rub: float
    usd: float
    prev_balance_rub: float | None = None
    note: str = ""


class LineIn(BaseModel):
    name: str
    boxes: float | None = None
    stems: float = 0
    weight_kg: float | None = None
    price_usd: float = 0
    mrc: float | None = None


class InvoiceIn(BaseModel):
    topup_id: int
    country: str = ""
    client_code: str = "Люмен"
    invoice_no: str = ""
    invoice_date: str = ""
    awb: str = ""
    farm: str = ""
    weight_kg: float | None = None
    invoice_total_usd: float | None = None
    usd_paid: float = 0
    rub_paid_override: float | None = None
    alloc_mode: str = "value"
    paid: bool = True
    paid_date: str = ""
    note: str = ""
    source_file: str | None = None
    lines: list[LineIn] = []


class LogisticsIn(BaseModel):
    topup_id: int | None = None
    awb: str
    leg: str = "air"
    provider: str = ""
    invoice_no: str = ""
    usd: float | None = None
    rub: float | None = None
    basis: str = "auto"
    paid_date: str = ""
    note: str = ""
    source_file: str | None = None
    farm_kg: dict[str, float] = {}     # optional: set weight_kg on farm invoices of this AWB


class FarmIn(BaseModel):
    name: str
    country: str
    aliases: str = ""
    is_forwarder: bool = False
    notes: str = ""


# ---------- helpers ------------------------------------------------------------------
def _all(s):
    return (s.exec(select(TopUp)).all(), s.exec(select(Invoice)).all(),
            s.exec(select(Line)).all(), s.exec(select(Logistics)).all())


def _norm_name(x: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", (x or "").lower())


def _farm_keys(s, farm: str) -> set:
    """All spellings of a farm: its name + aliases from the directory."""
    keys = {_norm_name(farm)}
    for f in s.exec(select(Farm)).all():
        names = [f.name] + [a for a in f.aliases.split(",") if a.strip()]
        if _norm_name(farm) in {_norm_name(n) for n in names}:
            keys |= {_norm_name(n) for n in names}
    return keys


def _kg_for(s, farm: str, farm_kg: dict):
    keys = _farm_keys(s, farm)
    for k, v in farm_kg.items():
        nk = _norm_name(k)
        if nk in keys or any(nk.startswith(x) or x.startswith(nk) for x in keys if len(x) >= 4 and len(nk) >= 4):
            return v
    return None


def _apply_farm_kg(s, awb, farm_kg):
    """Forwarder breakdown -> weight_kg of every farm invoice already on this AWB."""
    if not farm_kg:
        return
    key = norm_awb(awb)
    for inv in s.exec(select(Invoice)).all():
        if norm_awb(inv.awb) == key:
            v = _kg_for(s, inv.farm, farm_kg)
            if v:
                inv.weight_kg = v
                s.add(inv)


def _awb_kg(s) -> dict:
    """{MAWB: total kg of its breakdown} for the calc engine."""
    out = {}
    for w in s.exec(select(AwbWeights)).all():
        out[w.awb] = sum(float(v) for v in json.loads(w.farm_kg_json or "{}").values() if v)
    return out


def _store_weights(s, awb, farm_kg, source_file=None):
    """Save/merge a per-farm kg breakdown for a MAWB and push it onto the farm invoices."""
    farm_kg = {k: float(v) for k, v in (farm_kg or {}).items() if v not in (None, "", 0)}
    if not awb or not farm_kg:
        return
    key = norm_awb(awb)
    row = s.get(AwbWeights, key) or AwbWeights(awb=key)
    row.farm_kg_json = json.dumps({**json.loads(row.farm_kg_json or "{}"), **farm_kg}, ensure_ascii=False)
    if source_file:
        row.source_file = source_file
    s.add(row)
    _apply_farm_kg(s, awb, farm_kg)


def _fill_weight_from_logistics(s, inv):
    """Invoice added AFTER the Expolanka bill: take its kg from the stored breakdown."""
    if inv.weight_kg or not inv.awb:
        return
    row = s.get(AwbWeights, norm_awb(inv.awb))
    sources = [json.loads(row.farm_kg_json or "{}")] if row else []
    sources += [json.loads(lg.farm_kg_json or "{}") for lg in s.exec(select(Logistics)).all()
                if norm_awb(lg.awb) == norm_awb(inv.awb)]          # older data kept on the freight record
    for bd in sources:
        v = _kg_for(s, inv.farm, bd)
        if v:
            inv.weight_kg = v
            s.add(inv)
            return


def snapshot(s, topup_id):
    tops, invs, lines, logs = _all(s)
    t = next((x for x in tops if x.id == topup_id), None)
    if not t:
        raise HTTPException(404)
    res = compute(topup_id, tops, invs, lines, logs, _awb_kg(s))
    lines_by = defaultdict(list)
    for l in lines:
        lines_by[l.invoice_id].append(l)
    out_inv = []
    for inv in (i for i in invs if i.topup_id == topup_id):
        ls = []
        for l in lines_by[inv.id]:
            c = res.lines[l.id]
            ls.append({**l.model_dump(), "price_rub": round(c.price_rub, 2), "air_rub": round(c.air_rub_stem, 2),
                       "msk_rub": round(c.msk_rub_stem, 2), "total_rub": round(c.total_rub_stem, 2)})
        out_inv.append({**inv.model_dump(), "rub_paid": res.invoice_rub[inv.id], "lines": ls})
    related = {norm_awb(i.awb) for i in invs if i.topup_id == topup_id}
    out_log = [lg.model_dump() for lg in logs if lg.topup_id == topup_id or norm_awb(lg.awb) in related]
    return {"topup": {**t.model_dump(), "rate": rate_of(t)}, "invoices": out_inv, "logistics": out_log,
            "usd_spent": round(res.usd_spent, 2), "rub_spent": round(res.rub_spent),
            "usd_left": round(t.usd - res.usd_spent, 2), "warnings": res.warnings}


def history(s):
    tops, invs, lines, logs = _all(s)
    rates = [{"date": t.date, "rate": round(rate_of(t), 4)} for t in tops]
    per = defaultdict(list)
    for t in tops:
        res = compute(t.id, tops, invs, lines, logs, _awb_kg(s))
        inv_by = {i.id: i for i in invs if i.topup_id == t.id}
        for l in lines:
            if l.invoice_id in inv_by:
                per[f"{inv_by[l.invoice_id].farm} | {l.name}"].append(res.lines[l.id].total_rub_stem)
    typical = {k: round(median(v), 2) for k, v in per.items() if v}
    return {"rates": rates[-15:], "typical_cost_per_stem_rub": dict(list(typical.items())[-300:])}


# ---------- routes -------------------------------------------------------------------
@router.get("/topups")
def list_topups(uid: int = Depends(user_id)):
    with session() as s:
        tops, invs, lines, logs = _all(s)
        out = []
        for t in sorted(tops, key=lambda x: x.id, reverse=True):
            spent = sum(i.usd_paid for i in invs if i.topup_id == t.id) + sum(l.usd or 0 for l in logs if l.topup_id == t.id)
            out.append({**t.model_dump(), "rate": rate_of(t), "usd_left": round(t.usd - spent, 2),
                        "n_invoices": sum(1 for i in invs if i.topup_id == t.id)})
        return out


@router.post("/topups")
def create_topup(body: TopUpIn, uid: int = Depends(user_id)):
    with session() as s:
        t = TopUp(**body.model_dump())
        s.add(t); s.commit(); s.refresh(t)
        return t


@router.put("/topups/{tid}")
def update_topup(tid: int, body: TopUpIn, uid: int = Depends(user_id)):
    with session() as s:
        t = s.get(TopUp, tid)
        for k, v in body.model_dump().items():
            setattr(t, k, v)
        s.add(t); s.commit()
        return snapshot(s, tid)


@router.get("/topups/{tid}")
def get_topup(tid: int, uid: int = Depends(user_id)):
    with session() as s:
        return snapshot(s, tid)


@router.post("/invoices")
def save_invoice(body: InvoiceIn, inv_id: int | None = None, uid: int = Depends(user_id)):
    with session() as s:
        data = body.model_dump(exclude={"lines"})
        inv = s.get(Invoice, inv_id) if inv_id else Invoice(**data)
        if inv_id:
            for k, v in data.items():
                setattr(inv, k, v)
            for l in s.exec(select(Line).where(Line.invoice_id == inv_id)).all():
                s.delete(l)
        s.add(inv); s.commit(); s.refresh(inv)
        for l in body.lines:
            s.add(Line(invoice_id=inv.id, **l.model_dump()))
        _fill_weight_from_logistics(s, inv)
        s.commit()
        return snapshot(s, inv.topup_id)


@router.delete("/invoices/{inv_id}")
def delete_invoice(inv_id: int, uid: int = Depends(user_id)):
    with session() as s:
        inv = s.get(Invoice, inv_id)
        for l in s.exec(select(Line).where(Line.invoice_id == inv_id)).all():
            s.delete(l)
        tid = inv.topup_id
        s.delete(inv); s.commit()
        return snapshot(s, tid)


@router.post("/logistics")
def save_logistics(body: LogisticsIn, log_id: int | None = None, view_topup: int | None = None,
                   uid: int = Depends(user_id)):
    with session() as s:
        data = body.model_dump(exclude={"farm_kg"})
        lg = s.get(Logistics, log_id) if log_id else Logistics(**data)
        if log_id:
            for k, v in data.items():
                setattr(lg, k, v)
        s.add(lg)
        _store_weights(s, body.awb, body.farm_kg)
        s.commit()
        return snapshot(s, view_topup or body.topup_id)


@router.delete("/logistics/{log_id}")
def delete_logistics(log_id: int, view_topup: int, uid: int = Depends(user_id)):
    with session() as s:
        s.delete(s.get(Logistics, log_id)); s.commit()
        return snapshot(s, view_topup)


@router.get("/awb/{awb}")
def awb_invoices(awb: str, uid: int = Depends(user_id)):
    with session() as s:
        k = norm_awb(awb)
        return [{"id": i.id, "farm": i.farm, "weight_kg": i.weight_kg, "topup_id": i.topup_id}
                for i in s.exec(select(Invoice)).all() if norm_awb(i.awb) == k]


class WeightsIn(BaseModel):
    awb: str
    farm_kg: dict[str, float] = {}
    source_file: str | None = None


@router.get("/weights/{awb}")
def get_weights(awb: str, uid: int = Depends(user_id)):
    """Breakdown for a MAWB + the farm invoices already on it."""
    with session() as s:
        row = s.get(AwbWeights, norm_awb(awb))
        k = norm_awb(awb)
        invs = [{"id": i.id, "farm": i.farm, "weight_kg": i.weight_kg, "topup_id": i.topup_id}
                for i in s.exec(select(Invoice)).all() if norm_awb(i.awb) == k]
        return {"awb": awb, "farm_kg": json.loads(row.farm_kg_json) if row else {}, "invoices": invs}


@router.post("/weights")
def save_weights(body: WeightsIn, uid: int = Depends(user_id)):
    with session() as s:
        _store_weights(s, body.awb, body.farm_kg, body.source_file)
        s.commit()
    return get_weights(body.awb, uid)


@router.get("/farms")
def farms(uid: int = Depends(user_id)):
    with session() as s:
        return s.exec(select(Farm).order_by(Farm.country, Farm.name)).all()


@router.post("/farms")
def save_farm(body: FarmIn, farm_id: int | None = None, uid: int = Depends(user_id)):
    with session() as s:
        f = s.get(Farm, farm_id) if farm_id else Farm(**body.model_dump())
        if farm_id:
            for k, v in body.model_dump().items():
                setattr(f, k, v)
        s.add(f); s.commit(); s.refresh(f)
        return f


@router.post("/parse")
async def parse(file: UploadFile = File(...), uid: int = Depends(user_id)):
    data = await file.read()
    mime = file.content_type or "application/pdf"
    if mime not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
        raise HTTPException(400, "Нужен PDF или фото (jpg/png)")
    name = f"{uuid.uuid4().hex[:10]}_{file.filename}"
    (DATA_DIR / "files" / name).write_bytes(data)
    with session() as s:
        fs = [f.model_dump() for f in s.exec(select(Farm)).all()]
        catalog = sorted({l.name for l in s.exec(select(Line)).all()})
    out = await ai.parse_document(data, mime, fs, catalog)
    out["source_file"] = name
    return out


@router.post("/topups/{tid}/audit")
async def audit(tid: int, uid: int = Depends(user_id)):
    with session() as s:
        snap, hist = snapshot(s, tid), history(s)
    return await ai.audit_topup(snap, hist)


@router.post("/topups/{tid}/export")
async def export(tid: int, uid: int = Depends(user_id)):
    from aiogram.types import FSInputFile
    with session() as s:
        tops, invs, lines, logs = _all(s)
        t = s.get(TopUp, tid)
        tmp = DATA_DIR / f"export_{tid}.xlsx"
        if MASTER_XLSX.exists():
            shutil.copy(MASTER_XLSX, tmp)
        name, _ = excel.build(tmp, t, tops, invs, lines, logs, awb_kg=_awb_kg(s))
        shutil.copy(tmp, MASTER_XLSX)          # master always holds the latest version
        s.add(t); s.commit()
    if BOT:
        await BOT.send_document(uid, FSInputFile(MASTER_XLSX, filename="учет.xlsx"),
                                caption=f"Готово: лист «{name}» обновлён")
        from .backup import backup_now
        try:
            await backup_now(BOT, f"выгрузка «{name}»")
        except Exception as e:
            print(f"[lumen] backup after export failed: {e}", flush=True)
    return {"ok": True, "sheet": name}


# ---------- drafts: invoices sent straight into the bot chat, waiting for the operator ----
DRAFTS = DATA_DIR / "drafts"
DRAFTS.mkdir(exist_ok=True)


def save_draft(parsed: dict) -> str:
    did = uuid.uuid4().hex[:8]
    (DRAFTS / f"{did}.json").write_text(json.dumps(parsed, ensure_ascii=False))
    from .backup import mark_dirty
    mark_dirty()                      # drafts from the chat don't go through the API middleware
    return did


@router.get("/drafts")
def list_drafts(uid: int = Depends(user_id)):
    out = []
    for p in sorted(DRAFTS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        d = json.loads(p.read_text())
        out.append({"id": p.stem, **d})
    return out


@router.delete("/drafts/{did}")
def drop_draft(did: str, uid: int = Depends(user_id)):
    (DRAFTS / f"{did}.json").unlink(missing_ok=True)
    return {"ok": True}
