import asyncio
import json
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
    "Эквадор": ["Tessa", "Nintanga", "Agroterranorte", "Josar Flor", "Tikan", "EC Blooms (Starroses)", "Sand Flowers",
                "Allegro Farms", "Dayka", "Guaisa (Sunrite)", "Meral Flowers", "Monterosas", "Rosaprima", "Rosas Del Viento"],
    "Колумбия": ["Кондор (Гортензия)", "American Flowers", "Plazoleta", "La Conejera", "Serrezuela Flowers"],
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


FARM_ALIASES = {"Rosas Del Viento": "TIPANLUIZA LANCHIMBA JUAN MIGUEL,TIPANLUIZA,ROSAS DEL VENTO,Rosas del Viento",
                "Кондор (Гортензия)": "CONDOR ANDINO,CÓNDOR ANDINO,CONDOR ANDINO S.A.S,CÓNDOR ANDINO S.A.S,CONDOR",
                "Plazoleta": "PLAZOLETA BAZZANI,PLAZOLETA BAZZANI S.A.S",
                "Nintanga": "NINTANGA S.A.",
                "PjDave": "PJ FLORA,PJ FLOWERS,PJ",
                "Agriflora": "SIAN FLOWERS-AGRIFLORA,SIAN FLOWERS -AGRIFLORA,Агрифлора,Агри",
                "Massai": "SIAN FLOWERS-MAASAI,Maasai,Массай,Масаи"}


BOX_RULES = {"Zeeflora": {"Spray Rose Reflex Bicolour 60cm": 25, "Spray Rose Fire Works Bi-Pink 60cm": 25}}


BROKER = {"Tessa": ("Эквадор", "POSITANO,Позитано"), "Plazoleta": ("Колумбия", "")}


def upsert_forwarders():
    with session() as s:
        from .models import Invoice
        old = s.exec(select(Farm).where(Farm.name == "Rosas Del Vento")).first()   # old misspelling -> Rosas Del Viento
        if old:
            if s.exec(select(Farm).where(Farm.name == "Rosas Del Viento")).first():
                s.delete(old)
            else:
                old.name = "Rosas Del Viento"; s.add(old)
            for inv in s.exec(select(Invoice)).all():
                if inv.farm == "Rosas Del Vento":
                    inv.farm = "Rosas Del Viento"; s.add(inv)
            s.commit()
        for name, (country, aliases) in BROKER.items():   # bought through a broker: 3 % in, 7 % on the invoice
            f = s.exec(select(Farm).where(Farm.name == name)).first() or Farm(name=name, country=country)
            f.country = country
            if aliases and "POSITANO" not in (f.aliases or ""):
                f.aliases = ",".join(x for x in [f.aliases, aliases] if x)
            if not f.in_fee_pct and not f.markup_pct:
                f.in_fee_pct, f.markup_pct, f.account = 3, 7, "Брокер"
                f.notes = ((f.notes or "") + " Закупка через брокера: на биржу заходит 97% долларов (3% входящая комиссия), "
                           "к инвойсу фермы +7% комиссии брокера.").strip()
            s.add(f)
        from .models import Invoice
        for inv in s.exec(select(Invoice)).all():           # Positano is the old name of Tessa
            if (inv.farm or "").strip().lower() == "positano":
                inv.farm = "Tessa"; s.add(inv)
        for name, rules in BOX_RULES.items():
            f = s.exec(select(Farm).where(Farm.name == name)).first()
            if f and (f.box_kg_json or "{}") == "{}":
                f.box_kg_json = json.dumps(rules, ensure_ascii=False)
                s.add(f)
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
    _seed_box_dims()


SEED_CREATED = {"Bliss Flora", "Blooming Dale", "Dale Flora", "Equator", "Flora Delight", "Flora Ola",
                "Molo River", "Pamoja", "Valentine", "Pj Flora", "Pj Flowers"}


def _seed_box_dims():
    """Box sizes Expolanka reported (WhatsApp, 09.2025–09.2026) -> registry, once.
    While the registry is OFF: undo an earlier seed (clear sizes, drop farms it created that have no invoices)."""
    from .api import _settings, SETTINGS, learn_dims
    from .volumetric import REGISTRY_ENABLED, SEED_TEXT, parse_message
    from .models import Invoice
    st = _settings()
    if not REGISTRY_ENABLED:
        if st.get("box_dims_seeded"):
            with session() as s:
                used = {i.farm.lower() for i in s.exec(select(Invoice)).all()}
                for f in s.exec(select(Farm)).all():
                    if f.name in SEED_CREATED and f.name.lower() not in used:
                        s.delete(f)
                    elif (f.box_dims_json or "{}") != "{}":
                        f.box_dims_json = "{}"
                        s.add(f)
                s.commit()
            SETTINGS.write_text(json.dumps({**_settings(), "box_dims_seeded": False}))
        return
    if st.get("box_dims_seeded"):
        return
    learn_dims(parse_message(SEED_TEXT))
    SETTINGS.write_text(json.dumps({**_settings(), "box_dims_seeded": True}))


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
    from . import roles
    roles.seed()

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
        from .bot import scheduler_loop
        tasks.append(asyncio.create_task(scheduler_loop()))
        tasks.append(asyncio.create_task(_once_recalc_true_rate()))
        from . import reader
        if not reader.enabled():
            print("[lumen] бот-читатель ВЫКЛЮЧЕН: в .env нет READER_BOT_TOKEN", flush=True)
        if reader.enabled():                           # neutral second bot in the TK MSK chat
            rbot, rdp = reader.build()
            try:
                me = await rbot.get_me()
                print(f"[lumen] бот-читатель @{me.username} запущен (для чата ТК МСК)", flush=True)
            except Exception as e:
                print(f"[lumen] бот-читатель: неверный READER_BOT_TOKEN? {e}", flush=True)
            tasks.append(asyncio.create_task(rdp.start_polling(rbot, handle_signals=False,
                                                               allowed_updates=["message", "channel_post", "my_chat_member"])))
        from . import userbot
        if userbot.enabled():                          # invisible reader of the TK MSK chat
            from .bot import _handle_truck
            tasks.append(asyncio.create_task(userbot.run_forever(lambda text, sent: _handle_truck("", text, sent))))
    yield
    for t in tasks:
        t.cancel()
    if bot:
        try:
            await asyncio.wait_for(backup.flush(bot), timeout=20)
        except Exception as e:
            print(f"[lumen] backup on shutdown failed: {e}", flush=True)


async def _once_recalc_true_rate():
    """After switching to «истинный курс»: rebuild every top-up sheet once and send it to the system admins."""
    from .api import SETTINGS, _settings, export_topups
    from .models import TopUp
    from . import roles
    if _settings().get("true_rate_v2"):
        return
    await asyncio.sleep(10)
    with session() as s:
        ids = [t.id for t in s.exec(select(TopUp).order_by(TopUp.id)).all()]
    if not ids:
        return
    sent = False
    for uid in roles.sys_ids():
        try:
            await export_topups(ids, uid, "📊 Пересчёт по истинному курсу: все ₽ за инвойс (комиссия, налог, сборы) "
                                          "теперь в цене стебля. В строке ИТОГО у каждой фермы — истинный курс и косты, %.")
            sent = True
        except Exception as e:
            print(f"[lumen] recalc export to {uid} failed: {e}", flush=True)
    print(f"[lumen] пересчёт по истинному курсу: {'отправлен' if sent else 'НЕ отправлен'} ({len(ids)} пополнений)", flush=True)
    if sent:
        SETTINGS.write_text(json.dumps({**_settings(), "true_rate_v2": True}))


app = FastAPI(lifespan=lifespan)
app.include_router(api.router)


@app.middleware("http")
async def _changes_trigger_backup(request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        # Telegram's webview caches the page hard — always serve the fresh Mini App
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
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
