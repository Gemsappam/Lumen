import shutil
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message,
                           ReplyKeyboardRemove, WebAppInfo)
from sqlmodel import select

from . import ai
from .api import save_draft
from .config import ALLOWED_IDS, BOT_TOKEN, DATA_DIR, MASTER_XLSX, WEBAPP_URL
from .models import Farm, Line, session

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


@dp.message(CommandStart(), ops)
async def start(m: Message):
    kb = KB if WEBAPP_URL.startswith("https://") else None
    await m.answer("Обновил кнопки 👇", reply_markup=ReplyKeyboardRemove())   # remove the old reply keyboard
    await m.answer("Учёт поставок Люмен.\n\n"
                   "• Кнопка ниже или «Учёт» слева от поля ввода — пополнения, инвойсы, логистика, выгрузка в Excel.\n"
                   "• Кидай сюда PDF/фото инвойса или счёта за фрахт — распознаю и положу в черновики.\n"
                   "• Кинь .xlsx — он станет мастер-файлом учёта (новые листы пишутся в него)."
                   + ("" if kb else "\n\n⚠️ WEBAPP_URL не https — кнопка приложения отключена."),
                   reply_markup=kb)


@dp.message(ops, F.document.file_name.lower().endswith(".xlsx"))
async def master(m: Message):
    if MASTER_XLSX.exists():
        shutil.copy(MASTER_XLSX, DATA_DIR / f"учет_backup_{datetime.now():%Y%m%d_%H%M}.xlsx")
    await bot.download(m.document, destination=MASTER_XLSX)
    from .backup import mark_dirty
    mark_dirty()
    await m.answer("Мастер-файл обновлён ✅ (старый сохранён в бэкап)")


async def _parse_and_reply(m: Message, data: bytes, mime: str):
    note = await m.answer("Читаю документ…")
    with session() as s:
        fs = [f.model_dump() for f in s.exec(select(Farm)).all()]
        catalog = sorted({l.name for l in s.exec(select(Line)).all()})
    try:
        out = await ai.parse_document(data, mime, fs, catalog)
    except Exception as e:
        await note.edit_text(f"Не смог прочитать: {e}")
        return
    did = save_draft(out)
    stems = sum(l.get("stems") or 0 for l in out.get("lines", []))
    txt = (f"📄 {out.get('doc_type')} · {out.get('farm') or '?'} · AWB {out.get('awb') or '?'}\n"
           f"Строк: {len(out.get('lines', []))}, стеблей: {stems:g}, итог: ${out.get('invoice_total_usd') or '?'}")
    if out.get("warnings"):
        txt += "\n⚠️ " + "\n⚠️ ".join(out["warnings"])
    txt += f"\n\nЧерновик #{did} — открой «Учёт» → Черновики, проверь и впиши реально оплаченные $/₽."
    await note.edit_text(txt)


@dp.message(ops, F.document.mime_type.in_({"application/pdf", "image/jpeg", "image/png"}))
async def doc(m: Message):
    f = await bot.download(m.document)
    await _parse_and_reply(m, f.read(), m.document.mime_type)


@dp.message(ops, F.photo)
async def photo(m: Message):
    f = await bot.download(m.photo[-1])
    await _parse_and_reply(m, f.read(), "image/jpeg")


@dp.channel_post(F.text == "/id")
async def channel_id(m: Message):
    """Post /id in the backup channel -> bot replies with the channel id for BACKUP_CHAT_ID."""
    await m.answer(f"BACKUP_CHAT_ID={m.chat.id}")


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


@dp.message(ops)
async def other(m: Message):
    await m.answer("Жду PDF/фото инвойса или .xlsx мастер-файла. Всё остальное — в «Учёт».")
