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
