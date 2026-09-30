"""
Invisible reader of the TK MSK (FLORA TRUCK) chat.

Instead of adding a bot to their chat (members would see it), a Telegram *user* session that is
already in the chat (yours or an employee's) reads new messages and hands them to the main bot.
Nothing is ever written to their chat.

.env:
  TG_API_ID=12345                 # my.telegram.org -> API development tools
  TG_API_HASH=abcdef...
  TG_SESSION=1Aa...               # one-time: python -m app.userbot_login
  FT_CHAT=FloraMailing chat name, @username or -100… id
  FT_BACKFILL_DAYS=14             # on start, re-read the last N days once
"""
import asyncio
import os
from datetime import timedelta, timezone

API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "")
SESSION = os.getenv("TG_SESSION", "")
FT_CHAT = os.getenv("FT_CHAT", "").strip()
BACKFILL_DAYS = int(os.getenv("FT_BACKFILL_DAYS", "14") or 14)


def enabled() -> bool:
    return bool(API_ID and API_HASH and SESSION and FT_CHAT)


def _msk(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)


async def _resolve(client):
    """FT_CHAT may be @username, numeric id or (part of) the chat title."""
    if FT_CHAT.lstrip("-").isdigit():
        return await client.get_entity(int(FT_CHAT))
    if FT_CHAT.startswith("@"):
        return await client.get_entity(FT_CHAT)
    async for d in client.iter_dialogs():
        if FT_CHAT.lower() in (d.name or "").lower():
            return d.entity
    raise RuntimeError(f"чат «{FT_CHAT}» не найден среди диалогов аккаунта")


async def run(handle):
    """handle(text, sent_msk) — the main bot's processor (bot._handle_truck)."""
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("[lumen] userbot: сессия не авторизована — запусти python -m app.userbot_login", flush=True)
        return
    chat = await _resolve(client)
    print(f"[lumen] userbot: читаю «{getattr(chat, 'title', chat.id)}» (невидимо)", flush=True)

    # backfill: the last N days, oldest first (ETAs in the past close invoices as «прибыл» right away)
    from datetime import datetime
    since = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    old = []
    async for msg in client.iter_messages(chat, offset_date=None, reverse=False):
        if msg.date < since:
            break
        if msg.message:
            old.append(msg)
    for msg in reversed(old):
        await handle(msg.message, _msk(msg.date))

    @client.on(events.NewMessage(chats=chat))
    async def _new(ev):
        if ev.message.message:
            await handle(ev.message.message, _msk(ev.message.date))

    await client.run_until_disconnected()


async def run_forever(handle):
    while True:
        try:
            await run(handle)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[lumen] userbot: {e} — переподключусь через минуту", flush=True)
        await asyncio.sleep(60)
