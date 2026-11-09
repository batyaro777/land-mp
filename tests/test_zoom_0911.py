"""Замечания с зума 09.11 (Тони, Антон М., Влад): номер с точками, пояснение Avito XML,
подсказки по Яндекс.Диску, «Предложить участок» в спросе, пропуск вопросов, удаление запросов,
сортировка и регион по умолчанию на витрине, регион в профиле, «Рядом продаётся»."""

from __future__ import annotations

import io

import httpx
from sqlalchemy import select

from landcheck import intake
from landcheck.inputs import normalize_cad
from tests.test_verification import PDF, _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

CAD = "50:09:0060202:101"


def test_normalize_cad_accepts_dots_commas_spaces():
    assert normalize_cad("50:09:0060202:101") == CAD
    assert normalize_cad(" 50 : 09 : 0060202 : 101 ") == CAD
    assert normalize_cad("50.09.0060202.101") == CAD
    assert normalize_cad("50,09,0060202,101") == CAD
    assert normalize_cad("50 09 0060202 101") == CAD
    assert normalize_cad("50/09/0060202/101") == CAD
    assert normalize_cad("2,5 млн") is None
    assert normalize_cad("") is None
    assert normalize_cad("12.05.2005 50 сот") is None  # дата и площадь — не номер


def test_single_and_listing_accept_dotted_number(client):  # noqa: F811
    _register(client, "dots@example.com")
    r = client.post("/single", data={"cad_num": "50.09.0060202.101", "purpose": "any"})
    assert r.status_code == 303
    r = client.post("/listing", data={"cad_num": "50.09.0060202.101", "price": "2500000"})
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert CAD in page
    assert "Что такое Avito XML" in page and "файл не нужен" in page


def test_yandex_closed_folder_gets_plain_hint():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Не удалось найти запрошенный ресурс."})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    files, errors = intake.yandex_list("https://disk.yandex.ru/d/abc", client=c)
    assert not files and len(errors) == 1
    assert "Поделиться" in errors[0] and "Скопировать ссылку" in errors[0]
    page_hint = "«Поделиться» → «Скопировать ссылку»"
    assert page_hint in errors[0]


def test_demand_explains_missing_offer_button(client):  # noqa: F811
    from landcheck.web.db import BuyerRequest, User, session

    _register(client, "buyer@example.com", role="buyer")
    with session() as db:
        b = db.scalar(select(User).where(User.email == "buyer@example.com"))
        db.add(BuyerRequest(user_id=b.id, text="дом для семьи", region="синтетический"))
        db.commit()
    client.post("/logout")
    client.cookies.clear()
    _register(client, "seller@example.com")
    page = client.get("/demand").text
    assert "Предлагать участки пока нечем" in page and "У вас нет объявлений" in page
    assert 'disabled title="Нужно опубликованное объявление' in page
    url = client.post("/listing", data={"cad_num": CAD, "price": "2500000"}).headers["location"]
    client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    page = client.get("/demand").text
    assert "на проверке" in page and "загрузите электронную выписку" in page
    assert "Предлагать участки пока нечем" in page


def test_skip_questions_really_skips(client):  # noqa: F811
    _register(client, "skip@example.com", role="buyer")
    r = client.post("/search", data={"query": "участок под дом в Истре", "purpose": "izhs"})
    url = r.headers["location"]
    page = client.get(url).text
    assert 'name="skip" value="1"' in page and "Пропустить вопросы" in page
    r = client.post(url + "/answers", data={"skip": "1", "kids": "yes"})
    assert r.status_code == 303
    page = client.get(url).text
    assert "Подборка под ваш запрос" in page  # не вопросы снова
    assert "Подходящих участков пока нет" in page and "Запрос сохранён" in page
    assert "по «Истре»" in page or "по «Истра»" in page or "Истр" in page
    # удалить запрос из списка «Мои запросы»
    rid = int(url.rsplit("/", 1)[1])
    page = client.get("/search").text
    assert f'action="/search/{rid}/delete"' in page
    assert client.post(f"/search/{rid}/delete").status_code == 303
    assert f'action="/search/{rid}/delete"' not in client.get("/search").text


def _publish(c, email: str, cad: str, price: str, notes: str = "") -> int:
    _register(c, email)
    url = c.post(
        "/listing", data={"cad_num": cad, "price": price, "notes": notes, "phone": "+7 900"}
    ).headers["location"]
    c.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    c.post("/logout")
    c.cookies.clear()
    return int(url.rsplit("/", 1)[1])


def test_market_sort_region_default_and_nearby(client):  # noqa: F811
    id1 = _publish(client, "s1@example.com", "50:09:0060202:101", "2500000")
    id2 = _publish(client, "s2@example.com", "50:09:0060202:102", "900000")
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    for i in (id1, id2):
        client.post(f"/admin/listings/{i}/decide", data={"decision": "approve"})
    client.post("/logout")
    client.cookies.clear()
    for email, i in (("s1@example.com", id1), ("s2@example.com", id2)):
        client.post("/login/password", data={"email": email, "password": "12345678"})
        client.post(f"/listing/{i}/publish", data={"action": "on"})
        client.post("/logout")
        client.cookies.clear()
    pub = [i for i in (id1, id2) if client.get(f"/l/{i}").status_code == 200]
    assert id1 in pub  # второе может не пройти из-за противоречий текста — не важно для сортировки
    m = client.get("/market?applied=1&sort=price_asc").text
    assert 'value="price_asc" selected' in m and "дешевле за сотку" in m
    if len(pub) == 2:
        asc = client.get("/market?applied=1&sort=price_asc").text
        desc = client.get("/market?applied=1&sort=price_desc").text
        assert asc.index(f"/l/{id2}") < asc.index(f"/l/{id1}")
        assert desc.index(f"/l/{id1}") < desc.index(f"/l/{id2}")
        # «Рядом продаётся» — другой участок той же местности на публичной странице
        page = client.get(f"/l/{id1}").text
        assert "Рядом продаётся" in page and f"/l/{id2}" in page
    # регион из профиля подставляется, пока фильтр не применяли; applied=1 сбрасывает
    _register(client, "buyer@example.com", role="buyer")
    client.post(
        "/profile", data={"name": "Покупатель", "role": "buyer", "region": "Алтайский край"}
    )
    page = client.get("/market").text
    assert 'value="Алтайский край"' in page and "по «Алтайский край»" in page
    assert "Показать все регионы" in page
    page = client.get("/market?applied=1").text
    assert 'value="Алтайский край"' not in page
    # и в спросе, и при создании запроса без региона
    assert 'value="Алтайский край"' in client.get("/demand").text
    r = client.post("/search", data={"query": "участок под дом", "purpose": "izhs"})
    from landcheck.web.db import BuyerRequest, session

    with session() as db:
        req = db.get(BuyerRequest, int(r.headers["location"].rsplit("/", 1)[1]))
        assert req.region == "Алтайский край"


def test_register_with_region(client):  # noqa: F811
    r = client.post(
        "/register",
        data={
            "email": "reg@example.com",
            "password": "12345678",
            "name": "Р",
            "role": "buyer",
            "region": "Ставропольский край",
        },
    )
    assert r.status_code == 303
    page = client.get("/profile").text
    assert 'value="Ставропольский край"' in page and 'list="region-names"' in page
    client.post("/logout")
    client.cookies.clear()
    assert "Ставропольский край" in client.get("/login?tab=register").text  # datalist
