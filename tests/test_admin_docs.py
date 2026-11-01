"""Документы администрации по территории участка (ТЗ v3, п. 2.5): муниципалитет, автопоиск по
официальным доменам, чек-лист, блок в отчёте; участок внутри проекта межевания по НСПД."""

from __future__ import annotations

from landcheck import admin_docs
from landcheck.location import summarize_plans
from tests.helpers import CORNER, SQUARE
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета

PMT_URL = "https://kozelskij-r40.gosweb.gosuslugi.ru/deyatelnost/napravleniya/gradostroitelstvo/"
JUNK = [
    {
        "title": "Купить участок в Козельске — Авито",
        "url": "https://www.avito.ru/kozelsk/z",
        "snippet": "участки",
    },
    {"title": "Что-то на форуме", "url": "https://forum.example.com/t/1", "snippet": "обсуждение"},
    # приёмка d378c55 на проде: латвийские новости по слову «gorod», ГИСОГД чужого региона,
    # общая статья Градкодекса, администрация другого района
    {"title": "Gorod.lv: Новости Даугавпилса", "url": "https://gorod.lv/x", "snippet": "ПЗЗ"},
    {"title": "Портал ГИС ОГД", "url": "https://gisogd.mos.ru/", "snippet": "Москва"},
    {
        "title": "ГрК РФ Статья 30. Правила землепользования и застройки",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_51040/x/",
        "snippet": "Козельский район в статье не упоминается, но слово «землепользования» есть",
    },
    {
        "title": "Извещение о предоставлении земельного участка",
        "url": "https://nadezhdinskij-r25.gosweb.gosuslugi.ru/x",
        "snippet": "Надеждинский район Приморского края",
    },
]


def fake_search(query: str, *, max_results: int = 8, **_):
    """Выдача зависит от запроса, как у настоящего поиска; мусорные домены — для фильтра."""
    q = query.lower()
    if "гисогд" in q:
        hits = [
            {
                "title": "ГИСОГД Калужской области",
                "url": "https://gisogd.admoblkaluga.ru/",
                "snippet": "градостроительная информационная система",
            }
        ]
        return {"results": hits, "engine": "fake", "error": None}
    if "козельск" not in q:
        return {"results": list(JUNK), "engine": "fake", "error": None}
    if "постановление" in q:
        hits = [
            {
                "title": "Постановление № 207 об изменении проекта межевания",
                "url": "https://adm-kozelsk.ru/npa/207",
                "snippet": "внести изменения в проект межевания территории",
            }
        ]
    elif "правила землепользования" in q:
        hits = [
            {
                "title": "ПЗЗ города Козельска (решение № 73) — ГАРАНТ",
                "url": "https://base.garant.ru/46902194/",
                "snippet": "правила землепользования и застройки",
            }
        ]
    else:
        hits = [
            {
                "title": "Проекты планировки и межевания — Администрация Козельского района",
                "url": PMT_URL,
                "snippet": "проект межевания территории д. Дешовки, приложение 1",
            }
        ]
    return {"results": hits + JUNK, "engine": "fake", "error": None}


def test_municipality_and_official_filter():
    p = {"cad_num": "40:10:160500:1", "address": "Калужская область, Козельский район, д. Дешовки"}
    m = admin_docs.municipality_for(p, None)
    assert m["name"] == "Козельский район" and m["region"] == "Калужская область"
    assert m["registry"] and any("gosweb" in link["url"] for link in m["registry"]["links"])
    # без реестра — район из населённого пункта
    m2 = admin_docs.municipality_for(
        {"cad_num": "22:01:0000001:1", "address": "Алтайский край, Чемальский район, с. Элекмонар"},
        "Республика Алтай, Чемальский район, с. Элекмонар",
    )
    assert m2["name"] == "Чемальский район" and m2["registry"] is None
    # мусорный адрес НСПД муниципалитетом не считается
    m3 = admin_docs.municipality_for(
        {"cad_num": "50:09:0060202:101", "address": "Синтетический адрес, тестовые данные"},
        "тестовые данные",
    )
    assert m3["name"] is None
    assert admin_docs.is_official("https://kozelskij-r40.gosweb.gosuslugi.ru/x")
    assert admin_docs.is_official("https://adm-sochi.ru/x")
    assert admin_docs.is_official("https://base.garant.ru/1")
    assert not admin_docs.is_official("https://www.avito.ru/x")
    assert not admin_docs.is_official("https://forum.example.com/x")
    assert not admin_docs.is_official("https://gorod.lv/x")  # не .ru
    assert admin_docs.stems("Козельский район") == {"козельс"}
    assert admin_docs.stems("город-курорт Сочи") == {"сочи"}
    assert admin_docs.stems("Калужская область") == {"калужс"}
    assert admin_docs.is_about("ПЗЗ Козельского района", admin_docs.stems("Козельский район"))
    assert admin_docs.is_about(
        "https://adm-kozelsk.ru/npa/207", admin_docs.stems("Козельский район")
    )
    assert admin_docs.translit("козельс") == "kozels" and admin_docs.translit("сочи") == "sochi"
    assert not admin_docs.is_about("ГИСОГД Москвы", admin_docs.stems("Калужская область"))
    assert admin_docs.is_law_article("ГрК РФ Статья 30. Правила…")
    assert admin_docs.is_law_article("Статья 5.1. Общественные обсуждения")
    assert not admin_docs.is_law_article("Постановление № 207 об изменении проекта межевания")
    assert admin_docs.classify("Генеральный план поселения", "", "npa") == "genplan"
    assert admin_docs.classify("Без ключевых слов", "", "npa") == "npa"
    # слова вида запроса в приоритете
    assert admin_docs.classify("Постановление об изменении проекта межевания", "", "npa") == "npa"
    assert admin_docs.classify("Постановление об изменении проекта межевания", "", "pmt") == "pmt"


def test_discover_groups_official_docs_by_kind():
    found = admin_docs.discover("Козельский район", "Калужская область", search=fake_search)
    docs = found["docs"]
    assert found["engine"] == "fake" and found["errors"] == []
    assert "pmt" in docs and docs["pmt"][0]["url"] == PMT_URL
    assert "pzz" in docs and docs["pzz"][0]["host"] == "base.garant.ru"
    assert "npa" in docs and "207" in docs["npa"][0]["title"]
    assert "gisogd" in docs
    urls = [d["url"] for v in docs.values() for d in v]
    assert len(urls) == len(set(urls)) and not any("avito" in u or "forum" in u for u in urls)
    assert not any(
        "gorod.lv" in u or "gisogd.mos" in u or "r25" in u or "consultant" in u for u in urls
    )
    assert found["total"] == len(urls) == 4 and found["dropped"] >= 3
    # хост из реестра муниципалитета проходит и без упоминания названия в тексте
    plain = {
        "results": [
            {"title": "Документы", "url": "https://www.kozelskgp-adm.ru/docs", "snippet": ""}
        ],
        "engine": "fake",
        "error": None,
    }
    found2 = admin_docs.discover(
        "Козельский район", None, search=lambda q, **_: plain, known_hosts={"www.kozelskgp-adm.ru"}
    )
    assert found2["total"] == 1 and found2["dropped"] == 0
    found3 = admin_docs.discover("Козельский район", None, search=lambda q, **_: plain)
    assert found3["total"] == 1  # домен с транслитом названия (kozelsk…) — про этот муниципалитет
    other = {
        "results": [
            {"title": "Документы", "url": "https://www.adm-primorye.ru/docs", "snippet": ""}
        ],
        "engine": "fake",
        "error": None,
    }
    found4 = admin_docs.discover("Козельский район", None, search=lambda q, **_: other)
    assert found4["total"] == 0 and found4["dropped"] == 6
    s = admin_docs.summary(
        {"cad_num": "40:10:160500:1", "address": "Калужская область, Козельский район"},
        None,
        search=fake_search,
    )
    assert s["municipality"] == "Козельский район" and s["found"]["total"] == 4
    assert len(s["checklist"]) == 5 and "ППТ/ПМТ" in s["checklist"][0]["what"]

    def broken(query, **_):  # поиск упал — блок живой, ошибки перечислены
        raise TimeoutError("searx")

    bad = admin_docs.discover("Козельский район", None, search=broken)
    assert bad["total"] == 0 and len(bad["errors"]) == 6 and "TimeoutError" in bad["errors"][0]


def _pmt_feature(cad: str, geometry: dict, distance: float = 0.0) -> dict:
    return {
        "type": "Feature",
        "geometry": geometry,
        "distance_m": distance,
        "properties": {
            "options": {
                "cad_num": cad,
                "document_name": "Проект межевания территории д. Дешовки",
                "document_number": "412",
                "document_date": "2025-03-03",
                "document_issuer": "Администрация Козельского района",
                "land_use_text": "Для индивидуального жилищного строительства",
                "area": 1200,
            }
        },
    }


FAR = {
    "type": "Polygon",
    "coordinates": [
        [[37.003, 55.0], [37.004, 55.0], [37.004, 55.001], [37.003, 55.001], [37.003, 55.0]]
    ],
}


def test_summarize_plans_marks_parcel_inside_pmt():
    plans = summarize_plans(
        SQUARE, [_pmt_feature("40:10:160500:ЗУ1", SQUARE), _pmt_feature("x", FAR, 200.0)]
    )
    assert len(plans) == 1  # один документ, два образуемых участка
    assert plans[0]["covers_parcel"] and plans[0]["covering_parcels"] == ["40:10:160500:ЗУ1"]
    plans = summarize_plans(SQUARE, [_pmt_feature("x", FAR, 200.0)])
    assert not plans[0]["covers_parcel"] and "covering_parcels" not in plans[0]
    # четверть участка под образуемым — тоже «входит» (> 10 %)
    assert summarize_plans(SQUARE, [_pmt_feature("y", CORNER)])[0]["covers_parcel"]


def test_admin_docs_block_in_report(client, monkeypatch):  # noqa: F811
    from sqlalchemy import select

    from landcheck import websearch
    from landcheck.web.db import CheckItem, session

    monkeypatch.setattr(websearch, "search", fake_search)
    login(client)
    r = client.post("/checks", data={"numbers": "50:09:0060202:101", "purpose": "izhs"})
    base = r.headers["location"] + "/items/50:09:0060202:101"
    page = client.get(base).text
    assert "Документы администрации по территории участка" in page
    assert 'hx-get="' + base + '/admin-docs"' in page
    block = client.get(base + "/admin-docs").text
    assert "Документы администрации по территории участка" in block
    assert "Что именно смотреть в документах администрации" in block and "ППТ/ПМТ" in block
    # синтетический адрес фикстуры муниципалитет не даёт — блок честно говорит об этом
    assert "Муниципалитет по адресу НСПД не определён" in block
    # «Локация» не собрана — про проект межевания по НСПД ничего не утверждаем
    assert "появится после сбора раздела «Локация»" in block and "не входит" not in block
    page = client.get(base).text  # повторно — блок уже в данных строки, без HTMX-слота
    assert "admin-docs-slot" in page and 'hx-trigger="load delay:4s"' not in page
    assert client.get(base + "/admin-docs?refresh=1").status_code == 200
    with session() as db:  # публичный отчёт блока не показывает
        token = db.scalar(select(CheckItem.token))
    public = client.get(f"/r/{token}").text
    assert "Документы администрации по территории участка" not in public
