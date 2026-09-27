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
4. Logistics: each (AWB, leg) cost is spread over ALL farm invoices on that AWB
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


def compute(topup_id, topups, invoices, lines, logistics) -> Result:
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

    groups = defaultdict(lambda: {"usd": 0.0, "rub": 0.0, "ids": [], "own": True, "basis": "auto", "provider": "", "kg_total": 0.0})
    for lg in logistics:
        key = (norm_awb(lg.awb), lg.leg)
        g = groups[key]
        r = rate_of(tmap.get(lg.topup_id)) if lg.topup_id else 0
        rub = lg.rub if lg.rub is not None else (round((lg.usd or 0) * r) if r else None)
        if rub is None:
            res.warnings.append(f"Логистика AWB {lg.awb}: есть только $ и нет курса — внеси сумму в ₽")
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
        if lg.topup_id == topup_id:
            res.usd_spent += lg.usd or 0
            res.rub_spent += rub
    res.legs = dict(groups)

    for (awb, leg), g in groups.items():
        invs = inv_by_awb.get(awb, [])
        if not invs:
            res.warnings.append(f"Логистика AWB {awb} ({leg}): нет ни одного инвойса плантации с этим AWB")
            continue
        inv_stems = [sum(l.stems for l in lines_by_inv[i.id]) for i in invs]
        attr, w = (None, None) if g["basis"] == "stems" else _pick_basis(invs, ["weight_kg"])
        if attr is None:
            w = inv_stems
            if g["basis"] != "stems" and len(invs) > 1:
                res.warnings.append(f"AWB {awb}: не у всех плантаций указан вес — логистика разбита по стеблям")
        tot = sum(w) or 1
        if attr == "weight_kg" and g["kg_total"] > tot + 0.01:
            # forwarder bill covers farms not entered yet: they keep their share, we don't dump it on the others
            res.warnings.append(f"AWB {awb}: внесено {tot:g} из {g['kg_total']:g} кг по разбивке {g['provider'] or 'перевозчика'} — "
                                f"остальные плантации ещё без инвойсов")
            tot = g["kg_total"]
        for inv, wi in zip(invs, w):
            ls = lines_by_inv[inv.id]
            la, lw = _pick_basis(ls, ["weight_kg"])   # like the old sheet: equal per stem inside a farm
            if la is None:
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
            res.warnings.append(f"{inv.farm} (AWB {inv.awb}): логистика ещё не внесена")
    return res
