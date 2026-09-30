"""
Себестоимость engine. Pure functions — no DB, so it's testable against the old Excel.

Rules (reverse-engineered from учет.xlsx, then made consistent):

1. Rate of a top-up = RUB sent / USD received.
2. Invoice RUB = what the operator entered. Only if it's empty: round(USD * top-up rate).
3. Flower price in RUB per stem:
     value mode (default): price_usd * RUB_paid / USD_paid   (invoice's own rate;
        tax / doc fees above the line sum are NOT pushed into the stem price)
     stems mode: RUB_paid / Σ stems   (hydrangea style — one price for everything)
   RUB_paid and USD_paid are entered by the operator and are taken as-is.
4. Logistics: ₽ per kg = bill ₽ / bill kg (or breakdown total if the bill has no weight);
   farm cost = ₽/kg × farm kg; per stem = farm cost / farm stems.
   Each (AWB, leg) cost is spread over ALL farm invoices on that AWB
   (across any top-up), by farm kg if every farm has kg, else by stems.
   Inside an invoice: by line kg if all lines have it, else equally per stem (as in the old sheet).
   Per stem = share of leg RUB / stems.
5. Full cost per stem = flower + air leg + msk leg.
"""
from __future__ import annotations
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field


def norm_awb(s: str | None) -> str:
    s = (s or "").strip()
    d = re.sub(r"\D", "", s)
    return d if len(d) >= 6 else s.lower()


def rate_of(t) -> float:
    return (t.rub / t.usd) if t and t.usd else 0.0


def _pick_basis(items, attrs):
    """Return (attr_name, values) for the first attr every item has > 0."""
    for a in attrs:
        vals = [getattr(i, a, None) for i in items]
        if items and all(v not in (None, 0) for v in vals):
            return a, [float(v) for v in vals]
    return None, None


@dataclass
class LineCalc:
    line_id: int
    stems: float
    price_rub: float = 0.0
    air_share: float = 0.0     # fraction of the AWB's air leg borne by this line
    msk_share: float = 0.0
    air_usd_stem: float = 0.0
    air_rub_stem: float = 0.0
    msk_rub_stem: float = 0.0

    @property
    def total_rub_stem(self):
        return self.price_rub + self.air_rub_stem + self.msk_rub_stem


@dataclass
class Result:
    rate: float
    invoice_rub: dict = field(default_factory=dict)       # inv_id -> rub
    lines: dict = field(default_factory=dict)             # line_id -> LineCalc
    legs: dict = field(default_factory=dict)              # (awb, leg) -> {"usd","rub","ids","own"}
    usd_spent: float = 0.0
    rub_spent: float = 0.0
    warnings: list = field(default_factory=list)


def compute(topup_id, topups, invoices, lines, logistics, awb_kg=None) -> Result:
    """awb_kg: {normalized MAWB: total kg of the uploaded per-farm breakdown}"""
    awb_kg = awb_kg or {}
    tmap = {t.id: t for t in topups}
    rate = rate_of(tmap.get(topup_id))
    res = Result(rate=rate)
    lines_by_inv = defaultdict(list)
    for l in lines:
        lines_by_inv[l.invoice_id].append(l)

    # ---- flowers -------------------------------------------------------------
    for inv in invoices:
        r = rate_of(tmap.get(inv.topup_id))
        rub = inv.rub_paid_override if inv.rub_paid_override is not None else round(inv.usd_paid * r)
        res.invoice_rub[inv.id] = rub
        ls = lines_by_inv[inv.id]
        stems = sum(l.stems for l in ls)
        value = sum(l.price_usd * l.stems for l in ls)
        for l in ls:
            lc = LineCalc(line_id=l.id, stems=l.stems)
            if inv.alloc_mode == "stems" or value == 0:
                lc.price_rub = rub / stems if stems else 0
            else:
                lc.price_rub = l.price_usd * rub / inv.usd_paid if inv.usd_paid else 0
            res.lines[l.id] = lc
        if inv.topup_id == topup_id:
            res.usd_spent += inv.usd_paid
            res.rub_spent += rub
            if inv.rub_paid_override is None:
                res.warnings.append(f"{inv.farm}: сумма в ₽ не внесена — взял $ × курс пополнения")

    # ---- logistics -----------------------------------------------------------
    inv_by_awb = defaultdict(list)
    for inv in invoices:
        inv_by_awb[norm_awb(inv.awb)].append(inv)

    groups = defaultdict(lambda: {"usd": 0.0, "rub": 0.0, "ids": [], "own": True, "basis": "auto", "provider": "", "kg_total": 0.0, "kg_bill": 0.0})
    for lg in logistics:
        key = (norm_awb(lg.awb), lg.leg)
        g = groups[key]
        r = rate_of(tmap.get(lg.topup_id)) if lg.topup_id else 0
        rub = lg.rub if lg.rub is not None else (round((lg.usd or 0) * r) if r else None)
        if rub is None:
            res.warnings.append(f"Логистика MAWB {lg.awb}: есть только $ и нет курса — внеси сумму в ₽")
            rub = 0
        g["usd"] += lg.usd or 0
        g["rub"] += rub
        g["ids"].append(lg.id)
        g["own"] &= (lg.topup_id == topup_id and lg.rub is None)
        g["basis"] = lg.basis if lg.basis != "auto" else g["basis"]
        g["provider"] = g["provider"] or lg.provider
        try:
            bd = json.loads(getattr(lg, "farm_kg_json", "") or "{}")
        except ValueError:
            bd = {}
        g["kg_total"] = max(g["kg_total"], sum(float(v) for v in bd.values() if v))
        g["kg_bill"] += getattr(lg, "weight_kg", None) or 0
        if lg.topup_id == topup_id:
            res.usd_spent += lg.usd or 0
            res.rub_spent += rub
    res.legs = dict(groups)

    for (awb, leg), g in groups.items():
        invs = inv_by_awb.get(awb, [])
        if not invs:
            res.warnings.append(f"Логистика MAWB {awb} ({leg}): пока нет ни одного инвойса плантации с этим MAWB")
            continue
        inv_stems = [sum(l.stems for l in lines_by_inv[i.id]) for i in invs]
        attr, w = (None, None) if g["basis"] == "stems" else _pick_basis(invs, ["weight_kg"])
        if attr is None:
            if g["basis"] != "stems" and len(invs) > 1:
                # several farms, no kg breakdown yet: don't guess — leave freight unallocated until it's loaded
                res.warnings.append(f"MAWB {awb}: нет разбивки кг по плантациям — логистика "
                                    f"{g['provider'] or ''} пока не распределена. Загрузи «Разбивка кг по MAWB».")
                continue
            w = inv_stems
        tot = sum(w) or 1
        g["kg_total"] = max(g["kg_total"], awb_kg.get(awb, 0))
        if attr == "weight_kg" and g["kg_total"] > tot + 0.01:
            # forwarder bill covers farms not entered yet: they keep their share, we don't dump it on the others
            res.warnings.append(f"MAWB {awb}: внесено {tot:g} из {g['kg_total']:g} кг по разбивке {g['provider'] or 'перевозчика'} — "
                                f"остальные плантации ещё без инвойсов")
            tot = g["kg_total"]
        # one farm, no breakdown and no kg on the invoice -> nothing to split by: it takes the whole AWB.
        # If the operator typed the farm's kg (other farms' goods ride on the same AWB) -> ₽/kg × its kg.
        single = len(invs) == 1 and not awb_kg.get(awb) and not g["kg_total"] and not invs[0].weight_kg
        if single:
            tot = sum(w) or 1        # whole AWB belongs to this one farm: it takes 100% of the freight
        elif attr == "weight_kg" and g["kg_bill"]:
            # his rule: ₽ of the bill / kg of the bill = ₽ per kg; farm pays ₽/kg × its kg
            if g["kg_total"] and abs(g["kg_bill"] - g["kg_total"]) > 0.5:
                res.warnings.append(f"MAWB {awb}: вес по счёту {g['provider'] or ''} {g['kg_bill']:g} кг, "
                                    f"по разбивке {g['kg_total']:g} кг — ставка ₽/кг считается от веса счёта")
            tot = g["kg_bill"]
        g["rub_per_kg"] = g["rub"] / tot if attr == "weight_kg" and tot else None
        for inv, wi in zip(invs, w):
            ls = lines_by_inv[inv.id]
            la, lw = _pick_basis(ls, ["weight_kg"])   # like the old sheet: equal per stem inside a farm
            fixed = [l for l in ls if l.weight_kg]
            if la is None and fixed and inv.weight_kg and inv.weight_kg > sum(l.weight_kg for l in fixed):
                # some boxes have a known weight (Zeeflora spray = 25 kg box): they take their kg,
                # the rest of the farm's kg is shared by the other lines per stem
                rest_kg = inv.weight_kg - sum(l.weight_kg for l in fixed)
                rest_st = sum(l.stems for l in ls if not l.weight_kg) or 1
                lw = [l.weight_kg if l.weight_kg else rest_kg * l.stems / rest_st for l in ls]
            elif la is None:
                lw = [l.stems for l in ls]
            lt = sum(lw) or 1
            for l, x in zip(ls, lw):
                lc = res.lines[l.id]
                share = (wi / tot) * (x / lt)
                if leg == "air":
                    lc.air_share += share
                    if l.stems:
                        lc.air_rub_stem += g["rub"] * share / l.stems
                        lc.air_usd_stem += g["usd"] * share / l.stems
                else:
                    lc.msk_share += share
                    if l.stems:
                        lc.msk_rub_stem += g["rub"] * share / l.stems

    # invoices of this top-up with no freight yet
    have = {awb for (awb, _l) in groups}
    for inv in invoices:
        if inv.topup_id == topup_id and norm_awb(inv.awb) not in have and inv.awb:
            res.warnings.append(f"{inv.farm} (MAWB {inv.awb}): логистика ещё не внесена")
    return res
