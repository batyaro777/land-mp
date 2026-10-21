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
from landcheck.web.db import Check, Notification, User

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
        if r.status_code >= 400:
            # Текст httpx содержит URL с токеном — наружу только код и ответ Telegram
            raise RuntimeError(f"telegram sendMessage HTTP {r.status_code}: {r.text[:120]}")
    finally:
        if client is None:
            c.close()


# --- привязка Telegram одной кнопкой: /start <подписанный код> -----------------------------
_BOT_CACHE: dict[str, Any] = {}


def bot_username(token: str | None = None) -> str | None:
    """Имя бота по getMe (кэш на процесс); None, если токена нет или Telegram не ответил."""
    token = token or settings.telegram_bot_token
    if not token:
        return None
    if "username" in _BOT_CACHE:
        return _BOT_CACHE["username"]
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        name = (r.json().get("result") or {}).get("username")
    except Exception as exc:  # noqa: BLE001 — профиль не должен падать из-за Telegram
        log.warning("telegram getMe failed: %s", exc)
        name = None
    if name:
        _BOT_CACHE["username"] = name
    return name


def webhook_secret(token: str | None = None) -> str | None:
    """Неугадываемый хвост адреса вебхука: sha256 от токена, 24 знака."""
    import hashlib

    token = token or settings.telegram_bot_token
    if not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()[:24]


def _link_serializer():
    from itsdangerous import URLSafeSerializer

    return URLSafeSerializer(settings.secret, salt="tg-link")


def link_token(user_id: int) -> str:
    return _link_serializer().dumps({"uid": user_id})


def parse_link_token(token: str) -> int | None:
    from itsdangerous import BadSignature

    try:
        data = _link_serializer().loads(token)
    except BadSignature:
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return int(uid) if isinstance(uid, int) else None


def link_url(user_id: int) -> str | None:
    name = bot_username()
    return f"https://t.me/{name}?start={link_token(user_id)}" if name else None


MAX_BOT_NUMBERS = 20


def _enqueue_default(check_id: int) -> None:
    from landcheck.web import jobs  # ленивый импорт: jobs шлёт итог через этот модуль

    jobs.enqueue(check_id)


def start_bot_check(db, user: User, text: str, chat_id: str) -> Check:
    """Проверка по кадастровым номерам из сообщения в боте: цель/бюджет/площадь — из текста
    (как в голосовом вводе), итог придёт в этот же чат (request["tg_chat"]). В очередь ставит
    вызывающий — после ответа пользователю, чтобы итог не пришёл раньше подтверждения."""
    from landcheck.inputs import CAD_RE
    from landcheck.intent import parse_request
    from landcheck.web.serialize import request_to_dict

    nums = list(dict.fromkeys(m.group(0) for m in CAD_RE.finditer(text)))[:MAX_BOT_NUMBERS]
    req = parse_request(text)
    title = (
        f"Проверка из Telegram {nums[0]}"
        if len(nums) == 1
        else f"Проверка из Telegram ({len(nums)})"
    )
    check = Check(
        user_id=user.id,
        title=title,
        request={**request_to_dict(req), "tg_chat": chat_id},
        input_text="\n".join(nums),
        total=len(nums),
    )
    db.add(check)
    db.commit()
    return check


def handle_update(db, update: dict[str, Any], send=None, enqueue=None) -> str:
    """Обработка апдейта Telegram: /start <код> привязывает чат к пользователю, /stop отвязывает.
    Возвращает, что сделано (для логов и тестов)."""
    raw_send = send or send_telegram

    def send(chat: str, text: str) -> None:
        try:
            raw_send(chat, text)
        except Exception as exc:  # noqa: BLE001 — вебхук должен отвечать 200 всегда
            log.warning("telegram reply to %s failed: %s", chat, exc)

    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return "ignored"
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    if cmd == "/start" and len(parts) == 2:
        uid = parse_link_token(parts[1].strip())
        user = db.get(User, uid) if uid else None
        if user is None:
            send(
                chat_id,
                "Ссылка устарела или неверна. Откройте профиль в Land-MP и нажмите "
                "«Подключить Telegram» ещё раз.",
            )
            return "bad-token"
        user.telegram_chat_id = chat_id
        db.commit()
        send(
            chat_id,
            f"Готово, {user.brand or user.name}. Уведомления Land-MP будут приходить сюда: "
            "новые участки под ваши запросы, запросы контакта, решения модератора. "
            "Отключить — командой /stop.",
        )
        return f"linked:{user.id}"
    if cmd == "/stop":
        for user in db.scalars(select(User).where(User.telegram_chat_id == chat_id)):
            user.telegram_chat_id = None
        db.commit()
        send(chat_id, "Уведомления отключены. Подключить снова можно из профиля в Land-MP.")
        return "unlinked"
    from landcheck.inputs import CAD_RE

    if CAD_RE.search(text):
        user = db.scalar(select(User).where(User.telegram_chat_id == chat_id))
        if user is None:
            send(
                chat_id,
                "Чтобы проверять участки из Telegram, подключите чат: профиль на сайте → "
                "«Подключить Telegram».",
            )
            return "unlinked-check"
        check = start_bot_check(db, user, text, chat_id)
        what = f"участок {check.input_text}" if check.total == 1 else f"{check.total} участков"
        purpose = (check.request or {}).get("purpose")
        goal = "" if purpose in (None, "any") else f" Цель: {purpose}."
        send(
            chat_id,
            f"Проверяю {what}: обычно 1–2 минуты. Пришлю вердикт и ссылку на отчёт.{goal}",
        )
        (enqueue or _enqueue_default)(check.id)
        return f"check:{check.id}"
    send(
        chat_id,
        "Это бот Land-MP. Пришлите кадастровый номер (можно несколько и цель: «под ИЖС») — "
        "проверю участок и пришлю вердикт со ссылкой на отчёт. Подключить уведомления: профиль "
        "на сайте → «Подключить Telegram».",
    )
    return "help"
