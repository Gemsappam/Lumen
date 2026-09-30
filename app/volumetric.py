"""
Expolanka charges VOLUMETRIC weight: one box L×W×H cm / 6000 = kg.
  100.48.25 → 20.0 kg, 100.40.25 → 16.7 kg, 100.60.21 → 21.0 kg ...

Their WhatsApp messages look like:
  "Zeeflora - 11 - 100.48.25"      "Zee flora 16(100.48.25)"     "(100.48.25)24"
  "Tambuzi - 2 - 100.40.25\\n- 1 - 102.35.28"                     "Tambuzi  2 100,40,28\\n2 100,60,21"
  "Kikwetu 3 100.48.28"            "Heritage - 12boxes 100.40.25"  "zee flora  23.100.48.25"
parse_message() turns any of these into per-farm boxes + kg. A line with a farm and a count but no
size ("Zeeflora 11") uses that farm's usual box from the registry.
"""
import re
from collections import Counter

DIVISOR = 6000

DIM = r"(9\d|1[0-4]\d)\s*[.,x×*]\s*(\d{2})\s*[.,x×*]\s*(\d{2})(?!\d)"   # box length 90–149 cm (never a date)
DIM_RE = re.compile(DIM)
PAREN_DIM_COUNT = re.compile(r"\(\s*" + DIM + r"\s*\)\s*(\d{1,3})\b")          # (100.48.25)24
COUNT_PAREN_DIM = re.compile(r"(\d{1,3})\s*\(\s*" + DIM + r"\s*\)")            # 16(100.48.25)
COUNT_DIM = re.compile(r"(?<![\d.,])(\d{1,3})\s*(?:boxes|bxs|box|кор\w*)?\s*[-–,.>]?\s*" + DIM)  # 11 - 100.48.25 / 23.100.48.25
NAME_RE = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё .'&]+")
NOISE = {"boxes", "bxs", "box", "gross", "tare", "net", "kgs", "kg", "minus", "tare weight", "weight", "diluna",
         "diluna weight", "tentative", "breakdown", "bulgy", "and", "please", "for"}


def vol_kg(l: int, w: int, h: int) -> float:
    return round(l * w * h / DIVISOR, 2)


def dims_key(l, w, h) -> str:
    return f"{int(l)}.{int(w)}.{int(h)}"


def _name(line: str) -> str:
    """Farm name on a line (letters only), without service words."""
    cand = [m.group(0).strip(" .-'") for m in NAME_RE.finditer(DIM_RE.sub(" ", line))]
    cand = [c for c in cand if c and c.lower() not in NOISE and len(c) > 2]
    return cand[0] if cand else ""


def parse_message(text: str, usual_dims=None):
    """-> [{"farm", "boxes", "dims": [(n, "L.W.H", kg_per_box)], "kg"}]; usual_dims(farm) -> "L.W.H" or None."""
    farms, cur = [], None

    def add(farm, n, l, w, h):
        nonlocal cur
        if farm:
            cur = next((f for f in farms if f["farm"].lower() == farm.lower()), None)
            if not cur:
                cur = {"farm": farm, "boxes": 0, "dims": [], "kg": 0.0}
                farms.append(cur)
        if cur is None:
            cur = {"farm": "?", "boxes": 0, "dims": [], "kg": 0.0}
            farms.append(cur)
        k = vol_kg(int(l), int(w), int(h))
        cur["boxes"] += int(n)
        cur["dims"].append((int(n), dims_key(l, w, h), k))
        cur["kg"] = round(cur["kg"] + int(n) * k, 2)

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        name = _name(line)
        hits = ([(m.group(4), *m.group(1, 2, 3)) for m in PAREN_DIM_COUNT.finditer(line)] or
                [(m.group(1), *m.group(2, 3, 4)) for m in COUNT_PAREN_DIM.finditer(line)] or
                [(m.group(1), *m.group(2, 3, 4)) for m in COUNT_DIM.finditer(line)])
        if not hits and DIM_RE.search(line):          # size without a count on the line = 1 box
            hits = [("1", *DIM_RE.search(line).groups())]
        if hits:
            for i, (n, l, w, h) in enumerate(hits):
                add(name if i == 0 else None, n, l, w, h)
            continue
        # "Zeeflora 11" / "Zeeflora - 11boxes" without a size -> the farm's usual box
        m = re.search(r"(\d{1,3})\s*(?:boxes|bxs|box)?\s*$", line, re.I)
        if name and m and usual_dims:
            d = usual_dims(name)
            if d:
                l, w, h = d.split(".")
                add(name, m.group(1), l, w, h)
                cur["dims"][-1] = (*cur["dims"][-1], "обычная коробка")
        elif name and not m:
            cur = None                                  # a bare name line starts a new farm block
            farms.append({"farm": name, "boxes": 0, "dims": [], "kg": 0.0})
            cur = farms[-1]
    return [f for f in farms if f["boxes"]]


def usual(dims_counts: dict) -> str | None:
    """Most frequent box of a farm: {"100.48.25": 40, ...} -> "100.48.25"."""
    return Counter(dims_counts).most_common(1)[0][0] if dims_counts else None


# ---- seed: box sizes Expolanka reported for DiLuna, 09.2025 – 09.2026 (from the WhatsApp chat) ----
SEED_TEXT = """
Karen 6(100.40.25)
Pamoja 1(100.33.21)
Zeeflora 16(100.48.25)
Valentine 2 (100.33.21)
Zeeflora 20(100.48.25)
Zeeflora 21 100.48.25
Zeeflora 13 (100.48.25)
Tambuzi 1 (100.40.28)
Tambuzi 1 (100.52.25)
Dale flora 9 (100.48.21)
Zeeflora 13 (100.48.25)
Zeeflora 24(100.48.25)
Zeeflora 17 (100.48.25)
Zeeflora 23(100.48.25)
Zeeflora 17(100.48.25)
Dale flora 13(100.48.21)
Zeeflora 24(100.48.25)
Zeeflora 18(100.48.25)
Zeeflora 22 100.48.25
PJ flora 11(100.48.25)
Tambuzi 1(100.48.25)
Tambuzi 1(100.40.30)
Zeeflora 17(100.48.25)
Zeeflora 21(100.48.25)
Heritage 3(100.40.25)
Zeeflora 30 (100.48.25)
Zeeflora 16(100.48.25)
Zeeflora 16 (100.48.25)
Zeeflora 14 100.48.25
Zeeflora - 10 - 100.48.25
Zeeflora - 11 - 100.48.25
Zeeflora - 10 - 100.48.25
Zeeflora - 2 - 100.48.25
Zeeflora - 17 - 100.48.25
Zeeflora 40(100.48.25)
Blooming dale 35 (100.48.25)
Bliss flora 13(100.48.21)
Zeeflora 17(100.48.25)
Dale flora 4(100.48.21)
Zeeflora 19 100.48.25
Zeeflora 23 100.48.25
Zeeflora - 17 - 100.48.25
Zeeflora 24 100.48.25
Zeeflora 17 (100.48.25)
Zeeflora 23 100.48.25
Zeeflora - 23 - 100.48.25
Zeeflora 25 100.48.25
Heritage - 12 - 100.40.25
Zeeflora - 10 - 100.48.25
Zeeflora 24 100.52.25
Zeeflora - 24 - 100.48.25
Zeeflora - 10 - 100.48.25
Zeeflora - 10 - 100.48.25
Zeeflora - 12 - 100.48.25
Zeeflora 14 100.48.25
Zeeflora - 14 - 100.48.25
Tambuzi - 2 - 100.40.30
Zeeflora - 14 - 100.48.25
Flora delight - 1 - 130.44.15
Flora delight - 1 - 100.44.15
Zeeflora - 10 - 100.48.25
Flora ola - 2 - 100.40.25
Tambuzi - 2 - 100.60.25
Tambuzi - 1 - 100.40.25
Zeeflora 10 100.48.25
Zeeflora - 10 - 100.48.25
Molo River - 1 - 100.33.21
Molo River - 1 - 100.40.25
Tambuzi - 1 - 100.40.25
Tambuzi - 4 - 100.60.21
Zeeflora - 10 - 100.48.25
Redlands - 1 - 96.45.30
Tambuzi - 3 - 100.60.21
Zeeflora - 10 - 100.48.25
Kikwetu - 1 - 100.48.25
Karen - 4 - 100.40.25
Tambuzi - 3 - 100.40.35
Primarosa 2 (100.33.21)
Tambuzi 3 (100.60.21)
Tambuzi 1 (100.40.28)
Zeeflora 11 (100.48.24)
Zeeflora 17 100.48.25
PJ flowers 2 100.40.25
Tambuzi 2 100.40.28
Tambuzi 2 100.60.21
Zeeflora - 11 - 100.48.25
Tambuzi - 2 - 100.60.21
Maasai - 1 - 100.55.25
Maasai - 1 - 100.40.25
Agriflora - 1 - 100.55.25
Zeeflora - 10 - 100.48.25
Tambuzi - 3 - 100.60.21
Tambuzi - 3 - 100.40.25
Zeeflora - 11 - 100.48.25
Tambuzi - 2 - 100.60.21
Tambuzi - 3 - 100.45.30
Batian - 1 - 100.40.25
Tambuzi - 1 - 100.40.26
Tambuzi - 1 - 100.35.28
Karen 1 100.40.25
Kikwetu 3 100.48.28
Heritage 4 100.40.25
Subati 2 100.52.25
Zeeflora 12 100.48.25
Equator 2(100.55.25)
Maasai 3(100.40.25)
Kikwetu - 3 - 100.48.25
Zeeflora - 11 - 100.48.25
Tambuzi - 2 - 100.40.25
Tambuzi - 1 - 102.35.28
Agriflora 2(100.55.25)
Maasai 3(100.40.25)
Maasai 1(100.55.25)
Redlands 3 (96.45.30)
Zeeflora 12(100.48.25)
"""
