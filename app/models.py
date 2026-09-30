"""
Data model. One Пополнение (top-up) = RUB sent to the payment agent, received as USD.
Everything paid out of it (farm invoices, freight) is converted at THAT top-up's rate.
"""
from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session
from .config import DB_URL


class TopUp(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: str                      # "23.09.2026"
    rub: float                     # сколько рублей отправили
    usd: float                     # сколько долларов реально зачислено
    prev_balance_rub: Optional[float] = None
    note: str = ""
    sheet_name: Optional[str] = None


class Farm(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)          # canonical name, e.g. "Zeeflora"
    country: str                           # Кения / Колумбия / Эквадор
    aliases: str = ""                      # comma separated: "ZEEFLORA,zeeflora"
    is_forwarder: bool = False             # Expolanka etc.
    notes: str = ""                        # free-text nuances the AI should know
    box_kg_json: str = "{}"                # {"Spray Rose Reflex Bicolour 60cm": 25} — fixed kg per box of these items
    box_dims_json: str = "{}"              # Expolanka box sizes seen: {"100.48.25": 40} -> volumetric kg = L*W*H/6000


class Invoice(SQLModel, table=True):
    """A farm invoice. topup_id = the top-up it was paid from; 0 = not paid yet (груз в пути, не оплачен)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    topup_id: int = Field(default=0, index=True)
    country: str = ""
    client_code: str = "LUMEN"            # маркировка
    invoice_no: str = ""
    invoice_date: str = ""
    awb: str = ""
    farm: str = ""
    weight_kg: Optional[float] = None      # this farm's kg on the AWB (from forwarder breakdown)
    invoice_total_usd: Optional[float] = None  # what the paper says (for reference only)
    usd_paid: float = 0                    # what was ACTUALLY paid, entered by operator
    rub_paid_override: Optional[float] = None  # if the agent charged a different RUB sum
    alloc_mode: str = "value"              # value = split RUB by price*stems; stems = equal per stem
    paid: bool = True
    paid_date: str = ""
    note: str = ""
    source_file: Optional[str] = None
    est_usd: Optional[float] = None        # unpaid: approximate $ Arman expects to pay (with costs)
    eta: Optional[str] = None              # ISO datetime (MSK) when the goods count as arrived
    arrived_at: Optional[str] = None       # set when arrived; None = в пути
    truck: Optional[str] = None            # TK MSK truck the MAWB was loaded into


class Line(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    invoice_id: int = Field(foreign_key="invoice.id", index=True)
    name: str
    boxes: Optional[float] = None
    stems: float = 0
    weight_kg: Optional[float] = None
    price_usd: float = 0
    mrc: Optional[float] = None


class Logistics(SQLModel, table=True):
    """A freight / delivery cost tied to one AWB.
    leg = 'air'  -> Expolanka: Kenya → Amsterdam
    leg = 'msk'  -> Floratrack: Amsterdam → Moscow for Kenya, the whole route for Ecuador/Colombia"""
    id: Optional[int] = Field(default=None, primary_key=True)
    topup_id: Optional[int] = Field(default=None, foreign_key="topup.id", index=True)  # None = paid outside top-ups
    awb: str
    paid: bool = True                      # False = Expolanka bill on deferred payment (≈ ₽ at the latest top-up rate)
    leg: str = "air"
    provider: str = ""
    invoice_no: str = ""
    usd: Optional[float] = None
    rub: Optional[float] = None            # if set, used as-is; otherwise usd * top-up rate
    basis: str = "auto"                    # auto (kg if known, else stems) | kg | stems
    paid_date: str = ""
    note: str = ""
    source_file: Optional[str] = None
    farm_kg_json: str = "{}"               # forwarder breakdown {"Zeeflora": 250, ...}, applied to invoices on save
    weight_kg: Optional[float] = None      # weight on the forwarder's bill: ₽ per kg = rub / weight_kg
    ext_key: Optional[str] = Field(default=None, index=True)   # e.g. "ft:21.09-530:8245" — re-import updates, not duplicates


class User(SQLModel, table=True):
    """Who can use the bot. role: sys (sees money + manages users) | super (everything but money) | viewer (1С: read-only)."""
    tg_id: int = Field(primary_key=True)
    name: str = ""
    role: str = "viewer"


class AwbWeights(SQLModel, table=True):
    """Per-farm kg on one MAWB. Comes separately from the freight bill (Expolanka bills have no breakdown)."""
    awb: str = Field(primary_key=True)     # normalized MAWB (digits only)
    farm_kg_json: str = "{}"               # {"Zeeflora": 250, "Tambuzi": 30, ...}
    note: str = ""
    source_file: Optional[str] = None


engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


def init_db():
    SQLModel.metadata.create_all(engine)
    # add columns introduced after the first deploy (SQLite doesn't do it by itself)
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(engine).get_columns("logistics")}
    if "farm_kg_json" not in cols:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE logistics ADD COLUMN farm_kg_json VARCHAR DEFAULT '{}'"))
    fcols = {c["name"] for c in inspect(engine).get_columns("farm")}
    if "box_dims_json" not in fcols:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE farm ADD COLUMN box_dims_json VARCHAR DEFAULT '{}'"))
    if "box_kg_json" not in fcols:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE farm ADD COLUMN box_kg_json VARCHAR DEFAULT '{}'"))
    if "ext_key" not in cols:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE logistics ADD COLUMN ext_key VARCHAR"))
    if "weight_kg" not in cols:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE logistics ADD COLUMN weight_kg FLOAT"))
    if "paid" not in cols:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE logistics ADD COLUMN paid BOOLEAN DEFAULT 1"))
    icols = {c["name"] for c in inspect(engine).get_columns("invoice")}
    with engine.begin() as c:
        c.execute(text("UPDATE invoice SET client_code = 'LUMEN' WHERE client_code IN ('Люмен', 'люмен', '')"))
    for col, typ in (("est_usd", "FLOAT"), ("eta", "VARCHAR"), ("arrived_at", "VARCHAR"), ("truck", "VARCHAR")):
        if col not in icols:
            with engine.begin() as c:
                c.execute(text(f"ALTER TABLE invoice ADD COLUMN {col} {typ}"))
                if col == "arrived_at":   # everything booked before this feature is already on the shelf
                    c.execute(text("UPDATE invoice SET arrived_at = 'до учёта в пути'"))


def session() -> Session:
    return Session(engine)
