"""Уведомления (план 20.10, п.5): запись в кабинете всегда; email и Telegram — если настроены.

Доставка идёт в фоновом потоке и не блокирует запрос; сбой доставки пишется в лог,
запись в кабинете остаётся.
"""

from __future__ import annotations

import logging
import smtplib
import threading
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from sqlalchemy import func, select

from landcheck.web.config import settings
from landcheck.web.db import Notification, User

log = logging.getLogger(__name__)
KIND_LABELS = {
    "match": "Новый участок под запрос",
    "connect": "Запрос контакта",
    "decision": "Ответ по запросу контакта",
    "moderation": "Модерация",
    "system": "Система",
}


def notify(db, user_id: int, kind: str, text: str, url: str | None = None) -> Notification | None:
    """Создать уведомление (без commit — его делает вызывающий код) и запланировать доставку."""
    user = db.get(User, user_id)
    if user is None:
        return None
    n = Notification(user_id=user_id, kind=kind, text=text[:2000], url=url)
    db.add(n)
    db.flush()
    _schedule_delivery(user, kind, text, url)
    return n


def already_notified(db, user_id: int, kind: str, url: str | None) -> bool:
    return (
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.kind == kind,
                Notification.url == url,
            )
        )
        or 0
    ) > 0


def unread_count(db, user_id: int) -> int:
    return (
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id, Notification.read_at.is_(None)
            )
        )
        or 0
    )


def mark_all_read(db, user_id: int) -> None:
    now = datetime.now(UTC)
    for n in db.scalars(
        select(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    ):
        n.read_at = now


# --- доставка ----------------------------------------------------------------------------
def _schedule_delivery(user: User, kind: str, text: str, url: str | None) -> None:
    targets = []
    if settings.smtp_url and user.email and user.notify_email:
        targets.append("email")
    if settings.telegram_bot_token and user.telegram_chat_id:
        targets.append("telegram")
    if not targets:
        return
    payload = {
        "email": user.email,
        "chat_id": user.telegram_chat_id,
        "kind": kind,
        "text": text,
        "url": url,
    }
    threading.Thread(target=deliver, args=(targets, payload), daemon=True).start()


def deliver(targets: list[str], payload: dict[str, Any]) -> dict[str, str]:
    """Отправить по выбранным каналам; результат по каждому — «ok» или текст ошибки."""
    out: dict[str, str] = {}
    full_url = (settings.base_url or "") + (payload.get("url") or "")
    subject = KIND_LABELS.get(payload["kind"], "Уведомление") + " — " + settings.brand
    body = payload["text"] + (f"\n\n{full_url}" if payload.get("url") else "")
    for t in targets:
        try:
            if t == "email":
                send_email(payload["email"], subject, body)
            elif t == "telegram":
                send_telegram(payload["chat_id"], f"{subject}\n{body}")
            out[t] = "ok"
        except Exception as exc:  # noqa: BLE001 — доставка не должна ломать работу
            out[t] = f"{type(exc).__name__}: {exc}"
            log.warning(
                "notify %s to %s failed: %s",
                t,
                payload.get(t == "email" and "email" or "chat_id"),
                exc,
            )
    return out


def send_email(
    to: str, subject: str, body: str, *, smtp_url: str | None = None, smtp_cls=None
) -> None:
    url = urlparse(smtp_url or settings.smtp_url or "")
    if not url.hostname:
        raise RuntimeError("LANDCHECK_SMTP_URL не задан")
    q = parse_qs(url.query)
    sender = (q.get("from") or [unquote(url.username or "")])[0]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    port = url.port or (465 if url.scheme == "smtps" else 587)
    cls = smtp_cls or (smtplib.SMTP_SSL if url.scheme == "smtps" else smtplib.SMTP)
    with cls(url.hostname, port, timeout=20) as smtp:
        if url.scheme != "smtps":
            smtp.starttls()
        if url.username:
            smtp.login(unquote(url.username), unquote(url.password or ""))
        smtp.send_message(msg)


def send_telegram(
    chat_id: str, text: str, *, token: str | None = None, client: httpx.Client | None = None
) -> None:
    token = token or settings.telegram_bot_token
    if not token:
        raise RuntimeError("LANDCHECK_TG_BOT_TOKEN не задан")
    c = client or httpx.Client(timeout=15)
    try:
        r = c.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
        )
        r.raise_for_status()
    finally:
        if client is None:
            c.close()
