"""Роль администратора: не выбирается в формах, выдаётся кодом с флагом или через CLI;
обзор системы и список пользователей видны только ему."""

from __future__ import annotations

from sqlalchemy import select

from landcheck.web.db import User, grant_admin, session
from tests.test_verification import _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def test_admin_role_is_not_selectable_and_pages_are_hidden(client):  # noqa: F811
    page = client.get("/login?tab=register").text
    assert "Покупатель" in page and "Администратор" not in page
    r = client.post(
        "/register",
        data={"email": "x@example.com", "password": "12345678", "name": "x", "role": "admin"},
    )
    assert r.status_code == 200 and "Выберите роль" in r.text
    _register(client, "u@example.com", role="broker")
    assert client.get("/admin").status_code == 404
    assert client.get("/admin/users").status_code == 404
    # роль «admin» через профиль не берётся, в списке ролей профиля её нет
    client.post("/profile", data={"name": "u", "role": "admin"})
    profile = client.get("/profile").text
    assert "Администратор" not in profile and 'href="/admin"' not in profile


def test_admin_sees_overview_and_users(client):  # noqa: F811
    _register(client, "seller@example.com", role="seller")
    client.post("/logout")
    _register(client, "boss@example.com", role="broker")
    with session() as db:
        grant_admin(db, db.scalar(select(User).where(User.email == "boss@example.com")))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    page = client.get("/admin").text
    assert "Обзор системы" in page and "Пользователи" in page
    assert "seller" in page or "продавец" in page
    users = client.get("/admin/users").text
    assert "seller@example.com" in users and "boss@example.com" in users
    assert client.get("/checks").status_code == 200  # подборки доступны и администратору
    nav = client.get("/profile").text
    assert 'href="/admin"' in nav and 'href="/admin/users"' in nav
    assert "· администратор" in nav  # grant_admin выставил и роль
    client.post("/profile", data={"name": "boss", "role": "buyer"})  # роль админа не меняется
    assert "· администратор" in client.get("/profile").text


def test_admin_invite_code_creates_admin_without_role_choice(client):  # noqa: F811
    from landcheck.web.db import create_invites

    with session() as db:
        (code,) = create_invites(db, 1, "Тони", role="admin")
        (plain,) = create_invites(db, 1, "брокер")
    r = client.post("/login", data={"code": code, "name": "Тони"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Обзор системы" in client.get("/admin").text
    with session() as db:
        u = db.scalar(select(User).where(User.invite_code == code))
        assert u.is_admin and u.role == "admin"
    client.post("/logout")
    client.post("/login", data={"code": plain, "name": "Брокер"}, follow_redirects=False)
    assert client.get("/admin").status_code == 404
