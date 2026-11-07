"""Блок «Торги по этому участку» в «Документах и упоминаниях» (чат «МП Земля», сообщение 27)."""

from __future__ import annotations

from landcheck import torgi
from tests.test_torgi import _own_card
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета


def test_docs_block_shows_own_lot_from_torgi(client, monkeypatch):  # noqa: F811
    from landcheck import docs_search
    from landcheck.web import jobs
    from landcheck.web.serialize import fmt_money

    own = {
        "schema": torgi.OWN_LOT_SCHEMA,
        "cad_num": "50:09:0060202:101",
        "found": True,
        "lots": [torgi.own_lot_row(_own_card())],
        "history_note": None,
        "months": 36,
        "as_of": "2026-11-07T17:00:00+00:00",
        "search_url": torgi.SEARCH_URL,
        "error": None,
    }
    own["lot"] = own["lots"][0]
    calls: list[bool] = []

    def fake_lot(cad: str, refresh: bool = False):
        calls.append(refresh)
        return own

    monkeypatch.setattr(jobs, "fetch_parcel_lot", fake_lot)
    monkeypatch.setattr(
        docs_search,
        "search_documents",
        lambda cad: {"items": [], "groups": [], "queries": [], "fetched_at": None, "error": None},
    )
    login(client)
    r = client.post(
        "/checks", data={"numbers": "50:09:0060202:101; 2 500 000; 10 сот", "purpose": "izhs"}
    )
    base = r.headers["location"] + "/items/50:09:0060202:101"
    page = client.get(r.headers["location"] + "/items/50:09:0060202:101").text
    assert 'hx-get="' + base + '/docs"' in page  # блок грузится сам
    block = client.get(base + "/docs").text
    assert "Торги по этому участку" in block and "первоисточник: torgi.gov.ru" in block
    assert "Торги состоялись" in block and fmt_money(368_800.47) in block and "(×10.0)" in block
    assert "заявок 3, допущено 2" in block and "победитель — физлицо" in block
    assert "/new/file-store/v1/f-gpzu" in block and "К торгам приложен ГПЗУ" in block
    assert "Протокол о результатах аукциона от 23.10.2026" in block
    assert "(кроме torgi.gov.ru)" in block and "Иванов" not in block
    assert calls == [False]
    client.get(base + "/docs")  # повторно — из item.data, без нового запроса
    assert calls == [False]
    client.get(base + "/docs?refresh=1")
    assert calls == [False, True]
    page = client.get(base).text  # отчёт целиком: блок уже внутри страницы, без загрузчика
    assert "Торги по этому участку" in page


def test_torgi_lot_error_is_retried_after_an_hour():
    from datetime import UTC, datetime, timedelta

    from landcheck.web.app import _torgi_lot_stale

    now = datetime.now(UTC)
    assert _torgi_lot_stale(None) and not _torgi_lot_stale({"found": False, "error": None})
    fresh = {"error": "torgi.gov.ru недоступен", "as_of": now.isoformat()}
    old = {"error": "torgi.gov.ru недоступен", "as_of": (now - timedelta(hours=2)).isoformat()}
    assert not _torgi_lot_stale(fresh) and _torgi_lot_stale(old)
    assert _torgi_lot_stale({"error": "x", "as_of": "мусор"})


def test_gpzu_from_torgi_auto_then_retry(client, monkeypatch):  # noqa: F811
    """Чат «МП Земля», сообщение 30: ГПЗУ приложен к торгам — отчёт берёт и разбирает его сам."""
    from pathlib import Path

    from landcheck import docs_search, gpzu, ruproxy
    from landcheck.web import jobs
    from landcheck.web.db import CheckItem, session

    cad = "50:09:0060202:101"
    own_lot = torgi.own_lot_row(_own_card())
    own = {
        "schema": torgi.OWN_LOT_SCHEMA,
        "cad_num": cad,
        "found": True,
        "lots": [own_lot],
        "lot": own_lot,
        "history_note": None,
        "months": 36,
        "as_of": "2026-11-07T17:00:00+00:00",
        "search_url": torgi.SEARCH_URL,
        "error": None,
    }
    monkeypatch.setattr(jobs, "fetch_parcel_lot", lambda c, refresh=False: own)
    monkeypatch.setattr(
        docs_search,
        "search_documents",
        lambda c: {"items": [], "groups": [], "queries": [], "fetched_at": None, "error": None},
    )
    fixture = Path(__file__).parent / "fixtures" / "gpzu_scan_ocr_karelia.txt"
    text = fixture.read_text("utf-8").replace("10:20:0060108:281", cad)
    downloads: list[str] = []

    class FakeTorgi:
        def __init__(self, client=None):
            if client is not None:
                client.close()

        def file(self, file_id):
            downloads.append(file_id)
            return b"%PDF-1.7"

        def close(self):
            pass

    results = iter(
        [
            (gpzu.Gpzu(), {"ocr": False, "ocr_note": None}),  # первый раз — «не ГПЗУ»
            (gpzu.parse(text), {"ocr": True, "ocr_note": None}),
        ]
    )
    monkeypatch.setattr(ruproxy, "enabled", lambda: True)
    monkeypatch.setattr(torgi, "TorgiClient", FakeTorgi)
    monkeypatch.setattr(gpzu, "from_pdf", lambda raw: next(results))

    class Now:  # фоновая задача — синхронно
        def submit(self, fn, *args):
            fn(*args)

    monkeypatch.setattr(jobs, "executor", lambda: Now())
    from landcheck.web import app as webapp

    monkeypatch.setattr(webapp, "_gpzu_torgi_running", set())
    login(client)
    r = client.post("/checks", data={"numbers": f"{cad}; 2 500 000; 10 сот", "purpose": "izhs"})
    base = r.headers["location"] + f"/items/{cad}"
    page = client.get(base).text
    assert "Взять ГПЗУ из торгов" not in page  # торги ещё не запрошены

    block = client.get(base + "/docs").text  # торги найдены → ГПЗУ берётся сам
    assert 'id="gpzu" hx-swap-oob="true"' in block and downloads == ["f-gpzu"]
    page = client.get(base).text
    assert "Не удалось взять ГПЗУ из торгов" in page and "заголовок ГПЗУ" in page
    assert 'action="' + base + '/gpzu-torgi"' in page
    client.get(base + "/docs")  # повторно — уже пробовали, сам не перезапускает
    assert downloads == ["f-gpzu"]

    r = client.post(base + "/gpzu-torgi")  # кнопка «повторите»
    assert r.status_code == 303 and downloads == ["f-gpzu", "f-gpzu"]
    page = client.get(base).text
    assert "ГПЗУ приложен" in page and "Взят из документов торгов" in page
    assert "лот N_1" in page and "скан распознан OCR" in page
    assert (
        "<b>Ж</b> — Жилая зона" in page
        and "Для индивидуального жилищного строительства (2.1)" in page
    )
    assert "этажность: 3" in page and "процент застройки: 60%" in page
    block = client.get(base + "/gpzu-block").text
    assert 'id="gpzu"' in block
    assert "hx-trigger" not in block  # готово — опрос остановлен
    with session() as db:
        item = db.query(CheckItem).filter(CheckItem.cad_num == cad).one()
        assert item.data["gpzu"]["source"] == "torgi" and item.data["gpzu_torgi"]["state"] == "done"
        assert any(f["rule_id"] == "R20" for f in item.data["flags"])


def test_report_page_starts_gpzu_from_torgi_for_saved_docs(client, monkeypatch):  # noqa: F811
    """Проверка создана до 07.11: торги уже сохранены в docs, загрузчика блока нет —
    ГПЗУ из торгов запускается при открытии отчёта, страница опрашивает блок."""
    from landcheck.web import jobs
    from landcheck.web.db import CheckItem, session

    cad = "50:09:0060202:101"
    started: list[tuple] = []

    class Later:  # задача «в фоне» — не выполняем, только фиксируем запуск
        def submit(self, fn, *args):
            started.append(args)

    monkeypatch.setattr(jobs, "executor", lambda: Later())
    from landcheck.web import app as webapp

    monkeypatch.setattr(webapp, "_gpzu_torgi_running", set())  # «в фоне» не завершится
    login(client)
    r = client.post("/checks", data={"numbers": f"{cad}; 2 500 000; 10 сот", "purpose": "izhs"})
    base = r.headers["location"] + f"/items/{cad}"
    own_lot = torgi.own_lot_row(_own_card())
    with session() as db:
        item = db.query(CheckItem).filter(CheckItem.cad_num == cad).one()
        item.data = {
            **item.data,
            "docs": {"groups": [], "items": [], "error": None, "torgi": {"lots": [own_lot]}},
        }
        db.commit()
    page = client.get(base).text
    assert len(started) == 1 and started[0][2] == cad and started[0][3]["file_id"] == "f-gpzu"
    assert "Берём ГПЗУ из документов торгов" in page and "берём из торгов…" in page
    assert 'hx-get="' + base + '/gpzu-block" hx-trigger="every 8s"' in page
    client.get(base)  # идёт — второй раз не запускаем
    assert len(started) == 1
