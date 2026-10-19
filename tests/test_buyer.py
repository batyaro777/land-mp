"""Кабинет покупателя: вопросы по цели, быстрый вердикт, подборка из базы, сохранённые."""

from __future__ import annotations

from landcheck.buyer import (
    match_listing,
    questions_for,
    quick_verdict,
    region_from_text,
    seller_claims,
)
from landcheck.models import Purpose
from tests.test_market import _verified_listing
from tests.test_verification import _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def test_questions_and_region():
    keys = [q["key"] for q in questions_for(Purpose.IZHS)]
    assert keys == ["living", "kids", "gas", "budget_scope", "horizon"]
    assert len(questions_for(Purpose.SNT)) <= 5 and questions_for(Purpose.ANY)
    assert region_from_text("Построить дом для семьи до 3 млн, 8–12 соток в Истре") == "Истре"
    assert region_from_text("участок под дачу, Алтай") == "Алтай"
    assert region_from_text("дом до 3 млн") is None


def test_quick_verdict_and_claims():
    ok = quick_verdict(
        Purpose.IZHS, "Земли населенных пунктов", "Для индивидуального жилищного строительства"
    )
    assert ok["level"] == "ок"
    stop = quick_verdict(
        Purpose.IZHS,
        "Земли сельскохозяйственного назначения",
        "Для сельскохозяйственного производства",
    )
    assert stop["level"] == "стоп"
    lph = quick_verdict(
        Purpose.IZHS, "Земли населенных пунктов", "Для ведения личного подсобного хозяйства"
    )
    assert lph["level"] == "ок" and "112" in lph["text"] or "ЛПХ" in lph["text"]
    assert quick_verdict(Purpose.ANY, None, None)["level"] == "ок"
    claims = seller_claims(
        "газ по границе, скважина 30 м", {"gas": "required", "water": "yes", "kids": "yes"}
    )
    assert [(c["key"], c["mentioned"]) for c in claims] == [
        ("gas", True),
        ("water", True),
        ("kids", False),
    ]


def test_match_listing_filters_and_score():
    card = {
        "id": 1,
        "title": "Участок 10 сот. ИЖС — Бужарово",
        "address": "Московская область, Истринский район, д. Бужарово",
        "price_rub": 2_500_000,
        "area_sotka": 10.0,
        "category": "Земли населенных пунктов",
        "permitted_use": "Для индивидуального жилищного строительства",
    }
    kw = dict(
        purpose=Purpose.IZHS,
        budget_rub=3_000_000,
        min_area_m2=800,
        max_area_m2=1200,
        region="Истр",
        answers={"gas": "required"},
    )
    m = match_listing(card, text="газ по улице", **kw)
    assert m["verdict"]["level"] == "ок" and m["claims"][0]["mentioned"] and m["score"] > 100
    assert match_listing(card, **{**kw, "budget_rub": 2_000_000}) is None
    assert match_listing(card, **{**kw, "region": "Алтай"}) is None
    assert match_listing(card, **{**kw, "max_area_m2": 500}) is None
    assert match_listing(card, **{**kw, "region": None, "budget_rub": None}) is not None


def test_buyer_flow(client):  # noqa: F811
    # база: два подтверждённых и опубликованных объявления (ИЖС и сельхоз)
    url1 = _verified_listing(
        client, "s1@example.com", "50:09:0060202:101", "2500000", "газ по границе"
    )
    url2 = _verified_listing(client, "s2@example.com", "50:09:0060202:102", "900000", "ровный")
    ids = [int(u.rsplit("/", 1)[1]) for u in (url1, url2)]
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    for i in ids:
        client.post(f"/admin/listings/{i}/decide", data={"decision": "approve"})
    client.post("/logout")
    for email, url in (("s1@example.com", url1), ("s2@example.com", url2)):
        client.post("/login/password", data={"email": email, "password": "12345678"})
        client.post(url + "/publish", data={"action": "on"})
        client.post("/logout")
    # покупатель: дом — редирект на поиск
    _register(client, "buyer@example.com", role="buyer")
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303 and home.headers["location"] == "/search"
    assert "Какой участок ищете" in client.get("/search").text
    r = client.post(
        "/search",
        data={"query": "Построить дом для семьи до 3 млн, 8–12 соток", "region": "синтетический"},
    )
    assert r.status_code == 303
    rurl = r.headers["location"]
    q = client.get(rurl).text
    assert "Уточним, что важно" in q and "Газ обязателен" in q and "Детсад" not in q
    r = client.post(
        rurl + "/answers",
        data={"living": "permanent", "gas": "required", "kids": "yes", "nonsense": "x"},
    )
    assert r.status_code == 303
    res = client.get(rurl).text
    assert "Найдено: 2" in res and "50:09:0060202:101" in res and "50:09:0060202:102" in res
    # ИЖС-участок первым и «ок», сельхоз — «стоп»; газ отмечен у первого
    i1, i2 = res.index("50:09:0060202:101"), res.index("50:09:0060202:102")
    assert i1 < i2 and "газ ✓" in res and "полный отчёт под цель" in res
    assert "жилой дом для ИЖС не построить" in res or "стоп" in res
    # сохранить и сравнить
    r = client.post(f"/favorites/{ids[0]}", data={"back": rurl})
    assert r.status_code == 303 and r.headers["location"] == rurl
    assert "Убрать из сохранённых" in client.get(rurl).text
    saved = client.get("/saved").text
    assert "50:09:0060202:101" in saved and "50:09:0060202:102" not in saved and "газ ✓" in saved
    # полный отчёт под цель предзаполнен
    single = client.get("/single?cad_num=50:09:0060202:101&purpose=izhs").text
    assert 'value="50:09:0060202:101"' in single and 'value="izhs" selected' in single
    # чужой запрос недоступен, удаление своего работает
    client.post("/logout")
    _register(client, "buyer2@example.com", role="buyer")
    assert client.get(rurl).status_code == 404
    client.post("/logout")
    client.post("/login/password", data={"email": "buyer@example.com", "password": "12345678"})
    assert client.post(rurl + "/delete").status_code == 303
    assert client.get(rurl).status_code == 404
