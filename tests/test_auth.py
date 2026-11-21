"""Регистрация по email и паролю, роли, вход по коду как раньше, миграция колонок."""

from __future__ import annotations

from landcheck.web.db import hash_password, verify_password
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета


def test_password_hashing_roundtrip():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$") and verify_password("correct horse", h)
    assert not verify_password("wrong", h) and not verify_password("x", None)
    assert not verify_password("x", "plain")


def test_register_login_roles(client):  # noqa: F811
    page = client.get("/login?tab=register").text
    assert "Зарегистрироваться" in page and "Продавец" in page
    # ошибки регистрации
    r = client.post("/register", data={"email": "bad", "password": "12345678", "role": "buyer"})
    assert r.status_code == 200 and "настоящий email" in r.text
    r = client.post("/register", data={"email": "a@b.ru", "password": "short", "role": "buyer"})
    assert "не короче 8" in r.text
    # продавец: домашняя — объявления
    r = client.post(
        "/register",
        data={
            "email": "Seller@Example.com",
            "password": "12345678",
            "name": "Вера",
            "role": "seller",
        },
    )
    assert r.status_code == 303 and "lc_session" in r.cookies
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303 and home.headers["location"] == "/listings"
    lst = client.get("/listings").text
    assert "Мои объявления" in lst and "Новое объявление" in lst and "продавец" in lst
    # повторная регистрация того же email (регистр не важен)
    client.post("/logout")
    r = client.post("/register", data={"email": "seller@example.com", "password": "12345678"})
    assert "уже зарегистрирован" in r.text
    # вход по паролю
    r = client.post("/login/password", data={"email": "seller@example.com", "password": "nope"})
    assert r.status_code == 200 and "не подходят" in r.text
    r = client.post("/login/password", data={"email": "SELLER@example.com", "password": "12345678"})
    assert r.status_code == 303
    # смена роли в профиле: брокер видит подборки
    r = client.post("/profile", data={"name": "Вера", "role": "broker"})
    assert r.headers["location"] == "/profile?saved=1"
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200 and "Новая проверка подборки" in home.text
    assert "Объявления" in home.text and "брокер" in home.text


def test_invite_user_can_set_email_and_password(client):  # noqa: F811
    login(client)  # по коду, роль брокера по умолчанию
    assert "Подборки" in client.get("/").text
    r = client.post(
        "/profile",
        data={"name": "Антон", "email": "anton@example.com", "password": "12345678"},
    )
    assert r.headers["location"] == "/profile?saved=1"
    client.post("/logout")
    r = client.post("/login/password", data={"email": "anton@example.com", "password": "12345678"})
    assert r.status_code == 303 and "lc_session" in r.cookies
    # занятый email не перезаписывается
    client.post("/logout")
    client.post(
        "/register", data={"email": "other@example.com", "password": "12345678", "role": "buyer"}
    )
    r = client.post("/profile", data={"name": "Другой", "email": "anton@example.com"})
    assert "уже занят" in client.get(r.headers["location"]).text


def test_migration_adds_columns_to_old_schema(tmp_path):
    import sqlite3

    from landcheck.web import db as dbmod

    p = tmp_path / "old.sqlite"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(120), contact VARCHAR(200), "
        "brand VARCHAR(120), invite_code VARCHAR(40) NOT NULL UNIQUE, is_admin BOOLEAN, "
        "created_at DATETIME)"
    )
    con.execute("INSERT INTO users (name, invite_code, is_admin) VALUES ('Старый', 'LC-OLD', 0)")
    con.commit()
    con.close()
    dbmod.init_db(p)
    with dbmod.session() as db:
        u = db.get(dbmod.User, 1)
        assert u.role == "broker" and u.email is None and u.role_label == "Брокер"
