"""«Looks like X» for Excel files — used ONLY to fail loudly: when a file resembles a known format (Expolanka list,
Logiztik weight report, prealert, Floratrack, BiFlorica) but the strict reader didn't take it, the bot says so instead
of quietly handing it to the AI. That's how a renamed column (AWB -> MAWB) gets noticed in minutes, not days."""
import io

KINDS = [
    ("expolanka", "консолидационный лист Expolanka (Кения)", lambda t: "shipper full name" in t or ("consignee full name" in t and "packs" in t)),
    ("weight", "отчёт о весе Logiztik (Weight & Dimensions)", lambda t: "weight & dimensions" in t or ("exporter" in t and "volume weight" in t)),
    ("prealert", "преалерт Эквадор/Колумбия", lambda t: "awb/bl" in t or "prealerta" in t),
    ("floratrack", "отчёт Floratrack", lambda t: "импорт амс-мск" in t or "эквадор консолидация" in t or "floratrack" in t
                                           or ("переводы рубли" in t and "баланс хранения" in t)),
    ("biflorica", "выписка брокера BiFlorica", lambda t: "biflorica" in t or ("операция" in t and "списание" in t)),
]


def text_of(data: bytes, rows: int = 50) -> str:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = ["sheets: " + " / ".join(wb.sheetnames)]
        for ws in wb.worksheets[:6]:
            out.append(ws.title)
            for r in ws.iter_rows(max_row=rows, values_only=True):
                out.extend(str(v) for v in r if v not in (None, ""))
        return " | ".join(out).lower()
    except Exception:
        return ""


def guess(data: bytes):
    """-> (kind, human label) or None"""
    t = text_of(data)
    for kind, label, test in KINDS:
        if t and test(t):
            return kind, label
    return None


def loud(label: str, reason: str, fname: str = "") -> str:
    return (f"⛔ Похоже на {label}{f' («{fname}»)' if fname else ''}, но я не смог его прочитать: {reason}.\n"
            "Скорее всего, поменялся формат файла. Ничего не записал. Перешли этот файл разработчику — "
            "починит за минуты. Если срочно — пришли через /ai с пояснением, что это.")
