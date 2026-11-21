"""Хвосты зума 09.11: регистрация «физлицо или организация» (Влад), посадочная страница
проверки участка с запуском проверки сразу после регистрации (Антон), карта соседних
объявлений с ценами (Влад)."""

from __future__ import annotations

import json

from sqlalchemy import select

from tests.test_verification import _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

CAD = "50:09:0060202:101"


def _user(email: str):
    from landcheck.web.db import User, session

    with session() as db:
        return db.scalar(select(User).where(User.email == email))


def _reg(c, **data):
    base = {"email": "u@example.com", "password": "12345678", "name": "Имя", "role": "buyer"}
    return c.post("/register", data={**base, **data})


def test_register_form_asks_person_or_company_first(client):  # noqa: F811
    page = client.get("/login?tab=register").text
    assert "Вы регистрируетесь как" in page and "Физическое лицо" in page
    assert "Организация или ИП" in page and "Чем занимается организация" in page
    assert page.index("Вы регистрируетесь как") < page.index("Кто вы")
    assert "Агентство недвижимости, брокер" in page and 'name="inn"' in page


def test_register_person_keeps_chosen_role(client):  # noqa: F811
    assert _reg(client, email="p@example.com", entity="person", role="seller").status_code == 303
    u = _user("p@example.com")
    assert (u.entity, u.role, u.org_kind, u.inn, u.org_label) == (
        "person",
        "seller",
        None,
        None,
        None,
    )


def test_register_company_role_follows_org_kind(client):  # noqa: F811
    r = _reg(
        client,
        email="dev@example.com",
        entity="company",
        org_kind="developer",
        brand_name="ООО «Поле»",
        inn="7701234567",
        role="buyer",  # скрытое поле роли физлица приходит, но организации не указ
    )
    assert r.status_code == 303
    u = _user("dev@example.com")
    assert (u.entity, u.org_kind, u.role, u.inn) == ("company", "developer", "seller", "7701234567")
    assert u.brand == "ООО «Поле»" and u.org_label == "Застройщик, девелопер, владелец земли"
    client.post("/logout")
    client.cookies.clear()
    assert (
        _reg(
            client,
            email="ag@example.com",
            entity="company",
            org_kind="agency",
            brand_name="АН Земля",
        ).status_code
        == 303
    )
    assert _user("ag@example.com").role == "broker"
    client.post("/logout")
    client.cookies.clear()
    assert (
        _reg(
            client,
            email="inv@example.com",
            entity="company",
            org_kind="нет такого",
            brand_name="Фонд",
            inn="",
        ).status_code
        == 303
    )
    u = _user("inv@example.com")
    assert (u.org_kind, u.role, u.inn) == ("other", "broker", None)


def test_register_company_validation_keeps_form(client):  # noqa: F811
    r = _reg(client, email="x@example.com", entity="company", org_kind="agency", brand_name="")
    assert r.status_code == 200 and "Укажите название организации или ИП" in r.text
    assert 'value="x@example.com"' in r.text  # введённое не теряется
    r = _reg(client, email="x@example.com", entity="company", brand_name="АН", inn="12345")
    assert r.status_code == 200 and "ИНН — 10 цифр у организации или 12 у ИП" in r.text
    assert _user("x@example.com") is None
    # ИП с 12 цифрами и пробелами в ИНН
    assert (
        _reg(
            client,
            email="x@example.com",
            entity="company",
            brand_name="ИП Петров",
            inn="7701 2345 6789",
        ).status_code
        == 303
    )
    assert _user("x@example.com").inn == "770123456789"


def test_profile_switches_entity(client):  # noqa: F811
    _register(client, "sw@example.com", role="broker")
    page = client.get("/profile").text
    assert 'name="entity"' in page and "Чем занимается организация" in page
    data = {
        "name": "Имя",
        "brand_name": "АН Юг",
        "role": "broker",
        "entity": "company",
        "org_kind": "agency",
        "inn": "7701234567",
    }
    assert client.post("/profile", data=data).headers["location"] == "/profile?saved=1"
    u = _user("sw@example.com")
    assert (u.entity, u.org_kind, u.inn) == ("company", "agency", "7701234567")
    r = client.post("/profile", data={**data, "inn": "123"})
    assert "error=" in r.headers["location"] and _user("sw@example.com").inn == "7701234567"
    client.post("/profile", data={**data, "entity": "person"})
    u = _user("sw@example.com")
    assert (u.entity, u.org_kind, u.inn) == ("person", None, None)
    # администратор видит вид организации в списке пользователей
    from tests.test_verification import _make_admin

    client.post("/profile", data=data)
    _make_admin("sw@example.com")
    assert "Агентство недвижимости, брокер · ИНН 7701234567" in client.get("/admin/users").text


def test_landing_is_public_and_explains_the_report(client):  # noqa: F811
    r = client.get("/proverka")
    assert r.status_code == 200
    for text in (
        "Проверьте участок до сделки",
        "Права и обременения",
        "Зоны с ограничениями",
        "Торги",
        "Кому это нужно",
        "не юридическое заключение",
        'action="/proverka"',
    ):
        assert text in r.text
    assert "Посмотреть пример отчёта" not in r.text  # ссылка на пример не настроена
    from landcheck.web.config import settings

    settings.demo_report_url = "https://example.com/r/demo"
    try:
        assert "https://example.com/r/demo" in client.get("/proverka").text
    finally:
        settings.demo_report_url = None
    assert '<a href="/proverka">Что входит в проверку</a>' in client.get("/login").text


def test_landing_number_survives_registration_and_starts_check(client):  # noqa: F811
    bad = client.post("/proverka", data={"cad_num": "участок у леса"})
    assert bad.status_code == 400 and "Не похоже на кадастровый номер" in bad.text
    assert 'value="участок у леса"' in bad.text
    # номер с точками и пробелами — тоже номер
    r = client.post("/proverka", data={"cad_num": " 50.09.0060202.101 ", "purpose": "izhs"})
    assert r.status_code == 303 and r.headers["location"] == "/login?tab=register"
    page = client.get("/login?tab=register").text
    assert f"Участок <code>{CAD}</code> ждёт проверки" in page
    r = _reg(client, email="new@example.com")
    assert r.status_code == 303 and r.headers["location"].startswith("/checks/")
    from landcheck.web.db import Check, session

    with session() as db:
        check = db.scalar(select(Check))
        assert check.title == f"Разовая проверка {CAD}" and check.request["purpose"] == "izhs"
        assert check.user_id == _user("new@example.com").id
    report = client.get(r.headers["location"])
    assert report.status_code == 200 and CAD in report.text
    # номер из cookie использован один раз: повторный вход новую проверку не создаёт
    client.post("/logout")
    r = client.post("/login/password", data={"email": "new@example.com", "password": "12345678"})
    assert r.headers["location"] == "/"
    with session() as db:
        assert len(list(db.scalars(select(Check)))) == 1


def test_landing_for_logged_in_user_goes_straight_to_report(client):  # noqa: F811
    _register(client, "in@example.com", role="buyer")
    r = client.post("/proverka", data={"cad_num": CAD, "purpose": "мусор"})
    assert r.status_code == 303 and r.headers["location"].startswith("/checks/")
    from landcheck.web.db import Check, session

    with session() as db:
        assert db.scalar(select(Check)).request["purpose"] == "any"


def _publish(client, cad: str, price: str) -> int:  # noqa: F811
    from landcheck.web.db import Listing, session

    url = client.post("/listing", data={"cad_num": cad, "price": price}).headers["location"]
    lid = int(url.rsplit("/", 1)[1])
    with session() as db:
        row = db.get(Listing, lid)
        row.status, row.published = "verified", True
        db.commit()
    return lid


def test_nearby_listings_map_with_prices(client):  # noqa: F811
    _register(client, "s@example.com")
    a = _publish(client, "50:09:0060202:101", "2500000")
    b = _publish(client, "50:09:0060202:108", "850000")
    page = client.get(f"/l/{a}").text
    assert "Рядом продаётся" in page and 'id="listing-map"' in page
    # подписи цен в данных карты (в разметке кириллица экранирована, как в любом JSON)
    assert json.dumps("2,5 млн") in page and json.dumps("850 тыс.") in page
    assert '"current": true' in page and "leaflet@1.9.4" in page and "integrity=" in page
    # точка посчитана из кэша участка один раз и сохранена в объявлении
    from landcheck.web.db import Listing, session

    with session() as db:
        point = db.get(Listing, b).data["point"]
        assert abs(point[0] - 55.0005) < 0.01 and abs(point[1] - 37.0005) < 0.01
    market = client.get("/market").text
    assert "Показать на карте: 2 из 2 участков с границами" in market
    assert 'id="listing-map"' in market


def test_listing_without_borders_has_no_point_and_no_map(client):  # noqa: F811
    _register(client, "s2@example.com")
    lid = _publish(client, "50:09:0060202:103", "2400000")  # участок без границ
    page = client.get(f"/l/{lid}").text
    assert 'id="listing-map"' not in page
    from landcheck.web.db import Listing, session

    with session() as db:
        assert db.get(Listing, lid).data["point"] is None


# --- ТЗ v3, п. 6: «данные расходятся» — выписка ЕГРН ↔ НСПД по самому участку --------------
def test_land_diff_area_category_vri():
    from landcheck import egrn

    same = {
        "area_m2": 1010.0,
        "category": "Земли населённых пунктов",
        "permitted_use": "для индивидуального жилищного строительства",
    }
    nspd = dict(
        area_m2=1000.0,
        category="Земли населенных пунктов",
        permitted_use="Для индивидуального жилищного строительства",
    )
    assert egrn.land_diff(same, **nspd) == []  # 1 % по площади и «ё» — не расхождение
    other = {
        "area_m2": 1500.0,
        "category": "Земли сельскохозяйственного назначения",
        "permitted_use": "Для сельскохозяйственного производства",
    }
    rows = egrn.land_diff(other, **nspd)
    assert [r["what"] for r in rows] == [
        "площадь",
        "категория земли",
        "вид разрешённого использования",
    ]
    assert "в выписке 1 500 м², по НСПД 1 000 м² (разница 33%)" in rows[0]["text"]
    assert egrn.land_diff({"area_m2": None, "category": None, "permitted_use": None}, **nspd) == []


def _check_item(client) -> str:  # noqa: F811
    _register(client, "r@example.com", role="broker")
    url = client.post("/checks", data={"numbers": CAD}).headers["location"]
    return f"{url}/items/{CAD}"


def _upload(client, item: str, xml: bytes):  # noqa: F811
    import io

    return client.post(
        item + "/rights", files=[("files", ("egrn.xml", io.BytesIO(xml), "application/xml"))]
    )


def test_report_shows_extract_vs_nspd_discrepancy(client):  # noqa: F811
    from tests.test_egrn_xml import build

    item = _check_item(client)
    # выписка сходится с НСПД — строки «данные расходятся» нет
    assert _upload(client, item, build()).headers["location"].endswith("#rights")
    assert "Данные расходятся" not in client.get(item).text
    # в выписке другая категория и ВРИ, а площадь 1 200 м² против 1 000 м² в НСПД
    xml = build(
        category="Земли сельскохозяйственного назначения",
        vri="Для сельскохозяйственного производства",
    )
    xml = xml.replace(b"<area><value>1000</value>", b"<area><value>1200</value>")
    assert _upload(client, item, xml).status_code == 303
    page = client.get(item).text
    assert "Данные расходятся" in page
    assert "Выписка ↔ НСПД, площадь" in page and "в выписке 1 200 м², по НСПД 1 000 м²" in page
    assert "Выписка ↔ НСПД, категория земли" in page
    assert "Выписка ↔ НСПД, вид разрешённого использования" in page
    from landcheck.web.db import CheckItem, session

    with session() as db:
        doc = db.scalar(select(CheckItem)).data["rights_doc"]
        assert set(doc) == {"area_m2", "category", "permitted_use"}  # людей в фактах нет


def test_extract_for_another_parcel_is_rejected(client):  # noqa: F811
    from tests.test_egrn_xml import build

    item = _check_item(client)
    xml = build().replace(b"<cad_number>50:09:0060202:101", b"<cad_number>50:09:0060202:777")
    r = _upload(client, item, xml)
    assert r.status_code == 303 and r.headers["location"].endswith("?err=rights_cad#rights")
    page = client.get(r.headers["location"]).text
    assert "Выписка выдана на другой кадастровый номер" in page
    from landcheck.web.db import CheckItem, session

    with session() as db:
        data = db.scalar(select(CheckItem)).data
        assert "rights_doc" not in data  # чужие права к отчёту не приложены
        assert (data.get("rights") or {}).get("source") in (None, "nspd")
