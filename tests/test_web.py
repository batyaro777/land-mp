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
    url = r.headers["location"]
    page = client.get(url)
    assert page.status_code == 200
    assert "сельхозназначения" in page.text  # предупреждение: ИЖС на сельхозземле
    assert "по ЕГРН 10.0 сот." in page.text  # площадь из заметок расходится
    assert "Выгрузка заблокирована" in page.text
    assert "Данные НСПД от" in page.text  # дата данных, а не дата подготовки
    blocked = client.get(url + "/avito.xml")
    assert blocked.status_code == 409 and "противоречащие кадастру" in blocked.text
    # брокер исправил текст — противоречий нет, выгрузка открыта
    r2 = client.post(
        url,
        data={
            "title": "Участок 10 сот. садоводство",
            "description": "Продаётся участок 10 сот. на сельхозземле, газ по границе.",
            "price": "1,1 млн",
        },
    )
    assert r2.status_code == 303
    page = client.get(url)
    assert "Выгрузка заблокирована" not in page.text and "Участок 10 сот. садоводство" in page.text
    xml = client.get(url + "/avito.xml")
    assert xml.status_code == 200
    assert "<Category>Земельные участки</Category>" in xml.text
    assert "<ObjectType>Сельхозназначения (СНТ, ДНП)</ObjectType>" in xml.text
    assert "<LandArea>10</LandArea>" in xml.text and "<Price>1100000</Price>" in xml.text
    assert (
        client.post("/listing", data={"cad_num": "50:09:0060202:999"}).text.count("не найден") >= 1
    )


def test_query_purpose_kept_and_form_overrides(client: TestClient):
    login(client)
    from landcheck.web.db import Check, session

    r = client.post(
        "/checks", data={"numbers": "50:09:0060202:101", "query": "ИЖС до 3 млн", "purpose": "any"}
    )
    assert r.status_code == 303
    client.post(
        "/checks", data={"numbers": "50:09:0060202:101", "query": "ИЖС до 3 млн", "purpose": "snt"}
    )
    with session() as db:
        checks = db.query(Check).order_by(Check.id).all()
    assert checks[-2].request["purpose"] == "izhs" and checks[-2].request["budget_rub"] == 3_000_000
    assert checks[-1].request["purpose"] == "snt"


def test_rerun_sets_refresh_and_history(client: TestClient):
    login(client)
    from landcheck.web.db import Check, session

    r = client.post(
        "/checks", data={"numbers": "50:09:0060202:101\n50:09:0060202:104", "purpose": "izhs"}
    )
    first = r.headers["location"]
    page1 = client.get(first)
    assert "новый" not in page1.text  # первая подборка: истории ещё нет
    r = client.post(
        "/checks", data={"numbers": "50:09:0060202:101\n50:09:0060202:105", "purpose": "izhs"}
    )
    second = r.headers["location"]
    page2 = client.get(second)
    assert "новых для вас: 1" in page2.text  # 105 новый, 101 уже был
    with session() as db:
        check = db.get(Check, int(second.rsplit("/", 1)[1]))
        items = {i.cad_num: i.data["history"] for i in check.items}
    assert items["50:09:0060202:105"]["is_new"] and not items["50:09:0060202:105"]["seen_before"]
    assert (
        items["50:09:0060202:101"]["seen_before"] and items["50:09:0060202:101"]["changed"] is False
    )
    # повтор запрашивает данные заново: флаг ставится и снимается после завершения
    captured = {}
    import landcheck.web.jobs as jobs_mod

    orig = jobs_mod.check_batch

    def spy(*a, **kw):
        captured["refresh"] = kw.get("refresh")
        return orig(*a, **kw)

    jobs_mod.check_batch = spy
    try:
        assert client.post(second + "/rerun").status_code == 303
    finally:
        jobs_mod.check_batch = orig
    assert captured["refresh"] is True
    with session() as db:
        check = db.get(Check, int(second.rsplit("/", 1)[1]))
        assert "refresh" not in check.request and check.status == "done"


def test_report_distinguishes_fetch_error_from_not_found(client: TestClient, fixtures_dir: Path):
    login(client)
    import json

    from tests.helpers import make_payload

    (fixtures_dir / "50_09_0060202_777.json").write_text(
        json.dumps(make_payload("50:09:0060202:777", found=False, errors=["timeout"])),
        encoding="utf-8",
    )
    r = client.post("/checks", data={"numbers": "50:09:0060202:777\n50:09:0060202:999"})
    url = r.headers["location"]
    err = client.get(url + "/items/50:09:0060202:777").text
    nf = client.get(url + "/items/50:09:0060202:999").text
    assert "не получены" in err and "не найден." not in err
    assert "не найден." in nf and "не получены" not in nf


def test_login_rate_limit_and_photo_signature(client: TestClient):
    from landcheck.web import auth

    auth._ATTEMPTS.clear()
    for _ in range(auth.ATTEMPT_LIMIT):
        assert client.post("/login", data={"code": "LC-BAD", "name": "x"}).status_code == 200
    assert client.post("/login", data={"code": "LC-BAD", "name": "x"}).status_code == 429
    auth._ATTEMPTS.clear()
    login(client)
    fake_jpg = ("fake.jpg", b"not an image at all", "image/jpeg")
    real_png = ("ok.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")
    r = client.post(
        "/listing",
        data={"cad_num": "50:09:0060202:101", "notes": "лес рядом"},
        files=[("photos", fake_jpg), ("photos", real_png)],
    )
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "Не приняты файлы" in page and "fake.jpg" in page and "Фото (1)" in page


def test_resume_pending_reenqueues(client: TestClient, monkeypatch):
    login(client)
    from landcheck.web import jobs
    from landcheck.web.db import Check, session

    with session() as db:
        stuck = Check(
            user_id=1, title="зависла", request={}, input_text="50:09:0060202:101", status="running"
        )
        db.add(stuck)
        db.commit()
        stuck_id = stuck.id
    seen: list[int] = []
    monkeypatch.setattr(jobs, "enqueue", lambda cid: seen.append(cid))
    assert jobs.resume_pending() == 1 and seen == [stuck_id]
