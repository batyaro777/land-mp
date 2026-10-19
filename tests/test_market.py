"""Публикация в базу и витрина: только подтверждённые и без противоречий с кадастром."""

from __future__ import annotations

import io

from tests.test_verification import PDF, _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def _verified_listing(c, email: str, cad_num: str, price: str, notes: str = "") -> str:
    _register(c, email)
    url = c.post(
        "/listing", data={"cad_num": cad_num, "price": price, "notes": notes, "phone": "+7 900"}
    ).headers["location"]
    c.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    c.post("/logout")
    return url


def test_publish_and_market(client):  # noqa: F811
    url1 = _verified_listing(client, "s1@example.com", "50:09:0060202:101", "2500000")
    url2 = _verified_listing(client, "s2@example.com", "50:09:0060202:102", "900000", "под ИЖС")
    id1, id2 = (int(u.rsplit("/", 1)[1]) for u in (url1, url2))
    # до решения модератора публичной страницы нет
    assert client.get(f"/l/{id1}").status_code == 404
    assert "Пока ничего нет" in client.get("/market").text
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    for i in (id1, id2):
        client.post(f"/admin/listings/{i}/decide", data={"decision": "approve"})
    client.post("/logout")
    # владелец 1 публикует
    client.post("/login/password", data={"email": "s1@example.com", "password": "12345678"})
    page = client.get(url1).text
    assert "Опубликовать в базе" in page
    assert client.post(url1 + "/publish", data={"action": "on"}).status_code == 303
    page = client.get(url1).text
    assert "опубликовано" in page and f"/l/{id1}" in page and "Снять с публикации" in page
    client.post("/logout")
    # владелец 2: текст противоречит кадастру (ИЖС на сельхозземле) — публикации нет
    client.post("/login/password", data={"email": "s2@example.com", "password": "12345678"})
    page = client.get(url2).text
    assert "Публикация откроется" in page
    client.post(url2 + "/publish", data={"action": "on"})
    assert "опубликовано" not in client.get(url2).text
    client.post("/logout")
    # витрина и публичная страница без входа
    m = client.get("/market").text
    assert "50:09:0060202:101" in m and "50:09:0060202:102" not in m
    assert "право подтверждено" in m and "Найдено: 1" in m
    assert "Найдено: 0" in client.get("/market?price_max=2+млн").text
    assert "Найдено: 1" in client.get("/market?price_max=3+млн&area_min=8&area_max=12").text
    assert "Найдено: 0" in client.get("/market?region=Алтай").text
    assert "Найдено: 1" in client.get("/market?region=синтетический").text
    pub = client.get(f"/l/{id1}")
    assert pub.status_code == 200
    assert (
        "+7 900" in pub.text
        and "право подтверждено" in pub.text
        and "s1@example.com" not in pub.text
    )
    assert "Кадастровый номер" in pub.text and "50:09:0060202:101" in pub.text
    assert client.get(f"/l/{id2}").status_code == 404
    # снятие с публикации
    client.post("/login/password", data={"email": "s1@example.com", "password": "12345678"})
    client.post(url1 + "/publish", data={"action": "off"})
    client.post("/logout")
    assert client.get(f"/l/{id1}").status_code == 404


def test_publish_errors_are_visible_and_edits_unpublish(client):  # noqa: F811
    url = _verified_listing(client, "s9@example.com", "50:09:0060202:101", "2500000")
    lid = int(url.rsplit("/", 1)[1])
    client.post("/login/password", data={"email": "s9@example.com", "password": "12345678"})
    r = client.post(url + "/publish", data={"action": "on"})
    assert r.headers["location"].endswith("?err=not_verified")
    assert "сначала подтвердите право" in client.get(r.headers["location"]).text
    client.post("/logout")
    _register(client, "adm9@example.com", role="broker")
    _make_admin("adm9@example.com")
    client.post(f"/admin/listings/{lid}/decide", data={"decision": "approve"})
    client.post("/logout")
    client.post("/login/password", data={"email": "s9@example.com", "password": "12345678"})
    assert client.post(url + "/publish", data={"action": "on"}).headers["location"] == url
    assert client.get(f"/l/{lid}").status_code == 200
    # правка с обещанием «без обременений» для участка без ЗОУИТ — противоречий нет, остаётся
    r = client.post(url, data={"description": "Продаётся участок 10 сот. ИЖС, газ рядом."})
    assert r.headers["location"] == url and client.get(f"/l/{lid}").status_code == 200
    # правка с ложной площадью — снимается с публикации с сообщением
    r = client.post(url, data={"description": "Продаётся участок 25 сот. ИЖС."})
    assert r.headers["location"].endswith("?err=unpublished")
    assert "снято с публикации" in client.get(r.headers["location"]).text
    assert client.get(f"/l/{lid}").status_code == 404


def test_zone_name_from_nspd_is_not_a_seller_claim():
    from landcheck.listing import ParcelFacts, contradictions, static_warnings, strip_facts

    pf = ParcelFacts(
        cad_num="x",
        category="Земли сельскохозяйственного назначения",
        permitted_use="Для сельскохозяйственного производства",
        permitted_use_all="Для сельскохозяйственного производства",
        best_area_m2=1000.0,
        has_borders=True,
        zouit_count=0,
        cadastral_cost_rub=300_000,
    )
    facts = ["Территориальная зона по ПЗЗ: Ж-1 · Зона застройки ИЖС (50:09-7.1)"]
    text = "Продаётся участок. По данным ЕГРН: " + facts[0] + ". Ровный."
    assert contradictions(pf, text, None)  # без очистки факт выглядит как обещание ИЖС
    assert contradictions(pf, strip_facts(text, facts, None), None) == []
    tiny = ParcelFacts(
        cad_num="y",
        category=None,
        permitted_use=None,
        permitted_use_all="",
        best_area_m2=1.0,
        has_borders=True,
        zouit_count=0,
        cadastral_cost_rub=None,
    )
    assert contradictions(tiny, "участок 0 сот.", None) == []
    assert any("ошибку данных" in w for w in static_warnings(tiny))
