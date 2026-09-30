"""Roles.
sys    — системный супер-админ: всё, включая остатки денег, и назначает роли
super  — супер-админ: всё, кроме остатков
viewer — 1С оператор: только смотрит и скачивает Excel, ничего не грузит и не меняет
"""
from sqlmodel import select

from .config import ALLOWED_IDS, SYSADMIN_IDS
from .models import User, session

ROLE_NAMES = {"sys": "Системный супер-админ", "super": "Супер-админ", "viewer": "1С оператор"}
_cache: dict[int, str] | None = None


def _load():
    global _cache
    with session() as s:
        _cache = {u.tg_id: u.role for u in s.exec(select(User)).all()}
    return _cache


def role_of(uid) -> str | None:
    if uid is None:
        return None
    return (_cache if _cache is not None else _load()).get(int(uid))


def can_write(role) -> bool:
    return role in ("sys", "super")


def sees_money(role) -> bool:
    return role == "sys"


def sys_ids() -> list[int]:
    return [k for k, v in (_cache if _cache is not None else _load()).items() if v == "sys"]


def set_role(uid: int, role: str, name: str = ""):
    if role not in ROLE_NAMES:
        raise ValueError(role)
    with session() as s:
        u = s.get(User, uid) or User(tg_id=uid)
        if u.role == "sys" and role != "sys" and len(sys_ids()) <= 1:
            raise ValueError("Нельзя снять последнего системного супер-админа")
        u.role = role
        if name:
            u.name = name
        s.add(u)
        s.commit()
    _load()


def remove(uid: int):
    with session() as s:
        u = s.get(User, uid)
        if u and u.role == "sys" and len(sys_ids()) <= 1:
            raise ValueError("Нельзя удалить последнего системного супер-админа")
        if u:
            s.delete(u)
            s.commit()
    _load()


def users() -> list[dict]:
    with session() as s:
        return [{"tg_id": u.tg_id, "name": u.name, "role": u.role, "role_name": ROLE_NAMES.get(u.role, u.role)}
                for u in s.exec(select(User)).all()]


def seed():
    """First start: SYSADMIN_IDS -> sys, the rest of ALLOWED_IDS -> super. Never overwrites later changes."""
    with session() as s:
        have = {u.tg_id for u in s.exec(select(User)).all()}
        sys_env = set(SYSADMIN_IDS) or ({next(iter(sorted(ALLOWED_IDS)))} if ALLOWED_IDS and not have else set())
        for i in sys_env:
            if i not in have:
                s.add(User(tg_id=i, role="sys"))
        for i in ALLOWED_IDS - sys_env:
            if i not in have:
                s.add(User(tg_id=i, role="super"))
        s.commit()
        if not any(u.role == "sys" for u in s.exec(select(User)).all()) and SYSADMIN_IDS:
            for i in SYSADMIN_IDS:
                u = s.get(User, i) or User(tg_id=i)
                u.role = "sys"
                s.add(u)
            s.commit()
    _load()
