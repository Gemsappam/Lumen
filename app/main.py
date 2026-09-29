import asyncio
import os
import sys
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import select

from . import api
from .models import Farm, init_db, session

SEED = {
    "Кения": ["Zeeflora", "Tambuzi", "Heritage", "Agriflora", "Massai", "Mzurrie (Winchester farm)", "Subati",
              "Kikwetu", "Karen Roses", "Batian", "Black Tulip", "PjDave", "Primarosa", "Red Lands"],
    "Эквадор": ["Nintanga", "Agroterranorte", "Josar Flor", "Tikan", "EC Blooms (Starroses)", "Sand Flowers",
                "Allegro Farms", "Dayka", "Guaisa (Sunrite)", "Meral Flowers", "Monterosas", "Rosaprima", "Rosas Del Vento"],
    "Колумбия": ["Кондор (Гортензия)", "American Flowers", "Plazoleta", "Tessa", "La Conejera", "Serrezuela Flowers"],
}

# Bothost's proxy sends traffic to the port set in the panel. We open all the usual ones,
# and we do it from inside the app, so it works no matter how Bothost launches it
# (python -m app.main  OR  uvicorn app.main:app).
EXTRA_PORTS = [8000, 3000, 8080, 5000]


FORWARDERS = {
    "Expolanka": ("Кения", "EXPOLANKA,Expolanka Freight",
                  "Кения → Амстердам. Один MAWB на несколько плантаций, счёт за фрахт в $ БЕЗ разбивки по кг — "
                  "разбивка кг по плантациям приходит отдельным документом"),
    "Floratrack": ("Эквадор", "FLORATRACK,Флоратрак,Floratrak",
                   "Логистика всего, кроме Кении: Эквадор, Колумбия и остальные"),
}


FARM_ALIASES = {"Agriflora": "SIAN FLOWERS-AGRIFLORA,SIAN FLOWERS -AGRIFLORA,Агрифлора,Агри",
                "Massai": "SIAN FLOWERS-MAASAI,Maasai,Массай,Масаи"}


def upsert_forwarders():
    with session() as s:
        for name, al in FARM_ALIASES.items():
            f = s.exec(select(Farm).where(Farm.name == name)).first()
            if f and al.split(",")[0] not in (f.aliases or ""):
                f.aliases = ",".join(x for x in [f.aliases, al] if x)
                f.notes = (f.notes + " " if f.notes else "") + "Инвойсы приходят от трейдера NextWave одним файлом вместе с другой плантацией."
                s.add(f)
        for name, (country, aliases, notes) in FORWARDERS.items():
            f = s.exec(select(Farm).where(Farm.name == name)).first() or Farm(name=name, country=country)
            f.is_forwarder, f.aliases, f.notes = True, aliases, notes
            s.add(f)
        s.commit()


def seed():
    _seed_farms()
    upsert_forwarders()          # forwarders + aliases for existing farms (idempotent)


def _seed_farms():
    with session() as s:
        if s.exec(select(Farm).where(Farm.is_forwarder == False)).first():  # noqa: E712
            return
        for country, names in SEED.items():
            for n in names:
                s.add(Farm(name=n, country=country, aliases=n.upper()))
        s.commit()


def _main_port() -> int:
    """Port the main server binds itself (so we don't grab it first)."""
    argv = sys.argv
    if "--port" in argv:
        return int(argv[argv.index("--port") + 1])
    return int(os.getenv("PORT", "8000"))


def _port_free(p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", p))
            return True
        except OSError:
            return False


@asynccontextmanager
async def lifespan(app):
    from .bot import bot, dp, setup_menu_button
    from . import backup

    # 1) container may be fresh after a rebuild -> pull data back from the Telegram channel
    try:
        print(f"[lumen] restore: {await backup.restore_if_empty(bot)}", flush=True)
    except Exception as e:
        print(f"[lumen] restore failed: {e}", flush=True)

    init_db()
    seed()

    tasks = []
    opened = []
    main = _main_port()
    for p in EXTRA_PORTS:
        if p != main and _port_free(p):
            srv = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, lifespan="off", log_level="info"))
            srv.install_signal_handlers = lambda: None
            tasks.append(asyncio.create_task(srv.serve()))
            opened.append(p)
    print(f"[lumen] основной порт {main}, доп. порты: {opened}", flush=True)

    if bot:
        api.BOT = bot
        try:
            await setup_menu_button()
        except Exception as e:
            print(f"[lumen] menu button: {e}", flush=True)
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False,
                                                           allowed_updates=dp.resolve_used_update_types())))
        tasks.append(asyncio.create_task(backup.backup_loop(bot)))
    yield
    for t in tasks:
        t.cancel()
    if bot:
        try:
            await asyncio.wait_for(backup.flush(bot), timeout=20)
        except Exception as e:
            print(f"[lumen] backup on shutdown failed: {e}", flush=True)


app = FastAPI(lifespan=lifespan)
app.include_router(api.router)


@app.middleware("http")
async def _changes_trigger_backup(request, call_next):
    resp = await call_next(request)
    if request.method != "GET" and request.url.path.startswith("/api") and resp.status_code < 400:
        from .backup import mark_dirty
        mark_dirty()
    return resp


@app.get("/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "webapp", html=True), name="webapp")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
