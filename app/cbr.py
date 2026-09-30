"""Official Central Bank of Russia USD rate for today (cached per day).

Used for Floratrack before payment: ₽ = $ × (ЦБ сегодня + 3) / 0.96.
Source: cbr.ru daily XML; mirror cbr-xml-daily.ru as a fallback.
"""
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import DATA_DIR

CACHE = DATA_DIR / "cbr.json"
URLS = ("https://www.cbr.ru/scripts/XML_daily.asp", "https://www.cbr-xml-daily.ru/daily_json.js")


def _msk_today() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")


def parse_xml(text: str) -> float | None:
    """<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal>...<Value>81,1234</Value>"""
    m = re.search(r"<CharCode>USD</CharCode>.*?<Nominal>(\d+)</Nominal>.*?<Value>([\d,\.]+)</Value>", text, re.S)
    if not m:
        return None
    return float(m.group(2).replace(",", ".")) / int(m.group(1))


def parse_json(text: str) -> float | None:
    try:
        v = json.loads(text)["Valute"]["USD"]
        return float(v["Value"]) / float(v.get("Nominal", 1))
    except (ValueError, KeyError, TypeError):
        return None


def usd_today() -> tuple[float | None, str]:
    """(rate, source note). None if both sources are down and nothing cached for today."""
    today = _msk_today()
    try:
        cache = json.loads(CACHE.read_text())
    except (FileNotFoundError, ValueError):
        cache = {}
    if cache.get("date") == today and cache.get("usd"):
        return cache["usd"], f"ЦБ на {today}"
    for url in URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 lumen-uchet"})
            with urllib.request.urlopen(req, timeout=6) as r:
                raw = r.read()
            text = raw.decode("windows-1251" if url.endswith(".asp") else "utf-8", errors="replace")
            rate = parse_xml(text) if url.endswith(".asp") else parse_json(text)
            if rate:
                CACHE.write_text(json.dumps({"date": today, "usd": rate}))
                return rate, f"ЦБ на {today}"
        except Exception as e:
            print(f"[lumen] cbr {url}: {e}", flush=True)
    if cache.get("usd"):
        return cache["usd"], f"ЦБ на {cache.get('date')} (сайт ЦБ сейчас недоступен)"
    return None, "сайт ЦБ недоступен"


def floratrack_rate() -> tuple[float | None, str]:
    """Provisional Floratrack ₽ per $: (ЦБ today + 3) / 0.96."""
    cb, src = usd_today()
    if not cb:
        return None, src
    return (cb + 3) / 0.96, f"({src} {cb:.4f} + 3) / 0.96"
