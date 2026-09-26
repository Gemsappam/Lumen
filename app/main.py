import asyncio
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

# Bothost's proxy sends traffic to whatever port is set in the panel.
# We listen on all the usual ones so any of them works.
PORTS = [8000, 3000, 8080, 5000]


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


@asynccontextmanager
async def lifespan(app):
    init_db()
    seed()
    from .bot import bot, dp
    task = None
    if bot:
        api.BOT = bot
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.include_router(api.router)


@app.get("/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "webapp", html=True), name="webapp")


async def serve_all():
    servers = [uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0", port=p,
        lifespan="on" if i == 0 else "off",   # DB init + bot start only once
        log_level="info")) for i, p in enumerate(PORTS)]
    print("Слушаю порты:", PORTS, flush=True)
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(serve_all())
