"""Факторы спроса у лота по карте (ТЗ v5, п. 7): разбор ответа OpenStreetMap и итог."""

from landcheck import nearby

LAT, LON = 56.70, 47.80


def _node(dlat, dlon, **tags):
    return {"type": "node", "lat": LAT + dlat, "lon": LON + dlon, "tags": tags}


def _way(points, **tags):
    geometry = [{"lat": LAT + a, "lon": LON + b} for a, b in points]
    return {"type": "way", "geometry": geometry, "tags": tags}


# 0,001° широты — около 111 м; долготы на этой широте — около 61 м
ELEMENTS = [
    _node(0.003, 0.0, place="village", name="Руэм"),  # ~330 м
    _node(0.03, 0.0, place="town", name="Медведево", population="18 500"),  # ~3,3 км
    _node(0.09, 0.0, place="city", name="Йошкар-Ола", population="281248"),  # ~10 км
    _way([(-0.01, 0.004), (0.01, 0.004)], highway="tertiary", ref="88К-001"),  # ~245 м
    _way([(-0.01, 0.001), (0.01, 0.001)], highway="track"),  # грунтовка — не считается
    _way([(0.002, -0.01), (0.002, 0.01)], power="minor_line"),  # ~220 м
    _way([(0.0003, -0.01), (0.0003, 0.01)], power="line"),  # ~33 м: охранная зона
    _way([(0.004, 0.0), (0.004, 0.002)], landuse="cemetery", name="Сельское"),  # ~445 м
    _way([(0.02, 0.0), (0.02, 0.002)], landuse="landfill"),  # ~2,2 км — дальше порога
    _node(0.004, 0.001, amenity="school", name="Школа № 1"),
    {"type": "way", "tags": {"landuse": "industrial"}},  # без геометрии — пропускаем
]


def test_summary_takes_nearest_object_of_each_kind():
    f = nearby.summarize(LAT, LON, ELEMENTS)
    assert f["settlement"] == {"m": 334, "name": "Руэм", "kind": "село"}
    assert f["town"]["name"] == "Медведево" and f["town"]["population"] == 18500
    assert f["city"]["name"] == "Йошкар-Ола" and 9_900 < f["city"]["m"] < 10_100
    assert f["road"]["kind"] == "дорога" and f["road"]["name"] == "88К-001"
    assert 230 < f["road"]["m"] < 260  # расстояние до линии, а не до её концов
    assert 200 < f["power"]["m"] < 240 and f["hv_line"]["m"] < 50
    assert [b["kind"] for b in f["bad"]] == ["cemetery", "landfill"]
    assert "gas" not in f and "industrial" not in [b["kind"] for b in f["bad"]]


def test_access_levels_are_simple_and_explained():
    f = nearby.summarize(LAT, LON, ELEMENTS)
    a = nearby.access(f)
    assert a["level"] == "good" and a["why"] == [
        "село 350 м",
        "дорога с покрытием 250 м",
        "Йошкар-Ола 10 км",
    ]
    # нет дороги с покрытием или населённого пункта рядом — доступность слабая
    no_road = nearby.summarize(LAT, LON, [e for e in ELEMENTS if not e["tags"].get("highway")])
    weak = nearby.access(no_road)
    assert weak["level"] == "weak" and "нет дороги с твёрдым покрытием в 3 км" in weak["why"][0]
    assert nearby.access({})["why"][1] == "нет населённого пункта в 5 км"
    # всё есть, но город далеко — средняя
    far = nearby.summarize(LAT, LON, [e for e in ELEMENTS if e["tags"].get("place") != "city"])
    mid = nearby.access(far)
    assert mid["level"] == "mid" and mid["why"] == ["города от 50 тыс. жителей в 60 км нет"]


def test_nuisances_and_lines_for_investor():
    f = nearby.summarize(LAT, LON, ELEMENTS)
    # кладбище ближе 500 м и высоковольтная линия вплотную; свалка за 2 км — не в счёт
    assert nearby.nuisances(f) == [
        "кладбище 450 м",
        "высоковольтная линия над участком или вплотную",
    ]
    rows = dict(nearby.lines(f))
    assert rows["Ближайший населённый пункт"] == "село Руэм, 350 м"
    assert rows["Ближайший город"] == "Медведево, 3,3 км (18 тыс. жителей)"
    assert rows["Крупный город"] == "Йошкар-Ола, 10 км (281 тыс. жителей)"
    assert rows["Дорога с твёрдым покрытием"] == "дорога, 250 м"
    assert rows["Кладбище"] == "Сельское, 450 м" and rows["Свалка"] == "2,2 км"
    assert "Газопровод" not in rows  # на карте нет — молчим, а не пишем «газа нет»
    assert nearby.km(30) == "рядом" and nearby.km(None) == "—"
    assert "around:60000" in nearby.query(LAT, LON) and "56.7" in nearby.query(LAT, LON)


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
