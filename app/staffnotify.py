"""
«Уведомления по грузам» — the TK MSK (Flora Truck) messages, rewritten for our staff / warehouse chat.

- «Уважаемый клиент» -> «Дорогой клиент»
- «С уважением, FLORA TRUCK» -> «С уважением, Lumen Flora»
- truck numbers and AWB numbers: bold + underlined
- airport codes -> countries: NBO -> 🇰🇪 Кения, BOG -> 🇨🇴 Колумбия, UIO -> 🇪🇨 Эквадор
Posted by the neutral reader bot into the chats registered with /notify_here.
"""
import html
import json
import re

CODES = {"NBO": "🇰🇪 Кения", "BOG": "🇨🇴 Колумбия", "MDE": "🇨🇴 Колумбия", "UIO": "🇪🇨 Эквадор", "GYE": "🇪🇨 Эквадор"}
AWB_RE = r"\d{3}[\s\-]?\d{4}\s?\d{4}"
TRUCK_RE = r"[A-ZА-Я]{2,4}\d{2,4}/[A-ZА-Я0-9]{2,8}"


def rewrite(text: str) -> str:
    """Raw TK MSK message -> HTML for the staff chat."""
    t = (text or "").replace("**", "").replace("*", "")
    t = re.sub(r"Уважаемый клиент", "Дорогой клиент", t, flags=re.I)
    t = re.sub(r"^\s*Здравствуйте[.!]?", "Дорогой клиент!", t, flags=re.I)
    t = re.sub(r"С\s+уважением,?\s*FLORA\s*TRUCK[!.]*", "@@SIGN@@", t, flags=re.I)
    t = re.sub(r"\s+@@SIGN@@", "\n\n@@SIGN@@", t)
    t = re.sub(r"\s+([.,])", r"\1", t)          # «… NHS159/GY974 .» after removing the asterisks
    t = html.escape(t)
    t = re.sub(r"\(\s*(NBO|BOG|MDE|UIO|GYE)\s*\)", lambda m: f"({CODES[m.group(1)]})", t)
    t = re.sub(r"\b(NBO|BOG|MDE|UIO|GYE)\b(?=\s+AWB)", lambda m: CODES[m.group(1)], t)
    t = re.sub(TRUCK_RE, lambda m: f"<b><u>{m.group(0)}</u></b>", t)
    t = re.sub(AWB_RE, lambda m: f"<b><u>{m.group(0)}</u></b>", t)
    if "@@SIGN@@" not in t:
        t = t.rstrip() + "\n\n@@SIGN@@"
    return t.replace("@@SIGN@@", "С уважением, Lumen Flora").strip()


def targets() -> list[dict]:
    from .api import _settings
    ts = _settings().get("notify_targets") or []
    with_topic = {x["chat_id"] for x in ts if x.get("thread_id")}
    return [x for x in ts if x.get("thread_id") or x["chat_id"] not in with_topic]


def set_targets(ts: list[dict]):
    from .api import SETTINGS, _settings
    SETTINGS.write_text(json.dumps({**_settings(), "notify_targets": ts}, ensure_ascii=False))
    from .backup import mark_dirty
    mark_dirty()


def add(chat_id: int, thread_id, title: str):
    ts = [x for x in targets() if not (x["chat_id"] == chat_id and x.get("thread_id") == thread_id)]
    set_targets(ts + [{"chat_id": chat_id, "thread_id": thread_id, "title": title}])


def remove(chat_id: int, thread_id=None):
    set_targets([x for x in targets() if not (x["chat_id"] == chat_id and x.get("thread_id") == thread_id)])


async def post(sender, text: str):
    if not sender:
        return
    body = rewrite(text)
    for t in targets():
        try:
            await sender.send_message(t["chat_id"], body, parse_mode="HTML", message_thread_id=t.get("thread_id"))
        except Exception as e:
            print(f"[lumen] staff notify {t.get('title')}: {e}", flush=True)
