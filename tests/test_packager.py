"""Автоупаковка объявления через Gemini: промпт с фактами, разбор JSON, запасной шаблон."""

from __future__ import annotations

import httpx

from landcheck.packager import build_prompt, compose_listing, parse_json_text
from landcheck.web.config import settings
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _ok(text: str) -> _Resp:
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def test_prompt_and_json_parse():
    p = build_prompt(
        facts=["Площадь по ЕГРН 10 сот.", "ВРИ: ИЖС"],
        zouit_notes=[],
        notes="газ по границе",
        object_type="Поселений (ИЖС)",
        price_rub=2_500_000,
        fetched="2026-10-19",
    )
    assert "- Площадь по ЕГРН 10 сот." in p and "2 500 000 ₽" in p and "газ по границе" in p
    assert parse_json_text('```json\n{"title": "a", "description": "b"}\n```') == {
        "title": "a",
        "description": "b",
    }
    assert parse_json_text("not json") is None and parse_json_text("[1]") is None


def test_compose_fallback_chain_and_errors(monkeypatch):
    seq = iter(
        [
            _Resp(503, {"error": {"status": "UNAVAILABLE"}}),
            _ok('{"title": "Участок 10 сот. ИЖС, Бужарово  ", "description": "Текст."}'),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: next(seq))
    r = compose_listing(
        facts=[],
        zouit_notes=[],
        notes="",
        object_type="",
        price_rub=None,
        fetched="",
        api_key="k",
        model="a",
        fallbacks=("b",),
    )
    assert r["error"] is None and r["model"] == "b"
    assert r["title"] == "Участок 10 сот. ИЖС, Бужарово" and r["description"] == "Текст."
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok("просто текст"))
    r = compose_listing(
        facts=[], zouit_notes=[], notes="", object_type="", price_rub=None, fetched="", api_key="k"
    )
    assert r["error"] == "модель вернула не JSON"
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _Resp(403, {"error": {"status": "PERMISSION_DENIED"}})
    )
    r = compose_listing(
        facts=[], zouit_notes=[], notes="", object_type="", price_rub=None, fetched="", api_key="k"
    )
    assert r["error"] == "Gemini PERMISSION_DENIED"


def test_listing_uses_ai_text_and_keeps_contradiction_check(client, monkeypatch):  # noqa: F811
    login(client)
    settings.gemini_api_key = "test-key"
    try:
        # ИИ вернул текст с обещанием «без обременений» для участка с ЗОУИТ — контроль ловит
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: _ok(
                '{"title": "Участок 10 сот. ИЖС у леса", '
                '"description": "Участок 10 сот. ИЖС, без обременений, лес рядом."}'
            ),
        )
        url = client.post(
            "/listing", data={"cad_num": "50:09:0060202:104", "notes": "лес рядом"}
        ).headers["location"]
        page = client.get(url).text
        assert "Текст собран ИИ" in page and "Участок 10 сот. ИЖС у леса" in page
        assert "без обременений" in page and "Выгрузка заблокирована" in page
        # пересборка по новым заметкам — новый текст без обещаний
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: _ok(
                '{"title": "Участок 10 сот. ИЖС, лес и газ", '
                '"description": "Участок 10 сот. ИЖС. Газ по границе, лес рядом. Охранная зона ЛЭП."}'
            ),
        )
        assert client.post(url + "/compose", data={"notes": "газ по границе"}).status_code == 303
        page = client.get(url).text
        assert "лес и газ" in page and "Выгрузка заблокирована" not in page
        assert "газ по границе" in page
        # ошибка модели — остаётся прежний текст и честная пометка
        monkeypatch.setattr(
            httpx, "post", lambda *a, **k: _Resp(500, {"error": {"status": "INTERNAL"}})
        )
        client.post(url + "/compose", data={"notes": "ещё"})
        page = client.get(url).text
        assert "лес и газ" in page and "ИИ-сборка не удалась" in page
    finally:
        settings.gemini_api_key = None
    # без ключа — шаблон, без пометки ИИ
    url2 = client.post("/listing", data={"cad_num": "50:09:0060202:101"}).headers["location"]
    page = client.get(url2).text
    assert "Текст собран ИИ" not in page and "По данным ЕГРН" in page


def test_service_phrases_are_scrubbed_from_ai_text():
    from landcheck.packager import build_prompt, scrub_service_phrases

    title, desc = scrub_service_phrases(
        "Участок в поселении, категория не установлена",
        "Участок расположен в поселении. Точный населённый пункт не указан в предоставленных "
        "данных.\n\nКатегория: категория не установлена. Границы установлены, участок отмежёван."
        "\n\nПродавец отмечает: ровный участок.",
    )
    assert title == "Участок в поселении"
    assert desc == (
        "Участок расположен в поселении.\n\nГраницы установлены, участок отмежёван."
        "\n\nПродавец отмечает: ровный участок."
    )
    assert scrub_service_phrases("Категория не установлена", "")[0] == "Земельный участок"
    p = build_prompt(
        facts=[], zouit_notes=[], notes="", object_type="", price_rub=None, fetched="2026-10-20"
    )
    assert "Рубрика площадки не определена" in p and "Служебные фразы" in p
