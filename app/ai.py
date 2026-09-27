"""
Two AI jobs:
  parse_document() — any farm invoice / forwarder invoice / AWB (PDF or photo) -> structured draft
  audit_topup()    — reads the whole computed top-up + history and finds what's wrong

The AI never writes to the DB by itself: it fills a draft, the operator confirms in the Mini App.
"""
import base64
import json
import re
from anthropic import AsyncAnthropic

from .config import AI_PROVIDER, ANTHROPIC_API_KEY, AUDIT_MODEL, OPENROUTER_API_KEY, PARSE_MODEL, WEBAPP_URL

# Same Claude models either way. OpenRouter speaks Anthropic's Messages API at /api/v1/messages,
# so the official SDK works with just a different base_url + Bearer key.
if AI_PROVIDER == "openrouter":
    client = AsyncAnthropic(base_url="https://openrouter.ai/api", auth_token=OPENROUTER_API_KEY,
                            api_key=None,
                            default_headers={"HTTP-Referer": WEBAPP_URL or "https://t.me", "X-Title": "Lumen Uchet"}) \
        if OPENROUTER_API_KEY else None
else:
    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def model_id(name: str) -> str:
    """claude-opus-5-5 -> anthropic/claude-opus-5.5 on OpenRouter; unchanged for Anthropic direct."""
    if AI_PROVIDER != "openrouter" or "/" in name:
        return name
    name = re.sub(r"-\d{8}$", "", name)                 # drop date suffix (haiku-4-5-20251001)
    name = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", name)   # 5-5 -> 5.5
    return f"anthropic/{name}"


async def _structured(model: str, system: str, content, tool: dict, max_tokens: int) -> dict:
    """Get the tool's JSON back WITHOUT forced tool_choice (Opus 5.5 / Fable reject forcing).
    tool_choice=auto + explicit instruction, check, retry once, last resort: JSON from text."""
    last = ""
    for attempt in range(2):
        msg = await client.messages.create(
            model=model_id(model), max_tokens=max_tokens,
            system=system + f"\n\nОтветь ОДНИМ вызовом инструмента {tool['name']} — без текста вокруг.",
            tools=[tool], tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": content}],
        )
        for b in msg.content:                      # response may start with a thinking block
            if getattr(b, "type", "") == "tool_use" and b.name == tool["name"]:
                return b.input
        last = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", last, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
        if getattr(msg, "stop_reason", "") == "max_tokens":
            max_tokens *= 2
    raise RuntimeError(f"Модель не вернула данные. Ответ: {last[:300]}")

DOMAIN = """Ты — бухгалтер-логист оптовой компании по импорту срезанных цветов (Люмен).
Цветы приходят с плантаций Кении, Эквадора и Колумбии авиа (AWB) в Москву, дальше доставка.
Оплата плантациям идёт в $ через платёжного агента: мы делаем «пополнение» в рублях,
агент зачисляет $, курс пополнения = ₽/$. Всё, что оплачено из пополнения, пересчитывается по его курсу.

Нюансы, которые ты обязан учитывать:
- Кения: несколько плантаций летят одним AWB через консолидатора (Expolanka). Консолидатор
  выставляет отдельный инвойс за фрахт в $ и даёт разбивку кг по плантациям. Это НЕ цветы, это логистика (leg=air).
- Колумбия/Эквадор: логистика часто отдельным счётом карго-агента, иногда в ₽.
- Расходы на московской стороне (таможня, склад, доставка) — leg=msk.
- Коробки: FB=1, HB=0.5, QB=0.25, EB=0.125. Stems = bunches × stems per bunch. Длина (40cm/50cm/60cm)
  — часть номенклатуры, пиши её в название: "Rose Madam Red 40cm".
- Гортензии (Кондор, American Flowers): часто одна цена на всё, названия = цвета.
- В инвойсах бывает VAT/tax (Кения 16%), doc fees, упаковка. Это нормально и в себестоимость стебля
  НЕ идёт: цена строки — это цена стебля. Subtotal = сумма строк, total = с налогами/сборами.
- Суммы оплаты в $ и ₽ вносит оператор, они всегда верные. Не спорь с ними и не пересчитывай.
- Номера AWB бывают в форматах "065-4053 8245", "06540538245" — это одно и то же.
- Не выдумывай. Если поле не читается — null и warning."""

PARSE_TOOL = {
    "name": "submit_document",
    "description": "Structured content of one invoice / AWB / freight bill.",
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_type": {"type": "string", "enum": ["farm_invoice", "freight_invoice", "awb", "other"]},
            "farm": {"type": ["string", "null"], "description": "canonical farm name from the known list if it matches"},
            "country": {"type": ["string", "null"], "enum": ["Кения", "Эквадор", "Колумбия", None]},
            "invoice_no": {"type": ["string", "null"]},
            "invoice_date": {"type": ["string", "null"], "description": "DD.MM.YYYY"},
            "awb": {"type": ["string", "null"]},
            "subtotal_usd": {"type": ["number", "null"], "description": "sum of lines before tax/fees"},
            "invoice_total_usd": {"type": ["number", "null"], "description": "balance due incl. tax/fees"},
            "total_weight_kg": {"type": ["number", "null"]},
            "lines": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "boxes": {"type": ["number", "null"]},
                "stems": {"type": "number"}, "price_usd": {"type": "number"},
                "weight_kg": {"type": ["number", "null"]}, "line_total_usd": {"type": ["number", "null"]}},
                "required": ["name", "stems", "price_usd"]}},
            "freight": {"type": ["object", "null"], "description": "only for freight_invoice / awb", "properties": {
                "provider": {"type": ["string", "null"]}, "leg": {"type": "string", "enum": ["air", "msk"]},
                "total_usd": {"type": ["number", "null"]}, "total_rub": {"type": ["number", "null"]},
                "per_farm_kg": {"type": "array", "items": {"type": "object", "properties": {
                    "farm": {"type": "string"}, "kg": {"type": "number"}, "boxes": {"type": ["number", "null"]}}}}}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["doc_type", "lines", "warnings"],
    },
}

AUDIT_TOOL = {
    "name": "submit_audit",
    "description": "Problems found in the top-up.",
    "input_schema": {"type": "object", "properties": {
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["error", "warning", "info"]},
            "where": {"type": "string"}, "problem": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["severity", "where", "problem", "fix"]}}},
        "required": ["summary", "issues"]},
}


def _block(data: bytes, mime: str):
    b64 = base64.standard_b64encode(data).decode()
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}


async def parse_document(data: bytes, mime: str, farms: list[dict], catalog: list[str]) -> dict:
    if not client:
        raise RuntimeError(f"Нет ключа для AI_PROVIDER={AI_PROVIDER} (ANTHROPIC_API_KEY / OPENROUTER_API_KEY)")
    ctx = ("Известные плантации (name / country / aliases / notes):\n"
           + "\n".join(f"- {f['name']} / {f['country']} / {f['aliases']} / {f['notes']}" for f in farms)
           + "\n\nНоменклатура, которую мы уже использовали (приводи названия к этому виду, если это то же самое):\n"
           + "\n".join(catalog[:400]))
    out = await _structured(
        PARSE_MODEL, DOMAIN + "\n\n" + ctx,
        [_block(data, mime),
         {"type": "text", "text": "Распознай документ полностью, все строки. Проверь арифметику строк и итог."}],
        PARSE_TOOL, 16000)
    # arithmetic check on our side too — never trust one pass
    s = sum((l.get("stems") or 0) * (l.get("price_usd") or 0) for l in out.get("lines", []))
    sub = out.get("subtotal_usd")
    if sub and s and abs(s - sub) > 0.5:   # only an OCR check: lines must add up to the subtotal
        out.setdefault("warnings", []).append(f"Строки дают ${s:.2f}, а subtotal ${sub:.2f} — строка прочитана неверно?")
    return out


AUDIT_RULES = """

Ты проверяешь готовый расчёт пополнения перед тем, как он уйдёт в учёт. Ищи реальные проблемы:
дубли строк и инвойсов (один инвойс внесён дважды, одинаковые строки внутри инвойса), AWB без логистики
или логистика без инвойсов, разный формат одного AWB, вес плантаций не сходится с весом консолидатора,
курс сильно отличается от соседних пополнений, себестоимость стебля выбивается из истории этой
плантации/сорта, сумма оплаченного $ больше пополнения, странные цены (0, отрицательные, опечатки ×10).
НЕ считай проблемой разницу между оплаченным и суммой строк — это налог и сборы, так и должно быть.
Суммы $ и ₽ оплаты внесены оператором и верны — не предлагай их менять.
Конкретно: где, что, как исправить. Без воды."""


async def audit_topup(snapshot: dict, history: dict) -> dict:
    if not client:
        raise RuntimeError(f"Нет ключа для AI_PROVIDER={AI_PROVIDER} (ANTHROPIC_API_KEY / OPENROUTER_API_KEY)")
    return await _structured(AUDIT_MODEL, DOMAIN + AUDIT_RULES,
                             "ПОПОЛНЕНИЕ:\n" + json.dumps(snapshot, ensure_ascii=False)
                             + "\n\nИСТОРИЯ (курсы и типичная себестоимость):\n" + json.dumps(history, ensure_ascii=False),
                             AUDIT_TOOL, 32000)
