import shutil
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo)
from sqlmodel import select

from . import ai
from .api import save_draft
from .config import ALLOWED_IDS, BOT_TOKEN, DATA_DIR, MASTER_XLSX, WEBAPP_URL
from .models import Farm, Line, session

bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
ops = F.from_user.id.in_(ALLOWED_IDS)

KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[
    KeyboardButton(text="📒 Учёт", web_app=WebAppInfo(url=WEBAPP_URL))]])


@dp.message(CommandStart(), ops)
async def start(m: Message):
    await m.answer("Учёт поставок Люмен.\n\n"
                   "• Открывай «📒 Учёт» — пополнения, инвойсы, логистика, выгрузка в Excel.\n"
                   "• Кидай сюда PDF/фото инвойса или счёта за фрахт — распознаю и положу в черновики.\n"
                   "• Кинь .xlsx — он станет мастер-файлом учёта (новые листы пишутся в него).",
                   reply_markup=KB)


@dp.message(ops, F.document.file_name.lower().endswith(".xlsx"))
async def master(m: Message):
    if MASTER_XLSX.exists():
        shutil.copy(MASTER_XLSX, DATA_DIR / f"учет_backup_{datetime.now():%Y%m%d_%H%M}.xlsx")
    await bot.download(m.document, destination=MASTER_XLSX)
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
    txt += f"\n\nЧерновик #{did} — открой «📒 Учёт» → Черновики, проверь и впиши реально оплаченные $/₽."
    await note.edit_text(txt)


@dp.message(ops, F.document.mime_type.in_({"application/pdf", "image/jpeg", "image/png"}))
async def doc(m: Message):
    f = await bot.download(m.document)
    await _parse_and_reply(m, f.read(), m.document.mime_type)


@dp.message(ops, F.photo)
async def photo(m: Message):
    f = await bot.download(m.photo[-1])
    await _parse_and_reply(m, f.read(), "image/jpeg")


@dp.message(~ops)
async def stranger(m: Message):
    await m.answer("Нет доступа.")


@dp.message(ops)
async def other(m: Message):
    await m.answer("Жду PDF/фото инвойса или .xlsx мастер-файла. Всё остальное — в «📒 Учёт».", reply_markup=KB)
