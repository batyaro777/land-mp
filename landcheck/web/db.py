"""Хранилище кабинета: пользователи, инвайты, подборки, результаты, объявления.

SQLite через SQLAlchemy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
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
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
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


ROLES: dict[str, str] = {
    "buyer": "Покупатель",
    "seller": "Продавец",
    "broker": "Брокер",
    "admin": "Администратор",
}
# Роли, которые пользователь выбирает сам (регистрация, профиль). Администратор — только через
# код приглашения с флагом --admin или `landcheck admin <email>`: видит всю картину целиком.
PUBLIC_ROLES: tuple[str, ...] = ("buyer", "seller", "broker")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8


class User(Base):
    """Пользователь кабинета. Вход по коду приглашения (пилот) или по email и паролю (ТЗ v2, R3).

    Роль задаёт домашнюю страницу и навигацию: покупатель, продавец, брокер (всё сразу).
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    invite_code: Mapped[str] = mapped_column(String(40), unique=True)
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="broker", server_default="broker")
    # Инфопартнёр (К4): своя ссылка у каждого, два уровня привлечения.
    ref_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    referred_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Уведомления (план 20.10, п.5): в кабинете всегда; email и Telegram — если настроены.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notify_email: Mapped[bool] = mapped_column(default=True, server_default="1")
    api_key: Mapped[str | None] = mapped_column(String(64), nullable=True)  # R10: ключ API

    checks: Mapped[list[Check]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def role_label(self) -> str:
        return ROLES.get(self.role or "broker", "Брокер")


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Роль, которую получит пользователь по этому коду (None → брокер); «admin» — администратор.
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)


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


LISTING_STATUSES: dict[str, str] = {
    "draft": "черновик",
    "pending": "на проверке",
    "verified": "право подтверждено",
    "rejected": "отклонено",
}
PROOF_KINDS: dict[str, str] = {
    "owner": "Я собственник: выписка ЕГРН или свидетельство о праве",
    "agent": "Я представитель: агентский или комиссионный договор с собственником",
}


class Listing(Base):
    """Объявление (М3 → кабинет продавца К2): черновик → на проверке → право подтверждено.

    Файлы подтверждения лежат вне статики (proofs/<id>/) и удаляются после решения
    модератора: в базе остаются только статус, вид подтверждения и дата.
    """

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    cad_num: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    published: Mapped[bool] = mapped_column(default=False, server_default="0")
    proof_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    moderation_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def status_label(self) -> str:
        return LISTING_STATUSES.get(self.status or "draft", "черновик")

    @property
    def is_public(self) -> bool:
        return self.status == "verified" and bool(self.published)


class BuyerRequest(Base):
    """Запрос покупателя (К1): текст, разобранные параметры, ответы на уточняющие вопросы."""

    __tablename__ = "buyer_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    purpose: Mapped[str] = mapped_column(String(16), default="any")
    budget_rub: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Connection(Base):
    """Межагентский контур (К3): запрос контакта по объявлению от брокера или покупателя
    владельцу объявления. Раздел комиссии платформа не ведёт — стороны договариваются сами."""

    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="new")  # new|accepted|declined
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(Base):
    """Уведомление пользователю: новый подходящий участок, запрос контакта, решение модератора."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # match|connect|decision|moderation|system
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered: Mapped[str | None] = mapped_column(String(40), nullable=True)  # email,telegram


class Watch(Base):
    """Слежение за участком (R9): снимок ключевых полей ЕГРН/ЗОУИТ и уведомление при изменениях."""

    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("user_id", "cad_num", name="uq_watch_user_cad"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    cad_num: Mapped[str] = mapped_column(String(32), index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_diff: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class Favorite(Base):
    """Сохранённый объект покупателя для сравнения."""

    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
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
    _migrate(_engine)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


# Колонки, добавленные после первого релиза: SQLite умеет только ADD COLUMN, этого хватает.
_NEW_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "email": "VARCHAR(200)",
        "password_hash": "VARCHAR(200)",
        "role": "VARCHAR(16) NOT NULL DEFAULT 'broker'",
        "ref_code": "VARCHAR(16)",
        "referred_by": "INTEGER",
        "telegram_chat_id": "VARCHAR(32)",
        "notify_email": "BOOLEAN NOT NULL DEFAULT 1",
        "api_key": "VARCHAR(64)",
    },
    "invites": {"role": "VARCHAR(16)"},
    "listings": {
        "status": "VARCHAR(16) NOT NULL DEFAULT 'draft'",
        "published": "BOOLEAN NOT NULL DEFAULT 0",
        "proof_kind": "VARCHAR(16)",
        "submitted_at": "DATETIME",
        "verified_at": "DATETIME",
        "moderation_note": "TEXT",
    },
}
_NEW_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_api_key ON users(api_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_ref_code ON users(ref_code)",
)


def _migrate(engine) -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in _NEW_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        for ddl in _NEW_INDEXES:
            conn.execute(text(ddl))


def session() -> Session:
    assert SessionLocal is not None, "init_db() не вызван"
    return SessionLocal()


# --- инвайты ---------------------------------------------------------------------------
def create_invites(
    db: Session, count: int = 1, note: str | None = None, role: str | None = None
) -> list[str]:
    if role is not None and role not in ROLES:
        raise ValueError(f"неизвестная роль: {role}")
    codes: list[str] = []
    for _ in range(count):
        code = "LC-" + secrets.token_hex(3).upper()
        db.add(Invite(code=code, note=note, role=role))
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
    role = inv.role or "broker"
    user = User(
        name=name.strip()[:120],
        contact=(contact or "").strip()[:200] or None,
        brand=(brand or "").strip()[:120] or None,
        invite_code=code,
        role=role,
        is_admin=role == "admin",
    )
    db.add(user)
    db.flush()
    inv.used_by = user.id
    db.commit()
    return user


def grant_admin(db: Session, user: User, on: bool = True) -> None:
    """Права администратора вместе с ролью: обзор системы, пользователи, модерация, чужие данные."""
    user.is_admin = on
    user.role = "admin" if on else ("broker" if user.role == "admin" else user.role)
    db.commit()


# --- email + пароль -----------------------------------------------------------------------
def hash_password(password: str) -> str:
    """scrypt из стандартной библиотеки: без внешних зависимостей, соль на пользователя."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_hex, digest_hex = stored.split("$", 2)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def ensure_ref_code(db: Session, user: User) -> str:
    """Код партнёрской ссылки выдаётся при первом обращении."""
    if not user.ref_code:
        while True:
            code = secrets.token_hex(4)
            if db.scalar(select(User.id).where(User.ref_code == code)) is None:
                break
        user.ref_code = code
        db.commit()
    return user.ref_code


def user_by_ref(db: Session, code: str | None) -> User | None:
    code = (code or "").strip().lower()
    if not code:
        return None
    return db.scalar(select(User).where(User.ref_code == code))


def register_user(
    db: Session,
    email: str,
    password: str,
    name: str,
    role: str,
    brand: str | None = None,
    referred_by: int | None = None,
) -> tuple[User | None, str | None]:
    """Новый пользователь по email: (user, None) или (None, текст ошибки)."""
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        return None, "Укажите настоящий email."
    if len(password) < MIN_PASSWORD:
        return None, f"Пароль не короче {MIN_PASSWORD} знаков."
    if role not in PUBLIC_ROLES:
        return None, "Выберите роль."
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        return None, "Такой email уже зарегистрирован — войдите."
    user = User(
        name=name.strip()[:120] or email.split("@")[0],
        brand=(brand or "").strip()[:120] or None,
        invite_code="REG-" + secrets.token_hex(6).upper(),
        email=email,
        password_hash=hash_password(password),
        role=role,
        referred_by=referred_by,
    )
    db.add(user)
    db.commit()
    return user, None


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
