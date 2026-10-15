"""Модуль «Регион»: разбор ответа Gemini с поиском, локация из адреса, блок в отчёте."""

from __future__ import annotations

from landcheck.region import locality_from_address, locality_key, parse_response
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета

SAMPLE = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {
                        "text": "## Развитие и инвестиции\n- В 2025 году утверждён проект курортного кластера [1].\n- данных о новых инвестициях нет\n## Дороги и инфраструктура\n* Реконструкция Чуйского тракта до 2027 года [2, 3]\n## Негативные факторы\n- данных не найдено\n## Рынок земли\n- ИЖС: 150–260 тыс. ₽ за сотку (объявления, 2026) [1]\n## Статус территории\n- Курорт федерального значения\n## Вывод для покупателя\n- Спрос растёт, но проверять ПЗЗ"
                    }
                ]
            },
            "groundingMetadata": {
                "webSearchQueries": ["Белокуриха инвестиции 2026"],
                "groundingChunks": [
                    {"web": {"uri": "https://example.org/a", "title": "Пример А"}},
                    {"web": {"uri": "https://example.org/b", "title": "Пример Б"}},
                    {"web": {"uri": "https://example.org/a", "title": "дубль"}},
                ],
                "groundingSupports": [],
            },
        }
    ],
    "usageMetadata": {"totalTokenCount": 1200},
}


def test_parse_response_sections_and_sources():
    r = parse_response(SAMPLE, model="gemini-test")
    assert r["grounded"] and r["error"] is None
    assert [s["title"] for s in r["sections"]] == [
        "Развитие и инвестиции",
        "Дороги и инфраструктура",
        "Негативные факторы",
        "Рынок земли",
        "Статус территории",
        "Вывод для покупателя",
    ]
    assert r["sections"][0]["items"][0] == "В 2025 году утверждён проект курортного кластера."
    assert r["sections"][1]["items"] == ["Реконструкция Чуйского тракта до 2027 года"]
    assert [s["url"] for s in r["sources"]] == ["https://example.org/a", "https://example.org/b"]
    assert r["queries"] == ["Белокуриха инвестиции 2026"]


def test_locality_from_address_strips_street_and_parcel():
    a = "Российская Федерация, Республика Алтай, Чемальский район, с. Элекмонар, ул. Советская, з/у 12"
    assert locality_from_address(a) == "Республика Алтай, Чемальский район, с. Элекмонар"
    assert (
        locality_from_address("Алтайский край, г. Белокуриха, ул. Бийская, 40/2")
        == "Алтайский край, г. Белокуриха"
    )
    assert locality_from_address("Алтайский край") == "Алтайский край"
    assert locality_from_address(None) is None
    assert locality_key("Республика  Алтай, с. Элекмонар") == locality_key(
        "республика алтай, с. элекмонар"
    )


def test_region_block_in_report(client, monkeypatch):  # noqa: F811 — фикстура
    from landcheck.web.config import settings

    login(client)
    r = client.post("/checks", data={"numbers": "50:09:0060202:101", "purpose": "izhs"})
    url = r.headers["location"]
    # без ключа: плейсхолдера нет, прямой запрос честно говорит, что модуль выключен
    monkeypatch.setattr(settings, "gemini_api_key", None)
    report = client.get(url + "/items/50:09:0060202:101").text
    assert "/region" not in report
    block = client.get(url + "/items/50:09:0060202:101/region").text
    assert "модуль выключен" in block
    # с ключом и подменённым исследованием: блок рендерится и сохраняется
    monkeypatch.setattr(settings, "gemini_api_key", "test")
    calls: list[tuple[str, str]] = []

    def fake(locality, purpose, *, api_key, model):
        calls.append((locality, purpose.value))
        return parse_response(SAMPLE, model=model)

    monkeypatch.setattr("landcheck.region.research_region", fake)
    block = client.get(url + "/items/50:09:0060202:101/region?refresh=1").text
    assert "Чуйского тракта" in block and "Источники (2)" in block
    assert calls == [("Синтетический адрес, тестовые данные", "izhs")]
    report2 = client.get(url + "/items/50:09:0060202:101").text
    assert (
        "Чуйского тракта" in report2
        and 'hx-get="' + url + '/items/50:09:0060202:101/region"' not in report2
    )
