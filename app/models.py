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


class Invoice(SQLModel, table=True):
    """A farm invoice, paid from a top-up."""
    id: Optional[int] = Field(default=None, primary_key=True)
    topup_id: int = Field(foreign_key="topup.id", index=True)
    country: str = ""
    client_code: str = "Люмен"
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
    leg = 'air'  -> farm country → Moscow (Expolanka, cargo agent)
    leg = 'msk'  -> Moscow side (customs, handling, delivery)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    topup_id: Optional[int] = Field(default=None, foreign_key="topup.id", index=True)  # None = paid outside top-ups
    awb: str
    leg: str = "air"
    provider: str = ""
    invoice_no: str = ""
    usd: Optional[float] = None
    rub: Optional[float] = None            # if set, used as-is; otherwise usd * top-up rate
    basis: str = "auto"                    # auto (kg if known, else stems) | kg | stems
    paid_date: str = ""
    note: str = ""
    source_file: Optional[str] = None


engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


def init_db():
    SQLModel.metadata.create_all(engine)


def session() -> Session:
    return Session(engine)
