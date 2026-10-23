"""Спрос (R11): витрина запросов, отклик с вердиктом, решение покупателя, контакты."""

from __future__ import annotations

from sqlalchemy import select

from tests.test_market import _verified_listing
from tests.test_verification import _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def _norm(html: str) -> str:
    """money-фильтр ставит узкий неразрывный пробел (U+202F) между разрядами."""
    return html.replace("\u202f", " ").replace("\xa0", " ")


def _login(c, email: str) -> None:
    c.post("/logout")
    assert (
        c.post("/login/password", data={"email": email, "password": "12345678"}).status_code == 303
    )


def test_demand_offer_flow(client):  # noqa: F811
    from landcheck.web.db import Notification, Offer, session

    # продавец: подтверждённое и опубликованное объявление ИЖС
    url = _verified_listing(
        client, "s1@example.com", "50:09:0060202:101", "2500000", "газ по границе"
    )
    lid = int(url.rsplit("/", 1)[1])
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    client.post(f"/admin/listings/{lid}/decide", data={"decision": "approve"})
    _login(client, "s1@example.com")
    client.post(url + "/publish", data={"action": "on"})
    # у продавца пока нет спроса
    assert "Активных запросов нет" in client.get("/demand").text
    page = client.get(url).text
    assert "Спрос на этот участок" in page and "пока нет" in page

    # покупатель оставляет запрос
    client.post("/logout")
    _register(client, "buyer@example.com", role="buyer")
    rurl = client.post(
        "/search",
        data={"query": "Построить дом для семьи до 3 млн, 8–12 соток", "region": "синтетический"},
    ).headers["location"]
    rid = int(rurl.rsplit("/", 1)[1])
    client.post(rurl + "/answers", data={"living": "permanent", "gas": "required"})
    assert "Предложения под ваш запрос" not in client.get(rurl).text
    assert "Активных запросов нет" in client.get("/demand").text  # свой запрос в спросе не виден

    # продавец видит обезличенный запрос и что его участок подходит
    _login(client, "s1@example.com")
    page = _norm(client.get("/demand").text)
    assert "Жилой дом" in page and "до 3 000 000" in page and "синтетический" in page
    assert "buyer" not in page and "подходит 1 из моих" in page and "Газ" in page
    page = client.get(url).text
    assert "Подходит под 1 запрос" in page and "Предложить" in page
    # фильтр по цели: сельхоз — пусто, ИЖС — есть
    assert "Активных запросов нет" in client.get("/demand?purpose=farming").text
    assert "Жилой дом" in client.get("/demand?purpose=izhs&region=синтет").text
    # отклик со страницы объявления
    r = client.post(
        f"/demand/{rid}/offer", data={"listing_id": lid, "note": "газ уже подведён", "back": url}
    )
    assert r.status_code == 303 and r.headers["location"] == f"{url}?offered=1"
    assert "уже предложен" in client.get(url).text
    page = client.get("/demand").text
    assert "Мои предложения" in page and "ждёт ответа" in page and "уже предложен" in page
    # повтор не создаёт второй отклик
    client.post(f"/demand/{rid}/offer", data={"listing_id": lid})
    with session() as db:
        offers = db.scalars(select(Offer)).all()
        assert len(offers) == 1 and offers[0].fit is True and offers[0].verdict["level"] == "ок"
        oid = offers[0].id
        n = db.scalar(select(Notification).where(Notification.kind == "offer"))
        assert n is not None and "предлагает участок" in n.text and n.url == f"/search/{rid}#offers"
    # чужое объявление и свой запрос — 404
    assert client.post(f"/demand/{rid}/offer", data={"listing_id": lid + 100}).status_code == 404

    # покупатель видит предложение с вердиктом и принимает
    _login(client, "buyer@example.com")
    page = client.get(rurl).text
    assert "Предложения под ваш запрос (1)" in page and "газ уже подведён" in page
    assert "Интересно, открыть контакты" in page and 'class="badge v-ok">ок' in page
    r = client.post(f"/offers/{oid}/decide", data={"decision": "accepted"})
    assert r.status_code == 303 and r.headers["location"] == f"/search/{rid}#offers"
    page = client.get(rurl).text
    # контакт продавца: сначала контакт профиля, потом email, потом телефон из объявления
    assert "контакты открыты" in page and ("s1@example.com" in page or "+7 900" in page)
    # продавец: уведомление и контакт покупателя (email) в «Моих предложениях»
    _login(client, "s1@example.com")
    page = client.get("/demand").text
    assert "принято" in page and "buyer@example.com" in page
    with session() as db:
        n = db.scalar(select(Notification).where(Notification.kind == "offer_decision"))
        assert n is not None and "принял" in n.text
    # чужой покупатель не может решать за другого
    client.post("/logout")
    _register(client, "buyer2@example.com", role="buyer")
    assert client.post(f"/offers/{oid}/decide", data={"decision": "declined"}).status_code == 404


def test_demand_contacts_paid_gate(client, monkeypatch):  # noqa: F811
    from landcheck.web import demand
    from landcheck.web.config import settings
    from landcheck.web.db import User, session

    _register(client, "s@example.com")
    with session() as db:
        u = db.scalar(select(User).where(User.email == "s@example.com"))
        assert demand.contacts_allowed(u)
        monkeypatch.setattr(settings, "demand_paid", True)
        assert not demand.contacts_allowed(u)
        u.is_admin = True
        assert demand.contacts_allowed(u)


def test_request_card_hides_contacts_in_text():
    from types import SimpleNamespace

    from landcheck.web.demand import request_card

    req = SimpleNamespace(
        id=1,
        user_id=2,
        purpose="izhs",
        budget_rub=None,
        min_area_m2=None,
        max_area_m2=None,
        region=None,
        text="Ищу участок, звоните +7 (900) 123-45-67 или пишите ivan@example.com",
        answers={},
        created_at=None,
    )
    card = request_card(req)
    assert "123-45-67" not in card["text"] and "ivan@" not in card["text"]
    assert card["text"].startswith("Ищу участок") and card["label"] == "Жилой дом (ИЖС)"
