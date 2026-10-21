"""Уведомления: подходящий участок покупателю, запрос контакта владельцу, решение —
отправителю, модерация — продавцу; страница и счётчик; доставка email/Telegram на фейках."""

from __future__ import annotations

from sqlalchemy import select

from landcheck.web.db import Notification, User, session
from tests.test_market import _verified_listing
from tests.test_verification import _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def _notes(email: str) -> list[Notification]:
    with session() as db:
        u = db.scalar(select(User).where(User.email == email))
        return list(
            db.scalars(
                select(Notification).where(Notification.user_id == u.id).order_by(Notification.id)
            )
        )


def test_notifications_flow(client):  # noqa: F811
    # покупатель с запросом под дом
    _register(client, "buyer@example.com", role="buyer")
    r = client.post(
        "/search",
        data={"query": "Построить дом для семьи до 3 млн, 8–12 соток", "region": "синтетический"},
    )
    assert r.status_code == 303
    client.post("/logout")
    # продавец: объявление → модерация → публикация
    url = _verified_listing(client, "seller@example.com", "50:09:0060202:101", "2500000", "газ")
    lid = int(url.rsplit("/", 1)[1])
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    client.post(f"/admin/listings/{lid}/decide", data={"decision": "approve"})
    client.post("/logout")
    seller_notes = _notes("seller@example.com")
    assert [n.kind for n in seller_notes] == ["moderation"] and "подтверждено" in seller_notes[
        0
    ].text
    client.post("/login/password", data={"email": "seller@example.com", "password": "12345678"})
    assert "🔔 <span" in client.get("/listings").text  # непрочитанное в шапке
    assert client.post(url + "/publish", data={"action": "on"}).status_code == 303
    client.post(url + "/publish", data={"action": "off"})
    client.post(url + "/publish", data={"action": "on"})  # повторная публикация — без дубля
    client.post("/logout")
    buyer_notes = _notes("buyer@example.com")
    assert len(buyer_notes) == 1 and buyer_notes[0].kind == "match"
    assert "Новый участок под ваш запрос" in buyer_notes[0].text and buyer_notes[0].url.startswith(
        "/search/"
    )
    assert "2 500 000 ₽" in buyer_notes[0].text and "?l=" in buyer_notes[0].url
    # брокер запрашивает контакт → владелец получает уведомление; решение → брокеру
    _register(client, "broker@example.com", role="broker")
    assert client.post(f"/l/{lid}/connect", data={"note": "есть покупатель?."}).status_code == 303
    client.post("/logout")
    seller_notes = _notes("seller@example.com")
    assert seller_notes[-1].kind == "connect" and "«есть покупатель»." in seller_notes[-1].text
    client.post("/login/password", data={"email": "seller@example.com", "password": "12345678"})
    page = client.get("/notifications").text
    assert "Запрос контакта" in page and "новое" in page
    assert "🔔 <span" not in client.get("/listings").text  # прочитано
    with session() as db:
        from landcheck.web.db import Connection

        cid = db.scalar(select(Connection.id).where(Connection.listing_id == lid))
    client.post(f"/connections/{cid}/decide", data={"decision": "accepted"})
    client.post("/logout")
    broker_notes = _notes("broker@example.com")
    assert (
        broker_notes and broker_notes[-1].kind == "decision" and "принял" in broker_notes[-1].text
    )
    # профиль: chat_id и email-флаг
    client.post("/login/password", data={"email": "broker@example.com", "password": "12345678"})
    client.post(
        "/profile", data={"name": "b", "telegram_chat_id": " 123456789 ", "notify_email": ""}
    )
    with session() as db:
        u = db.scalar(select(User).where(User.email == "broker@example.com"))
        assert u.telegram_chat_id == "123456789" and u.notify_email is False


def test_delivery_email_and_telegram_with_fakes(monkeypatch):
    import httpx

    from landcheck.web import notify

    sent: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent["host"], sent["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, msg):
            sent["to"], sent["from"], sent["subject"] = msg["To"], msg["From"], msg["Subject"]

    notify.send_email(
        "x@example.com",
        "Тема",
        "Текст",
        smtp_url="smtp://user%40mail.ru:p%40ss@smtp.mail.ru:587?from=land@mail.ru",
        smtp_cls=FakeSMTP,
    )
    assert sent == {
        "host": "smtp.mail.ru",
        "port": 587,
        "tls": True,
        "login": ("user@mail.ru", "p@ss"),
        "to": "x@example.com",
        "from": "land@mail.ru",
        "subject": "Тема",
    }
    posted = {}

    def handler(request):
        posted["url"] = str(request.url)
        posted["json"] = request.read()
        return httpx.Response(200, json={"ok": True})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    notify.send_telegram("42", "привет", token="T0K", client=c)
    assert posted["url"].endswith("/botT0K/sendMessage") and b'"chat_id":"42"' in posted["json"]
    # без настроек доставка честно падает, deliver() это глотает и возвращает ошибку
    monkeypatch.setattr(notify.settings, "smtp_url", None)
    monkeypatch.setattr(notify.settings, "telegram_bot_token", None)
    out = notify.deliver(
        ["email", "telegram"],
        {"email": "a@b", "chat_id": "1", "kind": "system", "text": "t", "url": None},
    )
    assert "не задан" in out["email"] and "не задан" in out["telegram"]


def test_cut_title_breaks_on_word():
    from landcheck.listing import cut_title

    assert cut_title("Участок 12.3 сот. для ИЖС, земли населенных пунктов — Чепош", 50) == (
        "Участок 12.3 сот. для ИЖС"
    )
    assert cut_title("Участок 12.3 сот. для ИЖС, земли населенных пунктов", 51) == (
        "Участок 12.3 сот. для ИЖС, земли населенных пунктов"
    )
    assert cut_title("Очень длинный заголовок без запятых который надо резать по слову", 30) == (
        "Очень длинный заголовок без"
    )
    assert cut_title("Короткий", 50) == "Короткий"
    assert len(cut_title("а" * 80, 50)) == 50
