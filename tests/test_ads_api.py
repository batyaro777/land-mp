"""Лента объявлений ads-api.ru: названия регионов, клиент (окно по времени, отказы, ключ не
утекает в ошибки), разбор объявления и цена рынка по объявлениям рядом с участком."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from landcheck import ads_api
from landcheck.regions import CADASTRAL_REGIONS

TOKEN = "s3cr3t-token-value"
USER = "user@example.test"


def test_region_names_come_from_feed_list():
    names = {code: ads_api.region_name(code) for code in CADASTRAL_REGIONS}
    assert {c for c, n in names.items() if n is None} == {"83"}  # Ненецкого АО в ленте нет
    assert all(n in ads_api.REGION_NAMES for n in names.values() if n)
    assert names["04"] == "Алтай" and names["22"] == "Алтайский край"
    assert names["24"] == "Красноярский край" and names["16"] == "Татарстан"
    assert names["14"] == "Саха (Якутия)" and names["86"] == "Ханты-Мансийский АО"
    assert names["20"] == "Чеченская Республика" and names["90"] == "Крым"
    assert names["88"] == "Красноярский край"  # старый округ → нынешний субъект
    assert ads_api.region_name(None) is None and ads_api.region_name("00") is None
    assert len(ads_api.REGION_NAMES) == 84  # 85 субъектов без Ненецкого АО


def _client(handler, **kw) -> ads_api.AdsApiClient:
    return ads_api.AdsApiClient(
        USER,
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=kw.pop("sleep", lambda s: None),
        **kw,
    )


def test_page_sends_documented_params_only():
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        assert url.netloc == "ads-api.ru" and url.path == "/main/api"
        seen.append({k: v[0] for k, v in parse_qs(url.query).items()})
        return httpx.Response(200, json={"code": 200, "status": "ok", "data": [{"id": 1}, "x"]})

    api = _client(handler, limit=50)
    items = api.page("Красноярский край", "2026-11-01 00:00:00", "2026-11-02 00:00:00")
    assert items == [{"id": 1}]  # мусор в data не проходит
    assert seen == [
        {
            "user": USER,
            "token": TOKEN,
            "category_id": "5",
            "nedvigimost_type": "1",
            "city": "Красноярский край",
            "date1": "2026-11-01 00:00:00",
            "date2": "2026-11-02 00:00:00",
            "withcoords": "1",
            "withphone": "0",
            "limit": "50",
        }
    ]


def test_errors_keep_the_key_out_of_messages():
    def bad_key(req):
        return httpx.Response(200, json={"code": 401, "error": "Неверный user или token"})

    with pytest.raises(ads_api.AdsApiError) as e:
        _client(bad_key).page("Алтай", "a", "b")
    assert e.value.code == 401 and e.value.code in ads_api.FATAL_CODES
    assert "неверный логин или ключ" in str(e.value) and TOKEN not in str(e.value)

    def captcha(req):
        return httpx.Response(200, text="<html>captcha</html>")

    with pytest.raises(ads_api.AdsApiError) as e:
        _client(captcha).page("Алтай", "a", "b")
    assert "не в формате ленты" in str(e.value)

    def down(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")  # в тексте httpx — адрес с ключом

    pauses: list[float] = []
    api = _client(down, sleep=pauses.append, interval=0)
    with pytest.raises(ads_api.AdsApiError) as e:
        api.page("Алтай", "a", "b")
    assert e.value.code == 0 and str(e.value) == "нет связи с лентой: ConnectError"
    assert TOKEN not in str(e.value) and USER not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert api.requests == 3 and pauses == list(ads_api.TRANSPORT_RETRY_S)

    def unknown(req):
        body = {"code": 400, "error": "В запросе передан несуществующий параметр " + "я" * 400}
        return httpx.Response(200, json=body)

    with pytest.raises(ads_api.AdsApiError) as e:
        _client(unknown).page("Алтай", "a", "b")
    assert e.value.code == 400 and len(e.value.text) == 200


def test_rate_limit_is_retried_and_requests_are_spaced():
    answers = [{"code": 429, "error": "Превышен лимит"}, {"code": 200, "data": [], "status": "ok"}]
    clock = {"t": 100.0}
    pauses: list[float] = []

    def sleep(s: float) -> None:
        pauses.append(s)
        clock["t"] += s

    api = _client(
        lambda req: httpx.Response(200, json=answers.pop(0)),
        sleep=sleep,
        clock=lambda: clock["t"],
        interval=5.5,
    )
    assert api.page("Алтай", "a", "b") == []
    # после 429 — пауза вдвое длиннее положенной, второй запрос не раньше чем через 5,5 с
    assert pauses == [11.0] and api.requests == 2
    clock["t"] += 2.0
    api._c = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 200, "data": []}))
    )
    api.page("Алтай", "a", "b")
    assert pauses == [11.0, 3.5]


def _item(i: int, stamp: str) -> dict:
    return {"id": i, "time": stamp}


def test_pages_walk_the_window_from_new_to_old():
    # 7 объявлений, по одному в 10 секунд: 10:00:10 … 10:01:10
    feed = [_item(i, f"2026-11-10 10:0{i // 6}:{i % 6}0") for i in range(1, 8)]
    asked: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
        asked.append((q["date1"], q["date2"]))
        hit = [x for x in feed if q["date1"] <= x["time"] <= q["date2"]]
        hit.sort(key=lambda x: x["time"], reverse=True)
        return httpx.Response(200, json={"code": 200, "data": hit[: int(q["limit"])]})

    api = _client(handler, limit=3, interval=0)
    lo, hi = "2026-11-10 00:00:00", "2026-11-10 12:00:00"
    got = list(api.pages("Алтай", lo, hi, max_pages=10))
    ids = [x["id"] for items, _ in got for x in items]
    # ничего не потеряно; на стыке страниц одно объявление приходит дважды — его снимет база
    assert set(ids) == set(range(1, 8)) and len(ids) == 10
    stops = ["2026-11-10 10:00:51", "2026-11-10 10:00:31", "2026-11-10 10:00:11", None]
    assert [c for _, c in got] == stops
    assert asked[0] == (lo, hi) and asked[1] == (lo, "2026-11-10 10:00:51")

    # предел страниц: остановились с курсором — следующий проход продолжит с него
    api = _client(handler, limit=3, interval=0)
    part = list(api.pages("Алтай", lo, hi, max_pages=1))
    assert len(part) == 1 and part[0][1] == "2026-11-10 10:00:51"
    rest = list(api.pages("Алтай", lo, part[0][1], max_pages=10))
    assert rest[-1][1] is None

    # полная страница в одной секунде: курсор всё равно идёт назад, цикла нет
    same = [_item(i, "2026-11-10 10:00:00") for i in range(5)]

    def stuck(req: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
        hit = [x for x in same if q["date1"] <= x["time"] <= q["date2"]]
        return httpx.Response(200, json={"code": 200, "data": hit[:3]})

    api = _client(stuck, limit=3, interval=0)
    out = list(api.pages("Алтай", lo, "2026-11-10 10:00:00", max_pages=10))
    assert [c for _, c in out] == ["2026-11-10 09:59:59", None]


def _ad(**kw) -> dict:
    base = {
        "id": 628486379,
        "title": "Участок 10 сот. (ИЖС)",
        "price": 1_500_000,
        "price_metric": "₽",
        "time": "2026-11-10 10:00:00",
        "coords": {"lat": "56.0101", "lng": "92.8525"},
        "url": "https://www.avito.ru/krasnoyarsk/zemelnye_uchastki/uchastok_10_sot._izhs_1",
        "source": "avito.ru",
        "region": "Красноярский край",
        "city1": "Красноярск",
        "address": "Красноярск, мкр. Солонцы",
        "params": {"Площадь": "10", "Категория земель": "Поселений (ИЖС)"},
        "phone": "89990000000",
        "person": "Иван Иванов",
        "contactname": "Иван",
        "description": "Продаю участок, звоните Ивану",
    }
    return {**base, **kw}


def test_parse_ad_takes_price_area_point_and_no_personal_data():
    ad = ads_api.parse_ad(_ad())
    assert ad["skip"] is None and ad["cls"] == "izhs" and ad["area_m2"] == 1000.0
    assert (ad["lat"], ad["lon"], ad["price"]) == (56.0101, 92.8525, 1_500_000.0)
    assert ad["unit"] == "сот" and ad["posted"] == "2026-11-10 10:00:00"
    assert ad["region_name"] == "Красноярский край"
    # телефон, имя продавца и текст описания в строку не попадают
    assert not {"phone", "person", "contactname", "description"} & set(ad)
    assert "Иван" not in str(ad) and "8999" not in str(ad)

    assert ads_api.parse_ad({"title": "без номера"}) is None
    big = ads_api.parse_ad(_ad(title="Участок 1,5 га (СНТ, ДНП)", params={}))
    assert big["area_m2"] == 15_000.0 and big["cls"] == "snt" and big["unit"] is None
    assert ads_api.parse_ad(_ad(title="Участок 2 га (промназначения)", params={}))["cls"] == "prom"
    assert ads_api.parse_ad(_ad(title="Участок 10 сот.", params={}))["cls"] is None

    def skip(**kw) -> str | None:
        return ads_api.parse_ad(_ad(**kw))["skip"]

    assert skip(price=0) == "no_price"  # тестовый доступ: цена всегда 0
    assert skip(price=500) == "no_price"
    assert skip(price_metric="₽ в месяц") == "metric"
    assert skip(coords={"lat": "0", "lng": "0"}) == "no_coords" and skip(coords=None) == "no_coords"
    assert skip(title="Участок (ИЖС)") == "no_area"
    assert skip(title="Участок 10 сот. (ИЖС), переуступка аренды") == "lease"
    assert skip(title="Дом 120 м² на участке 10 сот.") == "house"
    assert skip(time=None) == "no_time"
    # единица поля «Площадь» выясняется сверкой с заголовком
    assert ads_api.param_unit(1000.0, 1000.0) == "м2" and ads_api.param_unit(15_000.0, 1.5) == "га"
    assert ads_api.param_unit(1000.0, 37.0) == "другое" and ads_api.param_unit(None, 10) is None


NOW = datetime(2026, 11, 18, 12, 0, 0)
LAT, LON = 56.0, 93.0
KM_LAT = 1 / ads_api.KM_PER_DEG  # градусов широты в километре


def _row(i, km, price, area=1000.0, cls="izhs", posted="2026-11-01 10:00:00", east=0.0):
    return (i, LAT + km * KM_LAT, LON + east, float(price), float(area), cls, posted)


def test_near_market_widens_radius_until_enough_comparable_ads():
    rows = [_row(i, 1.0 + i * 0.1, 1_000_000 + i * 100_000) for i in range(3)]  # 3 в 2 км
    rows += [_row(10 + i, 4.0 + i * 0.1, 1_300_000 + i * 100_000) for i in range(3)]  # ещё 3 в 5 км
    rows += [
        _row(20, 1.0, 9_000_000, cls="prom"),  # другая категория земли
        _row(21, 1.0, 1_000_000, area=100_000),  # площадь в 100 раз больше лота
        _row(22, 1.0, 1_000_000, posted="2025-01-01 10:00:00"),  # старше полугода
        _row(23, 30.0, 1_000_000),  # дальше 20 км
    ]
    index = ads_api.AdIndex(rows)
    assert index.size == 10
    mk = ads_api.near_market((LAT, LON), 1000.0, "housing", index, now=NOW)
    assert mk["source"] == "ads" and mk["near"] is True and mk["confidence"] == 2
    assert mk["source_label"] == "объявления в радиусе 5 км: 6 шт." and mk["radius_km"] == 5.0
    assert mk["ads_n"] == 6 and mk["mid_m2"] == pytest.approx(1250.0)
    assert mk["min_m2"] < mk["mid_m2"] < mk["max_m2"]
    assert (mk["ads_lo_m2"], mk["ads_hi_m2"]) == (1000.0, 1500.0)
    assert list(mk["ad_dist"]) == [0, 1, 2, 10, 11, 12]  # ближние первыми
    assert mk["ad_dist"][0] == 1.0 and "земли поселений (ИЖС)" in mk["note"]
    assert "по кадастровому кварталу" not in mk["note"]
    approx = ads_api.near_market((LAT, LON), 1000.0, "housing", index, now=NOW, approx=True)
    assert "точка лота взята по кадастровому кварталу" in approx["note"]

    # мало объявлений даже в 20 км, нет точки, площади или сопоставимой категории — цены нет
    assert ads_api.near_market((LAT, LON), 1000.0, "industry", index, now=NOW) is None
    assert ads_api.near_market(None, 1000.0, "housing", index, now=NOW) is None
    assert ads_api.near_market((LAT, LON), None, "housing", index, now=NOW) is None
    assert ads_api.near_market((LAT, LON), 1000.0, "recreation", index, now=NOW) is None
    assert ads_api.near_market((LAT, LON), 1000.0, "housing", None, now=NOW) is None
    assert ads_api.near_market((LAT, LON), 1000.0, "housing", ads_api.AdIndex([]), now=NOW) is None
    # сад и поле — одна категория объявлений, разводит площадь
    assert ads_api.CLASS_OF_GROUP["garden"] == ads_api.CLASS_OF_GROUP["agri"] == "snt"


def test_near_market_drops_twins_and_suspiciously_cheap():
    rows = [_row(i, 0.5 + i * 0.1, 1_000_000 + i * 50_000) for i in range(6)]
    rows.append(_row(50, 0.5, 1_000_000))  # то же объявление на другой площадке
    rows.append(_row(51, 0.6, 300_000))  # в 3,8 раза ниже медианы — аренда под видом продажи
    rows.append(_row(52, 0.7, 90_000_000))  # в десятки раз выше — выброс
    mk = ads_api.near_market((LAT, LON), 1000.0, "housing", ads_api.AdIndex(rows), now=NOW)
    assert mk["radius_km"] == 2.0 and mk["ads_n"] == 6
    assert 50 not in mk["ad_dist"] and 51 not in mk["ad_dist"] and 52 not in mk["ad_dist"]
    assert list(mk["cheap_dist"]) == [51] and "подозрительно дешёвых не учтено: 1" in mk["note"]
    assert mk["mid_m2"] == pytest.approx(1125.0)


def test_index_measures_distance_along_longitude_too():
    k_lon = ads_api.KM_PER_DEG * 0.5591929  # cos(56°)
    index = ads_api.AdIndex([_row(1, 0.0, 1_000_000, east=3.0 / k_lon)])
    assert index.around(LAT, LON, 2.0) == []
    ((dist, row),) = index.around(LAT, LON, 5.0)
    assert dist == pytest.approx(3.0, abs=0.01) and row[0] == 1
