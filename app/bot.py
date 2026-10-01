import json
import re
import asyncio
import shutil
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message,
                           ReplyKeyboardRemove, WebAppInfo)
from sqlmodel import select

from . import ai
from .api import (DRAFTS, money_of, active_topup, awb_spread, breakdown_from_text, export_topups, book_document, import_floratrack, payments_for, split_by_farm, fill_mawb, money_from_text, save_draft,
                  set_active_topup, store_breakdown, topup_from_text)
from .config import ALLOWED_IDS, BOT_TOKEN, DATA_DIR, MASTER_XLSX, WEBAPP_URL
from .models import Farm, Line, session

LAST_DRAFT: dict[int, tuple[list, float]] = {}   # user -> ([draft ids], time)
bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
pv = F.chat.type == "private"          # every dp.message handler is private-only; groups -> `grp` router
from . import roles
ops = F.from_user.id.func(lambda i: roles.role_of(i) is not None)          # any registered user
wr = F.from_user.id.func(lambda i: roles.can_write(roles.role_of(i)))       # sys + super
sysf = F.from_user.id.func(lambda i: roles.role_of(i) == "sys")

# Only INLINE buttons and the menu button pass initData (login) to the Mini App.
# Reply-keyboard buttons open it without initData -> "bad initData".
KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="📒 Открыть учёт", web_app=WebAppInfo(url=WEBAPP_URL))]])


async def setup_menu_button():
    """Blue button next to the message field opens the Mini App."""
    if bot and WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Учёт", web_app=WebAppInfo(url=WEBAPP_URL)))


@dp.errors()
async def on_error(event):
    import logging
    logging.exception("handler error", exc_info=event.exception)
    try:
        msg = event.update.message
        if msg:
            await msg.answer(f"❌ Ошибка: {type(event.exception).__name__}: {str(event.exception)[:300]}")
    except Exception:
        pass
    return True


TRUCK_TEXT = F.text.func(lambda t: bool(re.search(r"товар\s+забран|едет на склад|время прибытия|прошла границу", t or "", re.I)))


@dp.message(pv, wr, TRUCK_TEXT)
async def truck_forwarded(m: Message):
    """Messages forwarded (or pasted) from the TK MSK chat: original date is used for «+1 hour»."""
    from datetime import timedelta
    origin = getattr(m, "forward_origin", None)
    sent = (origin.date if origin else m.date).replace(tzinfo=None) + timedelta(hours=3)
    text = m.text
    # pasted (not forwarded) text may start with the original date: «29.09 15:08 …» / «29.09.2026 15:08 …»
    d = re.match(r"\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?[ ,]+(\d{1,2}):(\d{2})\s*", text or "")
    if d and not origin:
        y = int(d.group(3)) if d.group(3) else _msk_now().year
        y = y + 2000 if y < 100 else y
        try:
            sent = datetime(y, int(d.group(2)), int(d.group(1)), int(d.group(4)), int(d.group(5)))
            text = text[d.end():]
        except ValueError:
            pass
    await _truck_event(None, text, sent, notify_uid=m.from_user.id)


@dp.message(pv, CommandStart(), ops)
async def start(m: Message):
    r = roles.role_of(m.from_user.id)
    if not roles.can_write(r):
        await m.answer("Учёт поставок Люмен · роль «1С оператор» (просмотр).\n"
                       "• «Учёт» — смотреть пополнения, инвойсы, себестоимость.\n• /excel — прислать актуальный Excel.",
                       reply_markup=KB if WEBAPP_URL.startswith("https://") else None)
        return
    kb = KB if WEBAPP_URL.startswith("https://") else None
    await m.answer("Обновил кнопки 👇", reply_markup=ReplyKeyboardRemove())   # remove the old reply keyboard
    await m.answer("Учёт поставок Люмен.\n\n"
                   "• Кнопка ниже или «Учёт» слева от поля ввода — пополнения, инвойсы, логистика, выгрузка в Excel.\n"
                   "• Кидай PDF/фото инвойса или счёта за фрахт. С суммами в подписи («1198$ 105472₽») — внесу сразу,"
                   " без подписи — спрошу суммы.\n"
                   "• Документы идут в активное пополнение (последнее). Сменить: /pop, разово — дата в подписи.\n"
                   "• Разбивку кг кидай с MAWB в подписи.\n"
                   "• Отчёт Floratrack (.xlsx) — привяжу машины к MAWB и посчитаю ₽ по вашим оплатам из «Баланса».\n"
                   "• Любой другой .xlsx станет мастер-файлом учёта."
                   + ("" if kb else "\n\n⚠️ WEBAPP_URL не https — кнопка приложения отключена."),
                   reply_markup=kb)


@dp.message(pv, wr, F.document.file_name.lower().endswith(".xlsx"))
async def master(m: Message):
    f = await bot.download(m.document)
    data = f.read()
    from io import BytesIO
    from openpyxl import load_workbook
    from . import floratrack as ft
    try:
        is_ft = ft.is_floratrack(load_workbook(BytesIO(data), read_only=True))
    except Exception:
        is_ft = False
    if is_ft:
        await _floratrack(m, data)
        return
    if MASTER_XLSX.exists():
        shutil.copy(MASTER_XLSX, DATA_DIR / f"учет_backup_{datetime.now():%Y%m%d_%H%M}.xlsx")
    MASTER_XLSX.write_bytes(data)
    from .backup import mark_dirty
    mark_dirty()
    await m.answer("Мастер-файл обновлён ✅ (старый сохранён в бэкап)")


async def _floratrack(m: Message, data: bytes):
    note = await m.answer("Отчёт Floratrack — разбираю машины и оплаты…")
    try:
        r = import_floratrack(data)
    except Exception as e:
        await note.edit_text(f"Не смог разобрать отчёт: {e}")
        return
    fmt = lambda x: f"{x:,.0f}".replace(",", " ")
    kinds = {"import": "Кения АМС-МСК", "ecuador": "Эквадор", "colombia": "Колумбия"}
    recent = sorted(r["matched"], key=lambda x: x[1].date or datetime.min, reverse=True)[:12]
    lines = [f"• {awb} · {kinds[c.kind]} · {c.kg:g} кг · ${c.usd:,.2f} × {c.rate:.2f} = {fmt(c.rub)} ₽"
             + (" (курс предв.)" if c.provisional else "") for awb, c in recent]
    txt = (f"🚚 Floratrack: привязано {len(r['matched'])} AWB (новых {r['added']}, обновлено {r['updated']}).\n"
           + (f"Баланс у Floratrack: ${r['balance_usd']:,.2f}\n" if roles.sees_money(roles.role_of(m.from_user.id)) else "")
           + "\n" + "\n".join(lines))
    fresh = [c for c in r["unmatched"] if c.date and (datetime.now() - c.date).days <= 21]
    if fresh:
        txt += "\n\nНет наших инвойсов/разбивки с этими AWB (последние 3 недели):\n" + "\n".join(
            f"• …{c.last4} · {kinds[c.kind]} · {c.kg:g} кг · {c.sheet}" for c in fresh[:15])
        txt += "\nВнеси их и перекинь отчёт — привяжутся."
    if r["ambiguous"]:
        txt += "\n\n⚠️ Несколько MAWB с такими 4 цифрами, взял по весу: " + ", ".join(r["ambiguous"][:10])
    affected = sorted({x["topup_id"] for awb, _c in r["matched"] for x in awb_spread(awb)})
    warn = [w for w in r["warnings"] if "MAWB" in w][:8]
    if warn:
        txt += "\n\n⚠️ " + "\n⚠️ ".join(warn)
    await note.edit_text(txt[:4000])
    if affected:
        await export_topups(affected, m.from_user.id, "🚚 Floratrack разнесён по пополнениям, где лежит этот товар.")


async def _parse_and_reply(m: Message, data: bytes, mime: str):
    """Guard: never leave the user staring at 'Читаю документ…'."""
    import asyncio, logging, traceback
    note_holder = {}
    try:
        await asyncio.wait_for(_parse_and_reply_inner(m, data, mime, note_holder), timeout=240)
    except asyncio.TimeoutError:
        await _say(m, note_holder, "⏱ AI не ответил за 4 минуты. Пришли документ ещё раз — обычно со второго раза проходит.")
    except Exception as e:
        logging.exception("parse failed")
        tb = traceback.format_exc().strip().splitlines()[-1]
        await _say(m, note_holder, f"❌ Ошибка при разборе: {tb[:300]}\nПерешли это сообщение мне (разработчику) — починю.")


async def _say(m: Message, holder: dict, text: str):
    try:
        if holder.get("note"):
            await holder["note"].edit_text(text)
            return
    except Exception:
        pass
    await m.answer(text)


async def _parse_and_reply_inner(m: Message, data: bytes, mime: str, holder: dict):
    note = await m.answer("Читаю документ…")
    holder["note"] = note
    with session() as s:
        fs = [f.model_dump() for f in s.exec(select(Farm)).all()]
        catalog = sorted({l.name for l in s.exec(select(Line)).all()})
    try:
        out = await ai.parse_document(data, mime, fs, catalog, note=m.caption or "")
    except Exception as e:
        await note.edit_text(f"Не смог прочитать: {e}")
        return
    warn = ("\n⚠️ " + "\n⚠️ ".join(out["warnings"])) if out.get("warnings") else ""

    if out.get("doc_type") == "topup_receipt":
        await note.edit_text(_register_topup(out.get("topup") or {}, m.caption or ""))
        return

    if out.get("doc_type") == "kg_breakdown":
        kg = out.get("per_farm_kg") or []
        head = (f"⚖️ Разбивка кг · MAWB {out.get('awb') or '?'}\n"
                + "\n".join(f"• {x.get('farm')}: {x.get('kg'):g} кг" for x in kg)
                + f"\nИтого {sum(x.get('kg') or 0 for x in kg):g} кг" + warn)
        if out.get("awb"):
            await note.edit_text(head + "\n\n" + _applied_text(store_breakdown(out["awb"], kg, out.get("source_file"))))
            return
        did = save_draft(out)
        LAST_DRAFT[m.from_user.id] = ([did], time.time())
        await note.edit_text(head + "\n\nНапиши MAWB следующим сообщением — сохраню разбивку.")
        return

    # farm invoice / freight bill -> «оплачен?» flow
    subs = split_by_farm(out) if out.get("doc_type") == "farm_invoice" else [out]
    t = topup_from_text(m.caption or "")
    if out.get("doc_type") == "farm_invoice":
        for d in subs:
            fill_mawb(d, t.id if t else None)     # MAWB from the Expolanka breakdown right away
    cu, _cr = money_from_text(m.caption or "")
    flow = {"ids": [save_draft(d) for d in subs], "paid": None, "topup": t.id if t else None,
            "pays": None, "kind": out.get("doc_type"), "ts": time.time(), "note": note}
    if "не оплач" in (m.caption or "").lower():
        flow["paid"] = False
    elif cu or t:
        flow["paid"] = True                       # sums or a top-up date in the caption = paid
    if cu:
        flow["pays"] = payments_for(m.caption or "", subs)
        if flow["paid"] is False:                 # «не оплачен 1100$» = approximate $
            flow["approx"] = True
    FLOW[m.from_user.id] = flow
    LAST_DRAFT[m.from_user.id] = (flow["ids"], time.time())
    await _step(m.from_user.id, head=_flow_head(subs) + warn)


# ---------- the «оплачен? → откуп → $» conversation --------------------------------------
FLOW: dict[int, dict] = {}   # user -> current document in progress


def _flow_docs(flow):
    docs = []
    for i in flow["ids"]:
        p = DRAFTS / f"{i}.json"
        if p.exists():
            docs.append((p, json.loads(p.read_text())))
    return docs


def _flow_head(subs) -> str:
    if len(subs) > 1:
        return (f"📄 Общий инвойс {subs[0].get('invoice_no') or ''} на {len(subs)} плантации — делю:\n" + "\n".join(
            f"• {d['farm']}: {sum(l.get('stems') or 0 for l in d['lines']):g} ст, ${d['invoice_total_usd']:g}"
            + (f", MAWB {d['awb']}" if d.get("awb") else ", MAWB ?") for d in subs))
    return _doc_head(subs[0])


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])


async def _send(uid: int, text: str, kb=None, note=None):
    if note is not None:
        try:
            await note.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await bot.send_message(uid, text, reply_markup=kb)


async def _step(uid: int, head: str = ""):
    """Ask the next missing thing, or book when everything is known."""
    from .models import TopUp
    flow = FLOW.get(uid)
    if not flow:
        return
    docs = _flow_docs(flow)
    if not docs:
        FLOW.pop(uid, None)
        return
    note, flow["note"] = flow.get("note"), None
    head = (head + "\n\n") if head else ""
    freight = flow["kind"] == "freight_invoice"
    if flow["paid"] is None:
        await _send(uid, head + "Оплачен?", _kb([[("✅ Оплачен", "fl:paid"), ("🚚 Не оплачен — в пути", "fl:unpaid")]]), note)
        return
    if flow["paid"] and not flow["topup"]:
        with session() as s:
            tops = s.exec(select(TopUp).order_by(TopUp.id.desc()).limit(8)).all()
        if not tops:
            await _send(uid, head + "Нет ни одного пополнения — создай его (скрин покупки USDT или «Учёт»).", None, note)
            return
        rows = [[(f"{x.date} · курс {x.rub / x.usd:.2f}", f"fl:tp:{x.id}")] for x in tops]
        await _send(uid, head + "Из какого пополнения оплачен?", _kb(rows), note)
        return
    if freight and not flow["pays"]:
        usd = (docs[0][1].get("freight") or {}).get("total_usd")
        if usd:
            flow["pays"] = [(usd, None)]           # the bill's own $
    if not flow["pays"]:
        many = len(docs) > 1
        if flow["paid"]:
            t = None
            with session() as s:
                t = s.get(TopUp, flow["topup"])
            ask = (f"Сколько $ оплатили{' — по строке на ферму' if many else ''}? ₽ посчитаю по курсу "
                   f"{t.date} ({t.rub / t.usd:.2f}). Если списали иначе — добавь ₽.")
        else:
            ask = (f"Примерно сколько $ будет оплата{' — по строке на ферму' if many else ''}? "
                   f"Себестоимость посчитаю заранее (±10%).")
        ex = "\n".join(f"{d['farm']} {round(d.get('invoice_total_usd') or 1000)}$" for _p, d in docs) if many else \
            f"{round(docs[0][1].get('invoice_total_usd') or 1000)}$"
        await _send(uid, head + ask + f"\nНапример:\n{ex}", None, note)
        return
    await _book_flow(uid, head, note)


async def _book_flow(uid: int, head: str, note):
    from .models import TopUp
    flow = FLOW.pop(uid, None)
    docs = _flow_docs(flow)
    t = None
    if flow["paid"]:
        with session() as s:
            t = s.get(TopUp, flow["topup"])
    pays = flow["pays"] + [None] * (len(docs) - len(flow["pays"]))
    out = []
    for (p, d), pr in zip(docs, pays):
        usd, rub = pr or (None, None)
        if d.get("doc_type") == "farm_invoice":
            fill_mawb(d, t.id if t else None)
        snap, kind = book_document(d, t.id if t else 0, usd, rub, uid, paid=bool(flow["paid"]))
        if not snap:
            out.append(f"❌ {d.get('farm') or ''}: не внёс — {kind}. Черновик в «Учёт».")
            continue
        p.unlink(missing_ok=True)
        pre = f"{d['farm']}: " if len(docs) > 1 else ""
        out.append(pre + (_booked_text(snap, d, kind, t, uid) if t else _transit_text(snap, d, kind)))
    await _send(uid, head + "\n\n".join(out), None, note)
    await _flush_export(uid, "📊 Логистика легла на товар из прошлого пополнения.")


def _transit_text(v: dict, d: dict, kind: str) -> str:
    fmt = lambda x: f"{x:,.0f}".replace(",", " ")
    if kind == "freight":
        lg = max(v["unpaid_logistics"], key=lambda x: x["id"]) if v["unpaid_logistics"] else None
        return (f"🚚 Фрахт в пути, не оплачен: ${lg['usd']:g} ≈ {fmt(lg['rub_est'])} ₽ по курсу {v['est_rate']:.2f} "
                f"(последнее пополнение {v['est_topup']})") if lg else "🚚 Фрахт внесён как неоплаченный"
    inv = max(v["invoices"], key=lambda x: x["id"])
    return (f"🚚 В пути, не оплачен: ≈${inv['est_usd']:g} ≈ {fmt(inv['rub_goods'])} ₽ (курс {v['est_rate']:.2f}, "
            f"пополнение {v['est_topup']})\n"
            f"≈ Себестоимость {inv['avg_rub']:.2f} ₽/ст ±10% (логистика: {inv['logi_source']})\n"
            "Оплатишь — нажми «Оплачен» в «В пути» или в напоминании во вторник/среду.")


@dp.callback_query(F.data.startswith("fl:"), wr)
async def flow_button(c: CallbackQuery):
    flow = FLOW.get(c.from_user.id)
    if not flow:
        await c.answer("Документ уже обработан или устарел", show_alert=True)
        return
    part = c.data.split(":")
    if part[1] == "paid":
        flow["paid"] = True
    elif part[1] == "unpaid":
        flow["paid"] = False
    elif part[1] == "tp":
        flow["topup"] = int(part[2])
    flow["note"] = c.message
    await c.answer()
    await _step(c.from_user.id)


_AUTO_EXPORT: set = set()


async def _flush_export(uid: int, caption: str):
    """Freight paid from a new top-up for goods from an older one -> resend both sheets."""
    if _AUTO_EXPORT:
        ids = sorted(_AUTO_EXPORT)
        _AUTO_EXPORT.clear()
        try:
            await export_topups(ids, uid, caption)
        except Exception as e:
            await bot.send_message(uid, f"Не смог обновить Excel: {e}")


async def _book_or_draft(m: Message, note, out: dict, t, usd, rub):
    """Both sums known -> straight into the top-up. Otherwise a draft waiting for '1198$ 105472₽'."""
    warn = ("\n⚠️ " + "\n⚠️ ".join(out["warnings"])) if out.get("warnings") else ""
    head = _doc_head(out)
    if usd:
        snap, kind = book_document(out, t.id, usd, rub, m.from_user.id)
        if snap:
            await note.edit_text(head + warn + "\n\n" + _booked_text(snap, out, kind, t, m.from_user.id))
            await _flush_export(m.from_user.id, "📊 Логистика легла на товар из прошлого пополнения.")
            return
        warn += f"\n⚠️ Не внёс сразу: {kind}"
    did = save_draft(out)
    LAST_DRAFT[m.from_user.id] = ([did], time.time())
    await _edit(note, head + warn + f"\n\n→ пополнение {t.date}. Ответь суммой оплаты в $, например "
                      f"`{round(out.get('invoice_total_usd') or 1000)}$` — ₽ посчитаю по курсу пополнения "
                      f"({t.rub / t.usd:.2f}). Если списали иначе — добавь ₽: `1115$ 98000₽`.")


async def _book_or_draft_multi(m: Message, note, subs: list, t):
    """Trader invoice (NextWave): one file, several farms -> one invoice per farm, each paid separately."""
    for d in subs:
        d["topup_id"] = t.id
        fill_mawb(d, t.id)
    head = f"📄 Общий инвойс {subs[0].get('invoice_no') or ''} на {len(subs)} плантации — делю:\n" + "\n".join(
        f"• {d['farm']}: {sum(l.get('stems') or 0 for l in d['lines']):g} ст, ${d['invoice_total_usd']:g}"
        + (f", MAWB {d['awb']}" if d.get("awb") else ", MAWB ?") for d in subs)
    pays = payments_for(m.caption or "", subs)
    res, left = [], []
    for d, pr in zip(subs, pays or [None] * len(subs)):
        if pr and pr[0]:
            snap, kind = book_document(d, t.id, pr[0], pr[1], m.from_user.id)
            if snap:
                res.append(f"{d['farm']}: " + _booked_text(snap, d, kind, t, m.from_user.id).split("\n")[0].replace("✅ ", "✅ "))
                continue
        left.append(d)
    if left:
        ids = [save_draft(d) for d in left]
        LAST_DRAFT[m.from_user.id] = (ids, time.time())
        ex = "\n".join(f"{d['farm']} {round(d['invoice_total_usd'])}$" for d in left)
        res.append("Ответь оплатой в $ по каждой плантации, по строке на каждую (₽ посчитаю по курсу пополнения):\n`" + ex + "`\n"
                   "Одна сумма на всех — разделю пропорционально инвойсу.")
    await _edit(note, head + "\n\n" + "\n".join(res))


async def _edit(note, text: str):
    try:
        await note.edit_text(text, parse_mode="Markdown")
    except Exception:
        await note.edit_text(text.replace("`", ""))


def _doc_head(out: dict) -> str:
    if out.get("doc_type") == "freight_invoice":
        fr = out.get("freight") or {}
        return f"✈️ Фрахт · {fr.get('provider') or out.get('farm') or '?'} · MAWB {out.get('awb') or '?'}" + (
            f" · ${fr['total_usd']:g}" if fr.get("total_usd") else "")
    stems = sum(l.get("stems") or 0 for l in out.get("lines", []))
    return (f"📄 {out.get('farm') or '?'} · MAWB {out.get('awb') or '?'}\n"
            f"Строк: {len(out.get('lines', []))}, стеблей: {stems:g}, итог: ${out.get('invoice_total_usd') or '?'}"
            + (f"\n🔗 {out['mawb_note']}" if out.get("mawb_note") else ""))


def _booked_text(snap: dict, out: dict, kind: str, t, uid: int | None = None) -> str:
    fmt = lambda x: f"{x:,.0f}".replace(",", " ")
    if kind == "freight":
        lg = max((l for l in snap["logistics"] if l["topup_id"] == t.id), key=lambda l: l["id"])
        txt = (f"✅ Фрахт внесён в пополнение {t.date}: ${lg['usd'] or 0:g} / {fmt(lg['rub'] or 0)} ₽"
               + (f" · {lg['weight_kg']:g} кг по счёту" if lg.get("weight_kg") else "")
               + (f" · {lg['rub_per_kg']:.2f} ₽/кг" if lg.get("rub_per_kg") else "")
               + (f" · разбивка {lg['kg_total']:g} кг ✓" if lg.get("kg_total") else " · разбивки кг ещё нет"))
        spread = awb_spread(lg["awb"])
        if spread:
            col = "air" if lg["leg"] == "air" else "msk"
            txt += "\nЛегло на:\n" + "\n".join(
                f"• {x['farm']} (пополнение {x['topup']}): {x[col]:.2f} ₽/ст" for x in spread)
            other = sorted({x["topup"] for x in spread if x["topup_id"] != t.id})
            if other:
                txt += f"\n↪️ Товар оплачен из другого пополнения ({', '.join(other)}) — его себестоимость обновлена, Excel пришлю."
                _AUTO_EXPORT.add(t.id)
                _AUTO_EXPORT.update(x["topup_id"] for x in spread)
    else:
        inv = max(snap["invoices"], key=lambda i: i["id"])
        st = sum(l["stems"] for l in inv["lines"]) or 1
        flower = sum(l["price_rub"] * l["stems"] for l in inv["lines"]) / st
        logi = sum((l["air_rub"] + l["msk_rub"]) * l["stems"] for l in inv["lines"]) / st
        txt = (f"✅ Внесено в пополнение {t.date}: ${inv['usd_paid']:g} / {fmt(inv['rub_paid'])} ₽"
               + (f" (по курсу {t.rub / t.usd:.2f})" if out.get("_rub_auto") else "") + "\n"
               f"Себестоимость в среднем {flower + logi:.2f} ₽/стебель (цветок {flower:.2f} + логистика {logi:.2f})")
    rel = [w for w in snap["warnings"] if (out.get("farm") or "~").lower()[:5] in w.lower()
           or (out.get("awb") or "~").replace("-", "")[:6] in w.replace("-", "")]
    txt += ("\n⚠️ " + "\n⚠️ ".join(rel) if rel else "")
    if uid and roles.sees_money(roles.role_of(uid)):
        txt += f"\nОстаток пополнения ${money_of(t.id)['usd_left']:g}"
    return txt


def _register_topup(tp: dict, caption: str) -> str:
    """Crypto purchase screenshot -> new top-up dated today, and it becomes the active one."""
    from .models import TopUp
    rub = tp.get("rub")
    usd = tp.get("usd_withdrawn") or tp.get("usd_bought")     # what actually left for payments
    cu, cr = money_from_text(caption)                          # caption «1728.86$ 152000₽» overrides
    usd, rub = cu or usd, cr or rub
    if not (rub and usd):
        return "Не нашёл на скрине обе суммы (₽ и USDT). Пришли ещё раз или подпиши: «1728.86$ 152000₽»."
    order = (tp.get("order_no") or "").lstrip("#")
    with session() as s:
        if order:
            dup = s.exec(select(TopUp).where(TopUp.note.contains(f"#{order}"))).first()
            if dup:
                return f"Это пополнение уже внесено: {dup.date}, {_n(dup.rub, 2)} ₽ → ${_n(dup.usd, 4)} (заявка #{order})."
        note = f"заявка #{order}" if order else ""
        if tp.get("usd_withdrawn") and tp.get("usd_bought"):
            note += f"; куплено {tp['usd_bought']} USDT, выведено {tp['usd_withdrawn']}"
        t = TopUp(date=datetime.now().strftime("%d.%m.%Y"), rub=rub, usd=usd, note=note.strip("; "))
        s.add(t); s.commit(); s.refresh(t)
    set_active_topup(t.id)
    fee = ""
    if tp.get("usd_withdrawn") and tp.get("usd_bought") and not cu:
        fee = f"\n(куплено {tp['usd_bought']:g} USDT, за вывод ушло {tp['usd_bought'] - tp['usd_withdrawn']:.4f} — считаю по выведенным)"
    return (f"💰 Новое пополнение {t.date}\n{_n(rub, 2)} ₽ → ${_n(usd, 4)}\nКурс {rub / usd:.4f} ₽/$" + fee +
            "\n\nДокументы из чата теперь идут в него.")


def _n(x: float, d: int) -> str:
    return f"{x:,.{d}f}".replace(",", " ")


def _applied_text(w: dict) -> str:
    on = {i["farm"] for i in w["invoices"] if i.get("weight_kg")}
    missing = [f for f in w["farm_kg"] if not any(f.lower()[:4] == x.lower()[:4] for x in on)]
    txt = "✅ Разбивка сохранена"
    if on:
        txt += f", вес проставлен: {', '.join(sorted(on))}"
    if missing:
        txt += f"\nЕщё без инвойсов: {', '.join(missing)} — вес встанет сам, когда их внесёшь"
    return txt


@dp.message(pv, wr, F.document.mime_type.in_({"application/pdf", "image/jpeg", "image/png"}))
async def doc(m: Message):
    f = await bot.download(m.document)
    await _parse_and_reply(m, f.read(), m.document.mime_type)


@dp.message(pv, wr, F.photo)
async def photo(m: Message):
    f = await bot.download(m.photo[-1])
    await _parse_and_reply(m, f.read(), "image/jpeg")


@dp.channel_post(F.text.startswith("/id"))
async def channel_id(m: Message):
    """Post /id in the backup channel -> bot replies with the channel id for BACKUP_CHAT_ID."""
    await m.answer(f"BACKUP_CHAT_ID={m.chat.id}")


@dp.message(pv, wr, F.forward_origin.chat)
async def forwarded_from_channel(m: Message):
    """Forward any post from the channel to the bot -> it tells the channel id. Works even if
    the bot isn't admin yet (but it must be admin for backups to work)."""
    ch = m.forward_origin.chat
    await m.answer(f"Канал «{ch.title}»\nBACKUP_CHAT_ID={ch.id}\n\nВставь эту строку в .env и перезапусти. "
                   "Бот должен быть админом канала с правом публиковать и закреплять.")


@dp.message(pv, sysf, Command("backup"))
async def manual_backup(m: Message):
    from .backup import BACKUP_CHAT_ID, backup_now
    if not BACKUP_CHAT_ID:
        await m.answer("BACKUP_CHAT_ID не задан в .env")
        return
    await backup_now(bot, "вручную")
    await m.answer("💾 Бэкап отправлен в канал и закреплён")


@dp.message(pv, ~ops)
async def stranger(m: Message):
    u = m.from_user
    await m.answer("Запрос на доступ отправлен администратору. Как только он выдаст роль — напиши /start.")
    who = f"{u.full_name}" + (f" (@{u.username})" if u.username else "") + f", ID {u.id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Супер-админ", callback_data=f"role:{u.id}:super"),
         InlineKeyboardButton(text="1С оператор", callback_data=f"role:{u.id}:viewer")],
        [InlineKeyboardButton(text="Системный супер-админ", callback_data=f"role:{u.id}:sys")],
        [InlineKeyboardButton(text="Отклонить", callback_data=f"role:{u.id}:no")]])
    for sid in roles.sys_ids():
        try:
            await bot.send_message(sid, f"🔑 Просит доступ: {who}", reply_markup=kb)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("role:"), sysf)
async def give_role(c: CallbackQuery):
    _, uid, role = c.data.split(":")
    uid = int(uid)
    if role == "no":
        await c.message.edit_text(c.message.text + "\n❌ Отклонено")
        await c.answer()
        return
    name = c.message.text.split(": ", 1)[-1].split(",")[0]
    try:
        roles.set_role(uid, role, name)
    except ValueError as e:
        await c.answer(str(e), show_alert=True)
        return
    from .backup import mark_dirty
    mark_dirty()
    await c.message.edit_text(c.message.text + f"\n✅ Роль: {roles.ROLE_NAMES[role]}")
    try:
        await bot.send_message(uid, f"Доступ выдан: {roles.ROLE_NAMES[role]}. Нажми /start.")
    except Exception:
        pass
    await c.answer()


@dp.message(pv, sysf, Command("users"))
async def list_users_cmd(m: Message):
    lines = [f"• {u['name'] or '—'} · ID {u['tg_id']} · {u['role_name']}" for u in roles.users()]
    await m.answer("Пользователи:\n" + "\n".join(lines) + "\n\nМенять роли — «Учёт» → Пользователи.")


@dp.message(pv, ops, Command("excel"))
async def excel_cmd(m: Message):
    """Everyone (including 1С) can get the current report."""
    from .models import TopUp
    with session() as s:
        ids = [t.id for t in s.exec(select(TopUp)).all()]
    if not ids:
        await m.answer("Пока нет ни одного пополнения.")
        return
    await m.answer("Собираю учёт…")
    aud = "owner" if roles.can_write(roles.role_of(m.from_user.id)) else "operator"
    await export_topups(ids, m.from_user.id, "📊 Актуальный учёт", audience=aud)


@dp.message(pv, ops, F.document | F.photo)
async def viewer_upload(m: Message):
    await m.answer("У тебя роль «1С оператор» — только просмотр. Отчёт: /excel или «Учёт» → «Excel в чат».")


@dp.message(pv, wr, F.text.func(lambda t: bool(re.search(r"(?:9\d|1[0-4]\d)\s*[.,x×*]\s*\d{2}\s*[.,x×*]\s*\d{2}(?!\d)", t or ""))))
async def weights_message(m: Message):
    """Expolanka weights from WhatsApp ('Zeeflora - 11 - 100.48.25 …') -> volumetric kg per farm."""
    from .api import learn_dims, volumetric_breakdown
    rows = volumetric_breakdown(m.text)
    if not rows:
        await m.answer("Не понял размеры. Формат как у Expolanka: `Zeeflora - 11 - 100.48.25`", parse_mode="Markdown")
        return
    learn_dims([r for r in rows if r["farm"] != "?"])
    fmt = lambda x: f"{x:g}"
    lines = []
    for r in rows:
        parts = " + ".join(f"{n}×{d} ({fmt(k)} кг)" + (" — обычная коробка" if len(rest) else "")
                           for n, d, k, *rest in r["dims"])
        lines.append(f"• {r['farm']}: {r['boxes']} кор. = {fmt(r['kg'])} кг  [{parts}]")
    total = sum(r["kg"] for r in rows)
    txt = "📦 Объёмный вес (Д×Ш×В / 6000):\n" + "\n".join(lines) + f"\nИтого {fmt(round(total, 1))} кг"
    if any(r["farm"] == "?" for r in rows):
        txt += "\n⚠️ У одной строки нет фермы — допиши имя и пришли ещё раз"
    per = [{"farm": r["farm"], "kg": round(r["kg"], 1), "boxes": r["boxes"]} for r in rows if r["farm"] != "?"]
    awb = ai.find_mawb(m.text)
    if awb:
        await m.answer(txt + "\n\n" + _applied_text(store_breakdown(awb, per)))
        return
    did = save_draft({"doc_type": "kg_breakdown", "awb": None, "per_farm_kg": per, "lines": [], "warnings": [],
                      "source": "volumetric"})
    LAST_DRAFT[m.from_user.id] = ([did], time.time())
    await m.answer(txt + "\n\nНапиши MAWB этой отправки — сохраню как разбивку.")


@dp.message(pv, wr, F.text.func(lambda t: any(money_from_text(t)) and not ai.find_mawb(t)))
async def money_followup(m: Message):
    flow = FLOW.get(m.from_user.id)
    if flow and time.time() - flow["ts"] < 60 * 60:
        docs = _flow_docs(flow)
        pays = payments_for(m.text, [d for _p, d in docs]) or []
        if not any(pr and pr[0] for pr in pays):
            await m.answer("Не вижу суммы в $ — напиши, например, `1115$`", parse_mode="Markdown")
            return
        flow["pays"] = pays
        await _step(m.from_user.id)
        return
    pay = PAY.get(m.from_user.id)
    if pay and pay.get("topup"):
        usd, rub = money_from_text(m.text)
        if not usd:
            await m.answer("Нужна сумма в $, например `1115$`", parse_mode="Markdown")
            return
        await _finish_pay(m.from_user.id, usd, rub)
        return
    await _money_followup_legacy(m)


async def _money_followup_legacy(m: Message):
    """'1198$ 105472₽' right after a document -> books the last draft(s).
    For a split trader invoice: one line per farm ('Agriflora 301$ 26500₽'), or one sum for all."""
    ids, ts = LAST_DRAFT.get(m.from_user.id, ([], 0))
    paths = [DRAFTS / f"{i}.json" for i in ids if (DRAFTS / f"{i}.json").exists()]
    if not paths or time.time() - ts > 30 * 60:
        await m.answer("Не к чему привязать суммы: сначала пришли документ (суммы можно прямо в подписи).")
        return
    docs = [json.loads(p.read_text()) for p in paths]
    pays = payments_for(m.text, docs) or []
    from .models import TopUp
    out, left = [], []
    for p, d, pr in zip(paths, docs, pays + [None] * (len(docs) - len(pays))):
        usd, rub = pr or (None, None)
        if d.get("doc_type") == "freight_invoice" and usd is None:
            usd = (d.get("freight") or {}).get("total_usd")
        if not usd:
            left.append(d.get("farm") or "документ")
            continue
        with session() as s:
            t = s.get(TopUp, d.get("topup_id") or 0)
        t = t or active_topup()
        snap, kind = book_document(d, t.id, usd, rub, m.from_user.id)
        if not snap:
            out.append(f"Не внёс {d.get('farm') or ''}: {kind}")
            continue
        p.unlink()
        out.append((f"{d['farm']}: " if len(docs) > 1 else "") + _booked_text(snap, d, kind, t, m.from_user.id))
        await _flush_export(m.from_user.id, "📊 Логистика легла на товар из прошлого пополнения.")
    if left:
        out.append("Без сумм остались: " + ", ".join(left) + " — пришли `$` для них.")
        LAST_DRAFT[m.from_user.id] = ([p.stem for p in paths if p.exists()], time.time())
    await m.answer("\n\n".join(out), parse_mode="Markdown")


@dp.message(pv, wr, Command("pop"))
async def choose_topup(m: Message):
    """Pick the top-up that chat documents go to."""
    from .models import TopUp
    act = active_topup()
    with session() as s:
        tops = s.exec(select(TopUp).order_by(TopUp.id.desc()).limit(8)).all()
    if not tops:
        await m.answer("Пополнений пока нет — создай в «Учёт».")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=("✅ " if act and t.id == act.id else "") + f"{t.date} · ${t.usd:g}", callback_data=f"pop:{t.id}")] for t in tops])
    await m.answer("Куда вносить документы из чата:", reply_markup=kb)


@dp.callback_query(F.data.startswith("pop:"), wr)
async def set_topup(c: CallbackQuery):
    from .models import TopUp
    tid = int(c.data.split(":")[1])
    set_active_topup(tid)
    with session() as s:
        t = s.get(TopUp, tid)
    await c.message.edit_text(f"Документы из чата теперь идут в пополнение {t.date} ✅\n"
                              f"Разово другое — напиши дату в подписи: «23.09 1198$ 105472₽»")
    await c.answer()


@dp.message(pv, wr, F.text.func(lambda t: bool(ai.find_mawb(t))))
async def mawb_followup(m: Message):
    """MAWB sent as a separate message right after a document -> goes into that draft.
    MAWB + 'Farm kg' pairs in one message -> a kg breakdown for that MAWB."""
    awb_t, pairs = breakdown_from_text(m.text)
    if len(pairs) >= 1:
        w = store_breakdown(awb_t, pairs)
        await m.answer(f"⚖️ Разбивка MAWB {awb_t}: " + ", ".join(f"{p['farm']} {p['kg']:g} кг" for p in pairs)
                       + "\n" + _applied_text(w))
        return
    ids, ts = LAST_DRAFT.get(m.from_user.id, ([], 0))
    did = ids[0] if ids else None
    path = DRAFTS / f"{did}.json" if did else None
    if not path or not path.exists() or time.time() - ts > 15 * 60:
        await m.answer("Не к чему привязать этот MAWB: сначала пришли документ (можно MAWB прямо в подписи к нему).")
        return
    d = json.loads(path.read_text())
    if d.get("doc_type") == "kg_breakdown":
        w = store_breakdown(ai.find_mawb(m.text), d.get("per_farm_kg"), d.get("source_file"))
        path.unlink()
        await m.answer(f"MAWB {ai.find_mawb(m.text)}\n" + _applied_text(w))
        return
    old, d["awb"] = d.get("awb"), ai.find_mawb(m.text)
    d["warnings"] = [w for w in d.get("warnings", []) if "MAWB" not in w and "awb" not in w.lower()]
    path.write_text(json.dumps(d, ensure_ascii=False))
    from .backup import mark_dirty
    mark_dirty()
    await m.answer(f"MAWB {d['awb']} записан в черновик #{did}" + (f" (было {old})" if old and old != d["awb"] else ""))


# ---------- payments from the reminder / «В пути» -----------------------------------------
PAY: dict[int, dict] = {}    # user -> {"type": "i"|"l", "id": .., "topup": ..}


async def _ask_pay_topup(uid: int, kind: str, oid: int, note=None):
    from .models import TopUp
    PAY[uid] = {"type": kind, "id": oid, "topup": None}
    with session() as s:
        tops = s.exec(select(TopUp).order_by(TopUp.id.desc()).limit(8)).all()
    rows = [[(f"{x.date} · курс {x.rub / x.usd:.2f}", f"pt:{x.id}")] for x in tops]
    await _send(uid, "Из какого пополнения оплачен?", _kb(rows), note)


@dp.callback_query(F.data.startswith("pay:"), wr)
async def pay_button(c: CallbackQuery):
    _, kind, oid = c.data.split(":")
    await c.answer()
    await _ask_pay_topup(c.from_user.id, kind, int(oid))


@dp.callback_query(F.data.startswith("pt:"), wr)
async def pay_topup_button(c: CallbackQuery):
    from .models import Invoice, Logistics, TopUp
    pay = PAY.get(c.from_user.id)
    if not pay:
        await c.answer("Устарело — нажми «Оплачен» ещё раз", show_alert=True)
        return
    pay["topup"] = int(c.data.split(":")[1])
    await c.answer()
    with session() as s:
        t = s.get(TopUp, pay["topup"])
        o = s.get(Invoice if pay["type"] == "i" else Logistics, pay["id"])
        hint = (o.est_usd if pay["type"] == "i" else o.usd) if o else None
        name = (o.farm if pay["type"] == "i" else o.provider) if o else "?"
    kb = _kb([[(f"Как было: ${hint:g}", f"pu:{hint}")]]) if hint else None
    await c.message.edit_text(f"{name} → пополнение {t.date}. Сколько $ оплатили? ₽ посчитаю по курсу {t.rub / t.usd:.2f}"
                              f" (если списали иначе — добавь ₽: `1115$ 98000₽`).", reply_markup=kb)


@dp.callback_query(F.data.startswith("pu:"), wr)
async def pay_same_usd(c: CallbackQuery):
    await c.answer()
    await _finish_pay(c.from_user.id, float(c.data.split(":")[1]), None, c.message)


async def _finish_pay(uid: int, usd: float, rub, note=None):
    from .api import pay_invoice, pay_logistics
    pay = PAY.pop(uid, None)
    if not pay:
        return
    try:
        r = pay_invoice(pay["id"], pay["topup"], usd, rub) if pay["type"] == "i" else \
            pay_logistics(pay["id"], pay["topup"], usd, rub)
    except ValueError as e:
        await _send(uid, f"Не вышло: {e}", None, note)
        return
    who = r.get("farm") or r.get("provider")
    await _send(uid, f"✅ {who}: оплачен из пополнения {r['topup']} — ${r['usd']:g} / {r['rub']:,.0f} ₽".replace(",", " ")
                + (" (по курсу)" if rub is None else "") + ". Себестоимость теперь точная.", None, note)
    _AUTO_EXPORT.add(pay["topup"])
    await _flush_export(uid, "📊 Инвойс оплачен — лист пополнения обновлён.")


# ---------- scheduler: arrivals + payment reminders (Tue 10:00 & Wed 10:00 MSK) ---------------
def _msk_now():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)


def _money_people():
    return [u["tg_id"] for u in roles.users() if u["role"] in ("sys", "super")]


async def payment_reminder(uid_list=None):
    from .api import unpaid_items
    u = unpaid_items()
    fmt = lambda x: f"{x:,.0f}".replace(",", " ")
    if not u["invoices"] and not u["logistics"]:
        text, kb = "💸 Платежи: неоплаченных инвойсов нет.", None
    else:
        rate = u["rate"] or 0
        lines, rows, tot_usd, tot_rub = [], [], 0.0, 0.0
        for x in u["invoices"]:
            usd = x.get("est_usd") or 0
            lines.append(f"• {x['farm']} · инв {x.get('invoice_no') or '—'} · MAWB {x.get('awb') or '?'} · ≈${usd:g} ≈ {fmt(usd * rate)} ₽")
            rows.append([(f"✅ Оплачен: {x['farm']} ${usd:g}", f"pay:i:{x['id']}")])
            tot_usd += usd; tot_rub += usd * rate
        for x in u["logistics"]:
            usd = x.get("usd") or 0
            lines.append(f"• {x['provider'] or 'Фрахт'} · MAWB {x['awb']} · ${usd:g} ≈ {fmt(usd * rate)} ₽")
            rows.append([(f"✅ Оплачен: {x['provider'] or 'фрахт'} ${usd:g}", f"pay:l:{x['id']}")])
            tot_usd += usd; tot_rub += usd * rate
        text = (f"💸 Платежи на среду ({len(lines)}):\n" + "\n".join(lines) +
                f"\n\nИтого ≈ ${fmt(tot_usd)} ≈ {fmt(tot_rub)} ₽ (курс {rate:.2f}, последнее пополнение {u['topup']})")
        kb = _kb(rows[:20])
    for uid in uid_list or _money_people():
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            print(f"[lumen] reminder to {uid} failed: {e}", flush=True)


@dp.message(pv, wr, Command("mark"))
async def mark_cmd(m: Message):
    """/mark — show the default marking; /mark ABC — set it for new invoices."""
    from .api import SETTINGS, _settings, marking
    arg = (m.text or "").split(maxsplit=1)[1:] 
    if not arg:
        await m.answer(f"Маркировка по умолчанию: {marking()}\nСменить: `/mark НОВАЯ`", parse_mode="Markdown")
        return
    new = arg[0].strip().upper()
    SETTINGS.write_text(json.dumps({**_settings(), "marking": new}))
    from .backup import mark_dirty
    mark_dirty()
    await m.answer(f"✅ Новые инвойсы идут с маркировкой {new}. У отдельного инвойса её можно поменять в приложении.")


@dp.message(pv, wr, Command("packing"))
async def packing_cmd(m: Message):
    from . import packing
    ts = packing.targets()
    txt = ("Пакинг-листы уходят в:\n" + "\n".join(f"• {t['title'] or t['chat_id']}" + (" (тема)" if t.get('thread_id') else "")
                                                  for t in ts)) if ts else "Чаты для пакинг-листов не заданы."
    await m.answer(txt + "\n\nДобавить чат: добавь меня в группу и напиши там /packing_here "
                         "(в супергруппе — внутри нужной темы). Убрать: /packing_off там же.")


@dp.message(pv, wr, Command("pay"))
async def pay_cmd(m: Message):
    """Show the payment list now (same as the Tue/Wed reminder)."""
    await payment_reminder([m.from_user.id])


REMINDER_SLOTS = [(1, 10), (2, 10)]   # (weekday Mon=0, hour MSK): Tuesday 10:00 and Wednesday 10:00


async def send_packing_lists():
    """Every invoice that got a MAWB -> its packing list (.xlsx, no prices) to every registered chat/topic."""
    from aiogram.types import BufferedInputFile
    from . import packing
    tg = packing.targets()
    if not tg:
        return
    for inv_id, data, farm, awb in packing.pending():
        ok = False
        safe = re.sub(r"[^\w\-]+", "_", f"{farm}_{awb}")
        for t in tg:
            try:
                await bot.send_document(t["chat_id"], BufferedInputFile(data, f"Packing_{safe}.xlsx"),
                                        caption=f"📦 Packing list · {farm} · MAWB {awb}",
                                        message_thread_id=t.get("thread_id"))
                ok = True
            except Exception as e:
                print(f"[lumen] packing to {t.get('title')}: {e}", flush=True)
        if ok:
            packing.mark_sent(inv_id)


async def scheduler_loop():
    from .api import _settings, SETTINGS, arrive_due
    while True:
        try:
            now = _msk_now()
            await send_packing_lists()
            done = arrive_due(now.strftime("%Y-%m-%dT%H:%M"))
            from . import clients, reader
            from .api import client_arrivals_due
            cdone = client_arrivals_due(now.strftime("%Y-%m-%dT%H:%M"))     # TK time + 6 h
            if cdone:
                await clients.send(reader.RBOT, clients.arrived_messages(cdone))
            if done:
                txt = "📦 Прибыло на склад (по сообщению Floratrack +1 ч):\n" + "\n".join(
                    f"• {d['farm']} · MAWB {d['awb']} · {'оплачен' if d['paid'] else 'НЕ оплачен'}" for d in done)
                for uid in _money_people():
                    try:
                        await bot.send_message(uid, txt)
                    except Exception:
                        pass
            slot = None
            for wd, hh in REMINDER_SLOTS:
                if now.weekday() == wd and now.hour == hh and now.minute < 15:
                    slot = now.strftime(f"%Y-%m-%d-{hh}")
            st = _settings()
            if slot and st.get("last_reminder") != slot:
                SETTINGS.write_text(json.dumps({**st, "last_reminder": slot}))
                await payment_reminder()
        except Exception as e:
            print(f"[lumen] scheduler: {e}", flush=True)
        await asyncio.sleep(30)


# ---------- Floratrack group chat: truck on the way -> arrival = their time + 1 h ------------
grp = Router()
grp.message.filter(F.chat.type.in_({"group", "supergroup"}))
dp.include_router(grp)
_FT_PENDING: dict[int, list] = {}


_TRUCK_BUF: dict[int, list] = {}


async def _truck_event(uid_list, text: str, sent_msk, notify_uid=None):
    """Parse + apply one FLORA TRUCK message; collect summaries and send one message per batch."""
    from . import truck
    ev = truck.parse(text, sent_msk)
    if not ev:
        return False
    from . import clients, reader
    fresh = clients.is_fresh(sent_msk, _msk_now())
    per_client = clients.messages_for(ev) if fresh else {}
    line = truck.apply(ev, _msk_now(), fresh=fresh)
    await clients.send(reader.RBOT, per_client)
    targets = [notify_uid] if notify_uid else uid_list
    for uid in targets:
        buf = _TRUCK_BUF.setdefault(uid, [])
        buf.append((sent_msk, line))
        if len(buf) == 1:
            asyncio.create_task(_flush_truck(uid))
    return True


async def _flush_truck(uid: int):
    await asyncio.sleep(4)                       # forwarded messages arrive one by one — wait for the batch
    items = sorted(_TRUCK_BUF.pop(uid, []), key=lambda x: x[0])
    text = "\n\n".join(l for _d, l in items if l)
    if text:
        try:
            await bot.send_message(uid, text[:4000])
        except Exception:
            pass


async def _handle_truck(chat_title: str, text: str, sent_msk):
    await _truck_event(_money_people(), text, sent_msk)


async def process_group_text(chat_id: int, chat_title: str, text: str, sent_msk):
    """A message from a group/channel (seen by the main bot OR by the reader bot).
    Only an approved TK MSK chat is processed; the first time the system admin is asked in private."""
    from .api import _settings
    if not re.search(r"товар\s+забран|едет на склад|время прибытия|прошла границу", text or "", re.I):
        return
    if chat_id in (_settings().get("ft_chats") or []):
        await _handle_truck(chat_title or "", text, sent_msk)
        return
    pending = _FT_PENDING.setdefault(chat_id, [])
    pending.append((text, sent_msk))
    if len(pending) > 1:
        return                                   # already asked; keep collecting until approved
    for uid in roles.sys_ids():
        try:
            await bot.send_message(uid, f"Вижу сообщения о машинах в чате «{chat_title}». Это чат ТК МСК (Floratrack)?",
                                   reply_markup=_kb([[("Да, это ТК МСК", f"ftc:{chat_id}:0"), ("Нет", f"ftn:{chat_id}")]]))
        except Exception:
            pass


_READER_SEEN: dict = {}


async def reader_joined(chat_id: int, title: str, chat_type: str, status: str, reads_all: bool, username: str):
    """The reader bot was added to (or removed from) a chat: health check to the system admin."""
    from .api import _settings
    key = (chat_id, "out" if status in ("left", "kicked") else "in")
    if _READER_SEEN.get(chat_id) == key[1]:
        return                                   # same state already reported
    _READER_SEEN[chat_id] = key[1]
    if status in ("left", "kicked"):
        text, kb = f"⚠️ Читатель @{username} удалён из чата «{title}». Сообщения о машинах больше не приходят.", None
    else:
        ok_privacy = reads_all or chat_type == "channel"
        ok_admin = chat_type != "channel" or status == "administrator"
        approved = chat_id in (_settings().get("ft_chats") or [])
        lines = [f"👀 Читатель @{username} добавлен в «{title}» ({'канал' if chat_type == 'channel' else 'группа'})",
                 ("✅" if ok_privacy else "❌") + " видит все сообщения" +
                 ("" if ok_privacy else " — в @BotFather: /setprivacy → Disable, потом удали и добавь бота заново"),
                 ("✅" if ok_admin else "❌") + (" права есть" if ok_admin else " в канале бот должен быть админом")]
        if approved:
            lines.append("✅ этот чат уже отмечен как ТК МСК")
            kb = None
        else:
            lines.append("Если это чат ТК МСК — подтверди, и сообщения о машинах пойдут сразу:")
            kb = _kb([[("Да, это ТК МСК", f"ftc:{chat_id}:0"), ("Нет", f"ftn:{chat_id}")]])
        if ok_privacy and ok_admin:
            lines.append("\nВсё готово — теперь просто жди сообщений о машинах, я буду присылать сводки сюда.")
        text = "\n".join(lines)
    for uid in roles.sys_ids():
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception:
            pass


@grp.message(Command("packing_here"), F.from_user.id.func(lambda i: roles.can_write(roles.role_of(i))))
async def packing_here(m: Message):
    """Type /packing_here in a group (or inside a topic of a supergroup) -> packing lists go here."""
    from . import packing
    t = {"chat_id": m.chat.id, "thread_id": m.message_thread_id if m.is_topic_message else None,
         "title": m.chat.title or ""}
    ts = [x for x in packing.targets() if not (x["chat_id"] == t["chat_id"] and x.get("thread_id") == t["thread_id"])]
    packing.set_targets(ts + [t])
    await m.reply("✅ Сюда будут приходить пакинг-листы (как только у инвойса появится MAWB).")


@grp.message(Command("packing_off"), F.from_user.id.func(lambda i: roles.can_write(roles.role_of(i))))
async def packing_off(m: Message):
    from . import packing
    th = m.message_thread_id if m.is_topic_message else None
    packing.set_targets([x for x in packing.targets() if not (x["chat_id"] == m.chat.id and x.get("thread_id") == th)])
    await m.reply("Пакинг-листы сюда больше не отправляю.")


@grp.message(F.text)
async def group_text(m: Message):
    from datetime import timedelta
    await process_group_text(m.chat.id, m.chat.title or "", m.text or "", m.date.replace(tzinfo=None) + timedelta(hours=3))


@dp.callback_query(F.data.startswith("ftc:"), sysf)
async def ft_chat_yes(c: CallbackQuery):
    from datetime import datetime
    from .api import _settings, SETTINGS
    _, cid, ts = c.data.split(":")
    cid = int(cid)
    st = _settings()
    SETTINGS.write_text(json.dumps({**st, "ft_chats": sorted(set((st.get("ft_chats") or []) + [cid]))}))
    await c.message.edit_text("✅ Запомнил чат Floratrack — машины из него буду закрывать как «прибыл» сами.")
    await c.answer()
    for text, sent in _FT_PENDING.pop(cid, []):
        await _handle_truck("", text, sent)


@dp.callback_query(F.data.startswith("ftn:"), sysf)
async def ft_chat_no(c: CallbackQuery):
    await c.message.edit_text("Ок, этот чат игнорирую.")
    await c.answer()


# catch-all goes LAST so /pay and other commands above get a chance first
@dp.message(pv, ops)
async def other(m: Message):
    await m.answer("Жду PDF/фото инвойса или .xlsx мастер-файла. Всё остальное — в «Учёт».")
