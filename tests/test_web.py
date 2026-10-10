"""Веб-кабинет на офлайн-фикстурах: вход, проверка, таблица, отчёт, экспорт, публичная ссылка."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from landcheck.web import jobs
from landcheck.web.config import settings


@pytest.fixture
def client(tmp_path: Path, fixtures_dir: Path, monkeypatch):
    settings.db_path = tmp_path / "web.sqlite"
    settings.cache_path = tmp_path / "cache.sqlite"
    settings.fixtures = fixtures_dir
    settings.base_url = "http://testserver"
    monkeypatch.setattr(jobs, "enqueue", jobs.run_check)  # синхронно, без пула
    from landcheck.web.app import create_app
    from landcheck.web.db import create_invites, session

    app = create_app()
    with session() as db:
        codes = create_invites(db, 1, "тест")
    c = TestClient(app, follow_redirects=False)
    c.codes = codes  # type: ignore[attr-defined]
    return c


def login(c: TestClient) -> None:
    r = c.post("/login", data={"code": c.codes[0], "name": "Антон", "brand_name": "Земля МО"})  # type: ignore[attr-defined]
    assert r.status_code == 303 and "lc_session" in r.cookies


def test_requires_login(client: TestClient):
    r = client.get("/")
    assert r.status_code == 307 and r.headers["location"] == "/login"
    assert client.get("/login").status_code == 200
    r = client.post("/login", data={"code": "LC-NOPE", "name": "x"})
    assert r.status_code == 200 and "Код не найден" in r.text


def test_check_flow(client: TestClient, sample_csv: Path):
    login(client)
    assert client.get("/").status_code == 200
    r = client.post(
        "/checks",
        data={
            "title": "Тест",
            "numbers": sample_csv.read_text(encoding="utf-8"),
            "purpose": "izhs",
            "budget": "3 млн",
        },
    )
    assert r.status_code == 303
    check_url = r.headers["location"]
    page = client.get(check_url)
    assert page.status_code == 200
    assert "50:09:0060202:101" in page.text and "стоп" in page.text
    assert "Скачать XLSX" in page.text
    # фильтр и сортировка
    assert "50:09:0060202:102" not in client.get(check_url + "?verdict=ок").text
    assert client.get(check_url + "?sort=price").status_code == 200
    # экспорт
    x = client.get(check_url + "/export.xlsx")
    assert x.status_code == 200 and x.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )
    assert client.get(check_url + "/export.csv").status_code == 200
    # отчёт по участку и публичная ссылка
    rep = client.get(check_url + "/items/50:09:0060202:104")
    assert rep.status_code == 200
    assert "Охранная зона ЛЭП" in rep.text and 'id="map"' in rep.text and "Чек-лист" in rep.text
    import re

    token = re.search(r"/r/([A-Za-z0-9_-]+)", rep.text).group(1)
    pub = client.get(f"/r/{token}")
    assert (
        pub.status_code == 200 and "Земля МО" in pub.text and "Ссылка для клиента" not in pub.text
    )
    # не найденный участок
    nf = client.get(check_url + "/items/50:09:0060202:999")
    assert nf.status_code == 200 and "не найден" in nf.text
    # статус после завершения просит перезагрузку
    st = client.get(check_url + "/status")
    assert st.headers.get("hx-refresh") == "true"


def test_file_upload_and_single(client: TestClient, tmp_path: Path):
    login(client)
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Кадастровый номер", "Цена", "Площадь, сот"])
    ws.append(["50:09:0060202:101", 2_500_000, 10])
    ws.append(["50:09:0060202:105", 2_000_000, 10])
    f = tmp_path / "in.xlsx"
    wb.save(f)
    with f.open("rb") as fh:
        r = client.post(
            "/checks",
            data={"purpose": "snt"},
            files={"file": ("in.xlsx", fh, "application/octet-stream")},
        )
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert "50:09:0060202:105" in page.text and "Архивный" in page.text
    r = client.post(
        "/single",
        data={"cad_num": "50:09:0060202:101", "purpose": "izhs", "price": "2500000", "area": "10"},
    )
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert "Разовая проверка" in page.text
    bad = client.post("/checks", data={"numbers": "ничего"})
    assert bad.status_code == 200 and "Не нашёл" in bad.text
    assert client.get("/healthz").json()["ok"] is True
    assert client.post("/logout").status_code == 303


def test_listing_composer(client: TestClient):
    login(client)
    assert client.get("/listing/new").status_code == 200
    r = client.post(
        "/listing",
        data={
            "cad_num": "50:09:0060202:102",
            "price": "900000",
            "notes": "Под ИЖС, 12 соток, без обременений, газ по границе",
            "phone": "+7 900 000-00-00",
        },
    )
    assert r.status_code == 303, r.text
    page = client.get(r.headers["location"])
    assert page.status_code == 200
    assert "сельхозназначения" in page.text  # предупреждение: ИЖС на сельхозземле
    assert "по ЕГРН 10.0 сот." in page.text  # площадь из заметок расходится
    xml = client.get(r.headers["location"] + "/avito.xml")
    assert xml.status_code == 200
    assert "<Category>Земельные участки</Category>" in xml.text
    assert "<ObjectType>Сельхозназначения (СНТ, ДНП)</ObjectType>" in xml.text
    assert "<LandArea>10</LandArea>" in xml.text and "<Price>900000</Price>" in xml.text
    r2 = client.post(
        r.headers["location"],
        data={"title": "Новый заголовок", "description": "", "price": "1,1 млн"},
    )
    assert r2.status_code == 303
    assert "Новый заголовок" in client.get(r.headers["location"]).text
    assert "<Price>1100000</Price>" in client.get(r.headers["location"] + "/avito.xml").text
    assert (
        client.post("/listing", data={"cad_num": "50:09:0060202:999"}).text.count("не найден") >= 1
    )
