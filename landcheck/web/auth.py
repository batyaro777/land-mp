"""Сессия в подписанной cookie. Пароли не храним: вход по инвайт-коду."""

from __future__ import annotations

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from landcheck.web.config import settings
from landcheck.web.db import User, session

COOKIE = "lc_session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 дней


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret, salt="lc-session")


def make_token(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def current_user(request: Request) -> User | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=MAX_AGE)
    except BadSignature:
        return None
    with session() as db:
        return db.get(User, int(data["uid"]))
