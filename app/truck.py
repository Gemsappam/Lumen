"""
FLORA TRUCK notifications -> «в пути» / «прибыл».

Messages we understand (from the FloraMailing chat):
  1) «Ваш импортный товар забран: Номер AWB: *065-40539435* … кол-во. мест: 17., загружен в машину *NCD379/1UY3673*»
     -> goods of that MAWB are on truck X (still в пути)
  2) «Машина NCD379/1UY3673 прошла границу …»                     -> info only
  3) «Машина NTN100/BY100 … едет на склад Химки, предварительное время прибытия на склад к 06:00 …
      • ( DILUNA ) → 34 BOG AWB 543-18688902»
     -> arrival = their time + 1 hour (for a range «17:00-18:00» the later time + 1 hour)
"""
import re
from datetime import datetime, timedelta

AWB = r"([0-9]{3}[\s\-]?[0-9]{4}\s?[0-9]{4})"


def _clean(text: str) -> str:
    return (text or "").replace("*", "").replace("\u00a0", " ")


def parse(text: str, sent_msk: datetime) -> dict | None:
    """-> {"kind": "eta"|"loaded"|"border", "truck", "awbs": [...], "arrive": datetime|None, "boxes": {awb: n}}"""
    t = _clean(text)
    truck = re.search(r"машин[ауе]\s+([A-ZА-Я0-9]{2,}[A-ZА-Я0-9/\-]*)", t, re.I)
    truck = truck.group(1) if truck else None
    if re.search(r"товар\s+забран", t, re.I):
        awbs = re.findall(r"AWB:?\s*" + AWB, t, re.I)
        boxes = re.search(r"мест:?\s*(\d+)", t)
        return {"kind": "loaded", "truck": truck, "awbs": awbs, "arrive": None,
                "boxes": {a: int(boxes.group(1)) for a in awbs} if boxes else {}}
    if re.search(r"едет на склад|время прибытия", t, re.I):
        awbs = re.findall(r"AWB\s*" + AWB, t, re.I)
        rng = re.search(r"прибыти[яе][^0-9]{0,40}?(\d{1,2})[:.](\d{2})(?:\s*[-–]\s*(\d{1,2})[:.](\d{2}))?", t, re.I)
        if not rng:
            return {"kind": "eta", "truck": truck, "awbs": awbs, "arrive": None, "boxes": {}}
        hh, mm = (rng.group(3), rng.group(4)) if rng.group(3) else (rng.group(1), rng.group(2))   # later end of a range
        eta = sent_msk.replace(hour=int(hh) % 24, minute=int(mm), second=0, microsecond=0)
        if eta < sent_msk - timedelta(hours=2):
            eta += timedelta(days=1)            # «к 06:00» written in the evening = next morning
        boxes = {a: int(n) for n, a in re.findall(r"(\d+)\s+[A-Z]{3}\s+AWB\s*" + AWB, t)}
        return {"kind": "eta", "truck": truck, "awbs": awbs, "arrive": eta + timedelta(hours=1), "boxes": boxes}
    if re.search(r"прошла границу", t, re.I):
        return {"kind": "border", "truck": truck, "awbs": [], "arrive": None, "boxes": {}}
    return None


def apply(ev: dict, now_msk: datetime) -> str:
    """Write the event into the books and return a one-line summary (no carrier names)."""
    from .api import arrive_due, set_eta, set_truck
    if not ev:
        return ""
    tr = ev.get("truck") or "?"
    if ev["kind"] == "border":
        return f"🛃 Машина {tr} прошла границу, на таможне"
    if ev["kind"] == "loaded":
        out = []
        for a in ev["awbs"]:
            farms = set_truck(a, tr)
            out.append(f"🚚 MAWB {a} загружен в машину {tr}" + (f" ({ev['boxes'].get(a)} мест)" if ev["boxes"].get(a) else "")
                       + (f": {', '.join(farms)}" if farms else " — наших инвойсов с ним нет"))
        return "\n".join(out)
    if ev["kind"] == "eta":
        if not ev["arrive"]:
            return f"Машина {tr}: не нашёл время прибытия"
        iso = ev["arrive"].strftime("%Y-%m-%dT%H:%M")
        matched, unknown = set_eta(ev["awbs"], iso)
        for a in ev["awbs"]:
            set_truck(a, tr)
        done = arrive_due(now_msk.strftime("%Y-%m-%dT%H:%M"))     # old message -> already arrived
        when = ev["arrive"].strftime("%d.%m %H:%M")
        head = f"📦 Машина {tr}: {'прибыла' if ev['arrive'] <= now_msk else 'прибудет'} {when} МСК (их время +1 ч)"
        lines = [f"• MAWB {a}: {', '.join(f)}" for a, f in matched]
        if unknown:
            lines.append("• нет наших инвойсов: " + ", ".join(unknown))
        if done:
            lines.append("✅ Закрыто как «прибыл»: " + ", ".join(f"{d['farm']} ({d['awb']})" for d in done))
        return head + ("\n" + "\n".join(lines) if lines else "")
    return ""
