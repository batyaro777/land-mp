"""Бесплатный стек (решение Тони 19.10): цепочка LLM-провайдеров, поиск без ключей, «Регион»
по открытым источникам, автоупаковка через бесплатную модель."""

from __future__ import annotations

import httpx

from landcheck import llm, websearch
from landcheck.models import Purpose
from landcheck.region import build_queries, collect_documents, research_region_search


class _Resp:
    def __init__(self, status_code: int, payload=None, text: str = "", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _chat(text: str) -> _Resp:
    return _Resp(200, {"choices": [{"message": {"content": text}}]})


def test_providers_from_env_order_and_models(monkeypatch):
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "LANDCHECK_LLM"):
        monkeypatch.delenv(k, raising=False)
    assert llm.providers_from_env() == []
    monkeypatch.setenv("OPENROUTER_API_KEY", "o")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GROQ_MODELS", "openai/gpt-oss-120b, qwen/qwen3.8-27b")
    ps = llm.providers_from_env()
    assert [p.name for p in ps] == ["groq", "openrouter"]
    assert ps[0].models == ("openai/gpt-oss-120b", "qwen/qwen3.8-27b") and ps[0].json_mode
    assert ps[1].models[0].endswith(":free")
    monkeypatch.setenv("LANDCHECK_LLM", "openrouter")
    assert [p.name for p in llm.providers_from_env()] == ["openrouter"]
    # Gemini — только если явно включён в порядок и есть ключ
    monkeypatch.setenv("GEMINI_API_KEY", "gm")
    monkeypatch.setenv("LANDCHECK_LLM", "groq,gemini")
    ps = llm.providers_from_env()
    assert [p.name for p in ps] == ["groq", "gemini"] and ps[1].base_url.endswith("/openai")
    monkeypatch.delenv("LANDCHECK_LLM")
    assert [p.name for p in llm.providers_from_env()] == ["groq", "openrouter"]


def test_chat_chain_skips_429_and_bad_json(monkeypatch):
    ps = [
        llm.Provider("groq", "https://g", "k", ("m1", "m2")),
        llm.Provider("openrouter", "https://o", "k", ("m3", "m4")),
    ]
    seq = iter(
        [
            _Resp(429, {"error": "rate"}),  # groq/m1 — лимит
            _chat("не json"),  # groq/m2 — не JSON
            _Resp(400, {"error": "Reasoning is mandatory"}),  # openrouter/m3 строгий режим
            _Resp(
                200, {"choices": [{"message": {"content": "", "reasoning": "думал"}}]}
            ),  # m3 повтор
            _chat('Вот ответ:\n```json\n{"title": "ok"}\n```'),  # openrouter/m4
        ]
    )
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((json["model"], "response_format" in json, "reasoning" in json))
        return next(seq)

    monkeypatch.setattr(httpx, "post", fake_post)
    r = llm.chat([{"role": "user", "content": "x"}], providers=ps, json_object=True)
    assert r["json"] == {"title": "ok"} and r["provider"] == "openrouter" and r["model"] == "m4"
    # groq: json-формат без reasoning; openrouter m3: строгий → 400 → повтор без полей; m4 строгий
    assert calls == [
        ("m1", True, False),
        ("m2", True, False),
        ("m3", True, True),
        ("m3", False, False),
        ("m4", True, True),
    ]
    assert r["tried"] == ["groq/m1", "groq/m2", "openrouter/m3", "openrouter/m4"]
    r = llm.chat([{"role": "user", "content": "x"}], providers=[], json_object=True)
    assert "нет ни одного провайдера" in r["error"]


def test_websearch_searxng_and_ddgs_fallback(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "https://sx.local/")
    payload = {
        "results": [
            {"title": "A", "url": "https://a.ru/1", "content": "a text", "engine": "google"},
            {
                "title": "B",
                "url": "https://b.ru/2",
                "content": "b",
                "publishedDate": "2026-07-23T10:00",
            },
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, payload))
    r = websearch.search("Белокуриха инвестиции", max_results=5)
    assert r["engine"] == "searxng" and [x["url"] for x in r["results"]] == [
        "https://a.ru/1",
        "https://b.ru/2",
    ]
    assert r["results"][1]["date"] == "2026-07-23"
    # SearXNG недоступен — DDGS
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(403, {}))

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, q, region, max_results, timelimit=None):
            return [{"title": "T", "href": "https://t.ru", "body": "snippet"}]

        def news(self, q, region, max_results, timelimit=None):
            return [
                {"title": "N", "url": "https://n.ru", "body": "news", "date": "2026-10-01T00:00"}
            ]

    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    r = websearch.search("x")
    assert r["engine"] == "ddgs" and r["results"][0]["url"] == "https://t.ru"
    r = websearch.search("x", news=True)
    assert r["results"][0]["date"] == "2026-10-01"


def test_fetch_text_extracts_main_content(monkeypatch):
    html = (
        "<html><head><title>Новости</title></head><body><nav>меню</nav><article><h1>Газопровод</h1>"
        "<p>В 2026 году в Белокурихе построят газопровод длиной 10 км за 1,2 млрд рублей. "
        "Работы начнутся весной, завершение запланировано на 2027 год.</p>"
        "<p>Подрядчик определён по итогам конкурса, сообщили в администрации города.</p>"
        "</article></body></html>"
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, None, text=html))
    txt = websearch.fetch_text("https://news.ru/1")
    assert "газопровод" in txt.lower() and "меню" not in txt
    assert websearch.fetch_text("https://vk.com/x") == ""
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x"))
    )
    assert websearch.fetch_text("https://down.ru") == ""


def test_region_search_pipeline_with_fakes():
    def search_fn(q, max_results, news, recent=False):
        if news:
            return {
                "results": [
                    {
                        "title": "Новость Белокурихи",
                        "url": "https://n.ru/1",
                        "snippet": "s",
                        "date": "2026-07-23",
                        "engine": "ddgs",
                    }
                ],
                "error": None,
            }
        return {
            "results": [
                {
                    "title": "Инвестпроект в Белокурихе",
                    "url": "https://a.ru/1",
                    "snippet": "кратко",
                    "engine": "ddgs",
                },
                {"title": "Дубль", "url": "https://a.ru/1", "snippet": "", "engine": "ddgs"},
            ],
            "error": None,
        }

    def fetch_fn(urls):
        return {
            u: ("Текст про мастер-план курорта на 17,6 млрд ₽ до 2030 года. " * 8) for u in urls
        }

    def chat_fn(messages, json_object, temperature, max_tokens):
        assert (
            "[1] Инвестпроект" in messages[1]["content"] and "[2] Новость" in messages[1]["content"]
        )
        return {
            "json": {
                "sections": [
                    {
                        "title": "Развитие и инвестиции",
                        "items": ["Мастер-план курорта 17,6 млрд ₽ до 2030 [1]"],
                    },
                    {"title": "Негативные факторы", "items": ["данных не найдено"]},
                    {"title": "Вывод для покупателя", "items": ["Спрос растёт [1, 2]"]},
                ],
                "used": [1],
            },
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "error": None,
        }

    r = research_region_search(
        "Алтайский край, г. Белокуриха",
        Purpose.IZHS,
        search_fn=search_fn,
        fetch_fn=fetch_fn,
        chat_fn=chat_fn,
    )
    assert r["error"] is None and r["grounded"] and r["model"] == "groq/openai/gpt-oss-120b"
    assert [s["title"] for s in r["sections"]] == [
        "Развитие и инвестиции",
        "Дороги и инфраструктура",
        "Негативные факторы",
        "Рынок земли",
        "Статус территории",
        "Вывод для покупателя",
    ]
    assert r["sections"][1]["items"] == ["данных не найдено"]
    assert [s["url"] for s in r["sources"]] == ["https://a.ru/1", "https://n.ru/1"]
    assert len(r["queries"]) == 6 and "Белокуриха" in r["queries"][0]

    # модель без ссылок — не показываем
    def chat_none(messages, **k):
        return {
            "json": {"sections": [{"title": "Рынок земли", "items": ["цены растут"]}], "used": []},
            "provider": "groq",
            "model": "m",
            "error": None,
        }

    r = research_region_search(
        "Белокуриха", Purpose.IZHS, search_fn=search_fn, fetch_fn=fetch_fn, chat_fn=chat_none
    )
    assert not r["grounded"] and r["sections"] == [] and "не сослалась" in r["error"]
    # поиск пустой — честная ошибка
    r = research_region_search(
        "Нигде",
        Purpose.IZHS,
        search_fn=lambda q, max_results, news, recent=False: {
            "results": [],
            "error": "ddgs: Ratelimit",
        },
        fetch_fn=lambda urls: {},
        chat_fn=chat_fn,
    )
    assert "ничего не найдено" in r["error"] and "Ratelimit" in r["error"]


def test_collect_documents_prefers_page_text_over_snippet():
    docs, queries, errors = collect_documents(
        "Белокуриха",
        Purpose.SNT,
        search_fn=lambda q, max_results, news, recent=False: {
            "results": [
                {
                    "title": "Новости Белокурихи",
                    "url": "https://x.ru/news/1",
                    "snippet": "В Белокурихе " + "новость " * 20,
                },
                {"title": "Белокуриха кратко", "url": "https://x.ru/news/2", "snippet": "sn"},
            ],
            "error": None,
        },
        fetch_fn=lambda urls: {"https://x.ru/news/1": "short", "https://x.ru/news/2": "short"},
    )
    assert [d["url"] for d in docs] == ["https://x.ru/news/1"]  # сниппет короче 150 знаков — мимо
    assert docs[0]["text"].startswith("В Белокурихе") and len(queries) == len(
        build_queries("Белокуриха", Purpose.SNT)
    )


def test_packager_uses_free_llm_first(monkeypatch):
    from landcheck.packager import compose_listing

    ps = [llm.Provider("groq", "https://g", "k", ("m",))]
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _chat('{"title": "Участок 10 сот. ИЖС", "description": "Текст."}'),
    )
    r = compose_listing(
        facts=["Площадь по ЕГРН 10 сот."],
        zouit_notes=[],
        notes="",
        object_type="",
        price_rub=None,
        fetched="",
        providers=ps,
        api_key="gem",
    )
    assert r["error"] is None and r["model"] == "groq/m" and r["title"] == "Участок 10 сот. ИЖС"
    r = compose_listing(
        facts=[],
        zouit_notes=[],
        notes="",
        object_type="",
        price_rub=None,
        fetched="",
        providers=[],
        api_key=None,
    )
    assert r["error"] == "нет провайдера LLM"


def test_junk_filter_and_locality_from_geometry(monkeypatch):
    from landcheck.region import is_junk, locality_from_geometry, resolve_locality
    from tests.helpers import SQUARE

    assert is_junk("https://www.google.ru/", "Google", "белокуриха")
    assert is_junk("https://belokuriha-gorod.ru/", "Город Белокуриха - главная", "белокуриха")
    assert is_junk("https://ru.wikipedia.org/wiki/%D0%93", "Г — Википедия", "белокуриха")
    assert not is_junk(
        "https://ru.wikipedia.org/wiki/Белокуриха", "Белокуриха — Википедия", "белокуриха"
    )
    assert not is_junk(
        "https://morein.pro/blog/belokuriha-2026", "Белокуриха 2026: что строится", "белокуриха"
    )
    payload = {
        "address": {
            "state": "Алтайский край",
            "county": "городской округ Белокуриха",
            "town": "Белокуриха",
        }
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, payload))
    assert locality_from_geometry(SQUARE) == "Алтайский край, Белокуриха"
    assert locality_from_geometry(None) is None
    # адрес НСПД «только регион» уточняется по контуру; полный адрес остаётся как есть
    assert resolve_locality("Алтайский край", SQUARE) == "Алтайский край, Белокуриха"
    assert (
        resolve_locality(
            "Республика Алтай, Чемальский район, с. Элекмонар, ул. Советская, 1", SQUARE
        )
        == "Республика Алтай, Чемальский район, с. Элекмонар"
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(503, None))
    assert resolve_locality("Алтайский край", SQUARE) == "Алтайский край"


def test_packager_prompt_forbids_area_on_data_error():
    from landcheck.packager import build_prompt

    p = build_prompt(
        facts=["Категория: земли населенных пунктов"],
        zouit_notes=[],
        notes="",
        object_type="",
        price_rub=None,
        fetched="",
        forbidden=["называть площадь участка"],
    )
    assert "Запрещено: обещать то, чего нет в фактах ЕГРН; называть площадь участка." in p


def test_short_place_strips_municipal_prefixes():
    from landcheck.region import build_queries, short_place

    assert short_place("Алтайский край, городской округ Белокуриха") == "Белокуриха"
    assert short_place("Республика Алтай, Чемальский район, с. Элекмонар") == "Элекмонар"
    assert short_place("Московская область") == "Московская область"
    assert build_queries("Алтайский край, г. Белокуриха", Purpose.IZHS)[0]["q"].startswith(
        "Белокуриха "
    )


def test_relevance_filter_drops_offtopic_documents():
    from landcheck.region import is_junk, is_relevant

    assert is_junk("https://domclick.ru/card/1", "Участок 10 сот", "элекмонар")
    assert is_junk("https://vk.com/wall-1_2", "пост", "элекмонар")
    assert not is_relevant(
        {"title": "Газпром: Калининградское ПХГ", "snippet": "", "text": "..."}, "Элекмонар"
    )
    assert is_relevant({"title": "Школа в Элекмонаре", "snippet": "", "text": ""}, "Элекмонар")
    assert is_relevant(
        {"title": "x", "snippet": "село Элекмонар получит газ", "text": ""}, "Элекмонар"
    )
    assert is_relevant(
        {"title": "x", "snippet": "", "text": "в Белокурихе построят… Белокуриха ждёт"},
        "Белокуриха",
    )


def test_collect_documents_interleaves_queries():
    from landcheck.region import collect_documents

    def search_fn(q, max_results, news, recent=False):
        tag = "инвест" if "инвестиции проект" in q else ("свалка" if "свалка" in q else "x")
        return {
            "results": [
                {
                    "title": f"Белокуриха {tag} {i}",
                    "url": f"https://{tag}.ru/{i}",
                    "snippet": "Белокуриха: " + "текст " * 30,
                }
                for i in range(6)
            ],
            "error": None,
        }

    docs, _, _ = collect_documents(
        "Белокуриха", Purpose.IZHS, search_fn=search_fn, fetch_fn=lambda urls: {}, max_docs=4
    )
    hosts = [d["url"].split("/")[2] for d in docs]
    # три темы (остальные запросы дают те же url и отсеиваются как дубли) → по кругу
    assert hosts == ["инвест.ru", "x.ru", "свалка.ru", "инвест.ru"]


def test_classifieds_and_passing_mentions_are_filtered():
    from landcheck.region import is_classified, is_relevant

    assert is_classified("https://ak.move.ru/objects/1", "Участок 10 соток")
    assert is_classified("https://dom.mirkvartir.ru/uchastki/", "Участки в Элекмонаре")
    assert is_classified("https://site.ru/x", "Купить участок в Белокурихе — 25 объявлений")
    assert is_classified("https://site.ru/x", "Продам участок ИЖС, цена от 300 000")
    assert not is_classified("https://tolknews.ru/news/1", "В Белокурихе построят дорогу")
    text_once = "Статистика по Республике Алтай. В том числе в Элекмонаре. " + "ещё текст " * 50
    assert not is_relevant(
        {"title": "Жильё в республике", "snippet": "", "text": text_once}, "Элекмонар"
    )
    assert is_relevant(
        {"title": "Жильё", "snippet": "", "text": text_once + " Элекмонар"}, "Элекмонар"
    )
    assert is_relevant({"title": "Школа в Элекмонаре", "snippet": "", "text": ""}, "Элекмонар")


def test_region_prompt_rules_for_market_and_status():
    from landcheck.region import _RAG_PROMPT

    assert "цены предложения" in _RAG_PROMPT and "по региону в целом" in _RAG_PROMPT
    assert "только про саму локацию" in _RAG_PROMPT


def test_packager_prompt_forbids_invented_features():
    from landcheck.packager import build_prompt

    p = build_prompt(
        facts=["Категория: земли населённых пунктов"],
        zouit_notes=[],
        notes="ровный участок",
        object_type="Земельный участок",
        price_rub=None,
        fetched="2026-10-20",
    )
    assert "Не добавляй характеристик" in p and "подъезд" in p


def test_postprocess_sections_marks_regional_drops_foreign_cad_and_filters_status():
    from landcheck.region import REGIONAL_MARK, postprocess_sections

    secs = [
        {
            "title": "Развитие и инвестиции",
            "items": [
                "В 2025 году введено 368 тыс. м² жилья в Республике Алтай [6]",
                "В Элекмонаре строится школа [2]",
            ],
        },
        {
            "title": "Негативные факторы",
            "items": [
                "Запрет на купание в Катуни [5]",
                "Участок ООПТ 2472 м² 04:09:020204:373 в частной собственности [5]",
            ],
        },
        {
            "title": "Статус территории",
            "items": [
                "В селе Элекмонар строится школа [4]",
                "Село входит в границы Чемальского района, ПЗЗ утверждены [3]",
            ],
        },
        {"title": "Рынок земли", "items": ["данных не найдено"]},
    ]
    out = postprocess_sections(secs, "Элекмонар", "Республика Алтай", "04:05:040101:12")
    assert out[0]["items"] == [
        f"В 2025 году введено 368 тыс. м² жилья в Республике Алтай {REGIONAL_MARK} [6]",
        "В Элекмонаре строится школа [2]",
    ]
    assert out[1]["items"] == ["Запрет на купание в Катуни [5]"]
    assert out[2]["items"] == ["Село входит в границы Чемальского района, ПЗЗ утверждены [3]"]
    assert out[3]["items"] == ["данных не найдено"]
    # без кадастрового номера чужие участки не отсеиваем, статус без ключевых слов → пусто
    out2 = postprocess_sections(secs[1:3], "Элекмонар", "Республика Алтай")
    assert len(out2[0]["items"]) == 2


def test_postprocess_drops_uncited_and_not_found_variants_and_marks_before_citation():
    from landcheck.region import REGIONAL_MARK, postprocess_sections

    secs = [
        {
            "title": "Негативные факторы",
            "items": ["Ржавая вода в сетях Чемала [4]", "Данных не найдено", "Шум без ссылки"],
        },
        {
            "title": "Рынок земли",
            "items": ["Цены по ИЖС в документах не указаны [данных не найдено]"],
        },
        {
            "title": "Развитие и инвестиции",
            "items": ["Туристический поток в Алтайском крае вырос на 12% [4]."],
        },
        {"title": "Вывод для покупателя", "items": ["Проверить ВРИ перед сделкой"]},
    ]
    out = postprocess_sections(secs, "Белокуриха", "Алтайский край")
    assert out[0]["items"] == ["Ржавая вода в сетях Чемала [4]"]
    assert out[1]["items"] == ["данных не найдено"]
    assert out[2]["items"] == [
        f"Туристический поток в Алтайском крае вырос на 12% {REGIONAL_MARK} [4]"
    ]
    assert out[3]["items"] == ["Проверить ВРИ перед сделкой"]


def test_sources_follow_final_citations_and_keep_numbers():
    from landcheck.region import research_region_search

    def search_fn(q, max_results, news, recent=False):
        return {
            "results": [
                {
                    "title": f"Белокуриха {i}",
                    "url": f"https://s{i}.ru/a",
                    "snippet": "Белокуриха: " + "текст " * 30,
                }
                for i in range(1, 4)
            ],
            "error": None,
        }

    def chat_fn(messages, **k):
        return {
            "json": {
                "sections": [
                    {"title": "Развитие и инвестиции", "items": ["Стройка [3]"]},
                    {
                        "title": "Негативные факторы",
                        "items": ["Участок ООПТ 04:09:020204:373 рядом [1]"],
                    },
                ],
                "used": [1, 2, 3],
            },
            "provider": "groq",
            "model": "m",
            "error": None,
        }

    r = research_region_search(
        "Алтайский край, г. Белокуриха",
        Purpose.IZHS,
        search_fn=search_fn,
        fetch_fn=lambda urls: {},
        chat_fn=chat_fn,
        cad_num="22:64:010303:10",
    )
    assert r["grounded"] and [s["n"] for s in r["sources"]] == [3]
    assert r["sections"][2]["items"] == ["данных не найдено"]


def test_generic_guides_are_junk():
    from landcheck.region import is_junk

    assert is_junk("https://shestakov-dom.com/blog/1", "Участок под глэмпинг: что нужно знать")
    assert is_junk("https://site.ru/a", "Как выбрать участок под дом — советы")
    assert not is_junk("https://tolknews.ru/news/1", "Обзор рынка земли Алтайского края 2026")
