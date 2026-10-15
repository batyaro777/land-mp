"""Хранилище кабинета: пользователи, инвайты, подборки, результаты, объявления.

SQLite через SQLAlchemy.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    invite_code: Mapped[str] = mapped_column(String(40), unique=True)
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    checks: Mapped[list[Check]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Check(Base):
    """Одна проверка подборки."""

    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|error
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_text: Mapped[str] = mapped_column(Text, default="")
    input_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="checks")
    items: Mapped[list[CheckItem]] = relationship(
        back_populates="check", cascade="all, delete-orphan", order_by="CheckItem.position"
    )


class CheckItem(Base):
    """Результат по одному кадастровому номеру внутри проверки."""

    __tablename__ = "check_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    cad_num: Mapped[str] = mapped_column(String(40), index=True)
    verdict: Mapped[str] = mapped_column(String(12))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    token: Mapped[str] = mapped_column(
        String(32), unique=True, default=lambda: secrets.token_urlsafe(12)
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # сериализованный CheckResult

    check: Mapped[Check] = relationship(back_populates="items")


class Listing(Base):
    """Черновик объявления (М3)."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    cad_num: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_engine = None
SessionLocal: sessionmaker[Session] | None = None


def init_db(path: str | Path) -> None:
    global _engine, SessionLocal
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{p}", connect_args={"check_same_thread": False})

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(_engine)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def session() -> Session:
    assert SessionLocal is not None, "init_db() не вызван"
    return SessionLocal()


# --- инвайты ---------------------------------------------------------------------------
def create_invites(db: Session, count: int = 1, note: str | None = None) -> list[str]:
    codes: list[str] = []
    for _ in range(count):
        code = "LC-" + secrets.token_hex(3).upper()
        db.add(Invite(code=code, note=note))
        codes.append(code)
    db.commit()
    return codes


def redeem_invite(
    db: Session, code: str, name: str, contact: str | None, brand: str | None
) -> User | None:
    code = code.strip().upper()
    inv = db.scalar(select(Invite).where(Invite.code == code))
    if inv is None:
        return None
    if inv.used_by is not None:
        return db.get(User, inv.used_by)
    user = User(
        name=name.strip()[:120],
        contact=(contact or "").strip()[:200] or None,
        brand=(brand or "").strip()[:120] or None,
        invite_code=code,
    )
    db.add(user)
    db.flush()
    inv.used_by = user.id
    db.commit()
    return user


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
