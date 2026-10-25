"""
Telegram channel as persistent storage.

Bothost can wipe the container on every rebuild. So:
- after any change (debounced ~60 s) we zip the DB + master xlsx + drafts and post it
  to a private channel, pinned;
- on startup, if there's no local DB, we download the pinned archive and restore it.

The channel doubles as a history of backups: every post is a snapshot you can roll back to
(download the zip, send it back... or just pin an older one and restart).
"""
import asyncio
import io
import os
import sqlite3
import zipfile
from datetime import datetime

from aiogram.types import BufferedInputFile

from .config import DATA_DIR, MASTER_XLSX

BACKUP_CHAT_ID = os.getenv("BACKUP_CHAT_ID", "").strip()
DB_PATH = DATA_DIR / "lumen.db"
DEBOUNCE_SEC = 60
MIN_GAP_SEC = 300          # at most one automatic backup per 5 minutes
HASH_FILE = DATA_DIR / ".last_backup_hash"

_dirty = asyncio.Event()


def mark_dirty():
    _dirty.set()


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if DB_PATH.exists():
            # consistent copy even if a write is happening right now
            snap = DATA_DIR / "_snapshot.db"
            src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(snap)
            src.backup(dst)
            src.close(); dst.close()
            z.write(snap, "lumen.db")
            snap.unlink()
        if MASTER_XLSX.exists():
            z.write(MASTER_XLSX, "учет.xlsx")
        if (DATA_DIR / "settings.json").exists():
            z.write(DATA_DIR / "settings.json", "settings.json")
        for p in (DATA_DIR / "drafts").glob("*.json"):
            z.write(p, f"drafts/{p.name}")
        for sub in ("breakdowns", "fixsrc"):          # consolidation sheets (packing needs them) + originals for /fix
            for p in (DATA_DIR / sub).glob("*"):
                if p.is_file() and p.stat().st_size < 5_000_000:
                    z.write(p, f"{sub}/{p.name}")
    return buf.getvalue()


def _unzip(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.startswith("/") or ".." in name:
                continue
            target = DATA_DIR / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))


def _content_hash() -> str:
    """What the backup would contain — the DB as SQL text (not its pages), settings, drafts, master file."""
    import hashlib
    h = hashlib.sha256()
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        try:
            for line in con.iterdump():
                h.update(line.encode())
        finally:
            con.close()
    for p in [MASTER_XLSX, DATA_DIR / "settings.json"] + sorted((DATA_DIR / "drafts").glob("*.json")) \
            + sorted((DATA_DIR / "breakdowns").glob("*.xlsx")):
        if p.exists():
            h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()


async def backup_now(bot, reason: str = "auto", force: bool = False):
    """Posts a backup ONLY if something really changed since the last one (manual /backup: always)."""
    if not (bot and BACKUP_CHAT_ID):
        return False
    digest = _content_hash()
    last = HASH_FILE.read_text().strip() if HASH_FILE.exists() else ""
    if not force and digest == last:
        return False
    data = _make_zip()
    name = f"lumen_{datetime.now():%Y-%m-%d_%H-%M}.zip"
    msg = await bot.send_document(BACKUP_CHAT_ID, BufferedInputFile(data, name),
                                  caption=f"💾 бэкап ({reason})", disable_notification=True)
    await bot.pin_chat_message(BACKUP_CHAT_ID, msg.message_id, disable_notification=True)
    print(f"[lumen] backup → channel ({len(data) // 1024} KB, {reason})", flush=True)
    HASH_FILE.write_text(digest)
    return True


async def restore_if_empty(bot) -> str:
    """Restore data from the pinned backup when the container starts empty."""
    if not (bot and BACKUP_CHAT_ID):
        return "backup off (BACKUP_CHAT_ID not set)"
    if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        return "local data present, restore skipped"
    chat = await bot.get_chat(BACKUP_CHAT_ID)
    pm = chat.pinned_message
    if not pm or not pm.document:
        return "no pinned backup in channel — starting clean"
    f = await bot.download(pm.document)
    _unzip(f.read())
    return f"restored from {pm.document.file_name}"


async def flush(bot):
    """On shutdown/restart: don't lose changes still waiting for the debounce."""
    if _dirty.is_set():
        _dirty.clear()
        await backup_now(bot, "перед перезапуском")


async def backup_loop(bot):
    while True:
        await _dirty.wait()
        await asyncio.sleep(DEBOUNCE_SEC)      # collect a burst of edits into one backup
        _dirty.clear()
        try:
            if await backup_now(bot):
                await asyncio.sleep(MIN_GAP_SEC)   # later edits wait and go out together in the next one
        except Exception as e:
            print(f"[lumen] backup failed: {e}", flush=True)
            _dirty.set()
            await asyncio.sleep(300)


async def cleanup_channel(bot, keep: int = 10) -> int:
    """Delete old backup posts from the channel, keep the newest `keep` (the pinned one is the newest)."""
    if not (bot and BACKUP_CHAT_ID):
        return 0
    probe = await bot.send_message(BACKUP_CHAT_ID, "🧹 чищу старые бэкапы…", disable_notification=True)
    top = probe.message_id
    ids = list(range(1, max(1, top - keep)))
    deleted = 0
    for i in range(0, len(ids), 100):
        try:
            if await bot.delete_messages(BACKUP_CHAT_ID, ids[i:i + 100]):
                deleted += len(ids[i:i + 100])
        except Exception as e:
            print(f"[lumen] cleanup batch: {e}", flush=True)
        await asyncio.sleep(0.05)
    try:
        await bot.delete_message(BACKUP_CHAT_ID, top)
    except Exception:
        pass
    return deleted
