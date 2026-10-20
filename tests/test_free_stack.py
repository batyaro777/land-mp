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
                        "title": "Новость",
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
                    "title": "Инвестпроект",
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
            "results": [{"title": "X", "url": "https://x.ru", "snippet": "sn"}],
            "error": None,
        },
        fetch_fn=lambda urls: {"https://x.ru": "short"},
    )
    assert docs[0]["text"] == "sn" and len(queries) == len(build_queries("Белокуриха", Purpose.SNT))


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
