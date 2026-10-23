"""Рельеф (DEM) и документы по кадастровому номеру: расчёты на фейковых данных, без сети."""

from __future__ import annotations

SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[84.99, 51.98], [84.992, 51.98], [84.992, 51.981], [84.99, 51.981], [84.99, 51.98]]
    ],
}


def test_sample_points_and_flat_terrain():
    from landcheck.terrain import sample_points, terrain_summary

    pts = sample_points(SQUARE)
    assert len(pts["parcel"]) == 9 and len(pts["ring"]) == 8 and pts["diag_m"] > 100
    t = terrain_summary(SQUARE, fetch=lambda points: [200.0] * len(points))
    assert t["level"] == "neutral" and t["slope_pct"] == 0.0 and "ровно" in t["notes"][0]


def test_steep_slope_and_lowland_near_water_are_risks():
    from landcheck.terrain import terrain_summary

    def steep(points):  # перепад 30 м по участку (~170 м диагональ) → ~18 %
        return [200 + (i % 3) * 15.0 for i in range(9)] + [205.0] * 8

    t = terrain_summary(SQUARE, fetch=steep)
    assert t["level"] == "risk" and "крутой склон" in t["notes"][0]

    def lowland(points):  # участок на 5 м ниже кольца
        return [100.0] * 9 + [105.0] * 8

    t = terrain_summary(SQUARE, fetch=lowland, water_distance_m=120)
    assert t["level"] == "risk" and any("риск подтопления" in n for n in t["notes"])
    t = terrain_summary(SQUARE, fetch=lowland)
    assert t["level"] == "neutral" and any("низина" in n for n in t["notes"])
    t = terrain_summary(SQUARE, fetch=lowland, water_zone=True)
    assert t["level"] == "risk" and any("водоохранной зоне" in n for n in t["notes"])
    hill = lambda points: [110.0] * 9 + [105.0] * 8  # noqa: E731
    assert terrain_summary(SQUARE, fetch=hill)["level"] == "plus"
    assert terrain_summary(SQUARE, fetch=lambda p: [None] * 17)["error"]
    assert terrain_summary(None) is None
    assert terrain_summary(SQUARE) is None  # LANDCHECK_ELEVATION_URL=off в тестах → без сети


def test_fetch_elevations_parses_both_apis(monkeypatch):
    import httpx

    from landcheck import terrain

    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "open-meteo" in str(request.url):
            return httpx.Response(200, json={"elevation": [10.0, 20.5]})
        return httpx.Response(200, json={"results": [{"elevation": 7.0}, {"elevation": None}]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    pts = [(51.98, 84.99), (52.0, 85.0)]
    assert terrain.fetch_elevations(pts, url=terrain.OPEN_METEO_URL) == [10.0, 20.5]
    assert terrain.fetch_elevations(pts, url="http://127.0.0.1:5000/v1/srtm90m") == [7.0, None]
    assert "latitude=" in calls[0] and "locations=" in calls[1]


def test_docs_classification_and_search():
    from landcheck.docs_search import classify, is_lookup, search_documents

    assert is_lookup("https://egrp365.org/reestr?cad=50:09:1", "Участок 50:09:1 — ЕГРП365")
    assert is_lookup("https://pkk.rosreestr.ru/#/search/50:09", "ПКК")
    assert is_lookup("https://e-ecolog.ru/e-cad/04:05:050201:306", "Участок")
    assert is_lookup("https://any-site.ru/uchastok/04:05:050201:306", "Участок", "04:05:050201:306")
    assert not is_lookup("https://torgi.gov.ru/lots/04:05:050201:306", "Лот", "04:05:050201:306")
    assert classify("https://sudact.ru/regular/doc/1", "Решение по делу 2-123/2026", "") == "court"
    assert classify("https://torgi.gov.ru/new/public/lots/1", "Лот", "") == "torgi"
    assert classify("https://www.avito.ru/x", "Продам участок 10 сот", "") == "ads"
    assert classify("https://admin.gosuslugi.ru/x", "Постановление", "") == "gov"
    assert classify("https://tolknews.ru/x", "В селе построят", "") == "news"

    cad = "22:64:010303:10"

    def search_fn(q, max_results, news, recent=False):
        return {
            "results": [
                {
                    "title": f"Решение по делу, участок {cad}",
                    "url": "https://sudact.ru/d/1",
                    "snippet": "",
                },
                {"title": "Карточка участка", "url": "https://egrp365.org/x", "snippet": cad},
                {
                    "title": "Похожий участок",
                    "url": "https://site.ru/1",
                    "snippet": "22:64:010303:11",
                },
                {"title": "Лот", "url": "https://torgi.gov.ru/l/1", "snippet": f"аукцион {cad}"},
                {"title": "Лот", "url": "https://torgi.gov.ru/l/1", "snippet": "дубль"},
            ],
            "error": None,
        }

    r = search_documents(cad, search_fn=search_fn)
    assert [d["kind"] for d in r["items"]] == ["court", "torgi"]
    assert [g["kind"] for g in r["groups"]] == ["court", "torgi"] and r["error"] is None
    assert len(r["queries"]) == 2 and r["queries"][0] == f'"{cad}"'
    empty = search_documents(cad, search_fn=lambda *a, **k: {"results": [], "error": "down"})
    assert empty["items"] == [] and empty["error"] == "down"


def test_deepseek_provider_first_when_key_present(monkeypatch):
    from landcheck import llm

    monkeypatch.delenv("LANDCHECK_LLM", raising=False)
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "y")
    ps = llm.providers_from_env()
    assert [p.name for p in ps] == ["deepseek", "openrouter"]
    assert ps[0].base_url == "https://api.deepseek.com/v1" and ps[0].models[0] == "deepseek-flash"
