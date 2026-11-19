"""Факторы спроса у лота по карте (ТЗ v5, п. 7): разбор ответа OpenStreetMap и итог."""

from landcheck import nearby

LAT, LON = 56.70, 47.80  # к северу от Йошкар-Олы


def _node(dlat, dlon, **tags):
    return {"type": "node", "lat": LAT + dlat, "lon": LON + dlon, "tags": tags}


def _way(points, **tags):
    geometry = [{"lat": LAT + a, "lon": LON + b} for a, b in points]
    return {"type": "way", "geometry": geometry, "tags": tags}


# 0,001° широты — около 111 м; долготы на этой широте — около 61 м
ELEMENTS = [
    _node(0.003, 0.0, place="village", name="Руэм"),  # ~330 м
    _node(0.03, 0.0, place="hamlet", name="Дальняя"),  # ~3,3 км — не ближайшая
    _way([(-0.01, 0.004), (0.01, 0.004)], highway="tertiary", ref="88К-001"),  # ~245 м
    _way([(-0.01, 0.001), (0.01, 0.001)], highway="track"),  # грунтовка — не считается
    _way([(0.002, -0.01), (0.002, 0.01)], power="minor_line"),  # ~220 м
    _way([(0.0003, -0.01), (0.0003, 0.01)], power="line"),  # ~33 м: охранная зона
    _way([(0.004, 0.0), (0.004, 0.002)], landuse="cemetery", name="Сельское"),  # ~445 м
    _way([(0.02, 0.0), (0.02, 0.002)], landuse="landfill"),  # ~2,2 км — дальше порога
    _node(0.004, 0.001, amenity="school", name="Школа № 1"),
    {"type": "way", "tags": {"landuse": "industrial"}},  # без геометрии — пропускаем
]


def test_towns_come_from_the_bundled_list_without_network():
    """Замер 19.11.2026: поиск городов в радиусе 60 км не успевал ни на одном общественном
    сервере карты. Города лежат в проекте — расстояние до них считается сразу."""
    towns = nearby.towns()
    assert len(towns) > 2500 and towns[0][0] == "Москва" and towns[0][3] > 10_000_000
    far = nearby.far_places(LAT, LON)
    assert far["town"]["name"] == "Медведево" and 7_000 < far["town"]["m"] < 8_000
    assert far["city"]["name"] == "Йошкар-Ола" and far["city"]["population"] > 250_000
    assert 9_000 < far["city"]["m"] < 9_600
    assert nearby.far_places(62.0, 95.0) == {}  # тайга: городов в 60 км нет
    q = nearby.query(LAT, LON)
    assert "around:5000,56.7,47.8" in q and "around:60000" not in q  # запрос — только вблизи


def test_summary_takes_nearest_object_of_each_kind():
    f = nearby.summarize(LAT, LON, ELEMENTS)
    assert f["settlement"] == {"m": 334, "name": "Руэм", "kind": "село"}
    assert f["town"]["name"] == "Медведево" and f["city"]["name"] == "Йошкар-Ола"
    assert f["road"]["kind"] == "дорога" and f["road"]["name"] == "88К-001"
    assert 230 < f["road"]["m"] < 260  # расстояние до линии, а не до её концов
    assert 200 < f["power"]["m"] < 240 and f["hv_line"]["m"] < 50
    assert [b["kind"] for b in f["bad"]] == ["cemetery", "landfill"]
    assert "gas" not in f and "industrial" not in [b["kind"] for b in f["bad"]]


def test_access_levels_are_simple_and_explained():
    f = nearby.summarize(LAT, LON, ELEMENTS)
    a = nearby.access(f)
    assert a["level"] == "good"
    assert a["why"] == ["село 350 м", "дорога 250 м", "Йошкар-Ола 9,3 км"]
    # участок в самом селе: дороги на карте нет, но за это не штрафуем — средняя
    in_village = nearby.summarize(LAT, LON, [e for e in ELEMENTS if not e["tags"].get("highway")])
    mid = nearby.access(in_village)
    assert mid["level"] == "mid" and "дороги на карте в 3 км нет" in mid["why"]
    # до ближайшего села три километра и дороги нет — до участка трудно добраться
    remote = nearby.summarize(LAT, LON, [ELEMENTS[1]])
    weak = nearby.access(remote)
    assert weak["level"] == "weak" and weak["why"] == ["на карте нет дороги в 3 км"]
    assert nearby.access({})["why"][0] == "нет населённого пункта в 5 км"
    # всё рядом, но города в часе езды нет — средняя
    taiga = [
        {"type": "node", "lat": 62.003, "lon": 95.0, "tags": {"place": "village"}},
        {
            "type": "way",
            "tags": {"highway": "secondary"},
            "geometry": [{"lat": 61.99, "lon": 95.004}, {"lat": 62.01, "lon": 95.004}],
        },
    ]
    far = nearby.access(nearby.summarize(62.0, 95.0, taiga))
    assert far["level"] == "mid" and far["why"][-1] == "города от 50 тыс. жителей в 60 км нет"


def test_nuisances_and_lines_for_investor():
    f = nearby.summarize(LAT, LON, ELEMENTS)
    # кладбище ближе 500 м и высоковольтная линия вплотную; свалка за 2 км — не в счёт
    assert nearby.nuisances(f) == [
        "кладбище 450 м",
        "высоковольтная линия над участком или вплотную",
    ]
    rows = dict(nearby.lines(f))
    assert rows["Ближайший населённый пункт"] == "село Руэм, 350 м"
    assert rows["Ближайший город"].startswith("Медведево, 7,")
    assert rows["Ближайший город"].endswith("(18 тыс. жителей)")
    assert (
        rows["Крупный город"].startswith("Йошкар-Ола, 9,") and "285 тыс." in rows["Крупный город"]
    )
    assert rows["Дорога"] == "дорога, 250 м"
    assert rows["Кладбище"] == "Сельское, 450 м" and rows["Свалка"] == "2,2 км"
    assert "Газопровод" not in rows  # на карте нет — молчим, а не пишем «газа нет»
    assert nearby.km(30) == "рядом" and nearby.km(None) == "—"


def test_background_query_tries_mirrors_one_by_one(monkeypatch):
    """Фоновый сбор не бьёт по всем зеркалам карты разом: идёт по очереди до первого ответа."""
    from landcheck import osm

    calls: list[str] = []

    def mirror(url: str, query: str, timeout: float) -> dict:
        calls.append(url)
        if len(calls) < 3:
            raise RuntimeError("HTTP 504")
        return {"elements": [{"type": "node"}], "url": url}

    monkeypatch.setattr(osm, "_query_mirror", mirror)
    res = osm.run_query("x", timeout=1, parallel=False)
    assert res["elements"] and calls == list(osm.OVERPASS_URLS[:3])
    calls.clear()
    monkeypatch.setattr(osm, "_query_mirror", lambda *a: (_ for _ in ()).throw(RuntimeError("no")))
    res = osm.run_query("x", timeout=1, parallel=False)
    assert res["elements"] == [] and "не ответили" in res["error"]
    # фоновый сбор ходит только на зеркала, которые отвечали с боевого сервера
    from landcheck.web import torgi_nearby as tn

    seen: list[str] = []
    monkeypatch.setattr(
        osm, "_query_mirror", lambda url, q, t: seen.append(url) or {"elements": []}
    )
    assert tn._fetch("x") == {"elements": []} and seen == [tn.MIRRORS[0]]
    assert set(tn.MIRRORS) <= set(osm.OVERPASS_URLS) and tn.MIRROR_TIMEOUT_S >= 90
