import json
import shutil
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message,
                           ReplyKeyboardRemove, WebAppInfo)
from sqlmodel import select

from . import ai
from .api import (DRAFTS, active_topup, book_document, import_floratrack, payments_for, split_by_farm, fill_mawb, money_from_text, save_draft,
                  set_active_topup, store_breakdown, topup_from_text)
from .config import ALLOWED_IDS, BOT_TOKEN, DATA_DIR, MASTER_XLSX, WEBAPP_URL
from .models import Farm, Line, session

LAST_DRAFT: dict[int, tuple[list, float]] = {}   # user -> ([draft ids], time)
bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
ops = F.from_user.id.in_(ALLOWED_IDS)

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


@dp.message(CommandStart(), ops)
async def start(m: Message):
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


@dp.message(ops, F.document.file_name.lower().endswith(".xlsx"))
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
           f"Баланс у Floratrack: ${r['balance_usd']:,.2f}\n\n" + "\n".join(lines))
    fresh = [c for c in r["unmatched"] if c.date and (datetime.now() - c.date).days <= 21]
    if fresh:
        txt += "\n\nНет наших инвойсов/разбивки с этими AWB (последние 3 недели):\n" + "\n".join(
            f"• …{c.last4} · {kinds[c.kind]} · {c.kg:g} кг · {c.sheet}" for c in fresh[:15])
        txt += "\nВнеси их и перекинь отчёт — привяжутся."
    if r["ambiguous"]:
        txt += "\n\n⚠️ Несколько MAWB с такими 4 цифрами, взял по весу: " + ", ".join(r["ambiguous"][:10])
    warn = [w for w in r["warnings"] if "MAWB" in w][:8]
    if warn:
        txt += "\n\n⚠️ " + "\n⚠️ ".join(warn)
    await note.edit_text(txt[:4000])


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

    t = topup_from_text(m.caption or "") or active_topup()
    if not t:
        did = save_draft(out)
        await note.edit_text("Сначала создай пополнение в «Учёт» — документ лежит в черновиках.")
        return
    out["topup_id"] = t.id
    if out.get("doc_type") == "farm_invoice":
        subs = split_by_farm(out)
        if len(subs) > 1:
            await _book_or_draft_multi(m, note, subs, t)
            return
        fill_mawb(out, t.id)
    usd, rub = money_from_text(m.caption or "")
    if out.get("doc_type") == "freight_invoice" and usd is None:
        usd = (out.get("freight") or {}).get("total_usd")          # $ of a freight bill is on the bill itself
    await _book_or_draft(m, note, out, t, usd, rub)


async def _book_or_draft(m: Message, note, out: dict, t, usd, rub):
    """Both sums known -> straight into the top-up. Otherwise a draft waiting for '1198$ 105472₽'."""
    warn = ("\n⚠️ " + "\n⚠️ ".join(out["warnings"])) if out.get("warnings") else ""
    head = _doc_head(out)
    if usd and rub:
        snap, kind = book_document(out, t.id, usd, rub, m.from_user.id)
        if snap:
            await note.edit_text(head + warn + "\n\n" + _booked_text(snap, out, kind, t))
            return
        warn += f"\n⚠️ Не внёс сразу: {kind}"
    did = save_draft(out)
    LAST_DRAFT[m.from_user.id] = ([did], time.time())
    need = "₽" if usd else "$ и ₽"
    await _edit(note, head + warn + f"\n\n→ пополнение {t.date}. Ответь суммой оплаты ({need}), например "
                      f"`{usd or 1198:g}$ 105472₽` — внесу сразу. Или «Учёт» → Черновики.")


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
        if pr and pr[0] and pr[1]:
            snap, kind = book_document(d, t.id, pr[0], pr[1], m.from_user.id)
            if snap:
                res.append(f"{d['farm']}: " + _booked_text(snap, d, kind, t).split("\n")[0].replace("✅ ", "✅ "))
                continue
        left.append(d)
    if left:
        ids = [save_draft(d) for d in left]
        LAST_DRAFT[m.from_user.id] = (ids, time.time())
        ex = "\n".join(f"{d['farm']} {round(d['invoice_total_usd'])}$ …₽" for d in left)
        res.append("Ответь оплатой по каждой плантации, по строке на каждую:\n`" + ex + "`\n"
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


def _booked_text(snap: dict, out: dict, kind: str, t) -> str:
    fmt = lambda x: f"{x:,.0f}".replace(",", " ")
    if kind == "freight":
        lg = max((l for l in snap["logistics"] if l["topup_id"] == t.id), key=lambda l: l["id"])
        txt = (f"✅ Фрахт внесён в пополнение {t.date}: ${lg['usd'] or 0:g} / {fmt(lg['rub'] or 0)} ₽"
               + (f" · {lg['weight_kg']:g} кг по счёту" if lg.get("weight_kg") else "")
               + (f" · {lg['rub_per_kg']:.2f} ₽/кг" if lg.get("rub_per_kg") else "")
               + (f" · разбивка {lg['kg_total']:g} кг ✓" if lg.get("kg_total") else " · разбивки кг ещё нет"))
    else:
        inv = max(snap["invoices"], key=lambda i: i["id"])
        st = sum(l["stems"] for l in inv["lines"]) or 1
        flower = sum(l["price_rub"] * l["stems"] for l in inv["lines"]) / st
        logi = sum((l["air_rub"] + l["msk_rub"]) * l["stems"] for l in inv["lines"]) / st
        txt = (f"✅ Внесено в пополнение {t.date}: ${inv['usd_paid']:g} / {fmt(inv['rub_paid'])} ₽\n"
               f"Себестоимость в среднем {flower + logi:.2f} ₽/стебель (цветок {flower:.2f} + логистика {logi:.2f})")
    rel = [w for w in snap["warnings"] if (out.get("farm") or "~").lower()[:5] in w.lower()
           or (out.get("awb") or "~").replace("-", "")[:6] in w.replace("-", "")]
    return txt + ("\n⚠️ " + "\n⚠️ ".join(rel) if rel else "") + f"\nОстаток пополнения ${snap['usd_left']:g}"


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


@dp.message(ops, F.document.mime_type.in_({"application/pdf", "image/jpeg", "image/png"}))
async def doc(m: Message):
    f = await bot.download(m.document)
    await _parse_and_reply(m, f.read(), m.document.mime_type)


@dp.message(ops, F.photo)
async def photo(m: Message):
    f = await bot.download(m.photo[-1])
    await _parse_and_reply(m, f.read(), "image/jpeg")


@dp.channel_post(F.text.startswith("/id"))
async def channel_id(m: Message):
    """Post /id in the backup channel -> bot replies with the channel id for BACKUP_CHAT_ID."""
    await m.answer(f"BACKUP_CHAT_ID={m.chat.id}")


@dp.message(ops, F.forward_origin.chat)
async def forwarded_from_channel(m: Message):
    """Forward any post from the channel to the bot -> it tells the channel id. Works even if
    the bot isn't admin yet (but it must be admin for backups to work)."""
    ch = m.forward_origin.chat
    await m.answer(f"Канал «{ch.title}»\nBACKUP_CHAT_ID={ch.id}\n\nВставь эту строку в .env и перезапусти. "
                   "Бот должен быть админом канала с правом публиковать и закреплять.")


@dp.message(ops, Command("backup"))
async def manual_backup(m: Message):
    from .backup import BACKUP_CHAT_ID, backup_now
    if not BACKUP_CHAT_ID:
        await m.answer("BACKUP_CHAT_ID не задан в .env")
        return
    await backup_now(bot, "вручную")
    await m.answer("💾 Бэкап отправлен в канал и закреплён")


@dp.message(~ops)
async def stranger(m: Message):
    await m.answer(f"Нет доступа. Твой ID: {m.from_user.id} — добавь его в ALLOWED_IDS в .env и перезапусти.")


@dp.message(ops, F.text.func(lambda t: any(money_from_text(t)) and not ai.find_mawb(t)))
async def money_followup(m: Message):
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
        if not (usd and rub):
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
        out.append((f"{d['farm']}: " if len(docs) > 1 else "") + _booked_text(snap, d, kind, t))
    if left:
        out.append("Без сумм остались: " + ", ".join(left) + " — пришли `$ и ₽` для них.")
        LAST_DRAFT[m.from_user.id] = ([p.stem for p in paths if p.exists()], time.time())
    await m.answer("\n\n".join(out), parse_mode="Markdown")


@dp.message(ops, Command("pop"))
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


@dp.callback_query(F.data.startswith("pop:"), F.from_user.id.in_(ALLOWED_IDS))
async def set_topup(c: CallbackQuery):
    from .models import TopUp
    tid = int(c.data.split(":")[1])
    set_active_topup(tid)
    with session() as s:
        t = s.get(TopUp, tid)
    await c.message.edit_text(f"Документы из чата теперь идут в пополнение {t.date} ✅\n"
                              f"Разово другое — напиши дату в подписи: «23.09 1198$ 105472₽»")
    await c.answer()


@dp.message(ops, F.text.func(lambda t: bool(ai.find_mawb(t))))
async def mawb_followup(m: Message):
    """MAWB sent as a separate message right after a document -> goes into that draft."""
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


@dp.message(ops)
async def other(m: Message):
    await m.answer("Жду PDF/фото инвойса или .xlsx мастер-файла. Всё остальное — в «Учёт».")
