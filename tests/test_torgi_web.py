"""Анализатор торгов в кабинете: сбор региона в базу (поиск → карточки → НСПД), доступ,
страницы лотов, ликвидности, слоя рынка и журнала сделок, выгрузки, проверка отобранных."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from landcheck import torgi
from tests.helpers import make_payload
from tests.test_torgi import _card, _notice, _page
from tests.test_verification import _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def M(v: float) -> str:
    """Сумма так, как её печатают страницы."""
    from landcheck.web.serialize import fmt_money

    return fmt_money(v)


NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
# Страницы считают «осталось дней» от сегодняшней даты, поэтому срок заявок у идущих лотов —
# от неё же: с жёсткой датой тесты страниц падали на следующий день после написания.
BIDD_END = (datetime.now(UTC).date() + timedelta(days=40)).isoformat()
BIDD_END_TS = f"{BIDD_END}T06:00:00Z"
SECRET_NAME = "Сидоров Пётр Петрович"


def _active(i: int, **kw) -> dict:
    card = _card(
        f"ACT_{i}",
        f"40:13:120101:{100 + i}",
        area=1000,
        start=100_000,
        fin=None,
        status="APPLICATIONS_SUBMISSION",
        **kw,
    )
    card["biddEndTime"] = BIDD_END_TS
    return card


def _done(i: int, status: str = "SUCCEED") -> dict:
    return _card(
        f"{status[:1]}_{i}",
        f"40:13:120102:{i + 1}",
        area=1000,
        start=100_000,
        fin=None,
        status=status,
    )


def _full(card: dict, *, fin: float | None, applied: int) -> dict:
    """Карточка лота: итог, шаг, протокол с участниками (имена не должны попасть в базу)."""
    out = {**card, "priceStep": 3_000, "deposit": 20_000}
    if fin:
        out["priceFin"] = fin
    out["protocols"] = [
        {
            "participantAdmissions": [
                {"admissionStatus": "ADMITTED", "participantName": SECRET_NAME}
                for _ in range(applied)
            ],
            "results": [{"isFirst": True, "participantPrice": fin, "orgType": "INDIVIDUAL_PERSON"}]
            if fin
            else [],
        }
    ]
    out["lotAttachments"] = [{"fileId": "g1", "attachmentTypeCode": "Plan_lands"}]
    return out


class FakeTorgi:
    """torgi.gov.ru в памяти: поиск по статусам со страницами по 10 и карточки."""

    def __init__(
        self,
        active: list[dict],
        succeed: list[dict],
        failed: list[dict],
        canceled: list[dict] | None = None,
    ):
        self.lists = {
            torgi.STATUS_ACTIVE: active,
            torgi.STATUS_SUCCEED: succeed,
            torgi.STATUS_FAILED: failed,
            "CANCELED": canceled or [],
        }
        self.cards: dict[str, dict] = {}
        self.seen: list[dict] = []
        self.fail_after: int | None = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = urlparse(str(req.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.seen.append({"path": url.path, **q})
        if self.fail_after is not None and len(self.seen) > self.fail_after:
            raise httpx.ConnectError("шлюз упал")
        if url.path.endswith("/lotcards/search"):
            assert q["catCode"] == "2" and q["subjRF"] == "40" and "text" not in q
            assert "biddForm" not in q and q["pubFrom"] == "2025-11-15"
            items = self.lists[q["lotStatus"]]
            page = int(q["page"])
            body = _page(items[page * 10 : page * 10 + 10], len(items))
            return httpx.Response(200, json=body)
        lot_id = url.path.rsplit("/", 1)[-1]
        if lot_id in self.cards:
            return httpx.Response(200, json=self.cards[lot_id])
        for items in self.lists.values():  # карточка идущего лота: то же, что в поиске, + шаг
            for hit in items:
                if hit["id"] == lot_id:
                    return httpx.Response(200, json={**hit, "priceStep": 3_000, "deposit": 20_000})
        return httpx.Response(404, json={})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeNspd:
    name = "nspd"

    def __init__(self):
        self.calls: list[str] = []

    def fetch_raw(self, cad_num: str) -> dict:
        self.calls.append(cad_num)
        payload = make_payload(cad_num, cost=250_000.0)
        payload["provider"] = "nspd"
        return payload


def _sync(fake: FakeTorgi, **kw):
    from landcheck.web import torgi_sync

    return torgi_sync.sync_region(
        "40", client=fake.client(), now=NOW, interval=0, sleep=lambda s: None, **kw
    )


def _seed(client):  # noqa: F811
    """Регион 40: 12 идущих, 15 состоявшихся (100 → 150 тыс.), 6 не состоявшихся."""
    fake = FakeTorgi(
        active=[_active(i) for i in range(12)],
        succeed=[_done(i) for i in range(15)],
        failed=[_done(i + 50, "FAILED") for i in range(6)],
    )
    for c in fake.lists[torgi.STATUS_SUCCEED]:
        fake.cards[c["id"]] = _full(c, fin=150_000, applied=3)
    for n, c in enumerate(fake.lists[torgi.STATUS_FAILED]):
        fake.cards[c["id"]] = _full(c, fin=None, applied=n % 2)
    return fake


def test_sync_region_collects_lots_cards_and_nspd(client):  # noqa: F811
    from landcheck.web.db import TorgiLot, TorgiSync, session

    fake = _seed(client)
    nspd = FakeNspd()
    state = _sync(fake, provider=nspd)
    assert state["state"] == "idle" and state["backfill_done"] is True
    assert (state["active"], state["succeed"], state["failed"]) == (12, 15, 6)
    assert state["succeed_priced"] == 15 and state["cards_left"] == 0
    # карточки: 12 идущих (шаг, задаток) + 15 состоявшихся + 6 не состоявшихся
    assert state["stats"]["new"] == 33 and state["stats"]["cards"] == 33
    assert state["stats"]["nspd"] == 12 and len(nspd.calls) == 12
    with session() as db:
        lot = db.get(TorgiLot, "S_0")
        assert lot.price_fin == 150_000 and lot.applied == 3 and lot.price_step == 3_000
        assert lot.winner_type == "физлицо" and lot.has_gpzu is True and lot.card_at is not None
        assert lot.district == "40:13" and lot.quarter == "40:13:120102" and lot.region == "40"
        act = db.get(TorgiLot, "ACT_3")
        assert act.status == "APPLICATIONS_SUBMISSION" and act.bidd_end == BIDD_END
        assert act.cad_cost == 250_000 and act.lat is not None
        assert act.price_step == 3_000 and act.deposit == 20_000 and act.card_at is not None
        # имён участников в базе нет
        dump = json.dumps(
            [x.to_dict() for x in db.scalars(select(TorgiLot))], ensure_ascii=False, default=str
        )
        assert SECRET_NAME.split()[0] not in dump
        assert db.get(TorgiSync, "40").progress["phase"] == "done"
    # урезанный ответ НСПД (без зон) в общий кэш участков не пишется
    from landcheck.cache import ParcelCache
    from landcheck.web.config import settings

    cache = ParcelCache(settings.cache_path)
    try:
        assert cache.get("40:13:120101:100", allow_stale=True) is None
    finally:
        cache.close()


def test_sync_second_pass_is_shallow_and_closes_finished_lots(client):  # noqa: F811
    from landcheck.web.db import TorgiLot, session

    fake = _seed(client)
    _sync(fake, max_nspd=0)
    first = len(fake.seen)
    # лот ACT_0 завершился (ушёл из идущих), появился новый ACT_99
    gone = fake.lists[torgi.STATUS_ACTIVE].pop(0)
    fake.cards[gone["id"]] = _full({**gone, "lotStatus": "SUCCEED"}, fin=180_000, applied=4)
    fake.lists[torgi.STATUS_ACTIVE].insert(0, _active(99))
    state = _sync(fake, max_nspd=0)
    assert state["state"] == "idle" and state["stats"]["new"] == 1
    with session() as db:
        lot = db.get(TorgiLot, "ACT_0")
        assert lot.status == "SUCCEED" and lot.price_fin == 180_000 and lot.applied == 4
        assert db.get(TorgiLot, "ACT_99") is not None
    second = fake.seen[first:]
    history = [s for s in second if s.get("lotStatus") in ("SUCCEED", "FAILED")]
    # история уже собрана: по одной странице на статус, новых лотов нет — дальше не листаем
    assert len(history) == 2 and all(s["page"] == "0" for s in history)
    # карточки второго прохода: новый идущий лот и итог завершившегося
    cards = [s["path"].rsplit("/", 1)[-1] for s in second if "search" not in s["path"]]
    assert cards == ["ACT_99", "ACT_0"]


def test_sync_error_keeps_progress_and_resumes(client):  # noqa: F811
    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiSync, session

    fake = _seed(client)
    # 2 страницы идущих, 12 карточек идущих, 1-я страница состоявшихся — потом шлюз падает
    fake.fail_after = 15
    state = _sync(fake, max_nspd=0)
    assert state["state"] == "error" and "сбор прерван" in state["error"]
    assert state["active"] == 12 and state["succeed"] == 10 and state["backfill_done"] is False
    with session() as db:
        assert db.get(TorgiSync, "40").progress["cursors"] == {"SUCCEED": 1}
    fake.fail_after = None
    before = len(fake.seen)
    state = _sync(fake, max_nspd=0)
    assert state["state"] == "idle" and state["succeed"] == 15 and state["backfill_done"] is True
    # состоявшиеся продолжены со второй страницы, а не с начала
    succeed_pages = [s["page"] for s in fake.seen[before:] if s.get("lotStatus") == "SUCCEED"]
    assert succeed_pages == ["1"]
    assert torgi_sync.due_regions(now=NOW) == []  # только что собран


def test_sync_without_gateway_reports_error(client, monkeypatch):  # noqa: F811
    from landcheck.web import torgi_sync

    monkeypatch.delenv("LANDCHECK_RU_PROXY", raising=False)
    state = torgi_sync.sync_region("40", now=NOW)
    assert state["state"] == "error" and "RU-шлюз не настроен" in state["error"]
    assert state["gateway"] is False


def test_access_admin_and_team_account_only(client):  # noqa: F811
    _register(client, "broker@example.com", role="broker")
    for url in ("/tools/torgi-scan", "/tools/torgi-liquidity", "/tools/torgi-deals"):
        assert client.get(url).status_code == 404
    assert "Торги" not in client.get("/").text
    client.post("/logout")
    client.cookies.clear()
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    assert client.get("/tools/torgi-scan").status_code == 200
    page = client.get("/admin/users").text
    assert "/pro-tools" in page and "Торги</th>" in page
    from landcheck.web.db import User, session

    with session() as db:
        uid = db.scalar(select(User.id).where(User.email == "broker@example.com"))
    assert client.post(f"/admin/users/{uid}/pro-tools", data={"action": "on"}).status_code == 303
    assert "торги ✓" in client.get("/admin/users").text
    client.post("/logout")
    client.cookies.clear()
    r = client.post("/login/password", data={"email": "broker@example.com", "password": "12345678"})
    assert r.status_code == 303
    home = client.get("/").text
    assert '<a href="/tools/torgi-scan">Торги</a>' in home
    assert client.get("/tools/torgi-scan").status_code == 200
    assert client.get("/tools/torgi-calc").status_code == 200
    assert client.get("/admin/users").status_code == 404  # прав администратора это не даёт


def _admin(client) -> None:  # noqa: F811
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")


def test_scan_page_lot_page_and_exports(client):  # noqa: F811
    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    page = client.get("/tools/torgi-scan?region=40&roi=40").text
    assert "Калужская область" in page and "идут сейчас 12" in page
    assert "завершены за год 21" in page and "с итоговой ценой 15" in page
    # 15 состоявшихся по 150 ₽/м² → перепродажа 150 тыс.; предел при 40 % = " + M(102_756) + "
    assert M(102_756) in page and "итоги торгов" in page
    assert "40:13:120101:100" in page and "ост. 40 дн." in page
    # в квартале идущих лотов истории нет, по району все торги с борьбой → ждём ×1,5 → «на грани»
    assert "на грани 12" in page
    assert 'id="torgi-map"' in page and "Скачать XLSX" in page
    # основание оценки названо: цена — по итогам торгов, не рынок; как проверить — рядом
    assert "с ценой рынка: 0 из 12" in page and "Как определяется, выгодна ли цена" in page
    assert "по одним торгам «интересен» не ставим" in page
    assert 'formaction="/tools/torgi-lot/ACT_0/ads?roi=40&amp;region=40"' in page
    # при цели 100 % старт уже выше предела
    assert "не подходит 12" in client.get("/tools/torgi-scan?region=40&roi=100").text
    # фильтр по кварталу и вердикту
    assert "40:13:120101:100" not in client.get("/tools/torgi-scan?region=40&d=40:99").text
    lot = client.get("/tools/torgi-lot/ACT_0?region=40&roi=40").text
    assert "Почему «на грани»" in lot and "Торги, из которых взята цена перепродажи" in lot
    assert "площадь от 1/5 до 5 площадей лота; 15 лот." in lot
    assert "40:13:120102:1" in lot and "×1.5" in lot  # те самые торги района
    assert "Это не цена рынка" in lot and "вся выборка, 15 лот.: от" in lot
    assert "попадает в ±30 % от итога" in lot
    # рост в блоке ликвидности — по той же сделке, что и в расчёте ожидаемой цены
    assert "Рост цены к старту, продажа, медиана" in lot and "15 лот." in lot
    assert "Кадастровая стоимость" in lot and "старт — 40 % от неё" in lot
    assert "/tools/torgi-calc?" in lot and "smid=150000" in lot and "roi=40" in lot
    x = client.get("/tools/torgi-scan/export.xlsx?region=40&roi=40")
    assert x.status_code == 200 and x.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )
    k = client.get("/tools/torgi-scan/export.kml?region=40&roi=40")
    assert k.status_code == 200 and k.text.count("<Placemark>") == 12
    assert client.get("/tools/torgi-scan/export.pdf?region=40").status_code == 404
    assert client.get("/tools/torgi-lot/NOPE").status_code == 404


def test_market_layer_changes_verdict_and_liquidity_page(client):  # noqa: F811
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    _admin(client)
    liq = client.get("/tools/torgi-liquidity?region=40").text
    assert "40:13" in liq and "Слой рынка" in liq and ">ликвид<" in liq
    quarters = client.get("/tools/torgi-liquidity?region=40&level=quarter").text
    assert "40:13:120102" in quarters
    # рынок района задан вручную: 60 тыс. за сотку → лот 10 соток = 600 тыс., предел при 100 %
    r = client.post(
        "/tools/torgi-market",
        data={
            "scope": "40:13",
            "kind": "sale",
            "vri_group": "any",
            "price_mid": "60000",
            "exposure_days": "90",
            "liquid": "liquid",
            "note": "по объявлениям направления",
            "back": "/tools/torgi-liquidity?region=40",
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/tools/torgi-liquidity?region=40"
    page = client.get("/tools/torgi-scan?region=40&roi=100").text
    assert "интересен 12" in page and M(279_144) in page and "слой рынка" in page
    liq = client.get("/tools/torgi-liquidity?region=40").text
    assert M(60_000) in liq and "90 дн." in liq and "по объявлениям направления" in liq
    # повторная запись обновляет, а не плодит; удаление возвращает оценку по торгам
    client.post("/tools/torgi-market", data={"scope": "40:13", "price_mid": "70000"})
    from landcheck.web.db import TorgiMarket, session

    with session() as db:
        rows = list(db.scalars(select(TorgiMarket)))
        assert len(rows) == 1 and rows[0].price_mid == 70_000
        mid = rows[0].id
    assert client.post(f"/tools/torgi-market/{mid}/delete").status_code == 303
    assert "не подходит 12" in client.get("/tools/torgi-scan?region=40&roi=100").text
    # мусор не принимается
    assert client.post("/tools/torgi-market", data={"scope": "Калуга"}).status_code == 400
    assert client.post("/tools/torgi-market", data={"scope": "40:13"}).status_code == 400
    # возврат только на свои страницы
    r = client.post(
        "/tools/torgi-market",
        data={"scope": "40:13", "price_mid": "1", "back": "https://evil.example"},
    )
    assert r.headers["location"] == "/tools/torgi-liquidity?region=40"


def test_ads_fill_market_layer(client, monkeypatch):  # noqa: F811
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    _admin(client)
    from landcheck import market_ads

    def fake_collect(place, kind, **kw):
        return {
            "n": 5,
            "median": 45_000.0,
            "p25": 38_000.0,
            "p75": 52_000.0,
            "scope_label": "Малоярославецкий район",
            "ads": [
                {
                    "title": "Продам участок 10 соток ИЖС",
                    "url": "https://example-board.ru/sale/123456789",
                    "host": "example-board.ru",
                    "price_rub": 450_000.0,
                    "area_m2": 1000.0,
                    "pps": 45_000.0,
                    "scope": "locality",
                },
                {"title": "не ссылка", "url": "javascript:alert(1)", "pps": 1.0},
            ],
        }

    monkeypatch.setattr(market_ads, "collect_ads", fake_collect)
    r = client.post("/tools/torgi-lot/ACT_0/ads")
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=ads")
    lot = client.get("/tools/torgi-lot/ACT_0?saved=ads&roi=40").text
    assert "Объявления найдены" in lot and "объявления, 40:13" in lot and M(450_000) in lot
    assert "5 объявл. с ценой и площадью, Малоярославецкий район" in lot
    # сами объявления — в карточке, со ссылками; не веб-ссылки отброшены
    assert "Объявления, из которых взята цена: 1" in lot
    assert 'href="https://example-board.ru/sale/123456789"' in lot and "javascript:" not in lot
    assert "Продам участок 10 соток ИЖС" in lot and "Это не цена рынка" not in lot
    # цена с рынка: при цели 40 % предел 308 тыс. выше ожидаемых 150 тыс. → «интересен»
    assert "Почему «интересен»" in lot
    page = client.get("/tools/torgi-scan?region=40&roi=40").text
    assert "интересен 12" in page and "с ценой рынка: 12 из 12" in page
    assert "/ads?region=40" not in page  # кнопка поиска объявлений больше не нужна
    # поиск из списка лотов возвращает в карточку с теми же критериями
    r = client.post("/tools/torgi-lot/ACT_2/ads?region=40&roi=40")
    assert r.headers["location"] == "/tools/torgi-lot/ACT_2?saved=ads&region=40&roi=40"
    monkeypatch.setattr(market_ads, "collect_ads", lambda *a, **k: {"n": 1, "median": 1.0})
    assert client.post("/tools/torgi-lot/ACT_1/ads").headers["location"].endswith("saved=noads")


def test_few_ads_are_shown_but_not_priced(client, monkeypatch):  # noqa: F811
    """Прод, 12.11.2026: по лоту нашлось одно объявление из трёх нужных — страница говорила «не
    нашлось». Теперь найденное показано, а цена в слой рынка не пишется."""
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    _admin(client)
    from landcheck import market_ads
    from landcheck.web.db import TorgiMarket, session

    one = {
        "title": "Участок 6,6 сот. в коттеджном посёлке",
        "url": "https://example-board.ru/sale/987654321",
        "host": "example-board.ru",
        "price_rub": 3_978_000.0,
        "area_m2": 663.0,
        "pps": 600_000.0,
        "scope": "district",
    }
    few = {"n": 1, "median": 600_000.0, "scope_label": "Малоярославецкий район", "ads": [one]}
    monkeypatch.setattr(market_ads, "collect_ads", lambda *a, **k: few)
    r = client.post("/tools/torgi-lot/ACT_0/ads?region=40&roi=40")
    assert r.headers["location"] == "/tools/torgi-lot/ACT_0?saved=fewads&region=40&roi=40"
    lot = client.get(r.headers["location"]).text
    assert "нашлось меньше трёх" in lot and "цена в слой рынка не записана" in lot
    assert "Найденные объявления (для оценки мало, цена не записана): 1" in lot
    assert 'href="https://example-board.ru/sale/987654321"' in lot
    assert "объявления: мало для оценки" in lot and "Почему «на грани»" in lot
    with session() as db:
        rows = list(db.scalars(select(TorgiMarket)))
        assert [(m.source, m.price_mid) for m in rows] == [("ads_few", None)]
    # на оценку и на вкладку рынка такая запись не влияет
    page = client.get("/tools/torgi-scan?region=40&roi=40").text
    assert "с ценой рынка: 0 из 12" in page and "на грани 12" in page
    liq = client.get("/tools/torgi-liquidity?region=40").text
    assert "ads_few" not in liq and "мало для оценки" not in liq
    # позже объявлений нашлось достаточно — запись «мало» убирается
    enough = {
        "n": 3,
        "median": 45_000.0,
        "p25": 38_000.0,
        "p75": 52_000.0,
        "scope_label": "Малоярославец",
        "ads": [
            {**one, "url": f"https://example-board.ru/sale/{i}", "pps": 45_000.0} for i in (1, 2, 3)
        ],
    }
    monkeypatch.setattr(market_ads, "collect_ads", lambda *a, **k: enough)
    assert client.post("/tools/torgi-lot/ACT_0/ads").headers["location"].endswith("saved=ads")
    with session() as db:
        assert [m.source for m in db.scalars(select(TorgiMarket))] == ["ads"]


def test_deals_journal_feeds_market(client):  # noqa: F811
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    _admin(client)
    assert "Сделок пока нет" in client.get("/tools/torgi-deals").text
    pre = client.get("/tools/torgi-deals?lot=S_0").text
    assert 'value="40:13:120102:1"' in pre and 'value="150000"' in pre
    data = {
        "cad_num": "40.13.120102.1",  # номер с точками нормализуется
        "kind": "sale",
        "vri_group": "housing",
        "area": "10",
        "bought_at": "2026-06-01",
        "buy_price": "150000",
        "extra_costs": "10000",
        "listed_at": "2026-06-15",
        "list_price": "450000",
    }
    assert client.post("/tools/torgi-deals", data=data).status_code == 303
    page = client.get("/tools/torgi-deals").text
    assert "40:13:120102:1" in page and "в продаже" in page and "по цене выставления" in page
    from landcheck.web.db import TorgiDeal, session

    with session() as db:
        deal = db.scalar(select(TorgiDeal))
        assert deal.district == "40:13" and deal.area_m2 == 1000 and deal.sell_price is None
        did = deal.id
    assert f'name="deal_id" value="{did}"' in client.get(f"/tools/torgi-deals?edit={did}").text
    # закрываем сделку: продано за 400 тыс. через 60 дней после выставления
    client.post(
        "/tools/torgi-deals",
        data={**data, "deal_id": str(did), "sold_at": "2026-08-14", "sell_price": "400000"},
    )
    page = client.get("/tools/torgi-deals").text
    assert "закрыто 1" in page and "медианная доходность 150 %" in page and "60 дн." in page
    assert M(240_000) in page  # прибыль 400 − 160
    # закрытая сделка стала ценой перепродажи района: 40 тыс. за сотку, срок 60 дней
    scan = client.get("/tools/torgi-scan?region=40&roi=100").text
    assert "свои сделки" in scan and M(400_000) in scan
    assert client.post("/tools/torgi-deals", data={"cad_num": "мусор"}).status_code == 400
    assert client.post(f"/tools/torgi-deals/{did}/delete").status_code == 303
    assert "Сделок пока нет" in client.get("/tools/torgi-deals").text


def test_check_selected_lots_creates_auction_check(client):  # noqa: F811
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    _admin(client)
    assert client.post("/tools/torgi-scan/check", data={}).status_code == 400
    r = client.post("/tools/torgi-scan/check", data={"lot": ["ACT_0", "ACT_1"]})
    assert r.status_code == 303 and r.headers["location"].startswith("/checks/")
    from landcheck.web.db import Check, session

    with session() as db:
        check = db.scalar(select(Check).order_by(Check.id.desc()))
        assert check.total == 2 and check.request["purpose"] == "auction"
        assert check.request["use"] == "izhs"  # все лоты — жильё
        assert "40:13:120101:100; 100000 руб; https://torgi.gov.ru" in check.input_text
        assert check.title.startswith("Торги Калужская область: 2 лот.")


def test_sync_buttons_and_status_block(client, monkeypatch):  # noqa: F811
    _admin(client)
    page = client.get("/tools/torgi-scan?region=40").text
    assert "RU-шлюз не настроен" in page and "Собрать торги региона</button>" not in page
    monkeypatch.setenv("LANDCHECK_RU_PROXY", "socks5h://127.0.0.1:1080")
    page = client.get("/tools/torgi-scan?region=40").text
    assert "Регион ещё не собирали" in page and "Собрать торги региона</button>" in page
    from landcheck.web import torgi_sync

    started: list[str] = []
    monkeypatch.setattr(torgi_sync, "start_sync", lambda region: started.append(region) or True)
    r = client.post("/tools/torgi-scan/sync", data={"region": "40"})
    assert r.status_code == 303 and started == ["40"]
    assert client.post("/tools/torgi-scan/sync", data={"region": "zz"}).status_code == 404
    torgi_sync._update("40", state="running", phase="history", progress={"page": 3, "pages": 40})
    with torgi_sync._lock:
        torgi_sync._queued.add("40")
    try:
        block = client.get("/tools/torgi-scan/status?region=40").text
        assert 'hx-trigger="every 15s"' in block and "3 из 40" in block
    finally:
        with torgi_sync._lock:
            torgi_sync._queued.discard("40")
    # процесс перезапустился (в очереди пусто), отметок прогресса давно не было
    old = "2026-01-01T00:00:00+00:00"
    from landcheck.web.db import TorgiSync, session

    with session() as db:
        st = db.get(TorgiSync, "40")
        st.progress = {**st.progress, "at": old}
        db.commit()
    assert "Сбор прервался" in client.get("/tools/torgi-scan/status?region=40").text
    assert (
        client.post("/tools/torgi-scan/auto", data={"region": "40", "action": "off"}).status_code
        == 303
    )
    with session() as db:
        assert db.get(TorgiSync, "40").auto is False
    assert torgi_sync.due_regions() == []


def test_failed_lots_are_reread_for_protocol_reason(client):  # noqa: F811
    """Причина «торги не состоялись» из протокола появилась в разборе карточки 13.11.2026
    (эксперт по торгам: «протокол публикуется, и в нём данные»). Лоты, прочитанные раньше,
    перечитываются фоном; страница ликвидности называет исходы по причинам."""
    from datetime import timedelta

    from sqlalchemy import select

    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiLot, session

    fake = _seed(client)
    _sync(fake, max_nspd=0)
    _admin(client)
    soon = datetime.now(UTC) + timedelta(minutes=torgi_sync.CARDS_RETRY_MIN + 1)
    assert torgi_sync.due_regions(soon) == []  # всё прочитано — ждём обычные сутки
    with session() as db:
        failed = db.scalars(select(TorgiLot).where(TorgiLot.status == "FAILED")).all()
        assert len(failed) == 6 and all(x.card_rev == torgi.CARD_REV for x in failed)
        for x in failed:  # как после выкладки: карточки прочитаны старым разбором
            x.card_rev = 0
        db.commit()
    st = torgi_sync.state_of("40")
    assert st["reread_left"] == 6 and st["cards_left"] == 0
    assert torgi_sync.due_regions(soon) == ["40"]  # есть что перечитать — через 10 минут
    page = client.get("/tools/torgi-scan/status?region=40").text
    assert "у 6 завершённых перечитывается карточка" in page
    # в карточках площадки причина есть: у нечётных одна заявка, у чётных заявок не подано
    for n, c in enumerate(fake.lists[torgi.STATUS_FAILED]):
        code = "ZK_EA_14_39.12_1" if n % 2 else "ZK_EA_14_39.12_2"
        name = "Подана только одна заявка" if n % 2 else "Не подано ни одной заявки"
        fake.cards[c["id"]]["protocols"][0]["abandonedReason"] = {"code": code, "name": name}
    state = _sync(fake, max_nspd=0)
    assert state["reread_left"] == 0 and state["stats"]["cards"] == 6
    with session() as db:
        failed = db.scalars(select(TorgiLot).where(TorgiLot.status == "FAILED")).all()
        assert all(x.card_rev == torgi.CARD_REV and x.fail_code for x in failed)
        assert sorted(x.fail_reason for x in failed)[0] == "Не подано ни одной заявки"
    html = client.get("/tools/torgi-liquidity?region=40").text
    assert "единственному участнику 3 (подана одна заявка — 3; всё по протоколу)" in html
    assert "не ушли 3 (заявок не подано — 3; всё по протоколу)" in html
    assert SECRET_NAME not in html
    # Причина прочитана, но исход не называет («менее двух лиц», 229-ФЗ): страница не пишет
    # «ещё не прочитана» (прод, 13.11.2026: так были подписаны 96 лотов Республики Алтай)
    with session() as db:
        lot = db.scalars(select(TorgiLot).where(TorgiLot.fail_code == "ZK_EA_14_39.12_2")).first()
        lot.fail_code, lot.fail_reason = "229FZ_EA_102_1", "Заявки на участие подали менее двух лиц"
        db.commit()
    html = client.get("/tools/torgi-liquidity?region=40").text
    assert "заявок не подано — 2; причина в протоколе исход не называет либо не прочитана" in html
    assert "ещё не прочитана" not in html


def test_report_block_shows_what_happened_to_failed_auctions(client):  # noqa: F811
    """Эксперт по торгам (чат, 13.11.2026): «не состоялись» — это и договор с единственным
    участником, и торги без заявок. Блок отчёта писал обе группы одним числом и называл их
    «нет заявок»; теперь показывает разбивку из собранной базы."""
    from landcheck.web import torgi_sync
    from landcheck.web.app import templates

    fake = _seed(client)
    _sync(fake, max_nspd=0)
    a = torgi_sync.auctions_from_store("40:13:120101:1", now=NOW)
    tpl = templates.get_template("_auctions.html")
    html = tpl.render(auctions=a, auctions_url="/x")
    assert "с борьбой 15, единственному участнику 3, не ушли 3, идут сейчас 12" in html
    assert "не состоялись 6" not in html and "нет заявок" not in html
    assert "хозяина нашли 86 % из 21 завершённых" in html
    # сводка, сохранённая в проверке до правки (без разбивки), рисуется по-прежнему
    old = {k: v for k, v in a.items() if not k.endswith("_total") or k[:6] in ("succee", "failed")}
    old |= {"active_total": a["active_total"], "ea_total": a["ea_total"]}
    html = tpl.render(auctions=old, auctions_url="/x")
    assert "состоялись 15, не состоялись 6, идут сейчас 12" in html


def test_report_block_reads_district_summary_from_store(client):  # noqa: F811
    """Блок «Торги рядом за год» в отчёте берёт сводку района из собранной базы региона."""
    from landcheck.web import jobs, torgi_sync

    assert torgi_sync.auctions_from_store("40:13:120101:1") is None  # регион ещё не собран
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    a = torgi_sync.auctions_from_store("40:13:120101:1", now=NOW)
    assert (a["succeed_total"], a["failed_total"], a["active_total"]) == (15, 6, 12)
    assert a["ea_total"] == 33 and a["from_store"] is True
    # шесть несостоявшихся: у трёх одна заявка (ушли единственному участнику), у трёх карточка
    # без заявок и цены договора (не ушли) — ликвидность считает и тех, кто ушёл без борьбы
    assert (a["single_total"], a["none_total"], a["unknown_total"]) == (3, 3, 0)
    assert a["liquidity"] == 0.86
    assert a["sale"]["with_price"] == 15 and a["sale"]["median_rub_m2"] == 150.0
    assert a["sale"]["median_ratio"] == 1.5 and len(a["items"]) == 15
    assert "хозяина нашли 86 % из 21 завершённых" in a["liquidity_text"] and a["error"] is None
    assert (
        "с борьбой 15, единственному участнику по начальной цене 3, не ушли 3"
        in a["liquidity_text"]
    )
    assert torgi_sync.auctions_from_store("40:99:000001:1", now=NOW)["ea_total"] == 0
    # сводка сериализуется в данные строки проверки
    json.dumps(a, ensure_ascii=False)
    # устаревшую базу не используем
    later = datetime(2026, 12, 1, tzinfo=UTC)
    assert torgi_sync.auctions_from_store("40:13:120101:1", now=later) is None
    # фоновая задача отчёта идёт в базу, а не на сайт (шлюза в тесте нет)
    from landcheck.web.db import TorgiSync, session

    with session() as db:
        db.get(TorgiSync, "40").finished_at = datetime.now(UTC)
        db.commit()
    assert jobs.fetch_auctions("40:13:120101:1", refresh=True)["from_store"] is True


def test_cli_pro_tools_status_and_probe(client, monkeypatch):  # noqa: F811
    from typer.testing import CliRunner

    from landcheck import ruproxy
    from landcheck.cli import app
    from landcheck.web.db import User, session

    runner = CliRunner()
    _register(client, "team@example.com", role="buyer")
    r = runner.invoke(app, ["pro-tools", "team@example.com"])
    assert r.exit_code == 0 and "pro_tools=True" in r.output
    with session() as db:
        assert db.scalar(select(User).where(User.email == "team@example.com")).can_torgi is True
    assert (
        "pro_tools=False" in runner.invoke(app, ["pro-tools", "team@example.com", "--off"]).output
    )
    assert runner.invoke(app, ["pro-tools", "nobody@example.com"]).exit_code == 1
    # регион ставится на автосбор командой: веб-процесс подхватит его сам
    from landcheck.web import torgi_sync

    assert torgi_sync.due_regions() == []
    out = runner.invoke(app, ["torgi-enable", "10"]).output
    assert "10 Республика Карелия: автосбор=вкл" in out and torgi_sync.due_regions() == ["10"]
    assert "автосбор=выкл" in runner.invoke(app, ["torgi-enable", "10", "--off"]).output
    assert torgi_sync.due_regions() == []
    assert runner.invoke(app, ["torgi-enable", "99"]).exit_code == 1
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    out = runner.invoke(app, ["torgi-status", "40"]).output
    assert "state=idle" in out and "active=12" in out and "succeed_priced=15" in out
    assert "notices=0, notices_active=0" in out
    # проба API: без шлюза — честный отказ; со шлюзом — счётчики и имена полей, без значений
    monkeypatch.delenv("LANDCHECK_RU_PROXY", raising=False)
    assert runner.invoke(app, ["torgi-probe", "40"]).exit_code == 1
    monkeypatch.setenv("LANDCHECK_RU_PROXY", "socks5h://127.0.0.1:1080")

    def probe(req: httpx.Request) -> httpx.Response:
        status = parse_qs(urlparse(str(req.url)).query)["lotStatus"][0]
        items = fake.lists[status]
        return httpx.Response(200, json=_page(items[:10], len(items)))

    monkeypatch.setattr(
        ruproxy, "client", lambda timeout=25.0: httpx.Client(transport=httpx.MockTransport(probe))
    )
    monkeypatch.setattr(torgi, "REQUEST_INTERVAL_S", 0.0)
    out = runner.invoke(app, ["torgi-probe", "40"]).output
    assert "идут: лотов 12, страниц 2" in out and "состоялись: лотов 15" in out
    assert "характеристики: CadastralNumber, PermittedUse, SquareZU" in out
    assert "запросов: 3" in out and "40:13:120101" not in out


def test_sync_stops_when_cards_do_not_answer_and_rereads_finished(client):  # noqa: F811
    """Сайт перестал отвечать на карточки — проход прерывается после нескольких сбоев подряд;
    лот, завершившийся после чтения карточки, получает итог при следующем проходе."""
    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiLot, session

    fake = _seed(client)
    fake.fail_after = 2  # обе страницы идущих прочитаны, дальше карточки не отвечают
    state = _sync(fake, max_nspd=0)
    assert state["state"] == "error" and state["active"] == 12
    # не все 12 карточек перебраны впустую: стоп после пяти сбоев подряд; каждый сбой связи
    # перед тем повторён (шлюз RU мог мигнуть, прод 18.11)
    per_card = len(torgi.TRANSPORT_RETRY_S) + 1
    assert len(fake.seen) == 2 + torgi_sync.MAX_CARD_FAILURES * per_card
    # через 15 минут после сбоя регион снова в очереди автосбора, а не через сутки
    from datetime import timedelta

    assert torgi_sync.due_regions(now=NOW) == []
    assert torgi_sync.due_regions(now=datetime.now(UTC) + timedelta(minutes=16)) == ["40"]
    fake.fail_after = None
    _sync(fake, max_nspd=0)
    # лот ACT_1 завершился, но в «пропавших» не дочитан (лимит), статус пришёл из поиска истории
    with session() as db:
        lot = db.get(TorgiLot, "ACT_1")
        assert lot.card_at is not None and lot.price_fin is None
        row = {**lot.to_dict(), "status": "SUCCEED"}
        _, what = torgi_sync.upsert(db, row, NOW)
        assert what == "changed" and lot.card_at is None  # карточку нужно перечитать: итог
        db.rollback()


# --- извещения о предоставлении и дочитывание карточек ---------------------------------------
def _card_ids(fake: FakeTorgi) -> list[str]:
    return [s["path"].rsplit("/", 1)[-1] for s in fake.seen if "/lotcards/search" not in s["path"]]


def test_sync_skips_notice_cards_and_counts_auctions_only(client):  # noqa: F811
    fake = _seed(client)
    fake.lists[torgi.STATUS_ACTIVE] += [_notice(f"IA_{i}", end=BIDD_END_TS) for i in range(5)]
    fake.lists[torgi.STATUS_SUCCEED] += [_notice(f"IS_{i}", status="SUCCEED") for i in range(30)]
    st = _sync(fake, provider=FakeNspd())
    assert st["state"] == "idle" and st["backfill_done"] is True
    # первый проход: карточки только у аукционов
    assert not [i for i in _card_ids(fake) if i.startswith("I")]
    assert (st["active"], st["succeed"], st["failed"]) == (12, 15, 6)
    assert st["succeed_priced"] == 15 and st["cards_left"] == 0
    assert (st["notices"], st["notices_active"]) == (35, 5)
    # история собрана: идущим извещениям дочитываем карточку (адрес), завершённым — никогда
    fake.seen.clear()
    _sync(fake, provider=FakeNspd())
    read = _card_ids(fake)
    assert sorted(i for i in read if i.startswith("IA_")) == [f"IA_{i}" for i in range(5)]
    assert not [i for i in read if i.startswith("IS_")]

    _admin(client)
    page = client.get("/tools/torgi-scan?region=40&roi=40").text
    assert "Всего 12" in page and "IA_0" not in page
    assert "Извещений о предоставлении (ст. 39.18): 35, из них идут " in page
    assert '<a href="/tools/torgi-scan?region=40&amp;form=notice">5</a>' in page  # чат 17.11 №79
    assert "сейчас 5 извещений о предоставлении" in page and "form=notice" in page
    notices = client.get("/tools/torgi-scan?region=40&form=notice").text
    assert "Всего 5" in notices and "извещений 5" in notices and "извещение 39.18" in notices
    assert "квартал 40:13:120105" in notices and "/tools/torgi-lot/IA_0" in notices
    assert "Всего 17" in client.get("/tools/torgi-scan?region=40&form=all").text
    # критерий переуступки доезжает до формы и до расчёта
    page = client.get("/tools/torgi-scan?region=40&roi=40&assign=50").text
    assert 'name="assign" inputmode="decimal" value="50"' in page
    # ликвидность района считается по аукционам: 15 с борьбой, 3 одному, 3 без заявок
    liq = client.get("/tools/torgi-liquidity?region=40").text
    assert "завершено 21, с борьбой 15" in liq
    assert client.get("/tools/torgi-lot/IA_0").status_code == 200


def test_auto_sync_returns_soon_while_auction_cards_remain(client):  # noqa: F811
    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiSync, session

    def finished_at() -> datetime:
        with session() as db:
            row = db.get(TorgiSync, "40")
            row.auto = True
            db.commit()
            return row.finished_at.replace(tzinfo=UTC)

    fake = _seed(client)
    st = _sync(fake, provider=FakeNspd(), max_cards=5)
    assert st["cards_left"] == 16
    # первыми читаются не состоявшиеся: по карточке видно, была ли единственная заявка;
    # цена состоявшихся приходит уже в поиске
    assert all(i.startswith("F_") for i in _card_ids(fake)[-5:])
    done = finished_at()
    assert torgi_sync.due_regions(done + timedelta(minutes=5)) == []
    assert torgi_sync.due_regions(done + timedelta(minutes=11)) == ["40"]
    # прошлый проход не прочитал ни одной карточки — не крутимся, ждём обычный срок
    with session() as db:
        row = db.get(TorgiSync, "40")
        row.stats = {**row.stats, "cards": 0}
        db.commit()
    assert torgi_sync.due_regions(done + timedelta(minutes=11)) == []
    assert torgi_sync.due_regions(done + timedelta(hours=25)) == ["40"]
    # всё дочитано — следующий сбор через сутки
    st = _sync(fake, provider=FakeNspd())
    assert st["cards_left"] == 0
    done = finished_at()
    assert torgi_sync.due_regions(done + timedelta(hours=2)) == []
    assert torgi_sync.due_regions(done + timedelta(hours=25)) == ["40"]


def test_district_name_drops_region_prefix():
    """Район в адресах лотов пишут вразнобой (прод, Карелия и Калуга, 11.11.2026)."""
    from landcheck.web.torgi_views import _REGION_PREFIX

    cases = {
        "Респ Карелия Питкярантский район": "Питкярантский район",
        "обл. Калужская Барятинский район": "Барятинский район",
        "обл Калужская Спас-Деменский район": "Спас-Деменский район",
        "Калужская обл Жуковский р-н": "Жуковский р-н",
        "Краснодарский край Анапский район": "Анапский район",
        "м.р-н Лахденпохский": "м.р-н Лахденпохский",
        "Кировский р-н": "Кировский р-н",
        "Юхновский район": "Юхновский район",
    }
    for raw, want in cases.items():
        assert _REGION_PREFIX.sub("", raw) == want


def test_scan_place_filter_parcel_number_and_back_link_keep_region(client):  # noqa: F811
    """Зум 13.11: номер участка в «Район или квартал» — его квартал и сам участок первым;
    пустой список объясняет себя; «Пересчитать» в карточке и «к списку лотов» не теряют регион."""
    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    page = client.get("/tools/torgi-scan?region=40&d=40:13:120101:100").text
    assert 'class="hl"' in page and "Всего 1" in page
    assert "среди идущих лотов нет" not in page
    other = client.get("/tools/torgi-scan?region=40&d=40:13:120101:999").text
    assert "Участка <code>40:13:120101:999</code> среди идущих лотов нет" in other
    empty = client.get("/tools/torgi-scan?region=40&d=40:99:000001&kind=rent").text
    assert "Сбор идёт" not in empty
    assert "В квартале 40:99:000001 идущих лотов по выбранным условиям нет" in empty
    hidden = client.get("/tools/torgi-scan?region=40&d=40:13:120101&kind=rent").text
    assert "Показать все лоты в квартале 40:13:120101</a>" in hidden
    lot = client.get("/tools/torgi-lot/ACT_0?region=40&roi=40&kind=sale&d=40:13").text
    assert '<a href="/tools/torgi-scan?roi=40&amp;kind=sale&amp;d=40%3A13&amp;region=40">' in lot
    assert '<input type="hidden" name="kind" value="sale">' in lot
    assert '<input type="hidden" name="roi"' not in lot
    bare = client.get("/tools/torgi-lot/ACT_0?roi=40").text
    assert '<a href="/tools/torgi-scan?roi=40&amp;region=40">' in bare
    assert '<input type="hidden" name="region" value="40">' in bare


def test_upsert_keeps_kind_and_zone_and_fix_kinds_by_name(client):  # noqa: F811
    """Ответ поиска без вида сделки и пояса не затирает прочитанное из карточки; уже записанные
    продажей лоты аренды исправляются по названию при старте (зум 13.11, 26:40)."""
    from landcheck import torgi_scan
    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiLot, session

    card = _card("RNT_1", "04:01:030907:128", area=1500, start=110_298, fin=None, kind="rent")
    card |= {"biddEndTime": "2026-12-06T17:00:00Z", "timezoneOffset": 420}
    card["lotName"] = "Земельный участок для ИЖС"
    now = datetime.now(UTC)
    with session() as db:
        torgi_sync.upsert(db, torgi_scan.lot_row(card), now, card=True)
        db.commit()
    hit = {k: v for k, v in card.items() if k not in ("typeTransaction", "timezoneOffset")}
    with session() as db:
        torgi_sync.upsert(db, torgi_scan.lot_row(hit), now)
        db.commit()
    with session() as db:
        lot = db.get(TorgiLot, "RNT_1")
        assert lot.kind == "rent" and lot.tz_offset == 420 and lot.bidd_end == "2026-12-07"
        d = lot.to_dict()
        assert d["bidd_end_hm"] == "00:00" and d["tz"] == "МСК+4"
        lot.kind, lot.name = "sale", "Право заключения договора аренды земельного участка"
        db.commit()
    assert torgi_sync.fix_kinds() == 1
    with session() as db:
        assert db.get(TorgiLot, "RNT_1").kind == "rent"


def test_scan_region_by_code_price_range_sorts_and_copy(client):  # noqa: F811
    """Зум 13.11: регион по коду или началу названия, начальная цена «от – до», сортировка по
    цене в обе стороны, копирование номера, расшифровка индекса в карточке лота."""
    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    by_code = client.get("/tools/torgi-scan?region=40 — Калужская область&roi=40").text
    assert "Всего 12" in by_code and 'value="40 — Калужская область"' in by_code
    assert "Всего 12" in client.get("/tools/torgi-scan?region=калуж&roi=40").text
    assert 'data-copy="40:13:120101:100"' in by_code
    narrow = client.get("/tools/torgi-scan?region=40&roi=40&pmin=200000").text
    assert "Всего 0" in narrow and "другая начальная цена" in narrow
    assert "Всего 12" in client.get("/tools/torgi-scan?region=40&roi=40&pmax=0,1").text
    desc = client.get("/tools/torgi-scan?region=40&roi=40&sort=price_desc").text
    assert 'class="small active">дороже</a>' in desc
    lot = client.get("/tools/torgi-lot/ACT_0?region=40&roi=40").text
    assert "Как сложился индекс района" in lot and "участки нашли хозяина" in lot


def test_lot_photos_in_list_and_lot_page(client):  # noqa: F811
    from landcheck.web.db import TorgiLot, session

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    with session() as db:
        db.get(TorgiLot, "ACT_0").images = ["f-1", "f-2"]
        db.commit()
    page = client.get("/tools/torgi-scan?region=40&roi=40").text
    assert 'src="https://torgi.gov.ru/new/file-store/v1/f-1"' in page and 'id="lot-gallery"' in page
    lot = client.get("/tools/torgi-lot/ACT_0?region=40").text
    assert lot.count('class="lot-thumb big"') == 2


def test_image_proxy_caches_and_rejects_non_images(client, monkeypatch):  # noqa: F811
    """Фото через сервер — когда браузер не достаёт torgi.gov.ru (из-за рубежа закрыт)."""
    from landcheck import ruproxy, torgi
    from landcheck.web import torgi_views
    from landcheck.web.config import settings

    _admin(client)
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def file(self, file_id, max_mb=5):
            calls.append(file_id)
            return b"\xff\xd8\xff\xe0jpeg" if file_id.startswith("6aed") else b"<html>"

        def close(self):
            pass

    monkeypatch.setattr(ruproxy, "enabled", lambda: True)
    monkeypatch.setattr(settings, "fixtures", None)
    monkeypatch.setattr(torgi, "TorgiClient", FakeClient)
    assert torgi_views.settings is settings
    r = client.get("/tools/torgi-img/6aed5f6b2c4c500c90717aef")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get("/tools/torgi-img/6aed5f6b2c4c500c90717aef").status_code == 200
    assert calls == ["6aed5f6b2c4c500c90717aef"]  # второй раз — с диска
    assert client.get("/tools/torgi-img/badbadbad0001").status_code == 404
    assert client.get("/tools/torgi-img/..%2Fsecret").status_code == 404


def test_scan_all_regions_one_list(client):  # noqa: F811
    """Запрос Тони 17.11: все собранные регионы одним списком — лоты обоих регионов, регион
    подписан у лота, выгрузки работают, из карточки лота возврат в общий список."""
    from landcheck import torgi_scan
    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiSync, session
    from tests.test_torgi import _card as card_of

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    card = card_of(
        "KAR_1",
        "10:03:0010901:1354",
        area=1500,
        start=90_000,
        fin=None,
        status="APPLICATIONS_SUBMISSION",
    )
    card["biddEndTime"] = BIDD_END_TS
    now = datetime.now(UTC)
    with session() as db:
        torgi_sync.upsert(db, torgi_scan.lot_row(card), now, card=True)
        db.add(TorgiSync(region="10", state="idle", finished_at=now))
        db.commit()
    page = client.get("/tools/torgi-scan?region=all&roi=40").text
    assert "Всего 13" in page
    assert "/tools/torgi-lot/ACT_0" in page and "/tools/torgi-lot/KAR_1" in page
    assert "<strong>Республика Карелия</strong>" in page
    assert "<strong>Калужская область</strong>" in page
    assert 'value="все — все собранные регионы"' in page
    assert "Общий список: 2 собранных региона" in page
    assert "Лучшие лоты всех собранных регионов" not in page
    # по подсказке поля и по слову «все» — тот же общий список
    assert "Всего 13" in client.get("/tools/torgi-scan?region=все — все собранные регионы").text
    # один регион — без чужих лотов, со ссылкой на общий список
    one = client.get("/tools/torgi-scan?region=40&roi=40").text
    assert "/tools/torgi-lot/KAR_1" not in one and "Лучшие лоты всех собранных регионов" in one
    x = client.get("/tools/torgi-scan/export.xlsx?region=all&roi=40")
    assert x.status_code == 200 and "torgi-all-" in x.headers["content-disposition"]
    assert client.get("/tools/torgi-scan/export.kml?region=all&roi=40").status_code == 200
    lot = client.get("/tools/torgi-lot/KAR_1?region=all&roi=40").text
    assert '<a href="/tools/torgi-scan?roi=40&amp;region=all">' in lot


def test_points_are_read_by_their_own_loop_across_regions(client, monkeypatch):  # noqa: F811
    """Прод, 18.11.2026: точка на карте была у четверти идущих продаж, в четырёх больших
    регионах — ни у одного лота: НСПД читался последним шагом прохода по региону, а регионы
    стоят в одной очереди. Теперь точки читает свой поток, пачками по всем регионам."""
    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiLot, session

    active = sorted(torgi.ACTIVE)

    def without_point() -> int:
        with session() as db:
            return sum(
                1
                for x in db.scalars(select(TorgiLot).where(TorgiLot.status.in_(active)))
                if x.nspd_at is None
            )

    def forget_points() -> None:
        with session() as db:
            for lot in db.scalars(select(TorgiLot).where(TorgiLot.status.in_(active))):
                lot.nspd_at = None
            db.commit()

    def step(failed, provider, **kw):
        return torgi_sync.points_step(failed, provider=provider, sleep=lambda s: None, **kw)

    assert torgi_sync.start_points_loop() is None  # демо на фикстурах: поток не нужен
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    with session() as db:  # лот другого региона — в той же очереди потока
        db.add(TorgiLot(id="KRSK", region="24", cad_num="24:11:0010101:5", status=active[0]))
        db.commit()
    assert without_point() == 13
    nspd = FakeNspd()
    failed: dict = {}
    assert step(failed, nspd, limit=5) == (5, 0) and without_point() == 8
    assert step(failed, nspd) == (8, 0) and without_point() == 0 and failed == {}
    assert step(failed, nspd) == (0, 0) and len(nspd.calls) == 13
    with session() as db:
        assert db.get(TorgiLot, "KRSK").lat is not None and db.get(TorgiLot, "ACT_3").cad_cost

    # НСПД споткнулся на одном участке: остальным это не мешает, участок ждёт сутки
    class Flaky(FakeNspd):
        def fetch_raw(self, cad_num: str) -> dict:
            if cad_num.endswith(":105"):
                self.calls.append(cad_num)
                raise TimeoutError("nspd")
            return super().fetch_raw(cad_num)

    forget_points()
    flaky = Flaky()
    assert step(failed, flaky) == (12, 1) and list(failed) == ["ACT_5"] and without_point() == 1
    assert step(failed, flaky) == (0, 0) and len(flaky.calls) == 13  # сутки не трогаем
    later = datetime.now(UTC) + timedelta(hours=25)
    assert step(failed, flaky, now=later) == (0, 1) and len(flaky.calls) == 14

    # НСПД лежит: выходим после восьми отказов подряд и участки «плохими» не считаем
    class Down:
        calls = 0

        def fetch_raw(self, cad_num: str) -> dict:
            Down.calls += 1
            raise ConnectionError("nspd")

    forget_points()
    failed = {}
    assert step(failed, Down()) == (0, torgi_sync.POINTS_FAIL_STREAK)
    assert Down.calls == torgi_sync.POINTS_FAIL_STREAK and failed == {} and without_point() == 13

    # пока поток точек работает, проход сбора региона НСПД не читает и очередь не держит
    asked: list = []
    monkeypatch.setattr(torgi_sync, "enrich_nspd", lambda *a, **kw: asked.append(a[0]) or 0)
    _sync(fake)
    assert asked == ["40"]
    monkeypatch.setattr(torgi_sync, "points_loop_alive", lambda: True)
    assert _sync(fake)["stats"]["nspd"] == 0 and asked == ["40"]


def test_hot_page_selects_checks_market_and_remembers_criteria(client):  # noqa: F811
    """ТЗ v5 (зум 18.11.2026): «Горячие» — отбор по ожидаемой цене победителя, сверка цены
    рынка, критерии запоминаются."""
    from landcheck.web.db import User, session

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    # цены рынка нет — под критерии не попадает никто, страница объясняет, что отсеяло
    page = client.get("/tools/torgi-hot").text
    assert "Горячие предложения" in page and "не попадает ни один лот" in page
    assert "цена перепродажи взята с рынка" in page and ">Горячие</a>" in page
    # рынок 45 тыс. за сотку: вход по ожидаемой цене 150 тыс. (старт × 1,5), продажа 450 тыс.
    market = {"scope": "40:13", "kind": "sale", "vri_group": "any", "price_mid": "45000"}
    assert client.post("/tools/torgi-market", data=market).status_code == 303
    page = client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=5&region=all").text
    assert "Показано 5 из 12" in page and "из 12 идущих аукционов прошло 12" in page
    assert M(150_000) in page and "старт × 1.5" in page
    # (450 − 150) × 0,87 = 261 тыс.; цена отсечения 450 × 0,87 / 1,87 = 209 358
    assert M(261_000) in page and M(209_358) in page
    assert "обременения ещё не проверены" in page and "задаток " + M(20_000) in page
    assert "Калужская обл., Малоярославецкий р-н" in page
    assert "Проверить руками:" not in page
    with session() as db:
        saved = db.scalars(select(User).where(User.is_admin)).first().hot_criteria
    assert saved == {"annual": "100", "cap": "2000000", "days": "3", "top": "5", "region": "all"}
    # без параметров — те же критерии, что в прошлый раз
    again = client.get("/tools/torgi-hot").text
    assert "Показано 5 из 12" in again and '<option value="5" selected>' in again
    # лимит ниже ожидаемой цены входа — пусто, и видно, какое условие сняло лоты
    capped = client.get("/tools/torgi-hot?annual=100&cap=140000&days=3").text
    assert "не попадает ни один лот" in capped
    # рынок вшестеро выше старта — не подарок, а повод проверить цену
    client.post("/tools/torgi-market", data={**market, "price_mid": "60000"})
    page = client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3").text
    assert "Проверить руками: 12" in page and "в 6 раз выше старта" in page
    assert "не попадает ни один лот" in page
    # один регион
    assert "не попадает ни один лот" in client.get("/tools/torgi-hot?region=10").text


def test_invest_summary_pick_and_public_link(client):  # noqa: F811
    """ТЗ v5, п. 4 (зум 18.11.2026, 01:10): сводка по лоту для инвестора и подборка «пять
    предложений на выбор» — по ссылке без входа, с теми же цифрами, что видел автор."""
    from landcheck.web.db import TorgiPick, session

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    market = {"scope": "40:13", "kind": "sale", "vri_group": "any", "price_mid": "45000"}
    client.post("/tools/torgi-market", data=market)
    hot = client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=5").text
    assert 'name="lot" value="ACT_0"' in hot and "В подборку для инвестора" in hot
    assert 'href="/tools/torgi-lot/ACT_0/invest"' in hot
    # без отмеченных лотов подборка не создаётся
    r = client.post("/tools/torgi-picks", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tools/torgi-hot?nopick=1"

    page = client.get("/tools/torgi-lot/ACT_0/invest").text
    for block in ("1. Что продаётся", "4. Сколько заработаем", "7. Риски", "9. Источники"):
        assert block in page
    assert M(150_000) in page and M(209_358) in page and M(261_000) in page
    assert "Сверка цены пройдена" in page and "174 % годовых" in page
    assert "с борьбой — 15" in page and "без единой заявки — 3" in page
    assert "Ссылка для инвестора" in page and "torgi.gov.ru, лот ACT_0" in page
    assert "/tools/torgi-lot/ACT_0/invest" in client.get("/tools/torgi-lot/ACT_0").text

    r = client.post(
        "/tools/torgi-picks",
        data={"lot": ["ACT_0", "ACT_1", "ACT_0", "нет такого"], "title": "Калуга, ноябрь"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    own = client.get(r.headers["location"]).text
    with session() as db:
        pick = db.scalars(select(TorgiPick)).one()
        token, pick_id = pick.token, pick.id
        assert pick.lot_ids == ["ACT_0", "ACT_1"] and pick.criteria["annual"] == "100"
    assert "Калуга, ноябрь" in own and f"/t/{token}" in own and "Удалить" in own
    assert "Калуга, ноябрь" in client.get("/tools/torgi-picks").text

    # автор меняет свои критерии — цифры в подборке остаются прежними
    client.get("/tools/torgi-hot?annual=300&cap=2000000&days=3&extra=50000")
    client.post("/logout")
    public = client.get(f"/t/{token}").text
    assert "Калуга, ноябрь" in public and "40:13:120101:100" in public
    assert M(261_000) in public and "174 %" in public and f"/t/{token}/ACT_1" in public
    # наружу не уходит ничего внутреннего
    assert (
        "Удалить" not in public and "/tools/" not in public and "Ликвидность и рынок" not in public
    )
    one = client.get(f"/t/{token}/ACT_1").text
    assert "1. Что продаётся" in one and M(209_358) in one and "ко всей подборке (2)" in one
    assert "/tools/" not in one and "Ссылка для инвестора" not in one
    assert client.get(f"/t/{token}/ACT_5").status_code == 404
    assert client.get("/t/nosuchtoken").status_code == 404
    # без входа внутренние страницы закрыты
    assert client.get(f"/tools/torgi-picks/{pick_id}", follow_redirects=False).status_code != 200
    assert client.get("/tools/torgi-lot/ACT_0/invest", follow_redirects=False).status_code != 200


def _hot_ready(client):  # noqa: F811
    """Регион собран, рынок задан так, что все 12 идущих лотов проходят в «горячие»."""
    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    market = {"scope": "40:13", "kind": "sale", "vri_group": "any", "price_mid": "45000"}
    client.post("/tools/torgi-market", data=market)
    return fake


def test_marks_take_lot_to_work_or_hide_it_with_reason(client):  # noqa: F811
    """ТЗ v5, п. 10: «В работу» и «Не подходит» с причиной — отказы показывают, какой
    критерий править."""
    from landcheck.web.db import TorgiMark, session

    _hot_ready(client)
    hot = "/tools/torgi-hot?annual=100&cap=2000000&days=3&top=5"
    page = client.get(hot).text
    assert 'formaction="/tools/torgi-mark/ACT_0/work"' in page and "не подходит" in page
    assert 'name="reason-ACT_0"' in page and "В работе:" not in page
    r = client.post(
        "/tools/torgi-mark/ACT_0/work", headers={"referer": hot}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == hot
    r = client.post(
        "/tools/torgi-mark/ACT_1/skip",
        data={"reason-ACT_1": "место неликвидное", "reason-ACT_2": "другое"},
        headers={"referer": "https://evil.example/x"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/tools/torgi-hot"  # чужой адрес возврата не берём
    page = client.get(hot).text
    assert "В работе: 1" in page and "в работе</span>" in page
    # лот в работе стоит в своём блоке, убранный скрыт: в предложениях осталось десять
    assert "Вы убрали из списков 1 лот." in page and "Показано 5 из 10" in page
    assert 'formaction="/tools/torgi-mark/ACT_1/' not in page
    shown = client.get(hot + "&skipped=1").text
    assert "убран: место неликвидное" in shown and "Показано 5 из 11" in shown
    # причины отказов видит администратор
    assert "Почему лоты убирали из списка — все пользователи: 1" in page
    assert "место неликвидное — 1." in page
    with session() as db:
        marks = {m.lot_id: (m.mark, m.reason) for m in db.scalars(select(TorgiMark))}
    assert marks == {"ACT_0": ("work", None), "ACT_1": ("skip", "место неликвидное")}
    # отметку можно сменить и снять
    client.post("/tools/torgi-mark/ACT_1/work")
    client.post("/tools/torgi-mark/ACT_0/clear")
    page = client.get(hot).text
    assert "В работе: 1" in page and "Вы убрали" not in page
    assert client.post("/tools/torgi-mark/ACT_0/delete").status_code == 404
    assert client.post("/tools/torgi-mark/NO_SUCH/work").status_code == 404


def test_morning_digest_sends_only_new_lots_once_a_day(client):  # noqa: F811
    """Зум 18.11.2026, 01:11: «утром проснулись — система оповестила». Раз в сутки в час
    пользователя, только новые лоты; нет нового — сообщение не шлём."""
    from landcheck.web import torgi_hotjobs as hj
    from landcheck.web.db import Notification, TorgiLot, User, session

    _hot_ready(client)
    form = client.get("/tools/torgi-hot").text
    assert "Утренняя сводка" in form and 'name="dg"' in form and "UTC+7" in form
    # включаем сводку в форме критериев: 09:00 по UTC+7, по пять лотов
    client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=5&dg=1&dh=9&dz=7")
    with session() as db:
        user = db.scalars(select(User).where(User.is_admin)).first()
        uid, crit = user.id, dict(user.hot_criteria)
    assert hj.digest_settings(crit) == (True, 9, 7.0)
    early = datetime(2026, 11, 20, 1, 30, tzinfo=UTC)  # 08:30 по UTC+7 — рано
    morning = datetime(2026, 11, 20, 2, 30, tzinfo=UTC)  # 09:30
    assert hj.digest_due(crit, None, early) is None
    assert hj.digest_due(crit, None, morning) == "2026-11-20"
    assert hj.digest_due({**crit, "dg": ""}, None, morning) is None
    assert hj.run_digests(early) == 0 and hj.run_digests(morning) == 1

    def notes() -> list[str]:
        with session() as db:
            q = select(Notification).where(Notification.user_id == uid, Notification.kind == "hot")
            return [n.text for n in db.scalars(q.order_by(Notification.id))]

    first = notes()[0]
    assert first.startswith("Новых лотов под ваши критерии: 5 (всего проходит 12)")
    assert "1. " in first and "5. " in first and "6. " not in first
    assert "вход " + M(150_000) in first and "не выше " + M(209_358) in first
    assert "174 % годовых" in first and "/tools/torgi-lot/ACT_" in first
    assert "В работе" not in first
    # в тот же день второй раз не шлём
    assert hj.run_digests(morning + timedelta(hours=3)) == 0 and len(notes()) == 1
    # лот в работе, приём заявок кончается послезавтра; один лот убран как неподходящий
    with session() as db:
        seen = set(db.get(User, uid).hot_digest["seen"])
        rest = sorted(f"ACT_{i}" for i in range(12) if f"ACT_{i}" not in seen)
        soon = (datetime.now(UTC).date() + timedelta(days=2)).isoformat()
        db.get(TorgiLot, rest[0]).bidd_end = soon
        db.commit()
    client.post(f"/tools/torgi-mark/{rest[0]}/work")
    client.post(f"/tools/torgi-mark/{rest[1]}/skip", data={f"reason-{rest[1]}": "не моя ниша"})
    assert hj.run_digests(morning + timedelta(days=1)) == 1
    second = notes()[1]
    # из семи оставшихся: один ушёл по сроку, один убран — новых пять
    assert second.startswith("Новых лотов под ваши критерии: 5 (всего проходит 11)")
    assert "В работе, приём заявок заканчивается:" in second and "осталось 2 дн." in second
    assert rest[1] not in second
    # на третий день нового нет, но срок по лоту в работе близко — напоминание идёт одно
    assert hj.run_digests(morning + timedelta(days=2)) == 1
    assert notes()[2].startswith("В работе, приём заявок заканчивается:")
    client.post(f"/tools/torgi-mark/{rest[0]}/clear")
    # ни новых лотов, ни сроков — сообщения нет, день отмечен, чтобы не считать каждые 10 минут
    assert hj.run_digests(morning + timedelta(days=3)) == 0 and len(notes()) == 3
    with session() as db:
        assert db.get(User, uid).hot_digest["day"] == "2026-11-23"


def test_digest_shows_one_line_per_auction(client):  # noqa: F811
    """Прод, 19.11.2026: пробная сводка — пять строк, и все пять лоты одного аукциона.
    Строка сводки — аукцион: лучший лот и сколько ещё лотов этого аукциона прошли отбор."""
    from landcheck.web import torgi_hotjobs as hj
    from landcheck.web.db import TorgiLot, User, session

    _hot_ready(client)
    with session() as db:
        for i in range(12):  # лоты 0–6 — один аукцион, 7–8 — второй, у остальных номера нет
            db.get(TorgiLot, f"ACT_{i}").notice = "N1" if i < 7 else ("N2" if i < 9 else None)
        db.commit()
        uid = db.scalars(select(User).where(User.is_admin)).first().id
    crit = {"annual": "100", "cap": "2000000", "days": "3", "top": "3", "dg": "1"}
    text, state = hj.build_digest(uid, crit, None)
    assert text.startswith("Новых лотов под ваши критерии: 10 в 3 аукционах (всего проходит 12)")
    assert "в этом аукционе ещё 6 лотов под критерии" in text
    assert "в этом аукционе ещё 1 лот под критерии" in text
    assert "3. " in text and "4. " not in text and text.count("/invest") == 3
    # показанными считаются все лоты названных аукционов: завтра они не «новые»
    assert len(state["seen"]) == 10
    text, state = hj.build_digest(uid, crit, state)
    assert text.startswith("Новых лотов под ваши критерии: 2 (всего проходит 12)")
    assert "в этом аукционе" not in text and len(state["seen"]) == 12
    assert hj.build_digest(uid, crit, state)[0] is None
    # у района без названия в адресах (город) место берём из адреса, а не кодом «18:26»
    row = {"id": "X_1", "region": "18", "district": "18:26"}
    assert hj._place_fallback(
        {**row, "address": "Удмуртская Республика, г. Ижевск, ул. Мира, 1"}
    ) == ("Удмуртская Республика, г. Ижевск")
    assert hj._place_fallback(row).endswith("район 18:26") and len(hj._place_fallback(row)) > 12
    # счёт слов
    assert hj._count(1, "лот", "лота", "лотов") == "1 лот"
    assert hj._count(3, "лот", "лота", "лотов") == "3 лота"
    assert hj._count(12, "лот", "лота", "лотов") == "12 лотов"
    assert hj._count(21, "аукционе", "аукционах", "аукционах") == "21 аукционе"
    # привязка Telegram — ссылкой прямо со страницы «Горячие», когда бот настроен
    page = client.get("/tools/torgi-hot?dg=1").text
    assert "Telegram не привязан" in page and 'href="/profile"' in page


def test_map_facts_for_candidates_feed_class_and_investor_summary(client):  # noqa: F811
    """ТЗ v5, п. 7: доступность места — населённый пункт, дорога с покрытием, город, что портит
    место. Карту спрашиваем фоном только по показанным лотам; итог виден в списке и в сводке."""
    from landcheck import nearby
    from landcheck.web import torgi_nearby as tn
    from landcheck.web.db import TorgiLot, session

    _hot_ready(client)
    with session() as db:
        for lot_id, point in (("ACT_0", (54.5, 36.2)), ("ACT_1", (54.6, 36.3)), ("ACT_2", None)):
            lot = db.get(TorgiLot, lot_id)
            lot.lat, lot.lon = point or (None, None)
        db.get(TorgiLot, "ACT_3").lat, db.get(TorgiLot, "ACT_3").lon = 54.7, 36.4
        db.commit()
    assert tn.missing(["ACT_0", "ACT_1", "ACT_2", "NOPE", None, "ACT_0"]) == [
        "ACT_0",
        "ACT_1",
        "ACT_2",
    ]

    def node(lat, lon, dlat, **tags):
        return {"type": "node", "lat": lat + dlat, "lon": lon, "tags": tags}

    asked: list[str] = []

    def fetch(query: str) -> dict:
        asked.append(query)
        if "54.5" in query:  # село в 300 м, дорога в 200 м, кладбище в 300 м; Калуга рядом
            return {
                "elements": [
                    node(54.5, 36.2, 0.003, place="village", name="Ерденево"),
                    {
                        "type": "way",
                        "tags": {"highway": "tertiary"},
                        "geometry": [
                            {"lat": 54.49, "lon": 36.203},
                            {"lat": 54.51, "lon": 36.203},
                        ],
                    },
                    node(54.5, 36.2, 0.0027, landuse="cemetery"),
                ]
            }
        if "54.6" in query:  # вокруг ничего: ни дороги, ни села
            return {"elements": []}
        return {"elements": [], "error": "серверы OpenStreetMap не ответили"}

    pauses: list[float] = []
    tn._queue.update(dict.fromkeys(["ACT_0", "ACT_1", "ACT_2", "ACT_3", "ACT_9"]))
    assert tn.queued() == 5
    assert tn.fill(["ACT_0", "ACT_1", "ACT_2", "ACT_3"], fetch=fetch, sleep=pauses.append) == 4
    assert tn.queued() == 1  # обработанные лоты уходят из очереди сразу, не в конце пачки
    tn._queue.clear()
    assert len(asked) == 3 and pauses == [tn.PAUSE_S] * 3  # лот без точки карту не спрашивал
    with session() as db:
        states = {i: db.get(TorgiLot, f"ACT_{i}").nearby_state for i in range(4)}
        facts = db.get(TorgiLot, "ACT_0").nearby
    assert states == {0: "ok", 1: "ok", 2: "nopoint", 3: "error"}
    assert facts["settlement"]["name"] == "Ерденево" and facts["city"]["name"] == "Калуга"
    assert nearby.access(facts)["level"] == "good"
    # повторно не спрашиваем: ответ есть, а ошибку и лот без точки пробуем через шесть часов
    assert tn.missing([f"ACT_{i}" for i in range(4)]) == []
    later = datetime.now(UTC) + timedelta(days=2)
    assert tn.missing([f"ACT_{i}" for i in range(4)], later) == ["ACT_2", "ACT_3"]
    assert tn.start_fill(["ACT_5"]) == 0  # демо на фикстурах: фоновый поток не стартует
    from landcheck.web import torgi_hotjobs as hj

    assert hj.warm_candidates() == 0  # прогрев считает кандидатов, но в демо сеть не трогает

    page = client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=25").text
    assert "Доступность хорошая: село 350 м, дорога 200 м, Калуга 4,0 км" in page
    assert "рядом: кладбище 300 м" in page
    # до участка не доехать — это гипотеза (класс C), причина названа
    assert "до участка трудно добраться: нет населённого пункта в 5 км" in page
    assert "Гипотезы — класс C: 1" in page or "класс C" in page
    invest = client.get("/tools/torgi-lot/ACT_0/invest").text
    assert "Место и доступность." in invest and "Доступность <strong>хорошая</strong>" in invest
    assert "<th>Ближайший город</th><td>Калуга, 4,0 км (334 тыс. жителей)</td>" in invest
    assert "Портит место: кладбище 300 м" in invest and "Карта неполна" in invest
    # в сравнении подборки — оборот и доступность рядом
    r = client.post("/tools/torgi-picks", data={"lot": ["ACT_0", "ACT_1"]}, follow_redirects=False)
    pick = client.get(r.headers["location"]).text
    assert "Доступность места" in pick and "хорошая" in pick and "слабая" in pick
    assert "Сделок в квартале за год" in pick
    assert "У участка нет точки на карте" in client.get("/tools/torgi-lot/ACT_2/invest").text
    failed = client.get("/tools/torgi-lot/ACT_3/invest").text
    # карта не ответила, но города из справочника видны и без неё
    assert "Серверы карты не ответили" in failed and "Крупный город: Калуга, 23 км" in failed
    assert "ещё не спрошена" in client.get("/tools/torgi-lot/ACT_5/invest").text


def test_hot_row_opens_summary_that_defends_the_deal(client):  # noqa: F811
    """Зум 20.11.2026, первый просмотр практиком. Щелчок по участку в «Горячих» открывал
    разбор лота со сплошным текстом, а сводка для инвестора была мелкой ссылкой под ним —
    практик её не увидел. Теперь название участка ведёт в сводку, а в ней — доводы за и
    против, цена продажи диапазоном с ROI и таблица сроков."""
    from landcheck.web import torgi_views as tv
    from landcheck.web.db import User, session

    _hot_ready(client)
    hot = client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=5").text
    assert '<a href="/tools/torgi-lot/ACT_0/invest" title="Сводка для инвестора' in hot
    assert 'class="small muted">подробный разбор</a>' in hot
    # заголовок таблицы закреплён, столбцы сортируются щелчком
    assert 'class="table-wrap sticky"' in hot and "table.sortable" in hot
    assert 'data-key="annual"' in hot and 'data-key="region"' in hot and 'data-annual="' in hot
    page = client.get("/tools/torgi-lot/ACT_0/invest").text
    assert "За сделку" in page and "Против и что проверить" in page and "<h4>Риски</h4>" in page
    assert "середина рынка выше ожидаемой цены входа" in page
    assert "от цены покупки, не от старта" in page and "ROI за сделку" in page
    assert "Если продать за 3, 6, 9 и 12 месяцев" in page and "<th>12 мес.</th>" in page
    assert "Дольше года участок не держим" in page
    # порядок «сначала Московская область»: регион 50 первым, остальные — по лучшему лоту
    items = [{"row": {"id": n, "region": r}} for n, r in enumerate(["66", "50", "59", "50", "66"])]
    assert [i["row"]["id"] for i in tv._hot_order(items, "mo")] == [1, 3, 0, 4, 2]
    assert tv._hot_order(items, "") is items and tv._hot_order(items, None) is items
    ordered = client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=5&order=mo").text
    assert "сначала Московская область, затем остальные регионы" in ordered
    assert '<option value="mo" selected>' in ordered
    with session() as db:
        assert db.scalars(select(User).where(User.is_admin)).first().hot_criteria["order"] == "mo"


def test_system_choice_page_ranks_deals_builds_a_set_and_keeps_criteria(client):  # noqa: F811
    """ТЗ v5, п. 9, в концепции Тони от 20.11.2026: лучшие сделки всех регионов одним списком
    без настройки критериев, стратегия и доводы по каждой, набор лотов под бюджет. Режим не
    трогает сохранённые критерии «Горячих» — по ним идёт утренняя сводка."""
    from landcheck.web import torgi_views as tv
    from landcheck.web.db import User, session

    _hot_ready(client)
    client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=5")
    with session() as db:
        saved = dict(db.scalars(select(User).where(User.is_admin)).first().hot_criteria)
    page = client.get("/tools/torgi-best")
    assert page.status_code == 200
    html = page.text
    assert "<h2>Выбор системы" in html and 'class="active">Выбор системы</a>' in html
    assert "Как система сравнивает сделки" in html and "годовая доходность в осторожном" in html
    assert html.count('<article class="best-card') == 10 and "Лучшие сделки: 10 из 12" in html
    assert '<span class="best-rank">1</span>' in html and "Стратегия:" in html
    assert "Недооценка" in html and "Ликвидность места" in html and "Срок продажи" in html
    assert "За сделку" in html and "Против и что проверить" in html and "<h4>Риски</h4>" in html
    assert 'href="/tools/torgi-lot/ACT_0/invest"' in html and "torgi.gov.ru/new/public/lots" in html
    assert 'hx-get="/tools/torgi-best/split"' in html and "Набор под бюджет" not in html
    assert "Что осталось за бортом: из 12 идущих аукционов" in html
    assert "имуществу должников система не берёт" in html
    # бюджет: набор собран, лоты набора помечены; бюджет в миллионах и в рублях — одно и то же
    rub = client.get("/tools/torgi-best?budget=250000&top=5").text
    mln = client.get("/tools/torgi-best?budget=0,25&top=5").text
    for html in (rub, mln):
        assert "Набор под бюджет 250" in html and "Лотов в наборе" in html
        assert "в наборе</span>" in html and 'class="best-card in-plan"' in html
        assert html.count('<article class="best-card') == 5
    assert "Прибыль, осторожно" in rub and "набор собран по ней" in rub
    # бюджета не хватает ни на один лот — так и сказано
    small = client.get("/tools/torgi-best?budget=5000").text
    assert "не проходит ни один лот" in small and '<article class="best-card' not in small
    # убранный пользователем лот в выбор не попадает
    client.post("/tools/torgi-mark/ACT_0/skip", data={"reason-ACT_0": "другое"})
    assert 'id="lot-ACT_0"' not in client.get("/tools/torgi-best?top=20").text
    # критерии «Горячих» остались прежними: режим ничего не запоминает
    with session() as db:
        assert db.scalars(select(User).where(User.is_admin)).first().hot_criteria == saved
    # гипотезы «под раздел» подгружаются отдельно; без сделок Росреестра — пустой блок
    tv._split_cache.clear()
    split = client.get("/tools/torgi-best/split")
    assert split.status_code == 200 and "Большие участки под раздел: 0" in split.text
    # раздел закрытый: без доступа к торгам страница не отдаётся
    client.post("/logout")
    client.cookies.clear()
    for url in ("/tools/torgi-best", "/tools/torgi-best/split"):
        assert client.get(url, follow_redirects=False).status_code == 307  # на вход


def test_background_writes_do_not_drop_the_scan_cache(client, monkeypatch):  # noqa: F811
    """Прод, 20.11.2026: пока фоновые потоки читали итоги торгов, расчёт устаревал быстрее,
    чем считался, и «Горячие» отвечали 8–15 с. Фоновая запись расчёт не сбрасывает — его
    обновляет прогрев; запись из запроса пользователя сбрасывает сразу."""
    import threading

    from landcheck import torgi_scan
    from landcheck.web import torgi_views as tv
    from landcheck.web.db import TorgiLot, session

    _hot_ready(client)
    sp = torgi_scan.ScanParams()

    def price() -> float:
        rows = tv._scan_all(sp)[0]["rows"]
        return next(r["price_start"] for r in rows if r["id"] == "ACT_0")

    def write(value: float) -> None:
        with session() as db:
            db.get(TorgiLot, "ACT_0").price_start = value
            db.commit()

    def in_background(value: float, name: str) -> None:
        thread = threading.Thread(target=write, args=(value,), name=name)
        thread.start()
        thread.join()

    was = price()
    in_background(was + 1000, "torgi-sync_0")
    assert price() == was  # страница отдаёт прежний расчёт
    assert tv.warm_recent() >= 0 and price() == was + 1000  # прогрев запись учёл
    write(was + 2000)  # запись из запроса пользователя видна сразу
    assert price() == was + 2000
    in_background(was + 3000, "torgi-hot-cards")
    assert price() == was + 2000
    monkeypatch.setattr(tv, "SOFT_STALE_S", 0)  # расчёт старше срока — считаем и без прогрева
    assert price() == was + 3000
    # города рядом ищем только для страницы одного лота: поиск по справочнику для каждого из
    # восьми тысяч лотов стоил «Горячим» две секунды на запрос
    from landcheck import nearby, torgi_hot

    calls: list[tuple[float, float]] = []
    real = nearby.far_places
    monkeypatch.setattr(nearby, "far_places", lambda a, b: calls.append((a, b)) or real(a, b))
    # страница считает города только для показанных строк (их десятки), отбор — ни для одной
    assert client.get("/tools/torgi-hot").status_code == 200 and len(calls) <= 12
    calls.clear()
    res = torgi_hot.select(tv._scan_all(sp)[0]["rows"])
    assert res["hot"] and "towns" not in res["hot"][0] and not calls
    assert client.get("/tools/torgi-hot").status_code == 200 and not calls  # второй раз — из строки
    with session() as db:
        lot = db.get(TorgiLot, "ACT_3")
        lot.lat, lot.lon = 54.60, 36.30
        db.commit()
    assert "Ближайший город: Калуга, " in client.get("/tools/torgi-lot/ACT_3/invest").text
    assert calls == [(54.60, 36.30)]
    # прогрев держит тёплыми и настройки, с которыми страницы открывали недавно
    other = torgi_scan.ScanParams(extra=50_000)
    tv._scan_all(other)
    assert {repr(sp), repr(other)} <= set(tv._recent_params)
    monkeypatch.setattr(tv, "RECENT_PARAMS_S", -1)
    tv.warm_recent()
    assert not tv._recent_params


def test_digest_holds_new_lot_until_place_demand_is_checked(client):  # noqa: F811
    """Прод, 20.11.2026: первые три строки первой сводки — лоты квартала, где семь торгов из
    восьми прошли без заявок; итоги этих торгов на момент сводки не были прочитаны. Лот с
    непроверенным спросом в сводку не идёт, итоги торгов его места читаются вне очереди."""
    from landcheck.web import torgi_hotjobs as hj
    from landcheck.web.db import TorgiLot, User, session

    _hot_ready(client)
    with session() as db:
        uid = db.scalars(select(User).where(User.is_admin)).first().id
        done = db.scalars(
            select(TorgiLot).where(TorgiLot.status.in_(["SUCCEED", "FAILED"])).order_by(TorgiLot.id)
        ).all()
        unread = [x.id for x in done[:12]]  # итог не прочитан у 12 торгов района из 21
        for lot in done[:12]:
            lot.status, lot.card_at, lot.price_fin = "FAILED", None, None
            lot.applied = lot.admitted = lot.fail_code = lot.fail_reason = None
        db.commit()
    crit = {"annual": "100", "cap": "2000000", "days": "3", "top": "5", "dg": "1"}
    text, state = hj.build_digest(uid, crit, None)
    # все двенадцать лотов района ждут: сообщения нет, показанными они не считаются
    assert text is None and state["seen"] == [] and len(state["waiting"]) == 12
    # о спросе у этих лотов расчёт судит по району — итоги читаем по району, свежие первыми
    from landcheck import torgi_hot, torgi_scan
    from landcheck.web import torgi_views as tv

    rows = tv._scan_all(torgi_scan.ScanParams())[0]["rows"]
    items = [{"row": r} for r in rows if r["id"] in state["waiting"]]
    assert all(torgi_hot.outcomes_unread(i["row"]) for i in items)
    assert hj.demand_places(items) == ([], ["40:13"])
    assert sorted(hj.unread_outcomes([], ["40:13"])) == sorted(unread)
    assert len(hj.unread_outcomes([], ["40:13", "40:14"], limit=4)) == 4
    assert hj.unread_outcomes([], ["40:99"]) == []
    assert hj.read_outcomes_of(state["waiting"]) == 0  # в тестах фоновое чтение выключено
    # трое ждали со вчера — идут с пометкой; остальные девять ждут первый раз
    part, st = hj.build_digest(uid, crit, {"waiting": state["waiting"][:3]})
    assert part.startswith("Новых лотов под ваши критерии: 3 (всего проходит 12)")
    assert "Ещё 9 лотов ждут проверки спроса" in part and len(st["waiting"]) == 9
    assert len(st["seen"]) == 3 and part.count("спрос не проверен") == 3
    # старше срока, по которому считается ликвидность, — не читаем: расчёт их не видит
    with session() as db:
        db.get(TorgiLot, unread[0]).published = "2020-01-01"
        db.commit()
    assert unread[0] not in hj.unread_outcomes([], ["40:13"])
    # сутки спустя площадка итоги так и не отдала — показываем, но с пометкой
    text, again = hj.build_digest(uid, crit, state)
    assert text.startswith("Новых лотов под ваши критерии: 5 (всего проходит 12)")
    assert text.count("спрос не проверен: итоги прошлых торгов") == 5
    assert len(again["seen"]) == 5 and again["waiting"] == []
    # итоги прочитаны, заявки на торгах были — пометки нет, лоты идут обычным порядком
    with session() as db:
        for lot_id in unread:
            lot = db.get(TorgiLot, lot_id)
            lot.card_at, lot.applied, lot.price_fin = NOW, 1, lot.price_start
        db.commit()
    text, state = hj.build_digest(uid, crit, None)
    assert text.startswith("Новых лотов под ваши критерии: 5") and "спрос не проверен" not in text
    assert state["waiting"] == [] and "ждут проверки спроса" not in text


def test_candidate_cards_are_read_out_of_turn(client):  # noqa: F811
    """Прод, 19.11.2026: у 994 из 1519 идущих продаж региона карточка не прочитана — нет
    задатка, шага, отметки о градплане. Кандидатам карточку читаем сразу."""
    from landcheck.web import torgi_hotjobs as hj
    from landcheck.web.db import TorgiLot, session

    fake = _hot_ready(client)
    with session() as db:
        for lot_id in ("ACT_3", "ACT_7", "S_0"):
            lot = db.get(TorgiLot, lot_id)
            lot.card_at, lot.deposit, lot.price_step = None, None, None
        db.commit()
    page = client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=25").text
    assert "задаток: карточка читается" in page
    # состоявшиеся торги и уже прочитанные карточки не трогаем; порядок — как попросили
    assert hj.missing_cards(["ACT_7", "ACT_0", "S_0", "ACT_3", None, "ACT_7"]) == ["ACT_7", "ACT_3"]
    # не состоявшиеся торги без карточки в кварталах кандидатов читаем тоже: без итога не
    # понять, был ли там единственный участник или заявок не было (прод, 20.11.2026)
    with session() as db:
        failed = [x for x in db.scalars(select(TorgiLot).where(TorgiLot.status == "FAILED"))]
        assert failed, "в наборе есть не состоявшиеся торги"
        quarter = failed[0].quarter
        same = [x.id for x in failed if x.quarter == quarter]
        for x in failed:
            x.card_at = None if x.quarter == quarter else x.card_at
        db.commit()
    unread = hj.unread_outcomes([quarter, None, quarter])
    assert sorted(unread) == sorted(same) and hj.unread_outcomes([]) == []
    assert hj.unread_outcomes([quarter], limit=1) == unread[:1]
    assert hj.missing_cards(unread) == unread
    # карточку кандидата ждёт человек на странице — она идёт впереди итогов прошлых торгов
    hj._cards_queue.clear()
    assert hj._enqueue(["H1", "H2"], False) == 2 and hj._enqueue(["C1", "H2"], True) == 1
    assert list(hj._cards_queue) == ["C1", "H2", "H1"] and hj._enqueue(["H1"], False) == 0
    hj._cards_queue.clear()
    api = torgi.TorgiClient(fake.client(), interval=0, sleep=lambda s: None)
    before = len(fake.seen)
    assert hj.fill_cards(["ACT_7", "ACT_0", "ACT_3"], api=api, now=NOW) == 2
    assert len(fake.seen) - before == 2
    with session() as db:
        lot = db.get(TorgiLot, "ACT_3")
        assert lot.card_at is not None and lot.deposit == 20_000 and lot.price_step == 3_000
    assert hj.missing_cards(["ACT_7", "ACT_3"]) == [] and hj.fill_cards(["ACT_7"], api=api) == 0
    # в тестах шлюза нет — фоновые потоки не запускаются
    assert hj.start_cards(["ACT_3"]) == 0 and hj.start_warm_loop() is None
    assert hj.start_digest_loop() is None


def test_backtest_page_returning_list_and_lot_back_on_auction(client):  # noqa: F811
    """ТЗ v5, п. 8 и п. 5: проверка модели на завершённых торгах, список участков без заявок,
    которые вернутся на торги, и сообщение, когда такой участок выставили снова."""
    from landcheck.web import torgi_hotjobs as hj
    from landcheck.web.db import Notification, TorgiLot, User, session

    _hot_ready(client)
    recent = (datetime.now(UTC).date() - timedelta(days=10)).isoformat()
    with session() as db:
        for lot in db.scalars(select(TorgiLot).where(TorgiLot.status == "FAILED")):
            lot.auction_date = recent
        db.commit()
    page = client.get("/tools/torgi-backtest").text
    assert "Насколько точна модель" in page and "Все посчитанные регионы: 1 из 1" in page
    # 15 состоявшихся и 6 не состоявшихся (три — одному участнику, три — без заявок)
    assert "21: с борьбой 15, одному участнику 3, без заявок 3" in page
    assert "Прошли бы в «горячие» — <strong>21</strong>" in page
    assert "без единой заявки 3 (14 %)" in page
    # ждали вход по 150 тыс.: с борьбой купили ровно за столько, без борьбы — по старту
    assert "×1,00" in page and "100 %" in page and ">Точность модели</a>" in page
    hot = client.get("/tools/torgi-hot?annual=100&cap=2000000&days=3&top=5&dg=1&dh=9&dz=7").text
    assert "Вернутся на торги: 3 участк." in hot and "скорее всего без борьбы" not in hot
    assert "обычно с борьбой" in hot
    # сводка запоминает, за какими участками следим
    morning = datetime(2026, 11, 20, 2, 30, tzinfo=UTC)
    assert hj.run_digests(morning) == 1
    with session() as db:
        user = db.scalars(select(User).where(User.is_admin)).first()
        watched = user.hot_digest["returning"]
        uid = user.id
        assert len(watched) == 3
        # один из них выставили снова
        db.get(TorgiLot, "ACT_11").cad_num = watched[0]
        db.commit()
    client.post("/tools/torgi-market", data={"scope": "40:13", "price_mid": "45001"})  # сброс кэша
    assert hj.run_digests(morning + timedelta(days=1)) == 1
    with session() as db:
        text = (
            db.scalars(
                select(Notification)
                .where(Notification.user_id == uid)
                .order_by(Notification.id.desc())
            )
            .first()
            .text
        )
    assert "Снова на торгах — участки, на которые раньше не было заявок:" in text
    assert watched[0] in text and "/tools/torgi-lot/ACT_11/invest" in text


def test_resume_after_restart_rotates_the_queue(client, monkeypatch):  # noqa: F811
    """Прод, 18.11.2026: выкладки шли чаще, чем длится проход большого региона, очередь каждый
    раз поднималась с него же, и три региона за ним простояли весь день. После рестарта первым
    идёт тот, кто дольше всех не начинал проход, а оборванный регион — последним."""
    from landcheck import ruproxy
    from landcheck.web import torgi_sync
    from landcheck.web.config import settings
    from landcheck.web.db import TorgiSync, session

    t0 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    with session() as db:
        db.add_all(
            [
                TorgiSync(region="50", state="running", started_at=t0 + timedelta(hours=6)),
                TorgiSync(region="66", state="queued", started_at=t0 + timedelta(minutes=25)),
                TorgiSync(region="45", state="queued", started_at=t0),
                TorgiSync(region="02", state="queued", started_at=None),  # ещё ни разу не шёл
                TorgiSync(region="24", state="idle", started_at=t0),  # не в очереди
            ]
        )
        db.commit()
    order: list[str] = []
    monkeypatch.setattr(torgi_sync, "start_sync", lambda r: order.append(r) or True)
    monkeypatch.setattr(ruproxy, "enabled", lambda: True)
    monkeypatch.setattr(settings, "fixtures", None)
    assert torgi_sync.resume_interrupted() == ["02", "45", "66", "50"]
    assert order == ["02", "45", "66", "50"]


def test_scan_cache_reuses_until_data_changes(client, monkeypatch):  # noqa: F811
    """Общий список всех регионов не пересчитывает регион на каждый запрос: расчёт хранится,
    пока в регионе не записали лот или запись рынка. Сортировка страницы не портит его."""
    import re

    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiLot, TorgiMarket, session

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    with session() as db:  # самый дорогой лот — первым при сортировке по цене
        db.get(TorgiLot, "ACT_5").price_start = 900_000
        db.commit()
    calls: list[str] = []
    real = torgi_sync.lots_of

    def counting(region, **kw):
        calls.append(region)
        return real(region, **kw)

    monkeypatch.setattr(torgi_sync, "lots_of", counting)

    def order(url: str) -> list[str]:
        return re.findall(r"/tools/torgi-lot/(\w+)", client.get(url).text)

    first = order("/tools/torgi-scan?region=40&roi=40")
    by_price = order("/tools/torgi-scan?region=40&roi=40&sort=price_desc")
    assert by_price[0] == "ACT_5" and first[0] != "ACT_5"
    assert order("/tools/torgi-scan?region=all&roi=40") == first  # тот же расчёт региона
    assert order("/tools/torgi-scan?region=40&roi=40") == first and calls == ["40"]
    with session() as db:
        db.add(TorgiMarket(scope="40:13", kind="sale", price_mid=50_000, source="manual"))
        db.commit()
    order("/tools/torgi-scan?region=40&roi=40")
    assert calls == ["40", "40"]
    order("/tools/torgi-scan?region=40&roi=10")  # другие критерии — свой расчёт
    assert calls == ["40", "40", "40"]


def test_liquidity_links_bids_slider_and_map(client):  # noqa: F811
    """Чат 17.11 (№72–75, 78–79): числа таблицы ведут к лотам, ползунок «заявок не меньше N»,
    заголовок говорит, что это медиана, идущие извещения открываются, карта районов."""
    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    page = client.get("/tools/torgi-liquidity?region=40").text
    assert "Заявок на лот, медиана" in page
    assert "/tools/torgi-history?region=40&amp;d=40:13&amp;outcome=none" in page
    assert "/tools/torgi-history?region=40&amp;d=40:13&amp;outcome=sold" in page
    assert "/tools/torgi-scan?region=40&amp;d=40:13&amp;form=all" in page
    assert 'id="liq-map"' in page and '"key": "40:13"' in page  # кружок по идущим лотам
    assert 'type="range" name="bids"' in page
    hidden = client.get("/tools/torgi-liquidity?region=40&bids=5").text
    assert "Скрыто районов: 1" in hidden and 'id="liq-map"' not in hidden
    assert ">Завершённые</a>" in page


def test_history_outcomes_winner_and_signals(client):  # noqa: F811
    """Чат 17.11 (№74, 76, 77): завершённые торги по исходу, кто взял (без имён), почему лот,
    вероятно, не ушёл: старт выше медианы проданных рядом, жалобы."""
    from landcheck import torgi_scan
    from landcheck.web import torgi_sync
    from landcheck.web.db import session

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    card = _done(99, "FAILED")
    card["priceMin"] = 500_000
    card["hasAppeals"] = True
    with session() as db:
        torgi_sync.upsert(db, torgi_scan.lot_row(_full(card, fin=None, applied=0)), NOW, card=True)
        db.commit()
    page = client.get("/tools/torgi-history?region=40").text
    assert "все завершённые 22" in page and "продано с борьбой 15" in page
    assert "единственному участнику 3" in page and "не ушли 4" in page
    assert "физлицо" in page and "×1.50" in page and SECRET_NAME not in page
    assert '/сот.</div><div class="small ok">×1.50' in page  # итог и за сотку (чат №81)
    none = client.get("/tools/torgi-history?region=40&outcome=none").text
    assert "40:13:120102:100" in none and "40:13:120102:51" in none
    assert "40:13:120102:52" not in none  # одна заявка — ушёл единственному участнику
    assert "старт ×3.3 к медиане проданных в квартале — цена, похоже, завышена" in none
    assert "по торгам были жалобы (ФАС)" in none
    empty = client.get("/tools/torgi-history?region=40&d=40:13:120101").text
    assert "В квартале 40:13:120101 завершённых торгов" in empty


def test_places_fill_store_retry_and_map_contour(client):  # noqa: F811
    """Контуры районов и кварталов: один раз из НСПД в базу, неудача — повтор через сутки;
    на карте ликвидности рисуется контур, а не кружок."""
    from landcheck.web import torgi_places

    class Nspd:
        def __init__(self):
            self.calls: list[str] = []

        def fetch_division_raw(self, key):
            self.calls.append(key)
            if key == "40:99":
                return None
            if key == "40:98":
                raise RuntimeError("timeout")
            ring = [[36.0, 54.0], [36.1, 54.0], [36.1, 54.1], [36.0, 54.1], [36.0, 54.0]]
            return {"type": "Polygon", "coordinates": [ring]}

    nspd = Nspd()
    done = torgi_places.fill(
        ["40:13", "40:99", "40:98", "40:13", None], provider=nspd, sleep=lambda s: None, now=NOW
    )
    assert done == 3 and nspd.calls == ["40:13", "40:99", "40:98"]
    have = torgi_places.shapes(["40:13", "40:99", "40:98"])
    assert have["40:13"]["geometry"]["type"] == "Polygon" and 54.0 < have["40:13"]["lat"] < 54.1
    assert have["40:99"]["error"] == "в НСПД не найден" and "timeout" in have["40:98"]["error"]
    assert torgi_places.missing(["40:13", "40:99"], now=NOW + timedelta(hours=1)) == []
    assert torgi_places.missing(["40:13", "40:99"], now=NOW + timedelta(days=2)) == ["40:99"]
    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    page = client.get("/tools/torgi-liquidity?region=40").text
    assert '"type": "Polygon"' in page


def test_pick_division_exact_number_largest_contour():
    """Поиск НСПД на номер района отдаёт район вместе с его кварталами (прод, 17.11.2026:
    AmbiguousSearchError у библиотеки) — берём объект ровно с этим номером."""
    from landcheck.providers.nspd import pick_division

    def feat(number: str, size: float, top: bool = False) -> dict:
        ring = [[0, 0], [size, 0], [size, size], [0, size], [0, 0]]
        props = {"label": number} if top else {"options": {"cad_num": number}}
        return {"properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}}

    feats = [feat("24:50:0300101", 1), feat("24:50", 9), feat("24:50:0300102", 2)]
    assert pick_division(feats, "24:50")["coordinates"][0][1] == [9, 0]
    assert pick_division(feats, "24:50:0300102")["coordinates"][0][1] == [2, 0]
    assert pick_division([feat("24:50", 3, top=True)], "24:50") is not None
    assert pick_division(feats, "24:51") is None
    child = feat("24:50:0300103", 50)
    child["properties"]["options"]["parent_cad_num"] = "24:50"
    assert pick_division([child, feat("24:50", 9)], "24:50")["coordinates"][0][1] == [9, 0]
    assert pick_division([{"properties": {"label": "24:50"}, "geometry": None}], "24:50") is None


def test_zones_summary_fill_retry_and_pages(client):  # noqa: F811
    """Чат 17.11 №76 (обещано к 19.11): обременения у лотов — ЗОУИТ, ООПТ, лесопарк из
    полного ответа НСПД; кэш участков первым; НСПД не ответил — повтор через сутки; в списке
    лотов строка «Обременения», у не ушедших — сигнал «обременения: …»."""
    from landcheck import torgi_scan
    from landcheck.web import torgi_sync, torgi_zones
    from landcheck.web.db import TorgiLot, session
    from tests.helpers import zone

    lep = zone("ЗОУИТ", "40:13-6.1", "Охранная зона", "Охранная зона ВЛ 10 кВ")
    park = zone("ООПТ", "40:00-6.9", None, "Национальный парк Угра")
    assert torgi_zones.summary({"found": False}) == (None, "notfound")
    assert torgi_zones.summary({"found": False, "errors": ["timeout"]}) == (None, "error")
    ok = make_payload("40:13:120101:100", zouit=[lep], oopt=[park])
    zones, state = torgi_zones.summary(ok)
    assert state == "ok" and len(zones) == 2 and zones[1].startswith("ООПТ: ")
    assert "Охранная зона ВЛ 10 кВ" in zones[0] and "40:13-6.1" in zones[0]
    assert torgi_zones.summary(make_payload("x", zouit=[]))[0] == []
    assert torgi_zones.summary(make_payload("x", borders=False, zouit_checked=False))[1] == (
        "noborders"
    )

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    card = _done(98, "FAILED")
    with session() as db:
        torgi_sync.upsert(db, torgi_scan.lot_row(_full(card, fin=None, applied=0)), NOW, card=True)
        db.commit()

    class Cache:
        def __init__(self):
            self.data = {"40:13:120101:101": make_payload("40:13:120101:101", zouit=[])}
            self.put_calls: list[str] = []

        def get(self, cad, allow_stale=False):
            return self.data.get(cad)

        def put(self, cad, payload):
            self.put_calls.append(cad)

    class Provider:
        calls: list[str] = []

        def fetch_raw(self, cad):
            self.calls.append(cad)
            if cad.endswith(":102"):
                raise TimeoutError("НСПД молчит")
            return make_payload(cad, zouit=[lep])

        def close(self):
            pass

    cache, provider = Cache(), Provider()
    with session() as db:
        failed_cad = db.get(TorgiLot, "F_98").cad_num
    ids = ["ACT_0", "ACT_1", "ACT_2", "F_98"]
    done = torgi_zones.fill(ids, provider=provider, cache=cache, sleep=lambda _s: None, now=NOW)
    assert done == 4 and "40:13:120101:101" not in provider.calls  # из кэша
    assert cache.put_calls == ["40:13:120101:100", failed_cad]  # ответ с зонами — в кэш
    with session() as db:
        lots = {i: db.get(TorgiLot, i) for i in ids}
        assert lots["ACT_0"].zones_state == "ok" and lots["ACT_0"].zones
        assert lots["ACT_1"].zones == [] and lots["ACT_1"].zones_state == "ok"
        assert lots["ACT_2"].zones is None and lots["ACT_2"].zones_state == "error"
    assert torgi_zones.missing(ids, now=NOW + timedelta(hours=1)) == []
    assert torgi_zones.missing(ids, now=NOW + timedelta(days=2)) == ["ACT_2"]

    scan = client.get("/tools/torgi-scan?region=40&roi=40").text
    assert "Обременения (ЗОУИТ): нет" in scan  # ACT_1
    assert "Обременения (ЗОУИТ): НСПД не ответил, повторим завтра" in scan  # ACT_2
    assert "Обременения (ЗОУИТ): Охранная зона ВЛ 10 кВ" in scan  # ACT_0
    hist = client.get("/tools/torgi-history?region=40&outcome=none").text
    assert "обременения: Охранная зона ВЛ 10 кВ" in hist


def test_canceled_lots_backfill_once_and_history_tab(client):  # noqa: F811
    """Чат 17.11 №76 «была отмена от администрации»: отменённые организатором торги
    собираются поиском (без карточек), у регионов, собранных раньше, — один раз за год
    вглубь; в «Завершённых» — своя вкладка, в «все завершённые» и индекс они не входят."""
    from landcheck.web.db import TorgiLot, TorgiSync, session

    fake = _seed(client)
    fake.lists["CANCELED"] = [_done(i + 70, "CANCELED") for i in range(12)]
    with session() as db:  # регион собран до 17.11: история есть, отменённых ещё не брали
        db.add(TorgiSync(region="40", state="idle", backfill_done=True, stats={}))
        db.commit()
    _sync(fake, provider=FakeNspd())
    pages = [int(s["page"]) for s in fake.seen if s.get("lotStatus") == "CANCELED"]
    assert pages == [0, 1]  # вглубь до конца, хотя история уже собрана
    with session() as db:
        st = db.get(TorgiSync, "40")
        assert st.stats["canceled_done"] is True
        canceled = db.scalars(select(TorgiLot).where(TorgiLot.status == "CANCELED")).all()
        assert len(canceled) == 12 and all(c.card_at is None for c in canceled)
    fake.seen.clear()
    _sync(fake, provider=FakeNspd())  # дальше — только сверху, пока есть новое
    assert [s["page"] for s in fake.seen if s.get("lotStatus") == "CANCELED"] == ["0"]
    _admin(client)
    page = client.get("/tools/torgi-history?region=40").text
    assert "отменены организатором 12" in page and "все завершённые 21" in page
    tab = client.get("/tools/torgi-history?region=40&outcome=canceled").text
    assert tab.count("отменён организатором") == 12


def test_zones_queue_keeps_requests_while_busy(client, monkeypatch):  # noqa: F811
    """Прод 17.11 (Клавдий d6a609f0): страница «не ушедших», открытая во время проверки
    списка лотов, теряла свои лоты. Теперь они встают в очередь за текущими."""
    import threading
    import time as _time

    from landcheck.web import torgi_zones
    from landcheck.web.config import settings

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    monkeypatch.setattr(settings, "fixtures", None)
    monkeypatch.setattr(torgi_zones, "MAX_PER_RUN", 2)
    gate, batches = threading.Event(), []

    def slow_fill(ids, **kw):
        batches.append(list(ids))
        gate.wait(5)
        return len(ids)

    monkeypatch.setattr(torgi_zones, "fill", slow_fill)
    assert torgi_zones.start_fill(["ACT_0", "ACT_1"]) == 2
    assert torgi_zones.start_fill(["ACT_1", "ACT_2", "ACT_3"]) == 2  # ACT_1 уже в очереди
    assert torgi_zones.queued() == 4
    gate.set()
    for _ in range(100):
        if torgi_zones.queued() == 0 and not torgi_zones._running:
            break
        _time.sleep(0.02)
    assert batches == [["ACT_0", "ACT_1"], ["ACT_2", "ACT_3"]] and torgi_zones.queued() == 0


def test_debtor_and_forest_lots_kept_out_of_market_stats(client):  # noqa: F811
    """Прод 17.11 (Клавдий 84ce037d): в сборе 395 торгов имущества должников (229-ФЗ, 86 % не
    состоялись), лесные (200-ФЗ), публичные торги изъятых участков. Это другой рынок: в
    ликвидность и медианы не идут; в «Завершённых» — отдельно, с пометкой вида торгов."""
    from landcheck import torgi_scan
    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiLot, session

    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    before = client.get("/tools/torgi-liquidity?region=40").text
    with session() as db:
        for i, code in ((80, "229FZ"), (81, "229FZ"), (82, "200FZ")):
            card = _done(i, "FAILED")
            torgi_sync.upsert(db, torgi_scan.lot_row(_full(card, fin=None, applied=0)), NOW)
            db.flush()
            db.get(TorgiLot, card["id"]).bidd_type = code
        db.commit()
    lots = torgi_sync.lots_of("40")
    assert torgi_scan.index_by(lots)["40:13"]["closed"] == 21  # 15 + 6, без чужих торгов
    after = client.get("/tools/torgi-liquidity?region=40").text
    assert after.count(">21</a>") == before.count(">21</a>") >= 1
    hist = client.get("/tools/torgi-history?region=40").text
    assert "все завершённые 21" in hist and "Ещё 3 завершённых торгов другого вида" in hist
    other = client.get("/tools/torgi-history?region=40&bt=other").text
    assert "все завершённые 3" in other and other.count("имущество должников</span>") == 2
    assert "лесной участок</span>" in other and 'name="bt" value="other"' in other


def test_history_limit_is_per_pass_and_resumes(client, monkeypatch):  # noqa: F811
    """Прод 18.11 (Клавдий 78f56b5b): Курганская область упёрлась в предел 800 страниц
    истории и никогда не стала бы «собранной». Предел — на проход; следующий проход
    продолжает с места остановки, место хранится между проходами."""
    from landcheck.web import torgi_sync
    from landcheck.web.db import TorgiSync, session

    fake = _seed(client)  # состоявшихся 15 — две страницы
    monkeypatch.setattr(torgi_sync, "MAX_HISTORY_PAGES", 1)
    _sync(fake, provider=FakeNspd())
    with session() as db:
        st = db.get(TorgiSync, "40")
        assert st.backfill_done is False
        assert st.progress["cursors"][torgi.STATUS_SUCCEED] == 1
    fake.seen.clear()
    _sync(fake, provider=FakeNspd())
    pages = [s["page"] for s in fake.seen if s.get("lotStatus") == torgi.STATUS_SUCCEED]
    assert pages == ["1"]  # с места остановки, не с начала
    with session() as db:
        st = db.get(TorgiSync, "40")
        assert st.backfill_done is True
        assert torgi.STATUS_SUCCEED not in (st.progress.get("cursors") or {})
