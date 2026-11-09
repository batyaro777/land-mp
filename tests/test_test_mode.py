"""Тестовый режим аккаунта и смена роли (Тони, 09.11): тестовый аккаунт публикует объявление
без проверки права со статусом «тест» — видно только админам и тестовым аккаунтам; любой
аккаунт, включая администратора, переключает роль из меню."""

from __future__ import annotations

import io

from sqlalchemy import select

from tests.test_verification import PDF, _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

CAD = "50:09:0060202:101"


def _set_test_mode(email: str, on: bool = True) -> None:
    from landcheck.web.db import User, session, set_test_mode

    with session() as db:
        u = db.scalar(select(User).where(User.email == email))
        set_test_mode(db, u, on=on)


def _login(c, email: str) -> None:
    r = c.post("/login/password", data={"email": email, "password": "12345678"})
    assert r.status_code == 303


def test_test_account_publishes_without_proof_and_market_hides_it(client):  # noqa: F811
    _register(client, "tester@example.com")
    url = client.post("/listing", data={"cad_num": CAD, "price": "2500000"}).headers["location"]
    lid = int(url.rsplit("/", 1)[1])
    # обычный аккаунт: без подтверждения права публикации нет
    assert "Опубликовать как тест" not in client.get(url).text
    client.post(url + "/publish", data={"action": "on"})
    assert "не выполнена: сначала подтвердите право" in client.get(url + "?err=not_verified").text
    assert client.get(f"/l/{lid}").status_code == 404
    # включили тестовый режим
    _set_test_mode("tester@example.com")
    page = client.get(url).text
    assert "Опубликовать как тест (без проверки права)" in page and "тестовый аккаунт" in page
    assert client.post(url + "/publish", data={"action": "on"}).status_code == 303
    page = client.get(url).text
    assert "тест: без проверки права" in page and f"/l/{lid}" in page
    assert "Снять с публикации" in page and "Отправить на проверку" in page
    # публичная страница открывается, но честно помечена
    pub = client.get(f"/l/{lid}").text
    assert "тестовое объявление" in pub and "Право на участок не проверялось" in pub
    assert "право подтверждено" not in pub
    # на витрине для тестового аккаунта видно с пометкой
    m = client.get("/market").text
    assert CAD in m and "тест: право не проверялось" in m
    # уведомлений покупателям и вердикта «право подтверждено» нет
    from landcheck.web.db import Listing, session

    with session() as db:
        row = db.get(Listing, lid)
        assert row.status == "test" and row.published and row.verified_at is None
    client.post("/logout")
    client.cookies.clear()
    # без входа и для обычных пользователей витрина тестовое объявление не показывает
    assert CAD not in client.get("/market").text
    _register(client, "buyer@example.com", role="buyer")
    assert CAD not in client.get("/market").text
    client.post("/logout")
    client.cookies.clear()
    # администратор видит
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    assert CAD in client.get("/market").text
    assert "тест: без проверки права" in client.get("/admin/listings?status=test").text


def test_test_listing_goes_to_moderation_with_real_proof(client):  # noqa: F811
    _register(client, "tester2@example.com")
    _set_test_mode("tester2@example.com")
    url = client.post("/listing", data={"cad_num": CAD, "price": "2500000"}).headers["location"]
    client.post(url + "/publish", data={"action": "on"})
    lid = int(url.rsplit("/", 1)[1])
    assert client.get(f"/l/{lid}").status_code == 200
    # загрузил выписку — обычный путь: «на проверке», с публикации снято
    r = client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    assert r.status_code == 303
    page = client.get(url).text
    assert "на проверке" in page
    assert client.get(f"/l/{lid}").status_code == 404
    # снятие с публикации тоже работает для теста
    client.post(url + "/publish", data={"action": "off"})
    assert "не выполнена" not in client.get(url).text


def test_test_listing_cannot_be_offered_to_buyers(client):  # noqa: F811
    from landcheck.web.db import BuyerRequest, session

    _register(client, "buyer2@example.com", role="buyer")
    with session() as db:
        buyer = db.execute(select(BuyerRequest)).scalars().all()
    client.post("/logout")
    client.cookies.clear()
    _register(client, "tester3@example.com")
    _set_test_mode("tester3@example.com")
    url = client.post("/listing", data={"cad_num": CAD, "price": "2500000"}).headers["location"]
    client.post(url + "/publish", data={"action": "on"})
    lid = int(url.rsplit("/", 1)[1])
    with session() as db:
        from landcheck.web.db import User

        b = db.scalar(select(User).where(User.email == "buyer2@example.com"))
        req = BuyerRequest(user_id=b.id, text="участок под дом")
        db.add(req)
        db.commit()
        rid = req.id
    assert not buyer
    r = client.post(f"/demand/{rid}/offer", data={"listing_id": lid, "back": "/demand"})
    assert r.status_code == 404


def test_admin_toggles_test_mode_from_users_page(client):  # noqa: F811
    _register(client, "adm2@example.com", role="broker")
    _make_admin("adm2@example.com")
    client.post("/logout")
    client.cookies.clear()
    _register(client, "someone@example.com")
    client.post("/logout")
    client.cookies.clear()
    _login(client, "adm2@example.com")
    from landcheck.web.db import User, session

    with session() as db:
        uid = db.scalar(select(User).where(User.email == "someone@example.com")).id
    page = client.get("/admin/users").text
    assert f"/admin/users/{uid}/test-mode" in page and "выкл" in page
    r = client.post(f"/admin/users/{uid}/test-mode", data={"action": "on"})
    assert r.status_code == 303
    with session() as db:
        assert db.get(User, uid).test_mode is True
    assert "тест ✓" in client.get("/admin/users").text
    client.post(f"/admin/users/{uid}/test-mode", data={"action": "off"})
    with session() as db:
        assert db.get(User, uid).test_mode is False
    # не админу — нельзя
    client.post("/logout")
    client.cookies.clear()
    _login(client, "someone@example.com")
    assert client.post(f"/admin/users/{uid}/test-mode", data={"action": "on"}).status_code in (
        303,
        403,
        404,
    )
    with session() as db:
        assert db.get(User, uid).test_mode is False


def test_role_switch_from_menu_for_everyone_including_admin(client):  # noqa: F811
    from landcheck.web.db import User, session

    _register(client, "sw@example.com", role="seller")
    home = client.get("/", follow_redirects=False)
    assert home.headers["location"] == "/listings"
    page = client.get("/listings").text
    assert 'action="/profile/role"' in page and "● Продавец" in page and "→ Покупатель" in page
    r = client.post("/profile/role", data={"role": "buyer"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/", follow_redirects=False).headers["location"] == "/search"
    assert client.post("/profile/role", data={"role": "admin"}).status_code == 400
    assert "→ Администратор" not in client.get("/search").text
    # администратор тоже переключается, права остаются; режим «Администратор» — только ему
    _make_admin("sw@example.com")
    assert "→ Администратор" in client.get("/search").text
    assert client.post("/profile/role", data={"role": "admin"}).status_code == 303
    assert client.get("/", follow_redirects=False).headers["location"] == "/admin"
    client.post("/profile/role", data={"role": "seller"})
    with session() as db:
        u = db.scalar(select(User).where(User.email == "sw@example.com"))
        assert u.role == "seller" and u.is_admin
    assert client.get("/", follow_redirects=False).headers["location"] == "/listings"
    page = client.get("/listings").text
    assert "/admin/users" in page  # админские ссылки на месте
    # и через профиль
    client.post("/profile", data={"name": "x", "role": "broker"})
    with session() as db:
        u = db.scalar(select(User).where(User.email == "sw@example.com"))
        assert u.role == "broker" and u.is_admin


def test_migration_adds_test_mode_column(tmp_path):
    import sqlite3

    from landcheck.web.db import init_db

    p = tmp_path / "old.sqlite"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(120), contact VARCHAR(200), "
        "brand VARCHAR(120), invite_code VARCHAR(40), is_admin BOOLEAN, created_at DATETIME)"
    )
    con.commit()
    con.close()
    init_db(p)
    con = sqlite3.connect(p)
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    assert "test_mode" in cols
