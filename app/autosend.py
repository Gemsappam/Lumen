"""Operator file goes out by itself, per MAWB, twice at most:

  stage «tk»   — cost formed by Floratrack (Церера): every invoice of the MAWB paid, ТК МСК (and ТК Кения for Kenya)
                 billed with a final rate, the bills fully spread over the farms (all farm invoices are in).
                 Delivery to Опт Склад Бауманская not entered yet -> «неполная» in the file, said in the caption.
  stage «full» — the same + delivery to Bauманская entered -> final file.

settings.json: auto_op = {MAWB key: "tk" | "full"}; auto_op_seeded — on the first run everything already complete is
marked as sent (no flood of old MAWBs)."""
import json
from collections import defaultdict

RANK = {None: 0, "tk": 1, "full": 2}


def stages() -> dict:
    """{MAWB key: (stage, awb display)} for MAWBs whose cost is complete."""
    from .api import _all, _awb_kg
    from .calc import compute, norm_awb, broker_topup
    from .lastmile import status
    from .models import session
    out = {}
    with session() as s:
        tops, invs, lines, logs = _all(s)
        res = compute(0, tops, invs, lines, logs, _awb_kg(s))
    tids = {t.id for t in tops}
    by_inv = defaultdict(list)
    for l in lines:
        by_inv[l.invoice_id].append(l)
    by_awb = defaultdict(list)
    for i in invs:
        if (i.awb or "").strip():
            by_awb[norm_awb(i.awb)].append(i)
    for k, mine in by_awb.items():
        if not all(by_inv[i.id] for i in mine):
            continue
        paid = all((i.topup_id in tids) or (getattr(i, "via_broker", False) and broker_topup(res, i) in tids) for i in mine)
        if not paid:
            continue
        kenya = any((i.country or "") == "Кения" for i in mine)
        legs = ["msk"] + (["air"] if kenya else [])
        ok = True
        for leg in legs:
            g = res.legs.get((k, leg))
            if not g or not g.get("rub") or (k, leg) in res.estimated_legs:
                ok = False
                break
            spread = sum((res.lines[l.id].air_rub_stem if leg == "air" else res.lines[l.id].msk_rub_stem) * l.stems
                         for i in mine for l in by_inv[i.id] if l.id in res.lines)
            if spread < g["rub"] * 0.99:              # some farm of the bill has no invoice / kg yet
                ok = False
                break
        if ok:
            out[k] = ("full" if status(k) == "full" else "tk", mine[0].awb)
    return out


async def run(bot, people: list[int]):
    from aiogram.types import BufferedInputFile
    from .api import _settings, SETTINGS, awb_operator_xlsx
    from .backup import mark_dirty
    from .lastmile import WAREHOUSE
    st = _settings()
    sent = dict(st.get("auto_op") or {})
    now = stages()
    if not st.get("auto_op_seeded"):
        sent.update({k: v[0] for k, v in now.items()})
        SETTINGS.write_text(json.dumps({**_settings(), "auto_op": sent, "auto_op_seeded": True}, ensure_ascii=False))
        mark_dirty()
        return 0
    n = 0
    for k, (stage, awb) in now.items():
        if RANK[stage] <= RANK.get(sent.get(k), 0):
            continue
        try:
            data, fname, _unpaid = awb_operator_xlsx(awb)
        except Exception as e:
            print("[lumen] autosend", awb, e, flush=True)
            continue
        cap = (f"📊 Себестоимость по MAWB {awb} сформирована (Floratrack) — файл оператора.\n"
               f"Доставка до «{WAREHOUSE}» ещё не внесена — после «Забрал» и суммы за машину придёт финальный файл."
               if stage == "tk" else
               f"✅ Финальная себестоимость по MAWB {awb} — до склада «{WAREHOUSE}», файл оператора.")
        for uid in people:
            try:
                await bot.send_document(uid, BufferedInputFile(data, fname), caption=cap)
            except Exception:
                pass
        sent[k] = stage
        n += 1
        SETTINGS.write_text(json.dumps({**_settings(), "auto_op": sent}, ensure_ascii=False))
        mark_dirty()
    return n
