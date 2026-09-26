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


def seed():
    with session() as s:
        if s.exec(select(Farm)).first():
            return
        for country, names in SEED.items():
            for n in names:
                s.add(Farm(name=n, country=country, aliases=n.upper()))
        s.add(Farm(name="Expolanka", country="Кения", is_forwarder=True, aliases="EXPOLANKA",
                   notes="Консолидатор Кении: один AWB на несколько плантаций, счёт за фрахт в $ + разбивка кг по плантациям"))
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
    init_db()
    seed()
    tasks = []
    opened = []
    main = _main_port()
    for p in EXTRA_PORTS:
        if p != main and _port_free(p):   # the port the main server already holds is skipped
            srv = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, lifespan="off", log_level="info"))
            srv.install_signal_handlers = lambda: None
            tasks.append(asyncio.create_task(srv.serve()))
            opened.append(p)
    print(f"[lumen] основной порт {main}, доп. порты: {opened}", flush=True)

    from .bot import bot, dp, setup_menu_button
    if bot:
        api.BOT = bot
        try:
            await setup_menu_button()
        except Exception as e:
            print(f"[lumen] menu button: {e}", flush=True)
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)
app.include_router(api.router)


@app.get("/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "webapp", html=True), name="webapp")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
