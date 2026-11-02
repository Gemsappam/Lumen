#!/usr/bin/env python3
"""Regression tests on REAL documents and a real copy of the books. Run before every release:

    python tests/run_tests.py            # check — exit code 1 and a list of differences if something changed
    python tests/run_tests.py --update   # accept the current results as the new reference (after an INTENDED change)

What is checked:
  1. code: every module is valid for Python 3.11 (Bothost) and imports
  2. readers: Expolanka lists (old «AWB» and new «MAWB» format), Logiztik weight reports, prealert, Floratrack
     report, BiFlorica statement, farm statement — recognised as exactly their type, key numbers right
  3. «fail loudly»: every known format is recognised by the sniffer
  4. books: the cost of every stem (flower / ТК Кения / ТК МСК / доставка до Бауманской) on a copy of the books
     equals the reference to 0.01 ₽; no ТК bill spread over 100 %; the operator / owner Excel builds for every top-up
The books copy lives in tests/fixtures/db and is never modified (tests work on a temp copy).
"""
import ast
import io
import json
import os
import shutil
import sys
import tempfile
import tokenize
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
GOLDEN = ROOT / "tests" / "golden.json"
UPDATE = "--update" in sys.argv

TMP = Path(tempfile.mkdtemp(prefix="lumen_test_"))
shutil.copytree(FIX / "db", TMP, dirs_exist_ok=True)
os.environ.update({"DATA_DIR": str(TMP), "DEV_NO_AUTH": "1", "BOT_TOKEN": "", "OPENROUTER_API_KEY": "",
                   "ANTHROPIC_API_KEY": "", "BACKUP_CHAT_ID": "", "LOGIZTIK_TOKEN": ""})
sys.path.insert(0, str(ROOT))

FAILS, RESULTS = [], {}


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}{': ' + str(detail) if detail else ''}")


def f(name):
    return (FIX / "files" / name).read_bytes()


# ---------------------------------------------------------------- 1. code -----------------------------------------
def py311_problems(path: Path) -> list:
    src = path.read_text(encoding="utf-8")
    out = []
    try:
        ast.parse(src, feature_version=(3, 11))
    except SyntaxError as e:
        out.append(f"{path.name}:{e.lineno} {e.msg}")
    stack = []                                   # f-strings: 3.12-only quoting / backslashes
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == getattr(tokenize, "FSTRING_START", -1):
            q = tok.string.lstrip("rRfFbBuU")[0]
            if stack and q == stack[-1]:
                out.append(f"{path.name}:{tok.start[0]} f-string nested same quote (3.12 only)")
            stack.append(q)
        elif tok.type == getattr(tokenize, "FSTRING_END", -2):
            stack.pop()
        elif stack and tok.type != getattr(tokenize, "FSTRING_MIDDLE", -3):
            if "\\" in tok.string:
                out.append(f"{path.name}:{tok.start[0]} backslash inside f-string {{}} (3.12 only)")
            if tok.type == tokenize.STRING and tok.string.lstrip("rRbBuU")[:1] == stack[-1]:
                out.append(f"{path.name}:{tok.start[0]} same quote inside f-string (3.12 only)")
    return out


def test_code():
    for p in sorted((ROOT / "app").glob("*.py")):
        for prob in py311_problems(p):
            FAILS.append("python 3.11: " + prob)
    import importlib
    for mod in ("config", "models", "calc", "api", "excel", "kbreak", "packing", "floratrack", "biflorica",
                "lastmile", "watch", "autosend", "sniff", "statement", "xltext", "bot", "main"):
        try:
            importlib.import_module("app." + mod)
        except Exception as e:
            FAILS.append(f"import app.{mod}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 2. readers --------------------------------------
def test_readers():
    from openpyxl import load_workbook
    from app import kbreak, biflorica, floratrack, sniff
    from app.bot import _looks_like_statement
    from app.xltext import excel_to_text
    files = {
        "expolanka_25bxs_new_MAWB.xlsx": "expolanka", "expolanka_27bxs_old.xlsx": "expolanka",
        "weight_LOG_7081.xlsx": "weight", "weight_LOG_1361.xlsx": "weight",
        "prealert_36999583094.xlsx": "prealert", "floratrack_report.xlsx": "floratrack",
        "biflorica_02oct.xlsx": "biflorica", "statement_nintanga.xlsx": None,
    }
    for name, kind in files.items():
        d = f(name)
        got = {"expolanka": kbreak.is_breakdown(d), "weight": kbreak.is_weight_report(d),
               "prealert": kbreak.is_prealert(d), "biflorica": biflorica.is_statement(d),
               "floratrack": floratrack.is_floratrack(load_workbook(io.BytesIO(d), read_only=True))}
        hits = [k for k, v in got.items() if v]
        check(f"{name}: распознан как {kind or 'ничего из листов'}", hits == ([kind] if kind else []), f"распознан как {hits}")
        g = sniff.guess(d)
        if kind:
            check(f"{name}: сниффер (громкая ошибка) узнаёт формат", g and g[0] == kind, g)

    # Expolanka — new format with «MAWB» column, farm in the first column (the 02.11 packing that got stuck)
    i = kbreak.parse(f("expolanka_25bxs_new_MAWB.xlsx"))
    check("Expolanka 25bxs: MAWB", i["awb"] == "065-45087615", i["awb"])
    check("Expolanka 25bxs: 25 коробок", i["packs"] == 25, i["packs"])
    check("Expolanka 25bxs: 5 ферм", len(i["rows"]) == 5, len(i["rows"]))
    RESULTS["expolanka_25bxs"] = {"awb": i["awb"], "rows": [[r["farm_raw"], r["packs"], r["weight"], r["vw"]] for r in i["rows"]]}
    i = kbreak.parse(f("expolanka_27bxs_old.xlsx"))
    check("Expolanka 27bxs (старый формат): 27 коробок", i["packs"] == 27, i["packs"])
    RESULTS["expolanka_27bxs"] = {"awb": i["awb"], "rows": [[r["farm_raw"], r["packs"], r["weight"], r["vw"]] for r in i["rows"]]}

    # Logiztik weight report: farm kg by the column the AWB was billed by (TOTAL AWB 254 = actual)
    w = kbreak.parse_weight_report(f("weight_LOG_7081.xlsx"))
    nin = next((r["kg"] for r in w["rows"] if "NINTANGA" in r["farm_raw"].upper()), None)
    check("LOG-7081: AWB выставлена за 254 кг", w.get("awb_kg") == 254, w.get("awb_kg"))
    check("LOG-7081: колонка фактического веса", w["use"] == "act", w["use"])
    check("LOG-7081: Nintanga 9.9 кг", nin == 9.9, nin)
    check("LOG-7081: сумма кг ферм ≈ весу AWB", abs(sum(r["kg"] for r in w["rows"]) - 254) < 1)
    RESULTS["weight_7081"] = [[r["farm_raw"], r["packs"], r["kg"]] for r in w["rows"]]
    w = kbreak.parse_weight_report(f("weight_LOG_1361.xlsx"))
    check("LOG-1361: есть строки ферм", len(w["rows"]) > 0)
    RESULTS["weight_1361"] = {"use": w["use"], "awb_kg": w.get("awb_kg"), "rows": [[r["farm_raw"], r["packs"], r["kg"]] for r in w["rows"]]}

    p = kbreak.parse_prealert(f("prealert_36999583094.xlsx"))
    check("Преалерт: AWB 369-99583094", "".join(ch for ch in str(p["awb"]) if ch.isdigit()) == "36999583094", p["awb"])
    RESULTS["prealert"] = {"awb": p["awb"], "country": p["country"],
                           "rows": [[r.get("farm_raw"), r.get("packs"), r.get("kg")] for r in p["rows"]]}

    b = biflorica.parse(f("biflorica_02oct.xlsx"))
    check("BiFlorica: есть операции", len(b["ops"]) > 0)
    RESULTS["biflorica"] = {"opening": b["opening"], "deps": round(sum(o["usd"] for o in b["ops"] if o["kind"] == "dep"), 2),
                            "buys": round(sum(o["amount"] for o in b["ops"] if o["kind"] == "buy"), 2),
                            "n": len(b["ops"])}

    r = floratrack.parse(f("floratrack_report.xlsx"), today=(2026, 10, 9))
    check("Floratrack: есть машины", len(r.charges) > 0)
    RESULTS["floratrack"] = {"n": len(r.charges), "balance": round(r.balance_usd, 2),
                             "charges": sorted([c.last4, c.kind, c.kg, round(c.usd, 2), round(c.rub, 2), c.provisional]
                                               for c in r.charges)}

    t = excel_to_text(f("statement_nintanga.xlsx"), "statement.xlsx")
    check("Стейтмент Nintanga узнаётся как стейтмент", _looks_like_statement(t.encode(), "text/plain"))


# ---------------------------------------------------------------- 3. books ----------------------------------------
def test_books():
    from app.api import _all, _awb_kg, _awb_breakdown
    from app.calc import compute, norm_awb
    from app.models import session, TopUp
    from app import excel, watch, autosend
    with session() as s:
        tops, invs, lines, logs = _all(s)
        res = compute(0, tops, invs, lines, logs, _awb_kg(s))
        cost = {}
        for i in sorted(invs, key=lambda x: x.id):
            ls = [l for l in lines if l.invoice_id == i.id]
            st = sum(l.stems for l in ls) or 1
            part = lambda a: round(sum(getattr(res.lines[l.id], a) * l.stems for l in ls) / st, 2)
            cost[str(i.id)] = [i.farm, norm_awb(i.awb) if i.awb else "", st, part("price_rub"), part("air_rub_stem"),
                               part("msk_rub_stem"), part("lm_rub_stem")]
        RESULTS["cost_per_stem"] = cost
        # Excel for every top-up, both versions
        kg, bd = _awb_kg(s), _awb_breakdown(s)
        for t in tops:
            for op in (True, False):
                out = TMP / f"x_{t.id}_{op}.xlsx"
                try:
                    excel.build(out, t, tops, invs, lines, logs, awb_kg=kg, awb_breakdown=bd, operator=op)
                except Exception as e:
                    FAILS.append(f"Excel {'оператора' if op else 'учёта'} «{t.date}»: {type(e).__name__}: {e}")
                    traceback.print_exc()
        s.rollback()
    issues = watch.run(save=False)
    check("Сторож: ни один счёт ТК не разложен больше 100%", not [x for x in issues if "разложено" in x],
          [x for x in issues if "разложено" in x])
    RESULTS["autosend_ready"] = sorted(autosend.stages())


# ---------------------------------------------------------------- run ---------------------------------------------
def diff(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}/{k}: новое {json.dumps(b[k], ensure_ascii=False)[:160]}")
            elif k not in b:
                out.append(f"{path}/{k}: пропало (было {json.dumps(a[k], ensure_ascii=False)[:160]})")
            else:
                out += diff(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, float) or isinstance(b, float):
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= 0.01):
            out.append(f"{path}: было {a} → стало {b}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for n, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{n}]")
    elif a != b:
        out.append(f"{path}: было {json.dumps(a, ensure_ascii=False)[:160]} → стало {json.dumps(b, ensure_ascii=False)[:160]}")
    return out


def main():
    for t in (test_code, test_readers, test_books):
        try:
            t()
        except Exception as e:
            FAILS.append(f"{t.__name__} упал: {type(e).__name__}: {e}")
            traceback.print_exc()
    data = json.loads(json.dumps(RESULTS, ensure_ascii=False, default=str))
    if UPDATE:
        GOLDEN.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        print(f"Эталон обновлён: {GOLDEN}")
    elif GOLDEN.exists():
        d = diff(json.loads(GOLDEN.read_text()), data)
        if d:
            FAILS.append(f"Результаты отличаются от эталона ({len(d)}):\n    " + "\n    ".join(d[:60])
                         + ("\n    …" if len(d) > 60 else "")
                         + "\n    Если изменение задумано — python tests/run_tests.py --update")
    else:
        FAILS.append("нет эталона — запусти с --update")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print("❌ ТЕСТЫ НЕ ПРОШЛИ:\n- " + "\n- ".join(FAILS))
        sys.exit(1)
    print(f"✅ Все тесты прошли: код, {len(RESULTS) - 2} форматов файлов, себестоимость "
          f"{len(RESULTS.get('cost_per_stem', {}))} инвойсов, Excel по всем пополнениям.")


if __name__ == "__main__":
    main()
