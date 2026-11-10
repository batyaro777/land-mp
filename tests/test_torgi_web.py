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
    card["biddEndTime"] = "2026-12-20T06:00:00Z"
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

    def __init__(self, active: list[dict], succeed: list[dict], failed: list[dict]):
        self.lists = {
            torgi.STATUS_ACTIVE: active,
            torgi.STATUS_SUCCEED: succeed,
            torgi.STATUS_FAILED: failed,
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
        assert act.status == "APPLICATIONS_SUBMISSION" and act.bidd_end == "2026-12-20"
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
    # при цели 100 % старт уже выше предела
    assert "нет 12" in client.get("/tools/torgi-scan?region=40&roi=100").text
    # фильтр по кварталу и вердикту
    assert "40:13:120101:100" not in client.get("/tools/torgi-scan?region=40&d=40:99").text
    lot = client.get("/tools/torgi-lot/ACT_0?region=40&roi=40").text
    assert "Почему «на грани»" in lot and "Похожие завершённые торги рядом" in lot
    assert "40:13:120102:1" in lot and "×1.5" in lot  # сопоставимые из района
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
    assert "нет 12" in client.get("/tools/torgi-scan?region=40&roi=100").text
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
        }

    monkeypatch.setattr(market_ads, "collect_ads", fake_collect)
    r = client.post("/tools/torgi-lot/ACT_0/ads")
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=ads")
    lot = client.get("/tools/torgi-lot/ACT_0?saved=ads&roi=40").text
    assert "Объявления найдены" in lot and "объявления, 40:13" in lot and M(450_000) in lot
    assert "5 объявл. с ценой и площадью, Малоярославецкий район" in lot
    monkeypatch.setattr(market_ads, "collect_ads", lambda *a, **k: {"n": 1, "median": 1.0})
    assert client.post("/tools/torgi-lot/ACT_1/ads").headers["location"].endswith("saved=noads")


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


def test_report_block_reads_district_summary_from_store(client):  # noqa: F811
    """Блок «Торги рядом за год» в отчёте берёт сводку района из собранной базы региона."""
    from landcheck.web import jobs, torgi_sync

    assert torgi_sync.auctions_from_store("40:13:120101:1") is None  # регион ещё не собран
    fake = _seed(client)
    _sync(fake, max_nspd=0)
    a = torgi_sync.auctions_from_store("40:13:120101:1", now=NOW)
    assert (a["succeed_total"], a["failed_total"], a["active_total"]) == (15, 6, 12)
    assert a["ea_total"] == 33 and a["liquidity"] == 0.71 and a["from_store"] is True
    assert a["sale"]["with_price"] == 15 and a["sale"]["median_rub_m2"] == 150.0
    assert a["sale"]["median_ratio"] == 1.5 and len(a["items"]) == 15
    assert "состоялись 15 из 21" in a["liquidity_text"] and a["error"] is None
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
    # не все 12 карточек перебраны впустую: стоп после пяти сбоев подряд
    assert len(fake.seen) == 2 + torgi_sync.MAX_CARD_FAILURES
    # через час после сбоя регион снова в очереди автосбора, а не через сутки
    from datetime import timedelta

    assert torgi_sync.due_regions(now=NOW) == []
    assert torgi_sync.due_regions(now=datetime.now(UTC) + timedelta(minutes=61)) == ["40"]
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
    fake.lists[torgi.STATUS_ACTIVE] += [_notice(f"IA_{i}") for i in range(5)]
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
    assert "Извещений о предоставлении (ст. 39.18): 35, из них идут 5" in page
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
