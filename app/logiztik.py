"""Logiztik Alliance (Ecuador / Colombia consolidator) REST API.

LOGIZTIK_TOKEN    — Alliance Cloud API key (Bothost env, never in chat)
LOGIZTIK_CUSTOMER — our customer code at Logiztik

shipments(date)  -> AWBs of that shipment date
barcodes(awb)    -> every box: barcode, HAWB, exporter (farm), gross / volume kg, box size, status
farm_rows(boxes) -> consolidation rows per farm: boxes, chargeable kg, HAWB
"""
import json
import os
import ssl
from collections import OrderedDict
from datetime import date, timedelta

import aiohttp

TOKEN = os.getenv("LOGIZTIK_TOKEN", "").strip()
CUSTOMER = os.getenv("LOGIZTIK_CUSTOMER", "").strip()
BASE = os.getenv("LOGIZTIK_BASE", "https://cloud.logiztikalliance.com:5005/logCloudWS/api").rstrip("/")

# the docs don't say where the key goes — try the usual places once, remember what worked
AUTH_MODES = ["AuthenticationToken", "Api-Key", "ApiKey", "Authorization", "Bearer", "query"]


def enabled() -> bool:
    return bool(TOKEN and CUSTOMER)


def _mode() -> str | None:
    from .api import _settings
    return _settings().get("logiztik_auth")


def _save_mode(m: str):
    from .api import _settings, SETTINGS
    SETTINGS.write_text(json.dumps({**_settings(), "logiztik_auth": m}, ensure_ascii=False))


def _req(mode: str, url: str):
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if mode == "Bearer":
        h["Authorization"] = f"Bearer {TOKEN}"
    elif mode == "query":
        url += ("&" if "?" in url else "?") + f"authenticationToken={TOKEN}"
    else:
        h[mode] = TOKEN
    return url, h


async def _get(path: str):
    """-> parsed JSON. Raises RuntimeError with Logiztik's own message."""
    url0 = f"{BASE}/{path}"
    modes = [_mode()] if _mode() else AUTH_MODES
    last = ""
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for mode in modes:
            url, h = _req(mode, url0)
            try:
                async with http.get(url, headers=h, ssl=ssl.create_default_context()) as r:
                    txt = await r.text()
            except Exception as e:
                raise RuntimeError(f"нет связи с Logiztik ({type(e).__name__}: {str(e)[:150]}) — "
                                   "возможно, хостинг не пускает на порт 5005 или нужен IP в белом списке")
            try:
                data = json.loads(txt)
            except ValueError:
                data = txt.strip().strip('"')
            if data == "Unauthorized" or r.status == 401:
                last = "Unauthorized — токен не подошёл"
                continue
            if not _mode():
                _save_mode(mode)
            if isinstance(data, dict) and (data.get("error") or data.get("mensaje")):
                raise RuntimeError(f"Logiztik: {data.get('error') or data.get('mensaje')}")
            return data
    raise RuntimeError(last or "Logiztik не ответил")


async def shipments(day: date) -> list[dict]:
    d = await _get(f"ClientesExternosA/ListarTotalesGuiasPorClienteV2/{CUSTOMER}/{day:%Y-%m-%d}")
    return d if isinstance(d, list) else []


async def barcodes(awb: str) -> list[dict]:
    nr = "".join(ch for ch in awb if ch.isdigit()) or awb
    d = await _get(f"v2/ClientesExternosA/ListarCodigosDeBarraPorClienteNew/{CUSTOMER}/{nr}")
    return d if isinstance(d, list) else []


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def box_kg(b: dict) -> float:
    """Chargeable kg of one box: volume weight when the shipment is charged by volume, else gross."""
    g, v = _num(b.get("grossWeight")), _num(b.get("volumeWeight"))
    return v if b.get("shipmentMeasureVolume") and v else (g or v)


def farm_rows(boxes: list[dict]) -> list[dict]:
    rows = OrderedDict()
    for b in boxes:
        name = (b.get("exporterName") or b.get("exporterCode") or "?").strip()
        r = rows.setdefault(name, {"farm_raw": name, "packs": 0, "kg": 0.0, "hawb": None, "sizes": {},
                                   "received": 0, "price": 0.0})
        r["packs"] += 1
        r["kg"] += box_kg(b)
        r["hawb"] = r["hawb"] or (b.get("hawb") or "").strip() or None
        sz = (b.get("boxSize") or "").strip().upper()
        if sz:
            r["sizes"][sz] = r["sizes"].get(sz, 0) + 1
        if (b.get("status") or "").lower() == "received":
            r["received"] += 1
        r["price"] += _num(b.get("piecePrice"))
    for r in rows.values():
        r["kg"] = round(r["kg"], 2)
    return list(rows.values())


COUNTRY = {"UIO": "Эквадор", "GYE": "Эквадор", "BOG": "Колумбия", "MDE": "Колумбия"}


async def recent(days: int = 6) -> list[dict]:
    """AWBs shipped in the last `days` days: [{awb, date, origin, country, pieces}]"""
    out, seen = [], set()
    for n in range(days):
        day = date.today() - timedelta(days=n)
        for sh in await shipments(day):
            a = str(sh.get("awb") or "").strip()
            if not a or a in seen:
                continue
            seen.add(a)
            out.append({"awb": a, "date": str(sh.get("shipmentDate") or "")[:10], "origin": sh.get("origin") or "",
                        "country": COUNTRY.get((sh.get("origin") or "").upper(), "Эквадор"),
                        "pieces": sh.get("pieces")})
    return out
