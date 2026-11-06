import asyncio
import json
import os
import sys
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import select

from . import api
from .models import Farm, init_db, session

SEED = {
    "Кения": ["Zeeflora", "Tambuzi", "Heritage", "Agriflora", "Massai", "Mzurrie (Winchester farm)", "Subati",
              "Kikwetu", "Karen Roses", "Batian", "Black Tulip", "PjDave", "Primarosa", "Red Lands"],
    "Эквадор": ["Tessa", "Nintanga", "B&M Fiori", "Agroterranorte", "Josar Flor", "Tikan", "EC Blooms (Starroses)", "Sand Flowers",
                "Allegro Farms", "Dayka", "Guaisa (Sunrite)", "Meral Flowers", "Monterosas", "Rosaprima", "Rosas Del Viento"],
    "Колумбия": ["Кондор (Гортензия)", "American Flowers", "Plazoleta", "La Conejera", "Serrezuela Flowers"],
}

# Bothost's proxy sends traffic to the port set in the panel. We open all the usual ones,
# and we do it from inside the app, so it works no matter how Bothost launches it
# (python -m app.main  OR  uvicorn app.main:app).
EXTRA_PORTS = [8000, 3000, 8080, 5000]


FORWARDERS = {
    "Expolanka": ("Кения", "EXPOLANKA,Expolanka Freight",
                  "Кения → Амстердам. Один MAWB на несколько плантаций, счёт за фрахт в $ БЕЗ разбивки по кг — "
                  "разбивка кг по плантациям приходит отдельным документом"),
    "Floratrack": ("Эквадор", "FLORATRACK,Флоратрак,Floratrak",
                   "Логистика всего, кроме Кении: Эквадор, Колумбия и остальные"),
}


FARM_ALIASES = {"Josar Flor": "JOSARFLOR,FLORICULTORA JOSARFLOR,FLORICULTORA JOSARFLOR S. A.,FLORICULTORA JOSARFLOR S.A.",
                "B&M Fiori": "B&M FIORI,BUITRON MARTIN STEFANIA RENEE,BUITRON MARTIN STEFANIA RENEE (B&M FIORI)",
                "Rosas Del Viento": "TIPANLUIZA LANCHIMBA JUAN MIGUEL,TIPANLUIZA,ROSAS DEL VENTO,Rosas del Viento",
                "Кондор (Гортензия)": "CONDOR ANDINO,CÓNDOR ANDINO,CONDOR ANDINO S.A.S,CÓNDOR ANDINO S.A.S,CONDOR",
                "Plazoleta": "PLAZOLETA BAZZANI,PLAZOLETA BAZZANI S.A.S",
                "Nintanga": "NINTANGA S.A.",
                "PjDave": "PJ FLORA,PJ FLOWERS,PJ",
                "Agriflora": "SIAN FLOWERS-AGRIFLORA,SIAN FLOWERS -AGRIFLORA,Агрифлора,Агри",
                "Massai": "SIAN FLOWERS-MAASAI,Maasai,Массай,Масаи"}


BOX_RULES = {"Zeeflora": {"Spray Rose Reflex Bicolour 60cm": 25, "Spray Rose Fire Works Bi-Pink 60cm": 25}}


BROKER = {"Tessa": ("Эквадор", "POSITANO,Позитано,TESSA"), "Plazoleta": ("Колумбия", "PLAZOL,PLAZOLETA"),
          "Geoflora": ("Колумбия", "GEOF,GEOFLORA,GEOFLORA SAS")}
BROKER_MARKUP = {"Geoflora": 9}              # BiFlorica charges 9 % on Geoflora (statement), 7 % on the others


def upsert_forwarders():
    with session() as s:
        from .models import Invoice
        old = s.exec(select(Farm).where(Farm.name == "Rosas Del Vento")).first()   # old misspelling -> Rosas Del Viento
        if old:
            if s.exec(select(Farm).where(Farm.name == "Rosas Del Viento")).first():
                s.delete(old)
            else:
                old.name = "Rosas Del Viento"; s.add(old)
            for inv in s.exec(select(Invoice)).all():
                if inv.farm == "Rosas Del Vento":
                    inv.farm = "Rosas Del Viento"; s.add(inv)
            s.commit()
        for name, (country, aliases) in BROKER.items():   # bought through a broker: 3 % in, 7 % on the invoice
            f = s.exec(select(Farm).where(Farm.name == name)).first() or Farm(name=name, country=country)
            f.country = country
            have = {x.strip().upper() for x in (f.aliases or "").split(",") if x.strip()}
            add = [x for x in aliases.split(",") if x.strip() and x.strip().upper() not in have]
            if add:
                f.aliases = ",".join(x for x in [f.aliases] + add if x)
            if not f.in_fee_pct and not f.markup_pct:
                f.in_fee_pct, f.markup_pct, f.account = 3, BROKER_MARKUP.get(name, 7), "Брокер"
                f.notes = ((f.notes or "") + " Закупка через брокера: на биржу заходит 97% долларов (3% входящая комиссия), "
                           "к инвойсу фермы +7% комиссии брокера.").strip()
            s.add(f)
        from .models import Invoice
        for inv in s.exec(select(Invoice)).all():           # Positano is the old name of Tessa
            if (inv.farm or "").strip().lower() == "positano":
                inv.farm = "Tessa"; s.add(inv)
        for name, rules in BOX_RULES.items():
            f = s.exec(select(Farm).where(Farm.name == name)).first()
            if f and (f.box_kg_json or "{}") == "{}":
                f.box_kg_json = json.dumps(rules, ensure_ascii=False)
                s.add(f)
        for name, al in FARM_ALIASES.items():
            f = s.exec(select(Farm).where(Farm.name == name)).first()
            if f and al.split(",")[0] not in (f.aliases or ""):
                f.aliases = ",".join(x for x in [f.aliases, al] if x)
                f.notes = (f.notes + " " if f.notes else "") + "Инвойсы приходят от трейдера NextWave одним файлом вместе с другой плантацией."
                s.add(f)
        for name, (country, aliases, notes) in FORWARDERS.items():
            f = s.exec(select(Farm).where(Farm.name == name)).first() or Farm(name=name, country=country)
            f.is_forwarder, f.aliases, f.notes = True, aliases, notes
            s.add(f)
        s.commit()


def _fix_double_broker():
    """Older bug: two Plazoleta/Tessa purchases got the same MAWB. Keep the newest, free the older one."""
    from .models import Invoice
    from .calc import norm_awb, _d
    with session() as s:
        by = {}
        for i in s.exec(select(Invoice)).all():
            if getattr(i, "via_broker", False) and i.awb:
                by.setdefault((norm_awb(i.awb), i.farm), []).append(i)
        for (_k, _f), invs in by.items():
            if len(invs) > 1:   # keep the one that HAS the farm invoice (packing), else the newest
                invs.sort(key=lambda i: (bool(i.packing_lines_json),
                                         _d(i.invoice_date) or __import__("datetime").datetime.min, i.id))
                for old in invs[:-1]:
                    if old.ext_id:      # two real statement purchases can share a MAWB — never strip one
                        continue
                    from .autolog import log as _alog
                    _alog(s, old, "awb", "", "при запуске: две брокерские закупки на одном MAWB")
        s.commit()
    _repair_broker_packing()


def _repair_broker_packing():
    """A Tessa/Plazoleta farm invoice (packing) that lost its MAWB: put it back on its consolidation MAWB via HAWB,
    merging into the statement purchase there (money from the statement, packing from the farm invoice)."""
    from .models import Invoice, Line
    from .calc import norm_awb
    from . import kbreak
    st = kbreak._state()
    hawb_to = {(f.get("hawb") or "").upper(): (k, v.get("awb") or k) for k, v in st.items()
               for f in v.get("farms", []) if f.get("hawb")}
    with session() as s:
        invs = s.exec(select(Invoice)).all()
        for src in [i for i in invs if i.via_broker and i.packing_lines_json and not (i.awb or "").strip()]:
            hit = hawb_to.get((src.hawb or "").upper())
            if not hit:
                continue
            k, awb = hit
            tgt = next((i for i in invs if i.id != src.id and i.via_broker and i.farm == src.farm
                        and norm_awb(i.awb) == k and not i.packing_lines_json), None)
            if tgt:                                    # statement purchase on that MAWB gets the packing
                tgt.packing_lines_json, tgt.boxes_json, tgt.hawb = src.packing_lines_json, src.boxes_json, src.hawb
                tgt.packing_sent = src.packing_sent
                s.add(tgt)
                if not src.ext_id:                     # placeholder without money -> remove
                    for l in s.exec(select(Line).where(Line.invoice_id == src.id)).all():
                        s.delete(l)
                    s.delete(src)
                else:
                    src.packing_lines_json = None; s.add(src)
            else:
                from .autolog import log as _alog
                _alog(s, src, "awb", awb, "при запуске: MAWB по HAWB из консолидационного листа")
        s.commit()


def _seed_packing_sigs():
    """Once: Tessa/Plazoleta packings already in a sent shipment are marked «already posted»
    (an earlier version re-armed them on every restart)."""
    from .api import _settings, SETTINGS
    from .models import Invoice
    from .calc import norm_awb
    from . import kbreak, packing
    if _settings().get("packing_sigs_seeded"):
        return
    with session() as s:
        ids = [i.id for i in s.exec(select(Invoice)).all()
               if getattr(i, "via_broker", False) and i.awb and i.packing_lines_json and kbreak.is_sent(norm_awb(i.awb))]
    for i in ids:
        packing.remember_posted(i)
        packing.mark_sent(i)
    SETTINGS.write_text(json.dumps({**_settings(), "packing_sigs_seeded": True}))


def _merge_broker_dupes():
    """A broker farm's invoice uploaded BEFORE the farm was marked «via broker» (booked as a normal unpaid invoice)
    + its purchase from the BiFlorica statement = one purchase: money from the statement, packing/MAWB/kg from the invoice."""
    import re as _re
    from .models import Invoice, Line
    with session() as s:
        invs = s.exec(select(Invoice)).all()
        for b in [i for i in invs if getattr(i, "via_broker", False) and i.ext_id]:
            tail = _re.sub(r"\D", "", (b.invoice_no or "").split("#")[-1])
            d = next((i for i in invs if i.id != b.id and i.farm == b.farm and not i.ext_id and not i.topup_id
                      and not getattr(i, "via_broker", False) and tail
                      and _re.sub(r"\D", "", i.invoice_no or "").endswith(tail)), None)
            if not d:
                continue
            ls = s.exec(select(Line).where(Line.invoice_id == d.id)).all()
            b.packing_lines_json = b.packing_lines_json or json.dumps(
                [{"name": l.name, "stems": l.stems, "boxes": l.boxes} for l in ls], ensure_ascii=False)
            for k in ("awb", "hawb", "weight_kg", "boxes_json", "truck", "eta", "arrived_at", "client_code"):
                if getattr(d, k, None) and not getattr(b, k, None):
                    setattr(b, k, getattr(d, k))
            s.add(b)
            for l in ls:
                s.delete(l)
            s.delete(d)
            from .autolog import note as _anote
            _anote(s, b.id, b.farm, b.awb, "merge", f"инвойс фермы №{d.invoice_no} слит с закупкой из выписки {b.invoice_no}",
                   "при запуске: брокерская ферма, инвойс был внесён как прямой")
        s.commit()
        # a placeholder made from a re-sent farm invoice next to the real statement purchase -> one purchase
        from .calc import norm_awb
        invs = s.exec(select(Invoice)).all()
        gone = set()
        def _st(i):
            try:
                return sum(float(x.get("stems") or 0) for x in json.loads(i.packing_lines_json or "[]"))
            except ValueError:
                return 0
        for b, by_no in [(b, p) for p in (True, False)
                         for b in invs if getattr(b, "via_broker", False) and b.ext_id]:   # number matches first
            tail = _re.sub(r"\D", "", (b.invoice_no or "").split("#")[-1])
            for d in invs:
                if d.id == b.id or d.id in gone or d.ext_id or not getattr(d, "via_broker", False) or d.farm != b.farm:
                    continue
                same_no = tail and len(tail) >= 4 and _re.sub(r"\D", "", d.invoice_no or "").endswith(tail)
                same_ship = (d.awb and norm_awb(d.awb) == norm_awb(b.awb) and b.packing_lines_json
                             and abs(_st(d) - _st(b)) < 1)
                if not (same_no if by_no else same_ship):
                    continue
                if d.packing_lines_json:
                    b.packing_lines_json = d.packing_lines_json
                for k in ("awb", "hawb", "weight_kg", "boxes_json", "truck", "eta", "arrived_at"):
                    if k in ("awb", "weight_kg", "truck", "eta") and b.arrived_at:
                        continue          # purchase already arrived on its own shipment — don't move it
                    if getattr(d, k, None) and not getattr(b, k, None):
                        if k in ("awb", "weight_kg", "arrived_at"):
                            from .autolog import log as _alog
                            _alog(s, b, k, getattr(d, k), "при запуске: перенесено со слитого дубля закупки")
                        else:
                            setattr(b, k, getattr(d, k))
                s.add(b)
                for l in s.exec(select(Line).where(Line.invoice_id == d.id)).all():
                    s.delete(l)
                s.delete(d); gone.add(d.id)
                from .autolog import note as _anote
                _anote(s, b.id, b.farm, b.awb, "merge", f"дубль закупки №{d.invoice_no or d.id} слит в {b.invoice_no}",
                       "при запуске: тот же инвойс фермы прислан повторно")
                print(f"[lumen] merged duplicate broker purchase {d.farm} #{d.id} into #{b.id}", flush=True)
        s.commit()
        from .api import infer_mawb                 # statement purchases still without MAWB: from consolidation lists
        for b in s.exec(select(Invoice)).all():
            if getattr(b, "via_broker", False) and not (b.awb or "").strip() and not b.arrived_at:
                a, kg, _n = infer_mawb(b.farm)
                if a:
                    from .autolog import log as _alog
                    if _alog(s, b, "awb", a, "при запуске: брокерская закупка без MAWB, ферма есть в листе/разбивке"):
                        if not b.weight_kg and kg:
                            _alog(s, b, "weight_kg", kg, "разбивка кг")
        s.commit()


def _fix_swapped_dates():
    """Invoice dates read in US order (10.06.2026 for 06.10.2026): fix by the other invoices of the same MAWB."""
    from .api import fix_swapped_date, _dt
    from .calc import norm_awb
    from .models import Invoice
    with session() as s:
        invs = s.exec(select(Invoice)).all()
        by = {}
        for i in invs:
            if (i.awb or "").strip():
                by.setdefault(norm_awb(i.awb), []).append(i)
        n = 0
        for grp in by.values():
            for i in grp:
                sib = [_dt(x.invoice_date or "") for x in grp if x.id != i.id and _dt(x.invoice_date or "")]
                if not sib:
                    continue
                new = fix_swapped_date(i.invoice_date or "", ref=max(sib))
                if new != i.invoice_date:
                    from .autolog import log as _alog
                    if _alog(s, i, "invoice_date", new, "при запуске: дата прочитана как месяц.день"):
                        n += 1
        if n:
            s.commit()
            print(f"[lumen] fixed {n} swapped invoice dates", flush=True)


def _fix_033_034():
    """Once (06.11.2026), by the Nextwave statement «UPDATED NEXTWAVE VS DILUNA SOA – REVISED OCT 2026» + credit note:
      #033 (14.10, MAWB 074-48014956) = Agriflora $427.14 + Massai $306.02; credit note −$214 (500 st Madam Bombastic
            60cm, Massai) applied to #033 itself (paid after it: $519.16) -> those 500 stems at $0, Massai net $92.02.
      #034 (21.10, MAWB 074-48015004) = $750.82 (Massai box 4–5 at full $214); credit −$162.64 = Massai box 6
            (200 st Madam Bombastic 70cm, $92.02) + Agriflora 1 box assorted 60cm ($70.62).
    The bot had written #034 over #033 on 4956. Sets every invoice to exactly this; payments are not touched."""
    from .api import _settings, SETTINGS
    from .models import Invoice, Line
    from .autolog import note as _note
    if _settings().get("fix_033_034_v2"):
        return
    k = lambda a: (a or "").replace("-", "")
    with session() as s:
        invs = s.exec(select(Invoice)).all()
        get = lambda farm, awb: next((i for i in invs if i.farm == farm and k(i.awb) == awb
                                      and (i.invoice_no or "").startswith("NW2026#03")), None)
        lines = lambda i: s.exec(select(Line).where(Line.invoice_id == i.id)).all()

        def put(inv, name, boxes, stems, price):
            l = next((x for x in lines(inv) if x.name == name), None) or Line(invoice_id=inv.id, name=name)
            l.boxes, l.stems, l.price_usd = boxes, stems, price
            s.add(l)

        def done(inv, txt):
            inv.boxes_json = None
            s.add(inv)
            _note(s, inv.id, inv.farm, inv.awb, "merge", txt, "сверка со стейтментом Nextwave 06.11")

        a = get("Agriflora", "07448014956")
        if a:
            put(a, "Spray Rose Constellation 60cm", None, 30, 0.5885)
            a.invoice_no, a.invoice_date, a.invoice_total_usd = "NW2026#033", "14.10.2026", 427.14
            a.note = "#033 (14.10), по стейтменту Nextwave оплачен 28.10"
            done(a, "инвойс #033 на 4956: $427.14")
        m = get("Massai", "07448014956")
        if m:
            put(m, "Spray Rose Madam Bombastic 60cm", 2, 500, 0.0)
            put(m, "Spray Rose Madam Bombastic 70cm", 1, 200, 0.4601)
            m.invoice_no, m.invoice_date, m.invoice_total_usd = "NW2026#033", "14.10.2026", 92.02
            m.note = "#033 (14.10): credit note −$214 — 500 ст Madam Bombastic 60cm (брак/не тот сорт) по $0; итог $92.02"
            done(m, "инвойс #033 на 4956: 500 ст Madam Bombastic 60cm по $0 (credit note −$214), итог $92.02")
        m5 = get("Massai", "07448015004")
        if m5:
            put(m5, "Spray Rose Madam Bombastic 60cm", 2, 500, 0.428)
            put(m5, "Spray Rose Madam Bombastic 70cm", 1, 200, 0.0)
            m5.invoice_total_usd = 214.0
            m5.note = "#034 (21.10): коробки 4–5 по полной цене; credit −$92.02 — коробка 6 (200 ст Madam Bombastic 70cm) по $0"
            done(m5, "инвойс #034 на 5004: коробки 4–5 $214, коробка 6 по $0 (credit −$92.02), итог $214")
        a5 = get("Agriflora", "07448015004")
        if a5 and m5 and a5.topup_id and m5.topup_id == a5.topup_id:
            # #034 paid as ONE payment (~$588 = net $588.18): split it between the farms by their net totals,
            # it was recorded $493 / $95 (by the old totals)
            tot_usd = (a5.usd_paid or 0) + (m5.usd_paid or 0)
            tot_rub = (a5.rub_paid_override or 0) + (m5.rub_paid_override or 0)
            sh = 374.18 / (374.18 + 214.0)
            a5.usd_paid = round(tot_usd * sh, 2)
            m5.usd_paid = round(tot_usd - a5.usd_paid, 2)
            if tot_rub:
                a5.rub_paid_override = round(tot_rub * sh)
                m5.rub_paid_override = round(tot_rub - a5.rub_paid_override)
            a5.farm_usd = m5.farm_usd = None
            s.add(m5)
        if a5:
            a5.invoice_total_usd = 374.18
            a5.note = "#034 (21.10): credit −$70.62 — 1 коробка assorted 60cm; к оплате $374.18"
            done(a5, "инвойс #034 на 5004: credit −$70.62 (коробка assorted 60cm), итог $374.18")
        s.commit()
    SETTINGS.write_text(json.dumps({**_settings(), "fix_033_034_v2": True}, ensure_ascii=False))


def _fix_agri_5004_nocredit():
    """Once (06.11.2026): the brother doesn't want the Agriflora credit (−$70.62) on #034 / MAWB 074-48015004 —
    the invoice stays at its full $444.8 and counts as settled (no debt to the farm). The $ really paid for it stay."""
    from .api import _settings, SETTINGS
    from .models import Invoice
    from .autolog import note as _note
    if _settings().get("fix_agri_5004_nocredit"):
        return
    with session() as s:
        a5 = next((i for i in s.exec(select(Invoice)).all() if i.farm == "Agriflora"
                   and (i.awb or "").replace("-", "") == "07448015004" and (i.invoice_no or "").startswith("NW2026#034")), None)
        if a5:
            a5.invoice_total_usd = 444.8
            a5.farm_usd = 444.8 if a5.topup_id else None
            a5.note = "#034 (21.10): $444.8, кредит Nextwave −$70.62 не учитываем (решение 06.11) — расчёт с фермой закрыт"
            s.add(a5)
            _note(s, a5.id, a5.farm, a5.awb, "merge", "кредит −$70.62 не учитываем: инвойс $444.8, расчёт закрыт", "решение 06.11")
        s.commit()
    SETTINGS.write_text(json.dumps({**_settings(), "fix_agri_5004_nocredit": True}, ensure_ascii=False))


def _fix_agri_4956_kg():
    """Once (06.11.2026): Expolanka's weight report for 074-48014956 gives Agriflora 150 kg (the ТК bill is 705 kg),
    but her invoice said 65 kg — 85 kg of freight (~54 000 ₽) sat on no farm. Decision: Agriflora carries her 150 kg."""
    from .api import _settings, SETTINGS
    from .models import Invoice
    from .autolog import note as _note
    if _settings().get("fix_agri_4956_kg"):
        return
    with session() as s:
        a = next((i for i in s.exec(select(Invoice)).all() if i.farm == "Agriflora"
                  and (i.awb or "").replace("-", "") == "07448014956"), None)
        if a and abs((a.weight_kg or 0) - 65) < 0.01:
            a.weight_kg = 150.0
            a.note = ((a.note or "") + " · вес 150 кг по отчёту Expolanka (было 65 — 85 кг логистики ни на ком не висели)").strip(" ·")
            s.add(a)
            _note(s, a.id, a.farm, a.awb, "merge", "вес 65 → 150 кг по весовому отчёту Expolanka", "решение 06.11")
        s.commit()
    SETTINGS.write_text(json.dumps({**_settings(), "fix_agri_4956_kg": True}, ensure_ascii=False))


def _clear_discrepancies():
    """«Срочно узнай у кладовщика» is switched off (06.11.2026): forget the pending questions."""
    from .models import Invoice
    with session() as s:
        n = 0
        for i in s.exec(select(Invoice)).all():
            if i.discrepancy_json:
                i.discrepancy_json = None
                s.add(i)
                n += 1
        if n:
            s.commit()


def _fix_maua_merge():
    """Once (06.11.2026): Maua Agritech FL/DL/26/001 is ONE delivery, ONE invoice, ONE payment, kept on 074-48014956:
    box 1 — Giselle, Britney, Birdnest, Darlene, Gentle Trendsetter 70cm × 40 st; box 2 — Fireworks 70cm 180 st, which
    Expolanka flew on 074-48015004. The invoice also carries Maua's 5 kg of 5004 freight (extra_freight)."""
    from .api import _settings, SETTINGS
    from .models import Invoice, Line
    from .autolog import note as _note
    from .kbreak import skip_farm
    if _settings().get("fix_maua_merge"):
        return
    with session() as s:
        invs = [i for i in s.exec(select(Invoice)).all() if i.farm == "Maua Agritech"
                and (i.invoice_no or "").startswith("FL/DL/26/001")]
        p1 = next((i for i in invs if " / часть" not in i.invoice_no), None)
        if not p1:
            SETTINGS.write_text(json.dumps({**_settings(), "fix_maua_merge": True}, ensure_ascii=False))
            return
        for p2 in [i for i in invs if i.id != p1.id]:
            for f in ("usd_paid", "rub_paid_override", "invoice_total_usd", "farm_usd", "est_usd"):
                v2 = getattr(p2, f)
                if v2:
                    setattr(p1, f, round((getattr(p1, f) or 0) + v2, 2))
            for l in s.exec(select(Line).where(Line.invoice_id == p2.id)).all():
                s.delete(l)
            s.delete(p2)
        sorts = ["Spray Rose Giselle 70cm", "Spray Rose Britney 70cm", "Spray Rose Birdnest 70cm",
                 "Spray Rose Darlene 70cm", "Spray Rose Gentle Trendsetter 70cm"]
        have = {l.name: l for l in s.exec(select(Line).where(Line.invoice_id == p1.id)).all()}
        for n, name in enumerate(sorts):
            l = have.get(name) or Line(invoice_id=p1.id, name=name, price_usd=0.6)
            l.stems, l.boxes, l.price_usd = 40, (1 if n == 0 else None), 0.6
            s.add(l)
        fw = have.get("Spray Rose Fireworks 70cm") or Line(invoice_id=p1.id, name="Spray Rose Fireworks 70cm")
        fw.stems, fw.boxes, fw.price_usd = 180, 1, 0.35       # box 2: Fireworks $0.35 × 180 = $63
        s.add(fw)
        p1.boxes_json = json.dumps([
            {"qty": 1, "pack": None, "content": [{"name": n, "stems_per_box": 40} for n in sorts]},
            {"qty": 1, "pack": None, "content": [{"name": "Spray Rose Fireworks 70cm", "stems_per_box": 180}]}],
            ensure_ascii=False)
        p1.alloc_mode = "value"                      # by price: $0.60 roses dearer, Fireworks $0.35 cheaper
        p1.invoice_total_usd = 183.0                 # both boxes: $120 + $63
        p1.farm_usd = None                           # reached the farm exactly by the invoice ($183)
        p1.usd_paid, p1.rub_paid_override = 191.0, None   # paid 191 USDT from the 12.10 top-up (proforma FL/DL/26/001)
        p1.note = ("FL/DL/26/001: 2 коробки ($183), одна оплата 191 USDT с пополнения 12.10. Коробка 2 (Fireworks 180 ст) "
                   "прилетела рейсом 074-48015004 — логистика взята с обоих рейсов (+5 кг с 5004)")
        s.add(p1)
        pid, farm, awb = p1.id, p1.farm, p1.awb
        _note(s, pid, farm, awb, "split", "один инвойс на 4956: 380 ст (2 коробки), логистика 4956 + 5 кг с 5004",
              "исправление 06.11")
        s.commit()
    st = _settings()
    ex = dict(st.get("extra_freight") or {})
    ex[str(pid)] = {"07448015004": 5.0}
    sg = {c: p for c, p in (st.get("split_groups") or {}).items() if int(p) != pid}
    SETTINGS.write_text(json.dumps({**st, "extra_freight": ex, "split_groups": sg, "fix_maua_merge": True},
                                   ensure_ascii=False))
    skip_farm("07448015004", "Maua Agritech")


def _fix_7081_actual():
    """Once (31.10.2026): MAWB 369-11530551 was billed by ACTUAL weight (TOTAL AWB 254 kg = actual 254.35),
    but the farms got their volumetric kg. Put the actual kg (only if the stored ones are still the old numbers)."""
    from .api import _settings, SETTINGS
    from .models import AwbWeights, Invoice
    if _settings().get("fix_7081_actual"):
        return
    old = {"B&M FIORI": (105.18,), "Nintanga": (12.4,), "Tessa": (21.8, 27.7), "Rosas Del Viento": (157.2,)}
    new = {"B&M FIORI": 84.0, "Nintanga": 9.9, "Tessa": 27.7, "Rosas Del Viento": 132.75}
    with session() as s:
        aw = s.get(AwbWeights, "36911530551")
        if aw:
            d = json.loads(aw.farm_kg_json or "{}")
            if all(any(abs((d.get(f) or 0) - v) < 0.01 for v in vs) for f, vs in old.items()):
                aw.farm_kg_json = json.dumps({**d, **new}, ensure_ascii=False)
                s.add(aw)
                for i in s.exec(select(Invoice)).all():
                    if (i.awb or "").replace("-", "") == "36911530551" and i.farm in new \
                            and any(abs((i.weight_kg or 0) - v) < 0.01 for v in old[i.farm]):
                        i.weight_kg = new[i.farm]
                        s.add(i)
        s.commit()
    SETTINGS.write_text(json.dumps({**_settings(), "fix_7081_actual": True}, ensure_ascii=False))


def _fix_positano_7081():
    """Once (31.10.2026): MAWB 369-11530551 (Logiztik LOG-0131 7081) — Positano Farms (= Tessa) was taken by its
    volumetric 21.8 kg, but its actual weight 27.7 kg is larger, and the chargeable weight is the larger one."""
    from .api import _settings, SETTINGS
    from .models import AwbWeights, Invoice
    st = _settings()
    if st.get("fix_positano_7081"):
        return
    with session() as s:
        aw = s.get(AwbWeights, "36911530551")
        if aw:
            d = json.loads(aw.farm_kg_json or "{}")
            if abs((d.get("Tessa") or 0) - 21.8) < 0.01:
                d["Tessa"] = 27.7
                aw.farm_kg_json = json.dumps(d, ensure_ascii=False)
                s.add(aw)
        for i in s.exec(select(Invoice)).all():
            if i.farm == "Tessa" and (i.awb or "").replace("-", "") == "36911530551" and abs((i.weight_kg or 0) - 21.8) < 0.01:
                i.weight_kg = 27.7
                s.add(i)
        s.commit()
    SETTINGS.write_text(json.dumps({**_settings(), "fix_positano_7081": True}, ensure_ascii=False))


def _fix_broker_swap():
    """Once (12.10.2026): farm invoices Tessa № 90836926 and Plazoleta № 177858 were attached to the OLDER statement
    purchases (90832981 / 177151) before invoice numbers were matched. Move MAWB, truck, arrival and packing to the
    purchase with the same number; the older purchase goes back to what it was (arrived, no MAWB)."""
    import re as _re
    from .api import _settings, SETTINGS
    from .models import Invoice
    if _settings().get("fix_broker_swap_1012"):
        return
    pairs = [("Tessa", "90836926", "90832981"), ("Plazoleta", "177858", "177151")]
    with session() as s:
        invs = [i for i in s.exec(select(Invoice)).all() if getattr(i, "via_broker", False) and i.ext_id]
        num = lambda i: _re.sub(r"\D", "", (i.invoice_no or "").split("#")[-1])
        for farm, right_no, wrong_no in pairs:
            right = next((i for i in invs if i.farm == farm and num(i).endswith(right_no)), None)
            wrong = next((i for i in invs if i.farm == farm and num(i).endswith(wrong_no)), None)
            if not right or not wrong or not wrong.packing_lines_json and not (wrong.awb or "").strip():
                continue
            awb = (wrong.awb or "").strip() or (right.awb or "").strip()
            right_arr = right.arrived_at
            right.awb = awb
            for k in ("truck", "eta", "hawb", "weight_kg", "client_eta", "client_done"):
                if getattr(wrong, k, None):
                    setattr(right, k, getattr(wrong, k))
            right.arrived_at = wrong.arrived_at
            if wrong.packing_lines_json and not right.packing_lines_json:
                right.packing_lines_json, right.boxes_json = wrong.packing_lines_json, wrong.boxes_json
            right.packing_sent = right.packing_sent or wrong.packing_sent
            wrong.awb, wrong.packing_lines_json, wrong.boxes_json, wrong.hawb = "", None, None, None
            wrong.truck, wrong.eta, wrong.client_eta = None, None, None
            wrong.arrived_at = right_arr or "до учёта в пути"
            s.add(right); s.add(wrong)
            print(f"[lumen] {farm}: MAWB {awb} moved from purchase {wrong.invoice_no} to {right.invoice_no}", flush=True)
        s.commit()
    SETTINGS.write_text(json.dumps({**_settings(), "fix_broker_swap_1012": True}))


def seed():
    _seed_farms()
    try:
        from . import kbreak
        n = kbreak.restore_missing_sheets()
        if n:
            print(f"[lumen] rebuilt {n} lost consolidation sheets", flush=True)
    except Exception as e:
        print(f"[lumen] restore sheets: {e}", flush=True)
    try:
        from .api import seed_picked
        seed_picked()
    except Exception as e:
        print(f"[lumen] seed_picked: {e}", flush=True)
    _fix_broker_swap()
    _fix_positano_7081()
    _fix_7081_actual()
    _fix_033_034()
    _fix_agri_5004_nocredit()
    _clear_discrepancies()
    _fix_maua_merge()
    _fix_agri_4956_kg()
    _fix_swapped_dates()
    _fix_double_broker()
    _merge_broker_dupes()
    _seed_packing_sigs()
    upsert_forwarders()          # forwarders + aliases for existing farms (idempotent)
    _seed_box_dims()


SEED_CREATED = {"Bliss Flora", "Blooming Dale", "Dale Flora", "Equator", "Flora Delight", "Flora Ola",
                "Molo River", "Pamoja", "Valentine", "Pj Flora", "Pj Flowers"}


def _seed_box_dims():
    """Box sizes Expolanka reported (WhatsApp, 09.2025–09.2026) -> registry, once.
    While the registry is OFF: undo an earlier seed (clear sizes, drop farms it created that have no invoices)."""
    from .api import _settings, SETTINGS, learn_dims
    from .volumetric import REGISTRY_ENABLED, SEED_TEXT, parse_message
    from .models import Invoice
    st = _settings()
    if not REGISTRY_ENABLED:
        if st.get("box_dims_seeded"):
            with session() as s:
                used = {i.farm.lower() for i in s.exec(select(Invoice)).all()}
                for f in s.exec(select(Farm)).all():
                    if f.name in SEED_CREATED and f.name.lower() not in used:
                        s.delete(f)
                    elif (f.box_dims_json or "{}") != "{}":
                        f.box_dims_json = "{}"
                        s.add(f)
                s.commit()
            SETTINGS.write_text(json.dumps({**_settings(), "box_dims_seeded": False}))
        return
    if st.get("box_dims_seeded"):
        return
    learn_dims(parse_message(SEED_TEXT))
    SETTINGS.write_text(json.dumps({**_settings(), "box_dims_seeded": True}))


def _seed_farms():
    with session() as s:
        if s.exec(select(Farm).where(Farm.is_forwarder == False)).first():  # noqa: E712
            return
        for country, names in SEED.items():
            for n in names:
                s.add(Farm(name=n, country=country, aliases=n.upper()))
        s.commit()


def _main_port() -> int:
    """Port the main server binds itself (so we don't grab it first)."""
    argv = sys.argv
    if "--port" in argv:
        return int(argv[argv.index("--port") + 1])
    return int(os.getenv("PORT", "8000"))


def _port_free(p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", p))
            return True
        except OSError:
            return False


@asynccontextmanager
async def lifespan(app):
    from .bot import bot, dp, setup_menu_button
    from . import backup

    # 1) container may be fresh after a rebuild -> pull data back from the Telegram channel
    try:
        print(f"[lumen] restore: {await backup.restore_if_empty(bot)}", flush=True)
    except Exception as e:
        print(f"[lumen] restore failed: {e}", flush=True)

    init_db()
    seed()
    from . import roles
    roles.seed()

    tasks = []
    opened = []
    main = _main_port()
    for p in EXTRA_PORTS:
        if p != main and _port_free(p):
            srv = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, lifespan="off", log_level="info"))
            srv.install_signal_handlers = lambda: None
            tasks.append(asyncio.create_task(srv.serve()))
            opened.append(p)
    print(f"[lumen] версия: {VERSION}", flush=True)
    print(f"[lumen] основной порт {main}, доп. порты: {opened}", flush=True)

    if bot:
        api.BOT = bot
        try:
            await setup_menu_button()
        except Exception as e:
            print(f"[lumen] menu button: {e}", flush=True)
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False,
                                                           allowed_updates=dp.resolve_used_update_types())))
        tasks.append(asyncio.create_task(backup.backup_loop(bot)))
        from .bot import scheduler_loop
        tasks.append(asyncio.create_task(scheduler_loop()))
        tasks.append(asyncio.create_task(_once_recalc_true_rate()))
        from . import reader
        if not reader.enabled():
            print("[lumen] бот-читатель ВЫКЛЮЧЕН: в .env нет READER_BOT_TOKEN", flush=True)
        if reader.enabled():                           # neutral second bot in the TK MSK chat
            rbot, rdp = reader.build()
            try:
                me = await rbot.get_me()
                print(f"[lumen] бот-читатель @{me.username} запущен (для чата ТК МСК)", flush=True)
            except Exception as e:
                print(f"[lumen] бот-читатель: неверный READER_BOT_TOKEN? {e}", flush=True)
            tasks.append(asyncio.create_task(rdp.start_polling(rbot, handle_signals=False,
                                                               allowed_updates=["message", "channel_post", "my_chat_member"])))
        from . import userbot
        if userbot.enabled():                          # invisible reader of the TK MSK chat
            from .bot import _handle_truck
            tasks.append(asyncio.create_task(userbot.run_forever(lambda text, sent: _handle_truck("", text, sent))))
    yield
    for t in tasks:
        t.cancel()
    if bot:
        try:
            await asyncio.wait_for(backup.flush(bot), timeout=20)
        except Exception as e:
            print(f"[lumen] backup on shutdown failed: {e}", flush=True)


VERSION = "2026-11-06l · 4956: Agriflora 150 кг по отчёту; сторож ловит расхождение кг"


async def _once_recalc_true_rate():
    """After switching to «истинный курс»: rebuild every top-up sheet once and send it to the system admins."""
    from .api import SETTINGS, _settings, export_topups
    from .models import TopUp
    from . import roles
    if _settings().get("true_rate_v2"):
        return
    await asyncio.sleep(10)
    with session() as s:
        ids = [t.id for t in s.exec(select(TopUp).order_by(TopUp.id)).all()]
    if not ids:
        return
    sent = False
    for uid in roles.sys_ids():
        try:
            await export_topups(ids, uid, "📊 Пересчёт по истинному курсу: все ₽ за инвойс (комиссия, налог, сборы) "
                                          "теперь в цене стебля. В строке ИТОГО у каждой фермы — истинный курс и косты, %.")
            sent = True
        except Exception as e:
            print(f"[lumen] recalc export to {uid} failed: {e}", flush=True)
    print(f"[lumen] пересчёт по истинному курсу: {'отправлен' if sent else 'НЕ отправлен'} ({len(ids)} пополнений)", flush=True)
    if sent:
        SETTINGS.write_text(json.dumps({**_settings(), "true_rate_v2": True}))


app = FastAPI(lifespan=lifespan)
app.include_router(api.router)


@app.middleware("http")
async def _changes_trigger_backup(request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        # Telegram's webview caches the page hard — always serve the fresh Mini App
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    if request.method != "GET" and request.url.path.startswith("/api") and resp.status_code < 400:
        from .backup import mark_dirty
        mark_dirty()
    return resp


@app.get("/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "webapp", html=True), name="webapp")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
