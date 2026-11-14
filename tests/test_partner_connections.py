"""Инфопартнёр (К4): ссылка, два уровня, статистика. Межагентский контур (К3): запрос контакта."""

from __future__ import annotations

from tests.test_market import _verified_listing
from tests.test_verification import _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def test_referral_two_levels(client):  # noqa: F811
    _register(client, "partner@example.com", role="broker")
    page = client.get("/partner").text
    assert "Партнёрская ссылка" in page and "/login?ref=" in page
    import re

    code = re.search(r"/login\?ref=([0-9a-f]+)", page).group(1)
    assert client.get("/partner").text.count(code) >= 2  # код стабилен
    client.post("/logout")
    # уровень 1: через ссылку на витрину (cookie), регистрация позже
    r = client.get(f"/market?ref={code}")
    assert "lc_ref" in r.cookies
    r = client.post(
        "/register", data={"email": "l1@example.com", "password": "12345678", "role": "seller"}
    )
    assert r.status_code == 303
    l1_page = client.get("/partner").text
    code1 = re.search(r"/login\?ref=([0-9a-f]+)", l1_page).group(1)
    client.post("/logout")
    client.cookies.clear()
    # уровень 2: по ссылке уровня 1, скрытое поле формы
    login_page = client.get(f"/login?ref={code1}").text
    assert 'name="ref"' in login_page and "партнёрской ссылке" in login_page
    client.post(
        "/register",
        data={"email": "l2@example.com", "password": "12345678", "role": "buyer", "ref": code1},
    )
    client.post("/checks", data={"numbers": "50:09:0060202:101"})
    client.post("/logout")
    client.cookies.clear()
    # неизвестный код — без привязки, ошибок нет
    client.post(
        "/register", data={"email": "nobody@example.com", "password": "12345678", "ref": "zzz"}
    )
    client.post("/logout")
    client.post("/login/password", data={"email": "partner@example.com", "password": "12345678"})
    page = client.get("/partner").text
    assert "продавец 1" in page and "покупатель 1" in page
    from sqlalchemy import select

    from landcheck.web.db import User, session

    with session() as db:
        l1 = db.scalar(select(User).where(User.email == "l1@example.com"))
        l2 = db.scalar(select(User).where(User.email == "l2@example.com"))
        p = db.scalar(select(User).where(User.email == "partner@example.com"))
        nobody = db.scalar(select(User).where(User.email == "nobody@example.com"))
    assert l1.referred_by == p.id and l2.referred_by == l1.id and nobody.referred_by is None


def test_connection_request_flow(client):  # noqa: F811
    url = _verified_listing(client, "owner@example.com", "50:09:0060202:101", "2500000")
    lid = int(url.rsplit("/", 1)[1])
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    client.post(f"/admin/listings/{lid}/decide", data={"decision": "approve"})
    client.post("/logout")
    client.post("/login/password", data={"email": "owner@example.com", "password": "12345678"})
    client.post("/profile", data={"name": "Владелец", "contact": "@owner"})
    client.post(url + "/publish", data={"action": "on"})
    client.post("/logout")
    # без входа кнопки нет; брокер покупателя запрашивает контакт
    assert "Запросить контакт" not in client.get(f"/l/{lid}").text
    _register(client, "broker2@example.com", role="broker")
    client.post("/profile", data={"name": "Брокер покупателя", "contact": "@b2"})
    assert "Запросить контакт" in client.get(f"/l/{lid}").text
    r = client.post(f"/l/{lid}/connect", data={"note": "клиент ищет под дом"})
    assert r.status_code == 303
    page = client.get(f"/l/{lid}").text
    assert "ждёт ответа" in page and "Запросить контакт" not in page
    assert "@owner" not in client.get("/connections").text
    client.post("/logout")
    # владелец принимает — контакты открываются обеим сторонам
    client.post("/login/password", data={"email": "owner@example.com", "password": "12345678"})
    inbox = client.get("/connections").text
    assert "клиент ищет под дом" in inbox and "Принять и открыть контакты" in inbox
    import re

    conn_id = int(re.search(r"/connections/(\d+)/decide", inbox).group(1))
    assert (
        client.post(f"/connections/{conn_id}/decide", data={"decision": "accepted"}).status_code
        == 303
    )
    assert "@b2" in client.get("/connections").text
    # владелец не видит кнопку запроса на своём объявлении
    assert "Запросить контакт" not in client.get(f"/l/{lid}").text
    client.post("/logout")
    client.post("/login/password", data={"email": "broker2@example.com", "password": "12345678"})
    out = client.get("/connections").text
    assert "@owner" in out and "принят" in out
    # чужую связку решать нельзя
    assert (
        client.post(f"/connections/{conn_id}/decide", data={"decision": "declined"}).status_code
        == 404
    )
