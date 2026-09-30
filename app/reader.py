"""
Reader bot: a SECOND, neutral bot that sits in the TK MSK (Floratrack) chat.

Their chat only ever sees this bot (name it something neutral, e.g. "DL Logistics"). It never writes
there and ignores private messages. Everything it reads goes to the main (finance) bot, which the
TK MSK chat never sees.

.env:  READER_BOT_TOKEN=…   (a separate bot from @BotFather)
BotFather for this bot: /setprivacy -> Disable (so it sees normal group messages), /setjoingroups -> Enable.
If their chat is a CHANNEL (not a group), the reader must be added as a channel admin to see posts.
"""
import os
from datetime import timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

READER_BOT_TOKEN = os.getenv("READER_BOT_TOKEN", "").strip()


def enabled() -> bool:
    return bool(READER_BOT_TOKEN)


def build():
    rbot = Bot(READER_BOT_TOKEN)
    rdp = Dispatcher()

    async def _take(m: Message):
        from .bot import process_group_text
        text = m.text or m.caption or ""
        await process_group_text(m.chat.id, m.chat.title or "", text, m.date.replace(tzinfo=None) + timedelta(hours=3))

    @rdp.message(F.chat.type.in_({"group", "supergroup"}))
    async def group_msg(m: Message):
        await _take(m)

    @rdp.channel_post()
    async def channel_msg(m: Message):
        await _take(m)

    @rdp.message(F.chat.type == "private")
    async def private_msg(m: Message):
        return                                    # silent: the reader never talks to anyone

    return rbot, rdp
