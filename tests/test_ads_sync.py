"""Объявления ленты ads-api.ru в кабинете: запись в базу без персональных данных, загрузка
окном с продолжением после остановки, отказы ленты, цена рынка рядом с лотом в списке лотов и
в карточке, кнопка администратора."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from landcheck import ads_api, torgi_scan
from tests.test_ads_api import TOKEN, USER, _ad
from tests.test_torgi_web import FakeNspd, _admin, _seed, _sync
from tests.test_verification import _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

KALUGA = "Калужская область"
MSK_NOW = datetime(2026, 11, 18, 12, 0, 0)


def _near(i: int, lat: float, lon: float, *, km: float = 0.5, price: float = 3_000_000, **kw):
    """Объявление в `km` к северу от точки: 10 соток ИЖС."""
    stamp = kw.pop("time", ads_api.msk_now().strftime("%Y-%m-%d 10:00:00"))
    kw.setdefault("region", KALUGA)
    return _ad(
        id=i,
        price=price + i * 10_000,
        coords={"lat": str(lat + km / ads_api.KM_PER_DEG), "lng": str(lon)},
        url=f"https://www.avito.ru/kaluga/zemelnye_uchastki/uchastok_{i}",
        time=stamp,
        **kw,
    )


def _lot_point(lot_id: str = "ACT_0") -> tuple[float, float]:
    from landcheck.web.db import TorgiLot, session

    with session() as db:
        lot = db.get(TorgiLot, lot_id)
        return lot.lat, lot.lon


def test_store_keeps_usable_ads_without_personal_data(client):  # noqa: F811
    from landcheck.web import ads_sync
    from landcheck.web.db import MarketAd, session

    items = [_near(i, 54.5, 36.2) for i in range(1, 7)]
    items += [
        _near(20, 54.5, 36.2, region="Московская область"),  # лента принесла соседний регион
        _near(21, 54.5, 36.2, title="Участок 10 сот. (ИЖС), переуступка аренды"),
        _near(22, 54.5, 36.2, price=-22 * 10_000),  # цена 0: тестовый доступ
        _near(23, 54.5, 36.2, title="Участок 10 сот.", params={}),  # категория земли неизвестна
        {"title": "без номера"},
    ]
    stats: dict = {}
    before = ads_sync.version("40")
    assert ads_sync.store("40", KALUGA, items, stats) == 6
    assert ads_sync.version("40") == before + 1
    assert stats["got"] == 11 and stats["kept"] == 6
    assert stats["skip"] == {
        "other_region": 1,
        "lease": 1,
        "no_price": 1,
        "no_class": 1,
        "no_id": 1,
    }
    assert stats["sources"] == {"avito.ru": 10} and stats["metrics"] == {"₽": 10}
    assert stats["units"] == {"avito.ru": {"сот": 9}}  # поле «Площадь» сошлось с заголовком
    assert stats["has"] == {"lat": 10, "area_m2": 10, "cls": 9}
    with session() as db:
        rows = list(db.scalars(select(MarketAd).order_by(MarketAd.id)))
        assert [r.id for r in rows] == [1, 2, 3, 4, 5, 6] and rows[0].region == "40"
        assert rows[0].cls == "izhs" and rows[0].area_m2 == 1000 and rows[0].price == 3_010_000
        dump = json.dumps(
            [{c.name: getattr(r, c.name) for c in MarketAd.__table__.columns} for r in rows],
            ensure_ascii=False,
            default=str,
        )
    assert "Иван" not in dump and "8999" not in dump and "звоните" not in dump
    # то же объявление пришло снова с новой ценой — строка обновилась, а не задвоилась
    assert ads_sync.store("40", KALUGA, [_near(1, 54.5, 36.2, price=4_990_000)], bump=False) == 1
    assert ads_sync.version("40") == before + 1  # bump=False: расчёты лотов не тревожим
    with session() as db:
        assert db.get(MarketAd, 1).price == 5_000_000
        assert len(list(db.scalars(select(MarketAd)))) == 6


def test_market_order_manual_then_near_ads_then_district_ads():
    lot = {"id": "L", "kind": "sale", "vri_group": "housing", "district": "40:13"}
    lot["quarter"] = "40:13:120101"
    near = {"mid_m2": 3000.0, "min_m2": 2500.0, "max_m2": 3500.0, "source": "ads"}
    near.update(source_label="объявления в радиусе 2 км: 6 шт.", confidence=2, note=None)

    def row(source: str, mid: float, scope: str = "40:13") -> dict:
        return {"scope": scope, "kind": "sale", "vri_group": "any", "price_mid": mid} | {
            "source": source
        }

    def src(layer, near_=near, lot_=lot, ratio=None):
        mk = torgi_scan.market_for(lot_, {}, layer, 1.0, ratio, near=near_)
        return (mk["source_label"], round(mk["mid_m2"])) if mk else None

    assert src([]) == ("объявления в радиусе 2 км: 6 шт.", 3000)
    # объявления «по району» из слоя слабее объявлений вокруг самого лота
    assert src([row("ads", 100_000)]) == ("объявления в радиусе 2 км: 6 шт.", 3000)
    assert src([row("ads", 100_000)], near_=None) == ("объявления, 40:13", 1000)
    # ручной ввод и свои сделки сильнее — и в районе, и в квартале
    assert src([row("ads", 100_000), row("manual", 200_000)]) == ("слой рынка, 40:13", 2000)
    assert src([row("deals", 250_000)]) == ("свои сделки, 40:13", 2500)
    both = [row("manual", 200_000), row("deals", 400_000, "40:13:120101")]
    assert src(both) == ("свои сделки, 40:13:120101", 4000)
    # аренда: объявления о продаже напрямую не годятся, только через долю переуступки
    rent = {**lot, "kind": "rent"}
    assert src([], lot_=rent) is None
    label, mid = src([], lot_=rent, ratio=0.5)
    assert mid == 1500 and label.startswith("переуступка = 50% цены в собственности; объявления")
    assert src([], near_={**near, "mid_m2": None}) is None  # пустая оценка не подставляется


def test_scan_and_lot_page_price_lots_by_ads_nearby(client):  # noqa: F811
    from landcheck.web import ads_sync
    from landcheck.web.db import TorgiLot, TorgiMarket, session

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    url = "/tools/torgi-scan?region=40&roi=40"
    page = client.get(url).text
    assert "итоги торгов: " in page and "объявления в радиусе" not in page
    assert ads_sync.near_fn("40", []) is None  # объявлений нет — оценка прежняя

    lat, lon = _lot_point()
    ads = [_near(i, lat, lon, km=0.3 + i * 0.1) for i in range(1, 7)]
    ads.append(_near(7, lat, lon, km=0.4, price=900_000))  # втрое ниже медианы: подозрительно
    assert ads_sync.store("40", KALUGA, ads) == 7
    page = client.get(url).text  # расчёт списка устарел сам: у объявлений своя версия
    assert "объявления в радиусе 2 км: 6 шт." in page and "итоги торгов: " not in page
    assert "Вся выборка, 6 объявл.: от" in page
    # цена перепродажи теперь рыночная (3 млн за 10 соток): «интересен» разрешён
    from landcheck.web import torgi_sync

    lots = torgi_sync.lots_of("40")
    res = torgi_scan.scan(lots, near_fn=ads_sync.near_fn("40", lots))
    assert res["counts"]["go"] == 12 and res["market_based"] == 12
    assert torgi_scan.scan(lots)["counts"]["go"] == 0

    lot_page = client.get("/tools/torgi-lot/ACT_0?roi=40").text
    assert "Цена взята из объявлений о продаже вокруг участка" in lot_page
    assert "Ближайшие объявления из выборки: 6 из 6" in lot_page and "радиус 2 км" in lot_page
    assert "https://www.avito.ru/kaluga/zemelnye_uchastki/uchastok_1" in lot_page
    assert '<td class="num">0.4 км</td>' in lot_page and "вся выборка, 6 объявл." in lot_page
    assert "Подозрительно дёшево" in lot_page and "uchastok_7" in lot_page
    assert "Иван" not in lot_page and "8999" not in lot_page

    # у лота нет точки на карте — берём середину его квартала по соседним лотам
    with session() as db:
        lot = db.get(TorgiLot, "ACT_1")
        lot.lat = lot.lon = None
        db.commit()
    no_point = client.get("/tools/torgi-lot/ACT_1?roi=40").text
    assert "объявления в радиусе 2 км: 6 шт." in no_point
    assert "точка лота взята по кадастровому кварталу" in no_point

    # эксперт задал рынок района руками — объявления уступают
    with session() as db:
        db.add(TorgiMarket(scope="40:13", kind="sale", price_mid=200_000, source="manual"))
        db.commit()
    page = client.get(url).text
    assert "слой рынка, 40:13" in page and "объявления в радиусе" not in page


class Feed:
    """Лента в памяти: отдаёт объявления окна, новые первыми, не больше `limit`."""

    def __init__(self, items: list[dict]):
        self.items = items
        self.asked: list[tuple[str, str, str]] = []
        self.answer: dict | None = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
        self.asked.append((q["city"], q["date1"], q["date2"]))
        if self.answer is not None:
            return httpx.Response(200, json=self.answer)
        hit = [x for x in self.items if q["date1"] <= x["time"] <= q["date2"]]
        hit.sort(key=lambda x: x["time"], reverse=True)
        return httpx.Response(200, json={"code": 200, "data": hit[: int(q["limit"])]})

    def api(self, limit: int = 2) -> ads_api.AdsApiClient:
        return ads_api.AdsApiClient(
            USER,
            TOKEN,
            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            limit=limit,
            interval=0,
            sleep=lambda s: None,
        )


def test_sync_region_resumes_window_then_tops_up(client):  # noqa: F811
    from landcheck.web import ads_sync
    from landcheck.web.db import AdsSync, MarketAd, TorgiSync, session

    with session() as db:  # объявления грузим только по регионам, где собраны торги
        db.add(TorgiSync(region="40"))
        db.commit()
    days = [f"2026-11-{d:02d} 09:00:00" for d in (10, 11, 12, 13, 14)]
    feed = Feed([_near(i + 1, 54.5, 36.2, time=t) for i, t in enumerate(days)])
    feed.items.append(_near(90, 54.5, 36.2, time="2026-08-01 09:00:00"))  # старше окна в 90 дней
    api = feed.api()
    stats = ads_sync.sync_region("40", api, now=MSK_NOW, max_pages=1)
    assert stats["got"] == 2 and stats["kept"] == 2 and stats["requests"] == 1
    assert stats["window"] == {"lo": "2026-08-20 12:00:00", "hi": "2026-11-18 11:30:00"}
    assert feed.asked == [(KALUGA, "2026-08-20 12:00:00", "2026-11-18 11:30:00")]
    with session() as db:
        st = db.get(AdsSync, "40")
        assert st.state == "idle" and st.newest is None and st.finished_at is None
        assert st.pending["cursor"] == "2026-11-13 09:00:01" and st.count == 2
    assert ads_sync._next_region() == "40"  # незаконченное окно — сразу в очередь

    # следующий проход (хоть после рестарта) продолжает с места остановки, а не сначала
    stats = ads_sync.sync_region("40", api, now=MSK_NOW + timedelta(hours=5))
    assert feed.asked[1] == (KALUGA, "2026-08-20 12:00:00", "2026-11-13 09:00:01")
    # 2 с первого прохода + 7 со второго; на каждом стыке страниц одно объявление дважды
    assert stats["kept"] == 9 and stats["got"] == 9 and stats["requests"] == 5
    with session() as db:
        st = db.get(AdsSync, "40")
        assert st.pending is None and st.newest == "2026-11-18 11:30:00"
        assert st.state == "idle" and st.finished_at is not None and st.count == 5
        assert db.get(MarketAd, 90) is None
        # в базе состояния нет ни ключа, ни логина
        raw = json.dumps([st.stats, st.pending, st.error], ensure_ascii=False)
    assert TOKEN not in raw and USER not in raw
    assert ads_sync._next_region() is None  # собрано только что

    # догрузка через сутки: с прошлой границы с запасом в час, до «сейчас минус полчаса»
    feed.items.append(_near(6, 54.5, 36.2, time="2026-11-19 08:00:00"))
    feed.asked.clear()
    later = MSK_NOW + timedelta(days=1)
    assert ads_sync._next_region(ads_sync.utcnow() + timedelta(hours=25)) == "40"
    stats = ads_sync.sync_region("40", api, now=later)
    assert feed.asked == [(KALUGA, "2026-11-18 10:30:00", "2026-11-19 11:30:00")]
    assert stats["got"] == 1 and stats["window"]["lo"] == "2026-11-18 10:30:00"
    with session() as db:
        assert db.get(AdsSync, "40").count == 6 and db.get(MarketAd, 6) is not None

    # объявления старше года при завершении окна уходят из базы
    ads_sync.store("40", KALUGA, [_near(77, 54.5, 36.2, time="2025-01-01 09:00:00")])
    ads_sync.sync_region("40", api, now=later + timedelta(days=1))
    with session() as db:
        assert db.get(MarketAd, 77) is None and db.get(MarketAd, 6) is not None


def test_feed_refusals_stop_the_loop_until_asked_again(client, monkeypatch):  # noqa: F811
    from landcheck.web import ads_sync
    from landcheck.web.db import AdsSync, TorgiSync, session

    monkeypatch.setattr(ads_sync, "_fatal", None)
    monkeypatch.setattr(ads_sync, "_wanted", {})
    with session() as db:
        db.add_all([TorgiSync(region=r) for r in ("40", "04", "83")])
        db.commit()
    assert ads_sync._next_region() == "04"  # ни разу не собранные — по порядку
    assert ads_sync.sync_region("83", Feed([]).api()) == {}
    with session() as db:
        st = db.get(AdsSync, "83")
        assert st.state == "error" and "нет в справочнике ленты" in st.error

    feed = Feed([])
    feed.answer = {"code": 402, "error": "Срок действия token'а закончился"}
    api = feed.api()
    assert ads_sync.run_once(api) == "04"
    with session() as db:
        st = db.get(AdsSync, "04")
        assert st.state == "error" and st.error == "402: срок доступа закончился — нужна оплата"
        assert st.pending is not None  # окно не потеряно: после оплаты продолжим с него
    assert ads_sync._fatal == "402: срок доступа закончился — нужна оплата"
    # отказ по ключу: больше ни одного запроса, пока не попросят снова
    assert ads_sync.run_once(api) is None and len(feed.asked) == 1
    assert ads_sync.status("40")["fatal"].startswith("402")

    assert ads_sync.request("40") is False  # модуль выключен: логина и ключа нет
    monkeypatch.setattr(ads_sync, "enabled", lambda: True)
    assert ads_sync.request("40", full=True) is True and ads_sync._fatal is None
    assert ads_sync.status("40")["queued"] is True and ads_sync._next_region() == "40"
    feed.answer = None
    assert ads_sync.run_once(api) == "40" and ads_sync._wanted == {}
    assert ads_sync.status("40")["state"] == "idle"

    # временный отказ (связь, частота запросов) цикл не останавливает: повтор через полчаса
    feed.answer = {"code": 429, "error": "Превышен лимит частоты запросов"}
    ads_sync.request("40")
    assert ads_sync.run_once(api) == "40" and ads_sync._fatal is None
    st = ads_sync.status("40")
    assert st["state"] == "error" and st["error"] == "429: слишком частые запросы"
    with session() as db:
        db.get(AdsSync, "04").finished_at = ads_sync.utcnow()  # только что упал
        db.commit()
    assert ads_sync._next_region() is None
    soon = ads_sync.utcnow() + timedelta(minutes=31)
    assert ads_sync._next_region(soon) == "40"  # кто дольше ждёт повтора — первым


def test_loop_starts_only_with_credentials(client, monkeypatch):  # noqa: F811
    from landcheck.web import ads_sync
    from landcheck.web.config import settings

    assert ads_sync.enabled() is False and ads_sync.start_loop() is None
    monkeypatch.setattr(settings, "adsapi_user", USER)
    monkeypatch.setattr(settings, "adsapi_token", TOKEN)
    assert ads_sync.enabled() is False  # демо на фикстурах в сеть не ходит
    monkeypatch.setattr(settings, "fixtures", None)
    assert ads_sync.enabled() is True


def test_liquidity_page_shows_feed_state_and_admin_button(client, monkeypatch):  # noqa: F811
    from landcheck.web import ads_sync
    from landcheck.web.db import User, session

    monkeypatch.setattr(ads_sync, "_wanted", {})
    monkeypatch.setattr(ads_sync, "_fatal", None)
    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    url = "/tools/torgi-liquidity?region=40"
    assert "Объявления о продаже земли" not in client.get(url).text  # модуль выключен, пусто

    monkeypatch.setattr(ads_sync, "enabled", lambda: True)
    page = client.get(url).text
    assert "Объявления о продаже земли:</strong> пока не загружены" in page
    assert 'action="/tools/torgi-ads/sync"' in page and "Обновить объявления" in page
    stats: dict = {"requests": 1, "window": {"lo": "2026-08-20 12:00:00"}}
    stats["window"]["hi"] = "2026-11-18 11:30:00"
    lat, lon = _lot_point()
    ads_sync.store("40", KALUGA, [_near(i, lat, lon) for i in range(1, 4)] + [{"id": "x"}], stats)
    from landcheck.web.db import AdsSync

    with session() as db:
        db.add(AdsSync(region="40", newest="2026-11-18 11:30:00", stats=stats, count=3))
        db.commit()
    page = client.get(url).text
    assert "в базе 3, загружены по 2026-11-18 11:30 МСК" in page
    assert "Пришло 4, годятся в расчёт 3, запросов 1" in page and "нет номера — 1" in page
    assert "avito.ru (сот — 3)" in page and TOKEN not in page

    r = client.post("/tools/torgi-ads/sync", data={"region": "40"})
    assert r.status_code == 303 and r.headers["location"] == "/tools/torgi-liquidity?region=40"
    assert ads_sync.status("40")["queued"] is True
    assert "в очереди на загрузку" in client.get(url).text
    monkeypatch.setattr(ads_sync, "_fatal", "401: неверный логин или ключ")
    assert "Лента объявлений не отвечает как надо: 401: неверный логин или ключ" in (
        client.get(url).text
    )

    # эксперт с доступом к торгам видит состояние, но не кнопку и не разбор загрузки
    client.post("/logout")
    _register(client, "expert@example.com", role="broker")
    with session() as db:
        db.scalar(select(User).where(User.email == "expert@example.com")).pro_tools = True
        db.commit()
    page = client.get(url).text
    assert "Объявления о продаже земли" in page and "Обновить объявления" not in page
    assert "Разбор последней загрузки" not in page
    assert client.post("/tools/torgi-ads/sync", data={"region": "40"}).status_code == 404


def test_status_of_unknown_region(client):  # noqa: F811
    from landcheck.web import ads_sync

    st = ads_sync.status("83")
    assert st["known"] is False and st["count"] == 0 and st["state"] is None
    assert ads_sync.rows_for(None) == [] and ads_sync.rows_for({}) == []
    with pytest.raises(ads_api.AdsApiError):
        raise ads_api.AdsApiError(0, "нет связи")
