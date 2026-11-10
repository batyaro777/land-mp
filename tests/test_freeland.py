"""Свободная земля на территории (ТЗ v3, п. 8, этапы 3–4): отбор публичных участков без
видимых препятствий, отметки аукциона, проекта межевания и торгов, страницы поиска."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from sqlalchemy import select

from landcheck import freeland
from tests.helpers import SQUARE, make_payload
from tests.test_verification import _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

Q = "40:10:160500"
PUBLIC = "Собственность публично-правовых образований"
AGRI = "Земли сельскохозяйственного назначения"


def _plot(n: int, *, shift: float = 0.0, **kw) -> dict:
    """Признак слоя «Земельные участки из ЕГРН»; `shift` сдвигает квадрат по долготе."""
    f = copy.deepcopy(make_payload(f"{Q}:{n}", **kw)["landplot"])
    if shift and f["geometry"]["type"] == "Polygon":
        f["geometry"]["coordinates"] = [
            [[x + shift, y] for x, y in ring] for ring in f["geometry"]["coordinates"]
        ]
    return f


def _area() -> dict:
    """Квартал: 2 свободных публичных (поле и участок под дом), частный, архивный, общего
    пользования, лесфонд, электросеть; один на аукционе, один в проекте межевания."""
    landplots = [
        _plot(
            1,
            ownership=PUBLIC,
            category=AGRI,
            vri="Сенокошение",
            area=73_000.0,
            specified_area=73_000.0,
            cost=219_000.0,
        ),
        _plot(2, ownership="Муниципальная собственность", shift=0.01, cost=400_000.0),
        _plot(3),  # частный
        _plot(4, ownership=PUBLIC, status="Архивный"),
        _plot(5, ownership=PUBLIC, vri="Земельные участки (территории) общего пользования"),
        _plot(6, ownership=PUBLIC, category="Земли лесного фонда", vri="Заготовка древесины"),
        _plot(7, ownership=PUBLIC, vri="Для размещения объектов электросетевого хозяйства"),
        _plot(8, ownership="Государственная собственность", shift=0.02),
        _plot(9, ownership=PUBLIC, shift=0.03, borders=False),
        _plot(1, ownership=PUBLIC),  # дубль номера из соседней плитки карты
    ]
    pmt = [{"type": "Feature", "geometry": SQUARE, "properties": {"options": {}}}]
    for f in pmt:  # проект межевания накрывает участок 8
        f["geometry"] = copy.deepcopy(_plot(8, shift=0.02)["geometry"])
    return {
        "quarter": Q,
        "landplots": landplots,
        "auction": [_plot(2)],
        "pmt": pmt,
        "errors": [],
        "truncated": [],
    }


def test_analyze_keeps_public_parcels_without_obstacles():
    raw = _area()
    r = freeland.analyze(raw["landplots"], raw["auction"], raw["pmt"])
    c = r["counts"]
    assert (c["total"], c["private"], c["archived"], c["common"], c["unavailable"]) == (
        9,
        1,
        1,
        1,
        2,
    )
    assert c["public_free"] == 4
    assert dict(r["reasons"]) == {
        "категория не передаётся гражданам и бизнесу": 1,
        "занят инфраструктурой или учреждением": 1,
    }
    by = {i["cad_num"]: i for i in r["items"]}
    assert set(by) == {f"{Q}:1", f"{Q}:2", f"{Q}:8", f"{Q}:9"}
    field = by[f"{Q}:1"]
    assert field["how"] == "agri" and field["group"] == "agri" and field["area_m2"] == 73_000
    assert field["cad_cost"] == 219_000 and field["point"] is not None
    assert by[f"{Q}:2"]["on_auction"] is True and by[f"{Q}:2"]["how"] == "settlement"
    assert by[f"{Q}:8"]["in_pmt"] is True and by[f"{Q}:1"]["in_pmt"] is False
    # участок без границ: на карте стоит в точке, которую НСПД даёт по адресу
    assert by[f"{Q}:9"]["point"] == [55.0005, 37.0005] and by[f"{Q}:9"]["in_pmt"] is False
    # сельхозземля идёт первой, внутри — крупные сначала
    assert r["items"][0]["cad_num"] == f"{Q}:1"


def test_filter_items_and_torgi_note():
    raw = _area()
    items = freeland.analyze(raw["landplots"], raw["auction"], raw["pmt"])["items"]
    assert [i["cad_num"] for i in freeland.filter_items(items, how="agri")] == [f"{Q}:1"]
    assert len(freeland.filter_items(items, group="housing")) == 3
    assert len(freeland.filter_items(items, area_min_m2=5000)) == 1
    assert len(freeland.filter_items(items, area_max_m2=5000)) == 3
    # «только свободное»: без аукциона, проекта межевания и торгов
    free = freeland.filter_items(items, hide_busy=True)
    assert [i["cad_num"] for i in free] == [f"{Q}:1", f"{Q}:9"]
    lot = {"id": "L1", "status": "SUCCEED", "kind": "rent", "auction_date": "2026-10-23"}
    note = freeland.torgi_note(lot)
    assert note == {
        "text": "сдан в аренду с торгов",
        "busy": True,
        "date": "2026-10-23",
        "lot_id": "L1",
        "kind": "rent",
    }
    assert freeland.torgi_note({**lot, "kind": "sale"})["text"] == "продан с торгов"
    assert freeland.torgi_note({"status": "APPLICATIONS_SUBMISSION"})["text"] == "идут торги"
    # извещение о предоставлении (ст. 39.18): участок уже просит заявитель
    asked = freeland.torgi_note({"status": "APPLICATIONS_SUBMISSION", "form": "IPS"})
    assert asked["busy"] is True and "можно подать своё" in asked["text"]
    past = freeland.torgi_note({"status": "SUCCEED", "form": "IPS", "kind": "rent"})
    assert past["busy"] is True and "скорее всего, занят" in past["text"]
    assert freeland.torgi_note({"status": "CANCELED", "form": "IPS"})["busy"] is False
    single = freeland.torgi_note({"status": "FAILED", "applied": 1})
    assert single["busy"] is True and "единственным участником" in single["text"]
    empty = freeland.torgi_note({"status": "FAILED", "applied": 0})
    assert empty == {
        "text": "выставлялся на торги, заявок не было",
        "busy": False,
        "date": None,
        "lot_id": None,
        "kind": None,
    }


def test_xlsx_has_parcels_and_howto_sheet(tmp_path):
    raw = _area()
    items = freeland.analyze(raw["landplots"], raw["auction"], raw["pmt"])["items"]
    path = freeland.to_xlsx(items, tmp_path / "free.xlsx")
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb.active
    assert ws.max_row == 5 and ws["A1"].value == "Кадастровый номер" and ws["A2"].value == f"{Q}:1"
    assert ws["D2"].value == 730.0 and ws["K2"].value.startswith("101-ФЗ")
    assert "Как оформить" in wb.sheetnames
    assert "Сельхозземля у муниципалитета" in wb["Как оформить"]["A1"].value


def test_parse_scope():
    from landcheck.web.freeland_views import parse_scope

    assert parse_scope("40:10:160500") == ("quarter", "40:10:160500")
    assert parse_scope(" 40:10:160500:1 ") == ("around", "40:10:160500:1")
    assert parse_scope("40.10.160500.1") == ("around", "40:10:160500:1")
    assert parse_scope("Козельск") is None and parse_scope("40:10") is None


class FakeArea:
    """НСПД в памяти: поиск по кварталу и вокруг точки, карточка участка."""

    name = "nspd"

    def __init__(self, raw: dict | None = None):
        self.raw = raw or _area()
        self.calls: list[dict] = []

    def fetch_area_raw(self, **kw) -> dict:
        self.calls.append(kw)
        return {**self.raw, "center": list(kw["center"]) if kw.get("center") else None}

    def fetch_raw(self, cad_num: str) -> dict:
        payload = make_payload(cad_num)
        payload["provider"] = "nspd"
        return payload


def _setup(client, monkeypatch, fake):  # noqa: F811
    """Админ вошёл; поиск выполняется сразу (без пула потоков) на подставном НСПД."""
    from landcheck.web import freeland_views as fv

    monkeypatch.setattr(fv, "_provider", lambda: fake)
    monkeypatch.setattr(fv, "start_scan", lambda scan_id: fv.run_scan(scan_id))
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")


def test_access_and_demo_mode(client):  # noqa: F811
    _register(client, "b@example.com", role="broker")
    assert client.get("/tools/free-land").status_code == 404
    client.post("/logout")
    client.cookies.clear()
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    page = client.get("/tools/free-land").text
    assert "Свободная земля на территории" in page and "Демо-режим" in page
    assert '<a href="/tools/free-land" class="active">Свободная земля</a>' in page
    bad = client.post("/tools/free-land", data={"scope": "где-то под Калугой"})
    assert bad.headers["location"] == "/tools/free-land?error=scope"
    assert "Не понял территорию" in client.get("/tools/free-land?error=scope").text


def test_demo_mode_scan_reports_error(client, monkeypatch):  # noqa: F811
    from landcheck.web import freeland_views as fv

    monkeypatch.setattr(fv, "start_scan", lambda scan_id: fv.run_scan(scan_id))
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    url = client.post("/tools/free-land", data={"scope": Q}).headers["location"]
    page = client.get(url).text
    assert "Не получилось" in page and "в демо-режиме поиск по территории недоступен" in page
    assert "Повторить" in page


def test_quarter_scan_result_filters_export_and_check(client, monkeypatch):  # noqa: F811
    fake = FakeArea()
    _setup(client, monkeypatch, fake)
    # участок 2 квартала уже сдан с торгов — это видно из базы лотов
    from landcheck.web.db import TorgiLot, session

    now = datetime.now(UTC)
    with session() as db:
        db.add(
            TorgiLot(
                id="LOT_8",
                region="40",
                district="40:10",
                quarter=Q,
                cad_num=f"{Q}:8",
                kind="rent",
                status="SUCCEED",
                published="2026-09-01",
                auction_date="2026-10-01",
                first_seen_at=now,
                seen_at=now,
            )
        )
        db.commit()
    r = client.post("/tools/free-land", data={"scope": Q})
    assert r.status_code == 303
    url = r.headers["location"]
    assert fake.calls == [{"quarter": Q}]
    page = client.get(url).text
    assert "участков 9, из них частных 1" in page and "без видимых препятствий: 4" in page
    assert "категория не передаётся гражданам и бизнесу — 1" in page
    for cad in (f"{Q}:1", f"{Q}:2", f"{Q}:8", f"{Q}:9"):
        assert cad in page
    assert f"{Q}:3</code>" not in page and f"{Q}:6</code>" not in page
    assert "на аукционе" in page and "проект межевания" in page
    assert "сдан в аренду с торгов" in page and "/tools/torgi-lot/LOT_8" in page
    assert "препятствий не видно" in page
    # карточки «как оформить» под найденные категории со ссылками на закон
    assert "Сельхозземля у муниципалитета: как оформить" in page and "101-ФЗ" in page
    assert "Участок в населённом пункте у муниципалитета" in page and 'id="torgi-map"' in page
    # фильтры
    agri = client.get(url + "?how=agri").text
    assert f"{Q}:1" in agri and f"{Q}:2</code>" not in agri
    free = client.get(url + "?free=1").text
    assert f"{Q}:9" in free and f"{Q}:8</code>" not in free and f"{Q}:2</code>" not in free
    assert "ничего не подошло" in client.get(url + "?amin=100000").text
    # выгрузка с тем же фильтром
    x = client.get(url + "/export.xlsx?how=agri")
    assert x.status_code == 200 and x.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )
    assert 'filename="free-land-40-10-160500.xlsx"' in x.headers["content-disposition"]
    # полная проверка отмеченных: цель «у муниципалитета», чужие номера не берём
    assert client.post(url + "/check", data={}).status_code == 400
    r = client.post(url + "/check", data={"cad": [f"{Q}:1", "99:99:999999:9"]})
    assert r.status_code == 303 and r.headers["location"].startswith("/checks/")
    from landcheck.web.db import Check

    with session() as db:
        check = db.scalar(select(Check).order_by(Check.id.desc()))
        assert check.input_text == f"{Q}:1" and check.request["purpose"] == "municipal"
        assert check.request["use"] == "farming" and check.title.startswith("Свободная земля")
    # список прошлых поисков и удаление
    listing = client.get("/tools/free-land").text
    assert f"<code>{Q}</code>" in listing and "<strong>4</strong>" in listing
    sid = int(url.rsplit("/", 1)[1])
    assert client.post(f"/tools/free-land/{sid}/delete").status_code == 303
    assert client.get(url).status_code == 404


def test_scan_around_parcel_and_errors(client, monkeypatch):  # noqa: F811
    fake = FakeArea()
    _setup(client, monkeypatch, fake)
    url = client.post(
        "/tools/free-land", data={"scope": "40:10:160500:1", "radius": "2000"}
    ).headers["location"]
    assert fake.calls[0]["radius_m"] == 2000 and fake.calls[0]["center"] is not None
    page = client.get(url).text
    assert "± 2000 м" in page and "без видимых препятствий: 4" in page
    # квартал не найден — понятная ошибка и подсказка
    broken = FakeArea(
        {
            "landplots": [],
            "auction": [],
            "pmt": [],
            "errors": ["квартал не найден в НСПД"],
            "truncated": [],
        }
    )
    from landcheck.web import freeland_views as fv

    monkeypatch.setattr(fv, "_provider", lambda: broken)
    url = client.post("/tools/free-land", data={"scope": "40:10:999999"}).headers["location"]
    page = client.get(url).text
    assert "Не получилось: квартал не найден в НСПД" in page
    assert "задайте номер любого участка" in page
    # повтор после починки
    monkeypatch.setattr(fv, "_provider", lambda: fake)
    assert client.post(url + "/retry").status_code == 303
    assert "без видимых препятствий: 4" in client.get(url).text


def test_other_users_scan_is_hidden(client, monkeypatch):  # noqa: F811
    fake = FakeArea()
    _setup(client, monkeypatch, fake)
    url = client.post("/tools/free-land", data={"scope": Q}).headers["location"]
    client.post("/logout")
    client.cookies.clear()
    _register(client, "team@example.com", role="broker")
    from landcheck.web.db import User, session

    with session() as db:
        db.scalar(select(User).where(User.email == "team@example.com")).pro_tools = True
        db.commit()
    assert client.get("/tools/free-land").status_code == 200
    assert client.get(url).status_code == 404  # чужой поиск
    assert f"<code>{Q}</code>" not in client.get("/tools/free-land").text
