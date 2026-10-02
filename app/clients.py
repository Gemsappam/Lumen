"""
Client chats: shipment statuses (from the TK MSK chat) go to the chat of every marking present in the shipment.

- A marking (LUMEN, ABC …) has one or more client chats: in the client's group type  /marking_here ABC
  (the neutral reader bot must be in that group; only staff with write rights can do it).
- An invoice carries its markings in «Маркировка» (several: «LUMEN, ABC»). In «В пути» you can set them
  for a whole MAWB at once.
- Each client gets a short clean text about HIS goods only: no MAWB, no truck numbers, no box counts,
  no carrier names, no warehouse, no prices — just «Ваш груз из Колумбии …».
- Old messages (forwarded history) are not sent to clients — only fresh events (< 12 h).
"""
import json
from datetime import datetime, timedelta

from sqlmodel import select

from .models import Invoice, session

FRESH = timedelta(hours=12)


def _norm(m: str) -> str:
    return (m or "").strip().upper()


def markings_of(text: str) -> list[str]:
    return [_norm(x) for x in (text or "").replace(";", ",").split(",") if _norm(x)]


def registry() -> dict:
    from .api import _settings
    return _settings().get("markings") or {}


def save_registry(reg: dict):
    from .api import SETTINGS, _settings
    SETTINGS.write_text(json.dumps({**_settings(), "markings": reg}, ensure_ascii=False))
    from .backup import mark_dirty
    mark_dirty()


def add_chat(marking: str, chat_id: int, thread_id, title: str):
    reg = registry()
    m = _norm(marking)
    chats = [c for c in reg.get(m, []) if not (c["chat_id"] == chat_id and c.get("thread_id") == thread_id)]
    reg[m] = chats + [{"chat_id": chat_id, "thread_id": thread_id, "title": title}]
    save_registry(reg)


def remove_chat(chat_id: int, thread_id=None) -> list[str]:
    reg, gone = registry(), []
    for m, chats in reg.items():
        keep = [c for c in chats if not (c["chat_id"] == chat_id and c.get("thread_id") == thread_id)]
        if len(keep) != len(chats):
            gone.append(m)
        reg[m] = keep
    save_registry(reg)
    return gone


FROM = {"Кения": "из 🇰🇪 Кении", "Эквадор": "из 🇪🇨 Эквадора", "Колумбия": "из 🇨🇴 Колумбии"}


def _awb_info() -> dict:
    """{awb digits: {"marks": set, "countries": set}} for invoices still in transit."""
    from .calc import norm_awb
    out = {}
    with session() as s:
        for i in s.exec(select(Invoice)).all():
            if i.awb and not i.arrived_at:
                d = out.setdefault(norm_awb(i.awb), {"marks": set(), "countries": set()})
                d["marks"].update(markings_of(i.client_code))
                if i.country:
                    d["countries"].add(i.country)
    return out


def _truck_awbs(truck: str) -> list[str]:
    with session() as s:
        return sorted({i.awb for i in s.exec(select(Invoice)).all() if i.truck == truck and i.awb and not i.arrived_at})


def _what(countries: set) -> str:
    """'Ваш груз из Колумбии' / 'Ваши грузы из Колумбии и Эквадора' — no AWB, truck or box counts."""
    c = [FROM.get(x, "") for x in sorted(countries) if FROM.get(x)]
    many = len(c) > 1
    place = (" " + " и ".join([c[0]] + [x.replace("из ", "") for x in c[1:]])) if c else ""
    return ("Ваши грузы" if many else "Ваш груз") + place, many


def messages_for(ev: dict) -> dict:
    """{marking: text} for one TK MSK event — clients never see MAWB, truck numbers or box counts."""
    from .calc import norm_awb
    info = _awb_info()
    kind, tr = ev.get("kind"), ev.get("truck") or ""
    awbs = ev.get("awbs") or (_truck_awbs(tr) if kind == "border" else [])
    per = {}
    for a in awbs:
        d = info.get(norm_awb(a))
        if not d:
            continue
        for m in d["marks"]:
            per.setdefault(m, set()).update(d["countries"])
    out = {}
    for m, countries in per.items():
        who, many = _what(countries)
        if kind == "loaded":
            out[m] = f"🚚 {who} {'забраны и загружены' if many else 'забран и загружен'} в машину."
        elif kind == "border":
            out[m] = f"🛃 {who} {'прошли' if many else 'прошёл'} границу, идёт таможенное оформление."
        elif kind == "eta":
            when = ev.get("window") or ""
            out[m] = f"📦 {who} {'едут' if many else 'едет'} на склад." + (f" Ориентировочное прибытие: {when}." if when else "")
    return out


def arrived_messages(done: list[dict]) -> dict:
    """Invoices just closed as arrived -> {marking: text}."""
    with session() as s:
        invs = s.exec(select(Invoice)).all()
    per = {}
    for d in done:
        for i in invs:
            if i.awb == d["awb"]:
                for m in markings_of(i.client_code):
                    per.setdefault(m, set()).update({i.country} if i.country else set())
    out = {}
    for m, countries in per.items():
        who, many = _what(countries)
        out[m] = f"✅ {who} {'прибыли' if many else 'прибыл'} на склад."
    return out


async def send(sender, per_marking: dict):
    """Post texts to every chat of each marking (through the neutral reader bot)."""
    if not sender:
        return
    reg = registry()
    for m, text in per_marking.items():
        for c in reg.get(m, []):
            try:
                await sender.send_message(c["chat_id"], text, message_thread_id=c.get("thread_id"))
            except Exception as e:
                print(f"[lumen] client chat {m} / {c.get('title')}: {e}", flush=True)


def is_fresh(sent_msk: datetime, now_msk: datetime) -> bool:
    return now_msk - sent_msk <= FRESH
