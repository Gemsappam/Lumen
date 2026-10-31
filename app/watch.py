"""Night watch: catches calculation mistakes before a wrong Excel reaches the operator.

1) freight over-allocated: farms of a MAWB carry more ₽ than the ТК bill (> 100.5%)
2) logistics per stem of an invoice jumped > 5% although NOTHING about its freight changed
   (same bills, same kg breakdown, same farms and kg on the MAWB). Payments are deliberately ignored:
   the flower ₽ (rate, commission, advances, debts) is not watched at all, and freight with a provisional /
   «≈ last rate» ₽ is skipped — those move when money moves, that's normal.
3) a ТК bill with no farm invoice on its MAWB for more than 7 days

The previous state lives in DATA_DIR/watch.json.
"""
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta

JUMP = 0.05          # 5 %
OVER = 1.005         # 100.5 %


def _file():
    from .config import DATA_DIR
    return DATA_DIR / "watch.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text())
    except Exception:
        return {}


def _h(x) -> str:
    return hashlib.md5(json.dumps(x, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def run(save: bool = True) -> list[str]:
    """-> list of problems (empty = all good)."""
    from .api import _all, _awb_kg
    from .calc import compute, norm_awb
    from .models import AwbWeights, session
    from sqlmodel import select
    prev = _load()
    issues = []
    now = datetime.now()
    with session() as s:
        tops, invs, lines, logs = _all(s)
        kg = _awb_kg(s)
        weights = {w.awb: w.farm_kg_json for w in s.exec(select(AwbWeights)).all()}
        res = compute(0, tops, invs, lines, logs, kg)
    by_inv = defaultdict(list)
    for l in lines:
        by_inv[l.invoice_id].append(l)
    invs_by_awb = defaultdict(list)
    for i in invs:
        if (i.awb or "").strip():
            invs_by_awb[norm_awb(i.awb)].append(i)
    logs_by_awb = defaultdict(list)
    for lg in logs:
        logs_by_awb[norm_awb(lg.awb)].append(lg)
    fmt = lambda x: f"{x:,.0f}".replace(",", " ")
    nice = lambda k: f"{k[:3]}-{k[3:]}" if len(k) == 11 and k.isdigit() else k

    # 1) over-allocation
    alloc = defaultdict(float)
    for i in invs:
        k = norm_awb(i.awb) if i.awb else ""
        for l in by_inv[i.id]:
            c = res.lines.get(l.id)
            if c:
                alloc[(k, "air")] += c.air_rub_stem * l.stems
                alloc[(k, "msk")] += c.msk_rub_stem * l.stems
    for (k, leg), g in res.legs.items():
        a = alloc.get((k, leg), 0)
        if g.get("rub") and a > g["rub"] * OVER + 1:
            issues.append(f"MAWB {nice(k)} ({'ТК Кения' if leg == 'air' else 'ТК МСК'}): на фермы разложено {fmt(a)} ₽, "
                          f"а счёт {fmt(g['rub'])} ₽ ({a / g['rub'] * 100:.0f}%) — себестоимость завышена")

    # 2) freight per stem jumped with nothing changed
    snap = {}
    for i in invs:
        ls = by_inv[i.id]
        st = sum(l.stems for l in ls)
        if not st or not (i.awb or "").strip():
            continue
        k = norm_awb(i.awb)
        if (k, "air") in res.estimated_legs or (k, "msk") in res.estimated_legs:
            continue                                      # ₽ moves with the rate / payment — normal
        logi = sum((res.lines[l.id].air_rub_stem + res.lines[l.id].msk_rub_stem) * l.stems for l in ls) / st
        fp = _h({
            "bills": sorted((lg.id, lg.leg, lg.usd, lg.rub, lg.weight_kg, lg.farm_kg_json, lg.basis) for lg in logs_by_awb[k]),
            "bd": weights.get(k),
            "farms": sorted((x.id, x.weight_kg, sum(l.stems for l in by_inv[x.id]),
                             tuple(sorted((l.id, l.stems, l.weight_kg) for l in by_inv[x.id]))) for x in invs_by_awb[k]),
        })
        snap[str(i.id)] = {"logi": round(logi, 4), "fp": fp}
        p = (prev.get("inv") or {}).get(str(i.id))
        if p and p.get("fp") == fp and p.get("logi", 0) > 0.5 and abs(logi / p["logi"] - 1) > JUMP:
            issues.append(f"{i.farm} (MAWB {nice(k)}): логистика {p['logi']:.2f} → {logi:.2f} ₽/ст "
                          f"({(logi / p['logi'] - 1) * 100:+.0f}%), хотя счета, кг и фермы на MAWB не менялись")

    # 3) ТК bill without farm invoices for > 7 days
    seen = dict(prev.get("bill_seen") or {})
    for k, lgs in logs_by_awb.items():
        for lg in lgs:
            seen.setdefault(str(lg.id), now.strftime("%Y-%m-%d"))
        if not invs_by_awb.get(k):
            first = min(datetime.strptime(seen[str(lg.id)], "%Y-%m-%d") for lg in lgs)
            if now - first > timedelta(days=7):
                issues.append(f"MAWB {nice(k)}: счёт ТК ({fmt(sum(lg.rub or 0 for lg in lgs))} ₽) висит {(now - first).days} дн. "
                              "без инвойсов ферм — его ₽ не легли ни на один стебель")
    alive = {str(lg.id) for lg in logs}
    seen = {x: d for x, d in seen.items() if x in alive}
    if save:
        _file().write_text(json.dumps({"ts": now.strftime("%d.%m.%Y %H:%M"), "inv": snap, "bill_seen": seen},
                                      ensure_ascii=False))
    return issues


def report(issues: list[str], quiet_ok: bool = True):
    if not issues:
        return None if quiet_ok else "✅ Проверка расчётов: всё сходится — перерасхода по счетам ТК нет, скачков логистики нет."
    return ("🛡 Проверка расчётов нашла:\n" + "\n".join(f"• {x}" for x in issues[:20])
            + (f"\n…и ещё {len(issues) - 20}" if len(issues) > 20 else "")
            + "\n\nПока не разберёмся — Excel оператору лучше не отдавать. Скинь бэкап разработчику.")
