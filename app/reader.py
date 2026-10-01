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
from aiogram.types import ChatMemberUpdated, Message

READER_BOT_TOKEN = os.getenv("READER_BOT_TOKEN", "").strip()
RBOT = None          # the reader Bot instance — it also posts statuses into client chats


def enabled() -> bool:
    return bool(READER_BOT_TOKEN)


def build():
    global RBOT
    from aiogram.filters import Command
    from . import roles
    rbot = Bot(READER_BOT_TOKEN)
    RBOT = rbot
    rdp = Dispatcher()
    staff = F.from_user.id.func(lambda i: roles.can_write(roles.role_of(i)))

    @rdp.message(Command("marking_here"), staff)
    async def marking_here(m: Message):
        """In a client's group: /marking_here ABC -> statuses of shipments with marking ABC go here."""
        from . import clients
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.reply("Напиши маркировку: /marking_here ABC")
            return
        mk = parts[1].strip().upper()
        th = m.message_thread_id if m.is_topic_message else None
        clients.add_chat(mk, m.chat.id, th, m.chat.title or "")
        await m.reply(f"✅ Статусы грузов с маркировкой {mk} будут приходить сюда.")

    @rdp.message(Command("marking_off"), staff)
    async def marking_off(m: Message):
        from . import clients
        th = m.message_thread_id if m.is_topic_message else None
        gone = clients.remove_chat(m.chat.id, th)
        await m.reply("Статусы сюда больше не отправляю." + (f" ({', '.join(gone)})" if gone else ""))

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

    @rdp.my_chat_member()
    async def added_or_removed(ev: ChatMemberUpdated):
        """Reader added to / removed from a chat -> tell the system admin via the MAIN bot, with a health check."""
        from .bot import reader_joined
        me = await rbot.get_me()
        status = ev.new_chat_member.status
        await reader_joined(ev.chat.id, ev.chat.title or "", ev.chat.type, status,
                            bool(getattr(me, "can_read_all_group_messages", False)), me.username)

    @rdp.message(F.chat.type == "private")
    async def private_msg(m: Message):
        return                                    # silent: the reader never talks to anyone

    return rbot, rdp
