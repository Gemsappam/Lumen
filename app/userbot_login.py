"""One-time: log a Telegram user account in and print TG_SESSION for .env.
Run on your computer:  TG_API_ID=… TG_API_HASH=… python -m app.userbot_login
It asks for the phone number and the code Telegram sends you (and the 2FA password if set)."""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(os.getenv("TG_API_ID") or input("TG_API_ID: "))
    api_hash = os.getenv("TG_API_HASH") or input("TG_API_HASH: ")
    async with TelegramClient(StringSession(), api_id, api_hash) as c:
        me = await c.get_me()
        print(f"\nВошёл как {me.first_name} (@{me.username}).\nВставь в .env строку:\n")
        print("TG_SESSION=" + c.session.save())
        print("\nНикому не показывай эту строку — это доступ к аккаунту.")

asyncio.run(main())
