"""Веб-кабинет на офлайн-фикстурах: вход, проверка, таблица, отчёт, экспорт, публичная ссылка."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

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
    # реальный размер фото: картинки мельче 15 КБ приём считает превью и откладывает (R12)
    real_png = ("ok.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20_000, "image/png")
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


def test_report_shows_action_plan_for_flags_only(client: TestClient):
    login(client)
    r = client.post("/checks", data={"numbers": "50:09:0060202:104\n50:09:0060202:101"})
    url = r.headers["location"]
    with_lep = client.get(url + "/items/50:09:0060202:104").text
    assert "План действий по рискам" in with_lep and "Что подтвердить" in with_lep
    assert "сетевая организация" in with_lep and "Документы к запросу" in with_lep
    clean = client.get(url + "/items/50:09:0060202:101").text
    assert "План действий по рискам" not in clean


def test_telegram_bot_runs_check_from_cad_number(client: TestClient, monkeypatch):
    from sqlalchemy import select

    from landcheck.web import jobs, notify
    from landcheck.web.db import Check, User, session

    login(client)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "send_telegram", lambda chat, text, **k: sent.append((chat, text)))
    monkeypatch.setattr(jobs.settings, "base_url", "https://land-mp.test")
    with session() as db:
        user = db.scalar(select(User))
        user.telegram_chat_id = "777"
        db.commit()
        # не подключённый чат с номером — подсказка, проверки нет
        upd = {"message": {"chat": {"id": 555}, "text": "50:09:0060202:104"}}
        assert notify.handle_update(db, upd, enqueue=jobs.run_check) == "unlinked-check"
        assert "подключите" in sent[-1][1].lower() and db.scalar(select(Check)) is None
        # подключённый чат: номер + цель → проверка, итог в тот же чат
        upd = {"message": {"chat": {"id": 777}, "text": "50:09:0060202:104 под ИЖС"}}
        res = notify.handle_update(db, upd, enqueue=jobs.run_check)
        assert res.startswith("check:")
        check = db.get(Check, int(res.split(":")[1]))
        assert check.request["tg_chat"] == "777" and check.request["purpose"] == "izhs"
        assert check.status == "done" and check.title == "Проверка из Telegram 50:09:0060202:104"
    assert (
        sent[-2][1].startswith("Проверяю участок 50:09:0060202:104") and "Цель: izhs" in sent[-2][1]
    )
    final = sent[-1]
    assert final[0] == "777" and final[1].startswith("50:09:0060202:104 — ПРОВЕРИТЬ")
    assert "• участок пересекает ЗОУИТ" in final[1]
    assert f"https://land-mp.test/checks/{check.id}/items/50:09:0060202:104" in final[1]
    # несколько номеров — сводка и ссылка на подборку
    with session() as db:
        upd = {"message": {"chat": {"id": 777}, "text": "50:09:0060202:101, 50:09:0060202:105"}}
        res = notify.handle_update(db, upd, enqueue=jobs.run_check)
    assert sent[-1][1].startswith("Проверено 2: стоп 1, проверить 0, ок 1")
    assert "/checks/" in sent[-1][1]
    # обычный текст без номера — помощь с подсказкой про номер
    with session() as db:
        assert (
            notify.handle_update(db, {"message": {"chat": {"id": 777}, "text": "привет"}}) == "help"
        )
    assert "кадастровый номер" in sent[-1][1]


def test_watch_toggle_run_and_notify(client: TestClient, fixtures_dir: Path):
    import json

    from sqlalchemy import select

    from landcheck.web.db import Notification, Watch, session
    from landcheck.web.watch import run_watches

    login(client)
    cad = "50:09:0060202:104"
    assert (
        client.post(f"/watch/{cad}", data={"back": "/watch"}).status_code == 404
    )  # ещё не проверен
    r = client.post("/checks", data={"numbers": cad})
    report = r.headers["location"] + f"/items/{cad}"
    assert "Следить за изменениями" in client.get(report).text
    r = client.post(f"/watch/{cad}", data={"back": report})
    assert r.status_code == 303 and r.headers["location"] == report
    assert "Слежу за изменениями" in client.get(report).text
    page = client.get("/watch").text
    assert cad in page and "нет" in page and "Перепроверить все сейчас" not in page  # не админ
    assert client.post("/watch/run").status_code == 404

    # без изменений — проверено, уведомлений нет
    res = run_watches(max_age_h=0)
    assert res == {"checked": 1, "changed": 0, "errors": []}
    with session() as db:
        w = db.scalar(select(Watch))
        assert w.checked_at is not None and w.last_diff == [] and w.changed_at is None
        assert db.scalar(select(Notification)) is None

    # участок изменился в НСПД: статус и ЗОУИТ
    path = fixtures_dir / "50_09_0060202_104.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    props = payload["landplot"]["properties"]["options"]
    props["status"] = "Архивный"
    payload["zouit"] = []
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    res = run_watches(max_age_h=0)
    assert res["checked"] == 1 and res["changed"] == 1
    with session() as db:
        w = db.scalar(select(Watch))
        fields = [c["field"] for c in w.last_diff]
        assert "статус" in fields and "ЗОУИТ" in fields and w.changed_at is not None
        n = db.scalar(select(Notification))
        assert n.kind == "watch" and "Архивный" in n.text and n.url == f"/watch#{cad}"
    page = client.get("/watch").text
    assert "Архивный" in page and "статус:" in page
    assert "Изменения по участку" in client.get("/notifications").text
    # свежепроверенный участок при суточном пороге не трогаем
    assert run_watches(max_age_h=24) == {"checked": 0, "changed": 0, "errors": []}
    # выключить слежение
    client.post(f"/watch/{cad}", data={"back": "/watch"})
    with session() as db:
        assert db.scalar(select(Watch)) is None


def test_telegram_summary_short_flags_and_webhook_secret_masked(caplog):
    import logging
    from types import SimpleNamespace

    from landcheck.web.jobs import telegram_summary
    from landcheck.web.logmask import install

    long_flag = "участок пересекает ЗОУИТ (6): " + "; ".join(
        f"зона номер {i} с длинным названием" for i in range(12)
    )
    item = SimpleNamespace(
        cad_num="1:2:3:4",
        verdict="проверить",
        data={
            "flags": [{"level": "проверить", "message": long_flag}, {"level": "ок", "message": "x"}]
        },
    )
    check = SimpleNamespace(id=7, status="done", error=None)
    text = telegram_summary(check, [item])
    lines = text.split("\n")
    assert lines[0] == "1:2:3:4 — ПРОВЕРИТЬ" and lines[1].startswith("• участок пересекает ЗОУИТ")
    assert (
        lines[1].endswith("…")
        and len(lines[1]) <= 165
        and lines[-1].endswith("/checks/7/items/1:2:3:4")
    )

    install()
    logger = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:1",
            "POST",
            "/tg/webhook/abcdef0123456789abcdef01",
            "1.1",
            200,
        )
    assert "abcdef0123456789abcdef01" not in caplog.text and "/tg/webhook/***" in caplog.text


def test_api_v1_key_checks_export_and_revoke(client: TestClient, monkeypatch):
    import re

    r = client.get("/api/v1")
    assert r.status_code == 200 and "POST /api/v1/checks" in r.json()["endpoints"]
    assert client.post("/api/v1/checks", json={"numbers": ["50:09:0060202:104"]}).status_code == 401
    login(client)
    page = client.get("/profile").text
    assert "Выпустить ключ API" in page and "lmp_" not in page
    assert client.post("/profile/api-key").status_code == 303
    page = client.get("/profile").text
    key = re.search(r"lmp_[A-Za-z0-9_-]+", page).group(0)
    assert "curl -X POST" in page and key in page
    h = {"Authorization": f"Bearer {key}"}

    r = client.post(
        "/api/v1/checks",
        json={
            "numbers": ["50:09:0060202:104", "50:09:0060202:101"],
            "purpose": "izhs",
            "title": "Для юриста",
        },
        headers=h,
    )
    assert r.status_code == 202 and r.json()["status"] == "queued"
    cid = r.json()["id"]
    j = client.get(f"/api/v1/checks/{cid}", headers={"X-Api-Key": key}).json()
    assert j["status"] == "done" and j["title"] == "Для юриста" and len(j["items"]) == 2
    it = next(i for i in j["items"] if i["cad_num"] == "50:09:0060202:104")
    assert it["verdict"] == "проверить" and it["flags"][0]["rule_id"] == "R10"
    assert it["parcel"]["has_borders"] is True and it["report_url"].endswith(
        "/items/50:09:0060202:104"
    )
    assert "R10" not in str(
        next(i for i in j["items"] if i["cad_num"] == "50:09:0060202:101")["flags"]
    )
    lst = client.get("/api/v1/checks", headers=h).json()["checks"]
    assert lst[0]["id"] == cid and "items" not in lst[0]
    csv = client.get(f"/api/v1/checks/{cid}/export.csv", headers=h)
    assert (
        csv.status_code == 200
        and "50:09:0060202:104" in csv.text
        and csv.headers["content-type"].startswith("text/csv")
    )
    assert client.get(f"/api/v1/checks/{cid}/export.xlsx", headers=h).status_code == 200
    assert client.get(f"/api/v1/checks/{cid}/export.pdf", headers=h).status_code == 404
    pj = client.get("/api/v1/parcels/50:09:0060202:104", headers=h).json()
    assert pj["check_id"] == cid and pj["verdict"] == "проверить"
    assert client.get("/api/v1/parcels/50:09:0060202:999", headers=h).status_code == 404
    assert client.get(f"/api/v1/checks/{cid + 100}", headers=h).status_code == 404
    # ошибки входа
    assert client.post("/api/v1/checks", json={"text": "ничего"}, headers=h).status_code == 400
    assert (
        client.post(
            "/api/v1/checks", json={"numbers": ["50:09:0060202:104"], "purpose": "villa"}, headers=h
        ).status_code
        == 400
    )
    monkeypatch.setattr(settings, "max_items", 1)
    assert (
        client.post(
            "/api/v1/checks",
            json={"numbers": ["50:09:0060202:104", "50:09:0060202:101"]},
            headers=h,
        ).status_code
        == 400
    )
    # отзыв ключа
    assert client.post("/profile/api-key/off").status_code == 303
    assert client.get("/api/v1/checks", headers=h).status_code == 401
    assert (
        client.get("/api/v1/checks", headers={"Authorization": "Bearer lmp_wrong"}).status_code
        == 401
    )


# --- зум 28.10, P0: форма, фильтры шаблонов, чек-лист, локация ----------------------------
def test_cad_number_in_title_is_used_and_empty_input_gives_hint(client):
    login(client)
    # Андрей вставил номер в «Название подборки» (п. 1.9) — подборка создаётся по нему
    r = client.post(
        "/checks", data={"title": "50:09:0060202:101", "numbers": ""}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/checks/")
    r = client.post("/checks", data={"title": "Истра", "numbers": "нет номеров"})
    assert r.status_code == 200 and "поле «Кадастровые номера»" in r.text
    assert 'href="/"' in r.text


def test_template_filters_cite_lcfirst_short_category():
    from landcheck.web.app import _cite, _short_category, templates

    html = str(
        _cite(
            "Завод [2] и мост [1, 3]",
            [{"n": 2, "url": "https://a.ru/x"}, {"n": 1, "url": "https://b.ru"}],
        )
    )
    assert (
        'href="https://a.ru/x"' in html
        and 'title="источник 2"' in html
        and "[1, 3]"
        in html.replace(
            '<a href="https://b.ru" target="_blank" rel="noopener" title="источник 1">1</a>', "1"
        )
    )
    assert str(_cite("<b>x</b> [9]", [])) == "&lt;b&gt;x&lt;/b&gt; [9]"
    assert (
        templates.env.filters["lcfirst"]("Выращивать культуры (КФХ)") == "выращивать культуры (КФХ)"
    )
    long = "Земли промышленности, энергетики, транспорта, связи, радиовещания, телевидения"
    assert _short_category(long) == "земли промышленности и иного спецназначения"
    assert _short_category("Что-то своё") == "Что-то своё"


def test_checklist_depends_on_ownership_and_purpose():
    from landcheck.models import Purpose
    from landcheck.web.content import checklist_for

    muni = {"parcel": {"ownership_type": "Муниципальная"}}
    ck = checklist_for(muni, Purpose.FARMING)
    text = " ".join(i for b in ck["blocks"] for i in b["items"])
    assert ck["title"].startswith("Чек-лист перед оформлением") and "39.18" in text
    assert "супруга" not in text and "СНТ" not in text and "наследств" not in text.lower()
    private = {"parcel": {"ownership_type": "Частная собственность"}}
    ck = checklist_for(private, Purpose.IZHS)
    text = " ".join(i for b in ck["blocks"] for i in b["items"])
    assert "супруга" in text and ck["title"] == "Чек-лист перед сделкой"
    ck = checklist_for(private, Purpose.INDUSTRY)
    text = " ".join(i for b in ck["blocks"] for i in b["items"])
    assert "СНТ" not in text and "грузового транспорта" in text


def test_short_address_filter():
    from landcheck.web.app import _short_address

    addr = "Местоположение установлено относительно ориентира. Почтовый адрес ориентира: Калужская обл., р-н Козельский, д. Вырск."
    assert _short_address(addr) == "Калужская обл., р-н Козельский, д. Вырск"
    assert (
        _short_address("Московская область, д. Бражниково") == "Московская область, д. Бражниково"
    )
    assert _short_address(None) == ""


def test_locality_strips_landmark_prefix():
    from landcheck.region import locality_from_address

    addr = (
        "Местоположение установлено относительно ориентира, расположенного за пределами участка. "
        "Ориентир здание мед.пункта. Почтовый адрес ориентира: Калужская обл., р-н Козельский, д. Вырск"
    )
    assert locality_from_address(addr) == "Калужская обл., р-н Козельский, д. Вырск"
    assert (
        locality_from_address(
            "Российская Федерация, Московская область, Волоколамский район, д. Бражниково, ул. Лесная, 5"
        )
        == "Московская область, Волоколамский район, д. Бражниково"
    )


def test_rights_form_recomputes_verdict_and_table_badge(client: TestClient):
    from landcheck.web.db import CheckItem, session
    from tests.test_rights import LK_ARREST, LK_TEXT

    login(client)
    r = client.post("/checks", data={"numbers": "50:09:0060202:101"})
    url = r.headers["location"]
    item = url + "/items/50:09:0060202:101"
    page = client.get(item).text
    assert 'id="rights"' in page and "lk.rosreestr.ru" in page
    assert "в НСПД не показываются" in page
    # мусор вместо страницы Росреестра → подсветка ошибки, данные не трогаем
    r = client.post(item + "/rights", data={"text": "ничего полезного"})
    assert r.status_code == 303 and r.headers["location"].endswith("?err=rights#rights")
    assert "не нашёл раздела о правах" in client.get(r.headers["location"]).text
    # собственность без обременений → ок, строка «Право» и дата сведений
    r = client.post(item + "/rights", data={"text": LK_TEXT})
    assert r.status_code == 303 and r.headers["location"].endswith("#rights")
    page = client.get(item).text
    assert "40-40-10/002/2013-603" in page and "не зарегистрированы (на дату сведений)" in page
    assert "сведения на 28.10.2026" in page
    # запрещение регистрации → стоп по участку, бейдж в таблице подборки
    r = client.post(item + "/rights", data={"text": LK_ARREST})
    assert r.status_code == 303
    page = client.get(item).text
    assert "Запрет регистрационных действий" in page
    assert "сделка и регистрация прав невозможны до снятия" in page
    with session() as db:
        it = db.scalar(select(CheckItem).where(CheckItem.cad_num == "50:09:0060202:101"))
        assert it.verdict == "стоп" and it.data["rights"]["status"] == "arrest"
        assert "Иванов" not in str(it.data)
    table = client.get(url).text
    assert ">арест<" in table and 'title="Право и обременения' in table
