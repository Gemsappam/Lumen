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

# The docs show {CUSTOMERCODE}/{DATE} in the path but don't say where the key goes, and the live server answered
# «ErrorParametrosFaltantesFechaCodigo» to the path form. So: try path / query / headers × key placements once,
# remember the combination that gets past «missing parameters» and «unauthorized».
AUTH_MODES = ["AuthenticationToken", "query", "Api-Key", "ApiKey", "Authorization", "Bearer"]
PARAM_MODES = ["path", "header", "query", "path+header", "path+query"]


def enabled() -> bool:
    return bool(TOKEN and CUSTOMER)


def _modes():
    from .api import _settings
    st = _settings()
    return st.get("logiztik_auth2"), st.get("logiztik_params"), st.get("logiztik_datefmt") or "%Y-%m-%d"


def _save_modes(a: str, p: str, dfmt: str):
    from .api import _settings, SETTINGS
    SETTINGS.write_text(json.dumps({**_settings(), "logiztik_auth2": a, "logiztik_params": p,
                                    "logiztik_datefmt": dfmt}, ensure_ascii=False))


def _build(endpoint: str, params: list, auth: str, pmode: str):
    """params = [(name, value), …] in path order."""
    from urllib.parse import quote, urlencode
    url = f"{BASE}/{endpoint}"
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    q = {}
    if "path" in pmode:
        url += "".join("/" + quote(str(v), safe="") for _n, v in params)
    if "header" in pmode:
        for n, v in params:
            h[n] = str(v)
    if "query" in pmode:
        q.update({n: str(v) for n, v in params})
    if auth == "Bearer":
        h["Authorization"] = f"Bearer {TOKEN}"
    elif auth == "query":
        q["authenticationToken"] = TOKEN
    else:
        h[auth] = TOKEN
    if q:
        url += "?" + urlencode(q)
    return url, h


def _kind(data, status) -> str:
    """ok / auth / missing / datefmt / other"""
    if isinstance(data, list):
        return "ok"
    txt = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    low = txt.lower()
    if status == 401 or "unauthorized" in low or "autoriz" in low:
        return "auth"
    if "faltante" in low or "missing" in low:
        return "missing"
    if "format" in low and ("date" in low or "fecha" in low):
        return "datefmt"
    if isinstance(data, dict) and (data.get("error") or data.get("mensaje")):
        return "other"
    return "ok"


async def _fetch(http, url, h):
    try:
        async with http.get(url, headers=h, ssl=(ssl.create_default_context() if url.startswith('https') else None)) as r:
            txt, status = await r.text(), r.status
    except Exception as e:
        raise RuntimeError(f"нет связи с Logiztik ({type(e).__name__}: {str(e)[:150]}) — "
                           "возможно, хостинг не пускает на порт 5005 или нужен IP в белом списке")
    try:
        return json.loads(txt), status
    except ValueError:
        return txt.strip().strip('"'), status


async def _call(endpoint: str, params: list, date_param: int | None = None):
    """-> list. date_param = index of the date in params (its format may need to change)."""
    a0, p0, dfmt = _modes()
    combos = ([(a0, p0)] if a0 and p0 else []) + [(a, p) for p in PARAM_MODES for a in AUTH_MODES if (a, p) != (a0, p0)]
    tried, seen = [], {}
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for fmt in ([dfmt] + ([f for f in ("%Y/%m/%d", "%Y-%m-%d") if f != dfmt] if date_param is not None else [])):
            ps = [(n, (v.strftime(fmt) if (k == date_param and hasattr(v, "strftime")) else v))
                  for k, (n, v) in enumerate(params)]
            for a, p in combos:
                url, h = _build(endpoint, ps, a, p)
                data, status = await _fetch(http, url, h)
                k = _kind(data, status)
                seen[k] = data
                tried.append(f"{p}/{a}:{k}")
                if k in ("auth", "missing"):
                    continue
                if k == "datefmt":
                    break
                if (a, p, fmt) != (a0, p0, dfmt):
                    _save_modes(a, p, fmt)
                if k == "other":
                    raise RuntimeError(f"Logiztik: {data.get('error') or data.get('mensaje')}")
                return data if isinstance(data, list) else []
    msg = seen.get("missing") or seen.get("auth") or seen.get("datefmt") or "нет ответа"
    raise RuntimeError(f"Logiztik не принял запрос ни в одном варианте ({msg}). Проверено: {len(tried)} вариантов. "
                       "Спроси у них пример запроса (curl) — куда класть токен, код клиента и дату.")


async def shipments(day: date) -> list[dict]:
    return await _call("ClientesExternosA/ListarTotalesGuiasPorClienteV2",
                       [("CustomerCode", CUSTOMER), ("Date", day)], date_param=1)


async def barcodes(awb: str) -> list[dict]:
    nr = "".join(ch for ch in awb if ch.isdigit()) or awb
    return await _call("v2/ClientesExternosA/ListarCodigosDeBarraPorClienteNew",
                       [("CustomerCode", CUSTOMER), ("ShipmentNr", nr)])


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
