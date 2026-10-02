"""
Себестоимость engine. Pure functions — no DB, so it's testable against the old Excel.

Rules (reverse-engineered from учет.xlsx, then made consistent):

1. Rate of a top-up = RUB sent / USD received.
2. Invoice RUB = what the operator entered. Only if it's empty: round(USD * top-up rate).
3. Flower price in RUB per stem («истинный курс»):
     value mode (default): price_usd * RUB_paid / Σ(price_usd * stems)
        RUB_paid = $ paid with all costs × top-up rate (or the ₽ Arman typed).
        Every rouble paid for the invoice — commission, tax, doc fee — lands on the stems,
        each line in proportion to its price. Σ(stem cost × stems) = RUB_paid exactly.
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
    est_rate: float = 0.0
    true_rate: dict = field(default_factory=dict)       # inv_id -> ₽ per $ of flowers incl. all costs
    invoice_cost: dict = field(default_factory=dict)    # inv_id -> ₽ cost of the goods (from the farm ledger)
    inv_ledger: dict = field(default_factory=dict)      # inv_id -> how it was covered (advance / payment / debt)
    ledger: dict = field(default_factory=dict)          # farm -> balance, advances, debts
    cost_pct: dict = field(default_factory=dict)        # inv_id -> payment costs, % over the flower lines
    estimated_inv: set = field(default_factory=set)     # unpaid invoices (≈)
    estimated_legs: set = field(default_factory=set)    # (awb, leg) with ≈ ₽
    rub_spent: float = 0.0
    warnings: list = field(default_factory=list)


def invoice_total(inv, ls) -> float:
    """What the farm charges for the invoice (its Grand Total incl. doc fee / tax); fallback: flower lines."""
    v = sum(l.price_usd * l.stems for l in ls)
    t = getattr(inv, "invoice_total_usd", None)
    return float(t) if t and t >= v - 0.01 else v


OPENINGS: dict = {}   # farm/account (lower) -> (usd, rate, name): balance BEFORE the bot
RULES: dict = {}      # farm (lower) -> {"account", "in_fee", "markup"} (brokers: Tessa / Plazoleta)


def _rule(farm: str) -> dict:
    return RULES.get((farm or "").strip().lower(), {})


def farm_ledger(topups, invoices, lines_by_inv, est_rate) -> dict:
    """Settlements with each farm in farm-$ (what actually reached the farm).
    payment:  +farm_usd at «₽ per farm-$» = ₽ paid ÷ farm_usd  (agent/bank costs sit in this rate)
    invoice:  −Grand Total, covered first-in-first-out: old advance first (at ITS rate), then new money.
    Not enough money -> debt: valued at the latest top-up rate (≈) until a later payment covers it.
    -> {"inv": {id: {rub_cost, farm_usd, from_advance, debt_usd, parts}}, "farms": {farm: {...}}}"""
    from collections import deque
    tmap = {t.id: t for t in topups}
    tdate = {t.id: t.date for t in topups}
    by_farm = defaultdict(list)
    for inv in invoices:
        r = _rule(inv.farm)
        by_farm[(r.get("account") or inv.farm or "").strip().lower()].append(inv)
    names = {k: k for k in by_farm}
    for k in OPENINGS:
        by_farm.setdefault(k, [])
    out_inv, farms = {}, {}
    for key, invs in by_farm.items():
        credits = deque()          # [usd_left, rub_per_usd, label]
        debts = deque()            # [inv_id | None (old debt), usd_left]
        cost_factor = []           # typical ₽/farm-$ ÷ top-up rate of this farm (to value debts)
        o_usd, o_rate = (OPENINGS.get(key) or (0, None))[:2]
        if o_usd > 0.005:
            credits.append([o_usd, o_rate or est_rate, "начальный баланс"])
        elif o_usd < -0.005:
            debts.append([None, -o_usd])
        for inv in sorted(invs, key=lambda x: x.id):
            ls = lines_by_inv[inv.id]
            rule = _rule(inv.farm)
            total = invoice_total(inv, ls) * (1 + rule.get("markup", 0) / 100)   # broker: +7 % on the invoice
            rec = {"rub_cost": 0.0, "farm_usd": 0.0, "from_advance": 0.0, "debt_usd": 0.0, "parts": [], "total": total}
            out_inv[inv.id] = rec
            if inv.topup_id:                                   # this invoice brought money to the farm
                r = rate_of(tmap.get(inv.topup_id))
                rub = inv.rub_paid_override if inv.rub_paid_override is not None else round(inv.usd_paid * r)
                f = getattr(inv, "farm_usd", None)
                if f:
                    f = float(f)
                elif rule.get("in_fee"):                       # broker: only 97 % of the dollars arrive (3 % in)
                    f = inv.usd_paid * (1 - rule["in_fee"] / 100)
                else:
                    f = total                                  # default: exactly the invoice reached the farm
                rec["farm_usd"] = f
                if f > 0:
                    rpu = rub / f
                    credits.append([f, rpu, tdate.get(inv.topup_id, "")])
                    if r:
                        cost_factor.append(rpu / r)
                    while debts and credits:                   # new money pays old debts first
                        d = debts[0]
                        c = credits[0]
                        take = min(d[1], c[0])
                        if d[0] is not None:                   # None = old debt from before the bot: no invoice cost
                            old = out_inv[d[0]]
                            old["rub_cost"] += take * c[1]
                            old["debt_usd"] -= take
                            old["parts"].append({"usd": take, "rate": c[1], "src": f"оплата {c[2]}"})
                        d[1] -= take; c[0] -= take
                        if d[1] <= 1e-6: debts.popleft()
                        if c[0] <= 1e-6: credits.popleft()
            need = total
            while need > 1e-6 and credits:
                c = credits[0]
                take = min(need, c[0])
                rec["rub_cost"] += take * c[1]
                rec["parts"].append({"usd": take, "rate": c[1], "src": f"оплата {c[2]}"})
                need -= take; c[0] -= take
                if c[0] <= 1e-6:
                    credits.popleft()
            if need > 1e-6:
                rec["debt_usd"] = need
                debts.append([inv.id, need])
        # advance used by an invoice = what it consumed beyond its own payment
        for inv in invs:
            rec = out_inv[inv.id]
            rec["from_advance"] = max(0.0, min(rec["total"], rec["total"] - rec["farm_usd"] - rec["debt_usd"])) if rec["farm_usd"] < rec["total"] else 0.0
        factor = (sum(cost_factor) / len(cost_factor)) if cost_factor else 1.0
        for inv in invs:                                      # uncovered debt: ≈ latest rate × this farm's usual costs
            rec = out_inv[inv.id]
            if rec["debt_usd"] > 1e-6:
                est_usd = getattr(inv, "est_usd", None)
                rule = _rule(inv.farm)
                if rule.get("in_fee"):                         # broker: each exchange-$ costs rate ÷ 0.97
                    f = 1 / (1 - rule["in_fee"] / 100)
                else:
                    f = (est_usd / rec["total"]) if (not inv.topup_id and est_usd and rec["total"]) else factor
                rec["rub_cost"] += rec["debt_usd"] * est_rate * f
        adv = sum(c[0] for c in credits)
        debt = sum(d[1] for d in debts)
        op = OPENINGS.get(key)
        acc = _rule(invs[0].farm).get("account") if invs else None
        name = (f"{acc} ({', '.join(sorted({i.farm for i in invs}))})" if acc else invs[0].farm) if invs else \
            (op[2] if op and len(op) > 2 else key)
        farms[name] = {"farm": name, "advance_usd": round(adv, 2), "debt_usd": round(debt, 2),
                       "balance_usd": round(adv - debt, 2),
                       "advances": [{"usd": round(c[0], 2), "rate": round(c[1], 4), "from": c[2]} for c in credits],
                       "debts": [{"invoice_id": d[0], "usd": round(d[1], 2)} for d in debts]}
    return {"inv": out_inv, "farms": farms}


def compute(topup_id, topups, invoices, lines, logistics, awb_kg=None) -> Result:
    """awb_kg: {normalized MAWB: total kg of the uploaded per-farm breakdown}"""
    awb_kg = awb_kg or {}
    tmap = {t.id: t for t in topups}
    rate = rate_of(tmap.get(topup_id))
    res = Result(rate=rate)
    latest = max(topups, key=lambda t: t.id) if topups else None
    est_rate = rate_of(latest)            # unpaid goods / unpaid freight: ≈ at the last top-up rate
    res.est_rate = est_rate
    lines_by_inv = defaultdict(list)
    for l in lines:
        lines_by_inv[l.invoice_id].append(l)

    # ---- flowers: farm ledger (advances / debts) -------------------------------
    led = farm_ledger(topups, invoices, lines_by_inv, est_rate)
    res.ledger = led["farms"]
    for inv in invoices:
        unpaid = not inv.topup_id
        r = est_rate if unpaid else rate_of(tmap.get(inv.topup_id))
        usd = (inv.usd_paid or getattr(inv, "est_usd", None) or 0) if unpaid else inv.usd_paid
        rub = inv.rub_paid_override if (inv.rub_paid_override is not None and not unpaid) else round(usd * r)
        if unpaid or led["inv"][inv.id]["debt_usd"] > 0.005:
            res.estimated_inv.add(inv.id)
        res.invoice_rub[inv.id] = rub                       # cash that left (for the top-up)
        cost = led["inv"][inv.id]["rub_cost"]               # what the goods really cost (ledger)
        res.invoice_cost[inv.id] = cost
        res.inv_ledger[inv.id] = led["inv"][inv.id]
        ls = lines_by_inv[inv.id]
        stems = sum(l.stems for l in ls)
        value = sum(l.price_usd * l.stems for l in ls)
        res.true_rate[inv.id] = (cost / value) if value else 0
        f_usd = led["inv"][inv.id]["farm_usd"]
        res.cost_pct[inv.id] = ((usd / f_usd - 1) * 100) if f_usd and usd and not unpaid else 0
        for l in ls:
            lc = LineCalc(line_id=l.id, stems=l.stems)
            if inv.alloc_mode == "stems" or value == 0:
                lc.price_rub = cost / stems if stems else 0
            else:
                # «истинный курс» = ₽ себестоимости инвойса ÷ $ строк цветов
                lc.price_rub = l.price_usd * cost / value
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
        if rub is None and not getattr(lg, "paid", True) and lg.usd:
            rub = round(lg.usd * est_rate)            # Expolanka on deferred payment: ≈ at the last rate
            res.estimated_legs.add((norm_awb(lg.awb), lg.leg))
        if "предварительн" in (getattr(lg, "note", "") or ""):
            res.estimated_legs.add((norm_awb(lg.awb), lg.leg))
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
