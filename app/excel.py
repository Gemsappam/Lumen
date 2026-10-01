"""
Writes one 'Пополнение DD.MM' sheet into the master workbook (keeps all other sheets),
and adds/updates the row in 'Пополнения'. Formulas stay live: change a $ or a freight
sum in Excel and the себестоимость recalculates.
"""
from collections import defaultdict
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as CL

from .calc import compute, norm_awb, rate_of

HEAD = ["Страна", "Маркировка", "Номер / Дата инвойса", "MAWB", "Плантация", "Номенклатура",
        "Кол-во коробок", "Кол-во стеблей", "Вес (кг)", "Цена за цветок ($)", "Цена за цветок (Р)",
        "ТК Кения ($/стебель)", "ТК Кения (Р/стебель)", "ТК МСК (Р/стебель)",
        "Общая себестоимость стебля (Р)", "Сумма в $ (оплачено)", "Сумма в Р (оплачено)",
        "Оплачено", "Дата оплаты", "МРЦ", "Доля ТК Кения по MAWB", "Доля ТК МСК по MAWB"]
MERGE_COLS = [1, 2, 3, 4, 5, 16, 17, 18, 19]          # merged down each invoice block, like the original

F = "Arial"
BLUE = Font(name=F, color="0000FF")
BLACK = Font(name=F)
BOLD = Font(name=F, bold=True)
GREY = Font(name=F, color="808080", italic=True)
HFILL = PatternFill("solid", fgColor="D9E1F2")
YFILL = PatternFill("solid", fgColor="FFF2CC")
thin = Side(style="thin", color="BFBFBF")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
RUB = '#,##0.00'
RUB4 = '#,##0.0000'          # per-stem ₽: 4 decimals so 1С gets the exact number (cells hold full precision anyway)
RUB0 = '#,##0'
USD = '0.00##'
ACC_COST = PatternFill("solid", fgColor="E2EFDA")     # себестоимость — зелёный акцент
ACC_TOT = PatternFill("solid", fgColor="FCE4D6")      # итого коробки / вес — персиковый акцент
ACC_GRAND = PatternFill("solid", fgColor="F8CBAD")
TOTAL_FONT = Font(name=F, bold=True, size=11)


def hide_carriers(text: str) -> str:
    """Carrier names never appear in the export: Expolanka -> ТК Кения, Floratrack -> ТК МСК."""
    for a, b in (("Expolanka Freight Limited", "ТК Кения"), ("EXPOLANKA", "ТК Кения"), ("Expolanka", "ТК Кения"),
                 ("Floratrack", "ТК МСК"), ("FLORATRACK", "ТК МСК"), ("Флоратрак", "ТК МСК"), ("Floratrak", "ТК МСК")):
        text = text.replace(a, b)
    return text


def _sheet_name(wb, topup):
    base = f"Пополнение {topup.date[:5]}"
    if topup.sheet_name and topup.sheet_name in wb.sheetnames:
        return topup.sheet_name
    name, n = base, 2
    while name in wb.sheetnames:
        name, n = f"{base} ({n})", n + 1
    return name


def _topups_row(wb, topup):
    """Row of this top-up in 'Пополнения' (created if missing). Returns row number."""
    if "Пополнения" not in wb.sheetnames:
        ws = wb.create_sheet("Пополнения")
        for c, h in enumerate(["Дата пополнения", "Сумма в Руб (пополнение)", "",
                               "Сумма в $ (пополнение)", "Курс"], 1):
            ws.cell(1, c, h).font = BOLD
    ws = wb["Пополнения"]
    row = None
    for r in range(2, ws.max_row + 1):
        v = ws.cell(r, 1).value
        v = v.strftime("%d.%m.%Y") if hasattr(v, "strftime") else str(v or "").strip()
        if v == topup.date:
            row = r
    if row is None:
        row = ws.max_row + 1
        while row > 2 and not any(ws.cell(row - 1, c).value for c in range(1, 6)):
            row -= 1
    ws.cell(row, 1, topup.date)
    ws.cell(row, 2, topup.rub).font = BLUE
    ws.cell(row, 4, topup.usd).font = BLUE
    ws.cell(row, 5, f"=B{row}/D{row}")
    return row


def build(path, topup, topups, invoices, lines, logistics, out_path=None, awb_kg=None, awb_breakdown=None,
          operator=False):
    """operator=True: version for the 1С operator — plain numbers instead of formulas, no «Пополнения» sheet,
    no ТК share columns."""
    try:
        wb = load_workbook(path)
    except Exception:
        wb = Workbook()
        wb.remove(wb.active)

    res = compute(topup.id, topups, invoices, lines, logistics, awb_kg)
    trow = None if operator else _topups_row(wb, topup)
    name = _sheet_name(wb, topup)
    if name in wb.sheetnames:
        idx = wb.sheetnames.index(name)
        del wb[name]
        ws = wb.create_sheet(name, idx)
    else:
        idx = wb.sheetnames.index("Пополнения") if "Пополнения" in wb.sheetnames else len(wb.sheetnames)
        ws = wb.create_sheet(name, idx)
    topup.sheet_name = name

    # header
    for c, h in enumerate(HEAD[:20] if operator else HEAD, 1):
        cell = ws.cell(1, c, h)
        cell.font, cell.fill, cell.alignment, cell.border = BOLD, HFILL, CENTER, BOX
    ws.row_dimensions[1].height = 45
    ws["A2"], ws["B2"] = "Курс пополнения", (round(res.rate, 4) if operator else f"='Пополнения'!E{trow}")
    ws["B2"].number_format = "0.0000"
    ws["A2"].font = BOLD
    ws["B2"].font = Font(name=F, color="008000")

    own_invs = [i for i in invoices if i.topup_id == topup.id]
    lines_by = defaultdict(list)
    for l in lines:
        lines_by[l.invoice_id].append(l)

    # logistics section goes after the blocks; we need its rows before writing line formulas
    n_rows = sum(len(lines_by[i.id]) + 2 for i in own_invs if lines_by[i.id])
    grand_row = 4 + n_rows
    leg_start = grand_row + 3
    leg_keys = []
    for inv in own_invs:
        for leg in ("air", "msk"):
            k = (norm_awb(inv.awb), leg)
            if k in res.legs and k not in leg_keys:
                leg_keys.append(k)
    for (awb, leg), g in res.legs.items():   # freight paid in this top-up for AWBs of older top-ups
        if (awb, leg) not in leg_keys and any(lg.topup_id == topup.id for lg in logistics if lg.id in g["ids"]):
            leg_keys.append((awb, leg))
    awb_breakdown = awb_breakdown or {}
    leg_row, bd_rows, pos, shown = {}, {}, leg_start + 1, set()
    for k in leg_keys:
        leg_row[k] = pos
        pos += 1
        if k[0] not in shown and awb_breakdown.get(k[0]):        # farm breakdown once per MAWB
            shown.add(k[0])
            bd_rows[k] = (pos, awb_breakdown[k[0]])
            pos += len(awb_breakdown[k[0]])
    leg_end = pos

    usd_cells = []
    block_tot = []
    r = 4
    for inv in own_invs:
        ls = lines_by[inv.id]
        if not ls:
            continue
        r0, r1 = r, r + len(ls) - 1
        rub_paid = res.invoice_rub[inv.id]
        for j, l in enumerate(ls):
            rr = r0 + j
            lc = res.lines[l.id]
            vals = {6: l.name, 7: l.boxes, 8: l.stems, 9: l.weight_kg, 10: l.price_usd, 20: l.mrc}
            for c, v in vals.items():
                if v not in (None, ""):
                    ws.cell(rr, c, v).font = BLUE
            ka, km = (norm_awb(inv.awb), "air"), (norm_awb(inv.awb), "msk")
            if operator:                          # plain numbers, no shares
                for c, v in ((11, lc.price_rub), (12, lc.air_usd_stem), (13, lc.air_rub_stem),
                             (14, lc.msk_rub_stem), (15, lc.total_rub_stem)):
                    ws.cell(rr, c, round(v, 6) if v else (0 if c in (11, 15) else None))
            elif inv.alloc_mode == "stems":
                ws.cell(rr, 11, f"=IFERROR($Q${r0}/SUM($H${r0}:$H${r1}),0)")
            else:
                ws.cell(rr, 11, f"=IFERROR(J{rr}*$Q${r0}/SUMPRODUCT($J${r0}:$J${r1},$H${r0}:$H${r1}),0)")
            if operator:
                pass
            elif ka in leg_row:
                ws.cell(rr, 21, round(lc.air_share, 8)).font = GREY
                ws.cell(rr, 12, f"=IFERROR($E${leg_row[ka]}*U{rr}/H{rr},0)")
                ws.cell(rr, 13, f"=IFERROR($F${leg_row[ka]}*U{rr}/H{rr},0)")
            if km in leg_row and not operator:
                ws.cell(rr, 22, round(lc.msk_share, 8)).font = GREY
                ws.cell(rr, 14, f"=IFERROR($F${leg_row[km]}*V{rr}/H{rr},0)")
            if not operator:
                ws.cell(rr, 15, f"=K{rr}+M{rr}+N{rr}")
            ws.cell(rr, 15).fill = ACC_COST
            for c, fmt in ((10, USD), (11, RUB4), (12, "0.0000"), (13, RUB4), (14, RUB4), (15, RUB4), (21, "0.0000%"), (22, "0.0000%")):
                if operator and c > 20:
                    continue
                ws.cell(rr, c).number_format = fmt
            ws.cell(rr, 15).font = BOLD
            for c in range(1, 21 if operator else 23):
                ws.cell(rr, c).border = BOX
                if not ws.cell(rr, c).font or ws.cell(rr, c).font.name != F:
                    ws.cell(rr, c).font = BLACK

        head = {1: inv.country, 2: inv.client_code,
                3: " / ".join(x for x in (inv.invoice_no, inv.invoice_date) if x),
                4: inv.awb, 5: inv.farm, 16: inv.usd_paid,
                18: "да" if inv.paid else "нет", 19: inv.paid_date}
        for c, v in head.items():
            ws.cell(r0, c, v).font = BLUE if c == 16 else BLACK
        if inv.rub_paid_override is not None:
            ws.cell(r0, 17, inv.rub_paid_override).font = BLUE
        else:
            ws.cell(r0, 17, rub_paid if operator else f"=ROUND(P{r0}*$B$2,0)")
            ws.cell(r0, 17).comment = Comment("₽ не внесены оператором — посчитано $ × курс пополнения", "bot")
        ws.cell(r0, 16).number_format = USD
        ws.cell(r0, 17).number_format = RUB0
        if inv.note:
            ws.cell(r0, 6).comment = Comment(hide_carriers(inv.note), "bot")
        if r1 > r0:
            for c in MERGE_COLS:
                ws.merge_cells(start_row=r0, start_column=c, end_row=r1, end_column=c)
        for c in MERGE_COLS:
            ws.cell(r0, c).alignment = CENTER
        usd_cells.append(f"P{r0}")
        # ИТОГО: коробки, стебли, вес плантации (весь её вес в MAWB), себестоимость партии
        t = r1 + 1
        ws.cell(t, 6, "ИТОГО").font = TOTAL_FONT
        ws.cell(t, 7, f"=SUM(G{r0}:G{r1})").font = TOTAL_FONT
        ws.cell(t, 7).fill = ACC_TOT
        ws.cell(t, 8, f"=SUM(H{r0}:H{r1})").font = TOTAL_FONT
        if inv.weight_kg:
            ws.cell(t, 9, inv.weight_kg).font = Font(name=F, bold=True, color="0000FF")
        elif any(l.weight_kg for l in ls):
            ws.cell(t, 9, f"=SUM(I{r0}:I{r1})").font = TOTAL_FONT
        ws.cell(t, 9).fill = ACC_TOT
        ws.cell(t, 9).comment = Comment("Итого вес плантации в MAWB, кг", "bot")
        block_tot.append(t)
        ws.cell(t, 10, f"=SUMPRODUCT(J{r0}:J{r1},H{r0}:H{r1})").font = GREY
        ws.cell(t, 10).number_format = USD
        ws.cell(t, 10).comment = Comment("Сумма строк цветов, $", "bot")
        # истинный курс партии и косты оплаты
        ws.cell(t, 11, f"=IFERROR(Q{r0}/J{t},0)").number_format = "0.0000"
        ws.cell(t, 11).font = TOTAL_FONT
        ws.cell(t, 11).fill = ACC_TOT
        ws.cell(t, 11).comment = Comment("Истинный курс: все ₽ за инвойс ÷ $ строк цветов (с комиссией, налогом, сборами)", "bot")
        ws.cell(t, 12, f'=IFERROR(P{r0}/J{t}-1,0)').number_format = "0.0%"
        ws.cell(t, 12).font = GREY
        ws.cell(t, 12).comment = Comment("Косты оплаты сверх строк цветов (комиссия, налог, doc fee), %", "bot")
        r = t + 2

    if block_tot:
        g = grand_row
        ws.cell(g, 6, "ИТОГО ПО ПОПОЛНЕНИЮ").font = TOTAL_FONT
        for c, fmt in ((7, "0"), (8, "#,##0"), (9, "#,##0.0")):
            ws.cell(g, c, "=" + "+".join(f"{CL(c)}{t}" for t in block_tot)).font = TOTAL_FONT
            ws.cell(g, c).number_format = fmt
            ws.cell(g, c).fill = ACC_COST if c == 15 else ACC_GRAND
        for c in range(6, 16):
            ws.cell(g, c).border = Border(top=Side(style="medium"), bottom=Side(style="medium"))

    # logistics section (carriers shown only as ТК Кения / ТК МСК)
    ws.cell(leg_start - 1, 1, "ЛОГИСТИКА").font = BOLD
    for c, h in enumerate(["MAWB", "ТК", "", "Вес ТК, кг", "Сумма $", "Сумма Р", "Откуда", "Доля"], 1):
        ws.cell(leg_start, c, h).font = BOLD
        ws.cell(leg_start, c).fill = HFILL
    log_usd_cells = []
    provisional = []
    for (awb, leg), rr in leg_row.items():
        g = res.legs[(awb, leg)]
        ws.cell(rr, 1, f"{awb[:3]}-{awb[3:]}" if len(awb) == 11 and awb.isdigit() else awb)
        ws.cell(rr, 2, "ТК Кения" if leg == "air" else "ТК МСК").font = BOLD
        if g.get("kg_bill"):
            ws.cell(rr, 4, g["kg_bill"]).font = Font(name=F, bold=True, color="0000FF")
        elif g.get("kg_total"):
            ws.cell(rr, 4, g["kg_total"]).font = TOTAL_FONT
        ws.cell(rr, 4).fill = ACC_TOT
        ws.cell(rr, 4).number_format = "#,##0.0"
        ws.cell(rr, 5, g["usd"] or None).font = BLUE
        if (awb, leg) in bd_rows:                        # farm breakdown of this MAWB
            start, bd = bd_rows[(awb, leg)]
            tot = sum(bd.values()) or 1
            for n, (farm, kg) in enumerate(sorted(bd.items(), key=lambda x: -x[1])):
                br = start + n
                ws.cell(br, 3, f"  {farm}").font = GREY
                ws.cell(br, 4, kg).font = BLUE
                ws.cell(br, 4).number_format = "#,##0.0"
                ws.cell(br, 8, kg / tot).number_format = "0.0%"
                ws.cell(br, 8).font = GREY
        own_usd = sum((lg.usd or 0) for lg in logistics if lg.id in g["ids"] and lg.topup_id == topup.id)
        if g["own"] and g["usd"] and not any(lg.rub is not None for lg in logistics if lg.id in g["ids"]):
            ws.cell(rr, 6, f"=ROUND(E{rr}*$B$2,0)")
            ws.cell(rr, 7, "это пополнение")
        else:
            ws.cell(rr, 6, round(g["rub"], 2)).font = BLUE
            recs = [lg for lg in logistics if lg.id in g["ids"]]
            ft = [lg for lg in recs if (lg.ext_key or "").startswith("ft:")]
            rate = g["rub"] / g["usd"] if g["usd"] else 0
            if any("предварительн" in (lg.note or "") for lg in ft):
                ws.cell(rr, 7, f"курс {rate:.2f} ПРЕДВАРИТЕЛЬНЫЙ — оплаты ещё нет, (ЦБ + 3) / 0.96; "
                               f"уточнится со следующим отчётом ТК").font = Font(name=F, color="C00000")
                ws.cell(rr, 6).fill = YFILL
                provisional.append(f"MAWB {awb}: ТК МСК по предварительному курсу {rate:.2f} — перекинь боту следующий отчёт ТК МСК")
            elif ft:
                ws.cell(rr, 7, f"курс {rate:.2f} по оплатам из баланса ТК").font = GREY
            else:
                ws.cell(rr, 7, f"оплачено, курс {rate:.2f}").font = GREY
        if own_usd:
            log_usd_cells.append(f"E{rr}" if own_usd == g["usd"] else str(own_usd))
        ws.cell(rr, 5).number_format = USD
        ws.cell(rr, 6).number_format = RUB0

    # balance ("Остаток") intentionally not written: only the system super-admin sees it, in the app

    # warnings
    wr = leg_end + 2
    import re as _re
    warns = [_re.sub(r"MAWB (\d{3})(\d{8})", r"MAWB \1-\2", hide_carriers(w)) for w in provisional + res.warnings]
    if warns and not operator:
        ws.cell(wr, 1, "ПРОВЕРИТЬ").font = Font(name=F, bold=True, color="C00000")
        for n, w in enumerate(warns, 1):
            ws.cell(wr + n, 1, w).fill = YFILL

    widths = [14, 11, 18, 16, 18, 34, 9, 10, 9, 11, 12, 12, 12, 12, 14, 12, 13, 10, 12, 10, 12, 12]
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[CL(c)].width = w
    ws.freeze_panes = "G4"

    for row in ws.iter_rows():                  # no cell notes (red corners / popups) in the export
        for c in row:
            if c.comment:
                c.comment = None
    wb.save(out_path or path)
    return name, res
