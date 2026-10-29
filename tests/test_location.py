"""Модуль «Локация»: окружение по НСПД/OSM и ориентир стоимости по соседям (запрос Антона М. 15.10)."""

from __future__ import annotations

import json
from pathlib import Path

from landcheck.location import (
    distance_m,
    empty_location_raw,
    osm_class,
    summarize_location,
    summarize_neighbors,
    summarize_plans,
    summarize_zouit,
)
from tests.helpers import SQUARE
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета

# Участок — квадрат SQUARE (~70×110 м) с центром (37.0005, 55.0005).
PARCEL = SQUARE


def _poly(lon0: float, lat0: float, size: float = 0.001) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon0, lat0],
                [lon0 + size, lat0],
                [lon0 + size, lat0 + size],
                [lon0, lat0 + size],
                [lon0, lat0],
            ]
        ],
    }


def _zouit(type_zone: str, name: str, geom: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {"category": 36940, "options": {"type_zone": type_zone, "name_by_doc": name}},
    }


def _pmt(doc_num: str, date: str, vri: str | None, area: str, geom: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "category": 37158,
            "options": {
                "nominal_number": ":ЗУ1",
                "area": area,
                "land_use_text": vri,
                "document_name": "Постановление «Об утверждении проекта межевания»",
                "document_number": doc_num,
                "document_date": date,
                "document_issuer": "Администрация города",
            },
        },
    }


def _neighbor(
    cn: str, category: str, vri: str, cost: float, area: float, own: str = "Частная"
) -> dict:
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "options": {
                "cad_num": cn,
                "land_record_category_type": category,
                "permitted_use_established_by_document": vri,
                "cost_index": cost,
                "specified_area": area,
                "ownership_type": own,
            }
        },
    }


SETTLEMENT = "Земли населенных пунктов"


def raw_sample() -> dict:
    raw = empty_location_raw("50:09:0060202:101")
    raw["nspd"]["zouit"] = [
        # СЗЗ в ~300 м восточнее
        _zouit(
            "Санитарно-защитная зона предприятий, сооружений и иных объектов",
            "СЗЗ АЗС №24",
            _poly(37.0058, 55.0),
        ),
        # курортный округ накрывает участок
        _zouit(
            "Округ санитарной (горно-санитарной) охраны лечебно-оздоровительных местностей, курортов и природных лечебных ресурсов",
            "Граница III зоны округа горно-санитарной охраны курорта Белокуриха",
            _poly(36.99, 54.99, 0.03),
        ),
        _zouit(
            "Охранная зона инженерных коммуникаций", "Охранная зона ВЛ-10 кВ", _poly(37.002, 55.002)
        ),
        _zouit(
            "Охранная зона инженерных коммуникаций",
            "Охранная зона газопровода",
            _poly(37.004, 55.004),
        ),
    ]
    raw["nspd"]["pmt"] = [
        _pmt(
            "542",
            "2026-08-05T00:00:00",
            "Среднеэтажная жилая застройка",
            "135358",
            _poly(37.003, 55.001),
        ),
        _pmt(
            "542",
            "2026-08-05T00:00:00",
            "Среднеэтажная жилая застройка",
            "2000",
            _poly(37.0035, 55.001),
        ),
        _pmt("542", "2026-08-05T00:00:00", "Торговые центры", "8884", _poly(37.004, 55.001)),
        _pmt("17", "2023-02-16T00:00:00", None, "500", _poly(37.008, 55.008)),
    ]
    raw["nspd"]["neighbors"] = [
        _neighbor("50:09:0060202:101", SETTLEMENT, "ИЖС", 270.0, 1000),  # сам участок — исключается
        *[
            _neighbor(
                f"50:09:0060202:{200 + i}", SETTLEMENT, "Для строительства жилого дома", 346.0, 1200
            )
            for i in range(8)
        ],
        _neighbor(
            "50:09:0060202:300", SETTLEMENT, "Для строительства объектов рекреации", 469.4, 1312
        ),
        _neighbor(
            "50:09:0060202:301",
            "Земли сельскохозяйственного назначения",
            "Сельхозпроизводство",
            3.5,
            55440,
        ),
        _neighbor("50:09:0060202:302", SETTLEMENT, "Опоры ЛЭП", 222.8, 50, "Муниципальная"),
    ]
    raw["osm"] = {
        "elements": [
            {
                "type": "node",
                "lat": 55.0105,
                "lon": 37.0005,
                "tags": {"landuse": "landfill", "name": "Полигон ТКО"},
            },
            # контейнерная площадка — не свалка (зум 28.10, п. 1.11)
            {"type": "node", "lat": 55.001, "lon": 37.001, "tags": {"amenity": "waste_disposal"}},
            # промзона 0,2 га в 150 м — котельная, не промзона: отсекается по площади
            {
                "type": "way",
                "tags": {"landuse": "industrial"},
                "geometry": [
                    {"lat": 55.0025, "lon": 37.0},
                    {"lat": 55.0025, "lon": 37.0006},
                    {"lat": 55.0029, "lon": 37.0006},
                    {"lat": 55.0029, "lon": 37.0},
                    {"lat": 55.0025, "lon": 37.0},
                ],
            },
            {
                "type": "way",
                "tags": {"waterway": "river", "name": "Катунь"},
                "geometry": [{"lat": 55.0, "lon": 36.998}, {"lat": 55.001, "lon": 36.998}],
            },
            {"type": "node", "lat": 55.003, "lon": 37.003, "tags": {"amenity": "school"}},
            {"type": "node", "lat": 55.004, "lon": 37.004, "tags": {"amenity": "bench"}},
        ]
    }
    return raw


def test_distance_zero_when_intersecting_and_meters_when_apart():
    assert distance_m(PARCEL, _poly(36.99, 54.99, 0.03)) == 0
    d = distance_m(PARCEL, _poly(37.0058, 55.0))
    assert 250 <= d <= 350


def test_zouit_groups_sorted_by_risk_with_distance():
    groups = summarize_zouit(PARCEL, raw_sample()["nspd"]["zouit"])
    # СЗЗ в ~300 м не задевает участок — факт окружения, не риск (зум 28.10, п. 1.3)
    szz = next(g for g in groups if g["label"] == "санитарно-защитная зона предприятия")
    assert szz["severity"] == "neutral" and szz["far"] and 250 <= szz["min_distance_m"] <= 350
    assert "ближайшая в" in szz["note"]
    assert all(g["severity"] != "risk" for g in groups)
    # округ санитарной охраны курорта накрывает участок — факт на весь курорт, не «плюс»
    resort = next(g for g in groups if "курорт" in g["label"])
    assert resort["min_distance_m"] == 0 and resort["severity"] == "neutral"
    assert "на конкретный участок не влияет" in resort["note"]
    nets = next(g for g in groups if g["label"] == "охранная зона инженерных сетей")
    assert nets["count"] == 2
    # зона рядом с участком — риск
    near = summarize_zouit(PARCEL, [raw_sample()["nspd"]["zouit"][0] | {"geometry": PARCEL}])
    assert near[0]["severity"] == "risk" and near[0]["min_distance_m"] == 0


def test_plans_grouped_by_document_with_string_area_and_iso_date():
    plans = summarize_plans(PARCEL, raw_sample()["nspd"]["pmt"])
    assert len(plans) == 2
    first = plans[0]
    assert "542" in first["document"] and "05.08.2026" in first["document"]
    assert first["parcels"] == 3 and first["area_m2"] == 135358 + 2000 + 8884
    assert first["uses"][0] == ("Среднеэтажная жилая застройка", 2)
    assert (
        first["min_distance_m"] is not None and first["min_distance_m"] < plans[1]["min_distance_m"]
    )


def test_neighbors_stats_exclude_target_and_position_below_median():
    n = summarize_neighbors(
        "50:09:0060202:101", SETTLEMENT, 270.0, raw_sample()["nspd"]["neighbors"]
    )
    assert n["total"] == 11
    same = n["same_category"]
    assert same["category"] == SETTLEMENT and same["count"] == 10 and same["median_rub_m2"] == 346.0
    assert n["position"].startswith("ниже медианы соседей на 22%")
    assert n["uses"][0]["vri"] == "Для строительства жилого дома" and n["uses"][0]["count"] == 8
    assert dict(n["ownership"])["Муниципальная"] == 1


def test_osm_classes_and_nearest_distance():
    assert osm_class({"landuse": "landfill"}) == "landfill"
    assert osm_class({"highway": "trunk"}) == "motorway"
    assert osm_class({"amenity": "bench"}) is None
    assert osm_class({"amenity": "waste_disposal"}) is None  # контейнерная площадка
    loc = summarize_location(
        raw_sample(),
        parcel_geojson=PARCEL,
        cad_num="50:09:0060202:101",
        category=SETTLEMENT,
        cost_index=270.0,
        listing_price_rub=2_500_000,
        area_m2=1000,
    )
    osm = {o["kind"]: o for o in loc["osm"]}
    # полигон в 1,1 км — факт окружения, а не риск; мелкая промзона отсечена по площади
    assert osm["landfill"]["severity"] == "neutral"
    assert 1000 <= osm["landfill"]["distance_m"] <= 1200 and osm["landfill"]["count"] == 1
    assert "industrial" not in osm
    assert osm["water"]["distance_m"] < 300 and osm["school"]["name"] is None
    assert loc["price"]["per_sotka"] == 34_600 and loc["price"]["per_ha"] == 3_460_000
    assert loc["price"]["listing_vs_gko"] == round(2500 / 346, 2)
    assert loc["errors"] == [] and loc["auction_parcels"] == 0


# --- веб: блок подгружается по HTMX и сохраняется в item.data --------------------------------
def test_location_block_in_report(client, fixtures_dir: Path):  # noqa: F811 — фикстура
    (fixtures_dir / "50_09_0060202_101.location.json").write_text(
        json.dumps(raw_sample(), ensure_ascii=False), encoding="utf-8"
    )
    login(client)
    r = client.post(
        "/checks", data={"numbers": "50:09:0060202:101; 2 500 000; 10 сот", "purpose": "izhs"}
    )
    url = r.headers["location"]
    report = client.get(url + "/items/50:09:0060202:101").text
    assert 'hx-get="' + url + '/items/50:09:0060202:101/location"' in report  # плейсхолдер
    block = client.get(url + "/items/50:09:0060202:101/location").text
    assert "Ориентир стоимости по соседям" in block
    assert "санитарно-защитная зона предприятия" in block and "ближайшая в" in block
    assert "Среднеэтажная жилая застройка".lower() in block.lower()
    assert "медианы соседей" in block  # участок фикстуры 1500 ₽/м² против 346 у соседей
    # второй заход — уже из item.data, без фикстуры тоже отрисуется
    (fixtures_dir / "50_09_0060202_101.location.json").unlink()
    report2 = client.get(url + "/items/50:09:0060202:101").text
    assert "Ориентир стоимости по соседям" in report2 and 'hx-trigger="load"' not in report2


def test_slim_raw_drops_geometry_of_neighbors_and_keeps_zone_geometry():
    from landcheck.location import slim_raw

    raw = raw_sample()
    raw["nspd"]["neighbors"][1]["geometry"] = _poly(37.001, 55.001)
    raw["nspd"]["zouit"][0]["properties"]["options"]["content_restrict_encumbrances"] = "x" * 5000
    slim = slim_raw(raw, PARCEL)
    assert "geometry" not in slim["nspd"]["neighbors"][1]
    z0 = slim["nspd"]["zouit"][0]
    assert "geometry" not in z0 and 250 <= z0["distance_m"] <= 350
    assert slim["nspd"]["pmt"][0]["distance_m"] is not None
    river = slim["osm"]["elements"][1]
    assert "geometry" not in river and river["distance_m"] < 300
    assert "content_restrict_encumbrances" not in slim["nspd"]["zouit"][0]["properties"]["options"]
    # лишние OSM-элементы (лавочки) выброшены, нужные остались
    # лавочка и контейнерная площадка выброшены; свалка, промзона (с площадью), река, школа остались
    assert all(el["tags"] for el in slim["osm"]["elements"]) and len(slim["osm"]["elements"]) == 4
    industrial = next(
        el for el in slim["osm"]["elements"] if el["tags"].get("landuse") == "industrial"
    )
    assert 1500 <= industrial["area_m2"] <= 2000 and "geometry" not in industrial
    loc = summarize_location(
        slim,
        parcel_geojson=PARCEL,
        cad_num="50:09:0060202:101",
        category=SETTLEMENT,
        cost_index=270.0,
    )
    labels = [g["label"] for g in loc["zouit"]]
    assert "санитарно-защитная зона предприятия" in labels and labels[0].startswith("округ")


def test_osm_risk_only_near_and_large():
    from landcheck.location import summarize_osm

    def square(lat, lon, size):
        return [
            {"lat": lat, "lon": lon},
            {"lat": lat, "lon": lon + size},
            {"lat": lat + size, "lon": lon + size},
            {"lat": lat + size, "lon": lon},
            {"lat": lat, "lon": lon},
        ]

    elements = [
        # полигон ТКО 4 га в ~250 м к северу — риск
        {"type": "way", "tags": {"landuse": "landfill"}, "geometry": square(55.0032, 37.0, 0.002)},
        # промзона 0,5 га вплотную — отсечена по площади (меньше 2 га)
        {
            "type": "way",
            "tags": {"landuse": "industrial"},
            "geometry": square(55.0, 37.0012, 0.0007),
        },
        # военный объект в 100 м — никогда не риск по OSM, только по НСПД
        {"type": "way", "tags": {"landuse": "military"}, "geometry": square(55.0, 36.998, 0.0005)},
    ]
    osm = {o["kind"]: o for o in summarize_osm(PARCEL, elements)}
    assert osm["landfill"]["severity"] == "risk" and osm["landfill"]["distance_m"] < 500
    assert "industrial" not in osm
    assert osm["military"]["severity"] == "neutral"


def test_zouit_far_zones_are_facts_and_resort_district_is_neutral():
    from landcheck.location import summarize_zouit

    zones = [
        _zouit("Зона затопления", "Зона затопления р. Катунь 1 %", _poly(37.006, 55.0)),  # ~400 м
        _zouit(
            "Округ санитарной (горно-санитарной) охраны лечебно-оздоровительных местностей, курортов и природных лечебных ресурсов",
            "Граница округа санитарной охраны курорта Сочи",
            _poly(36.99, 54.99, 0.03),
        ),
    ]
    groups = {g["label"]: g for g in summarize_zouit(PARCEL, zones)}
    flood = groups["зона затопления"]
    assert flood["severity"] == "neutral" and flood["far"] and "вне участка" in flood["note"]
    resort = groups["округ санитарной охраны курорта"]
    assert resort["severity"] == "neutral" and "на конкретный участок не влияет" in resort["note"]


def test_constructions_at_border_and_nearby():
    from landcheck.location import summarize_constructions

    def constr(name, purpose, geom, cad="50:07:0070507:1645"):
        return {
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "options": {
                    "cad_number": cad,
                    "params_name": name,
                    "params_purpose": purpose,
                    "params_extension": "6366",
                }
            },
        }

    feats = [
        constr(
            "Газопровод среднего давления",
            "7.1. сооружения трубопроводного транспорта",
            _poly(37.0005, 55.0),
        ),
        constr("Дорога гравийная", "7.4. сооружения дорожного транспорта", _poly(37.0012, 55.0)),
        constr("ВЛ-10 кВ", "3.1.1. сооружения электроэнергетики", _poly(37.004, 55.0)),
    ]
    c = summarize_constructions(PARCEL, feats)
    assert [i["kind"] for i in c["at_border"]] == ["gas"]
    assert c["roads"][0]["name"] == "Дорога гравийная" and c["roads"][0]["length_m"] == 6366.0
    assert c["nearby"][0]["kind"] == "power" and 150 <= c["nearby"][0]["distance_m"] <= 300
    assert c["text"].startswith("по границе участка: газопровод")
    assert summarize_constructions(PARCEL, None) is None
    assert "не найдено" in summarize_constructions(PARCEL, [])["text"]


def test_price_falls_back_to_dominant_category_when_parcel_category_unset():
    from landcheck.location import price_reference

    n = summarize_neighbors(
        "50:09:0060202:101", "Категория не установлена", None, raw_sample()["nspd"]["neighbors"]
    )
    p = price_reference(n, None, None)
    assert p is not None and p["per_sotka"] == 34_600 and "своей группы у участка нет" in p["basis"]


def test_fetch_osm_takes_first_successful_mirror(monkeypatch):
    """Зеркала опрашиваются параллельно: одно падает, второе отвечает — ответ без ожидания
    остальных; все упали — одна фраза клиенту, хосты в error_detail."""
    import httpx

    from landcheck import osm

    calls: list[str] = []

    def fake_post(url, **kw):
        calls.append(url)
        if "overpass-api.de" in url:
            raise httpx.ReadTimeout("slow")
        if "kumi" in url:
            return httpx.Response(502, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={"elements": [{"type": "node", "tags": {"landuse": "landfill"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(osm.httpx, "post", fake_post)
    res = osm.fetch_osm(55.0, 37.0, 3000, timeout=5)
    assert res["elements"] and "mail.ru" in res["url"] or "private.coffee" in res["url"]
    assert "error" not in res

    monkeypatch.setattr(
        osm.httpx, "post", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("x"))
    )
    res = osm.fetch_osm(55.0, 37.0, 3000, timeout=5)
    assert res["elements"] == [] and "OpenStreetMap" in res["error"]
    assert "overpass-api.de" in res["error_detail"] and "overpass-api.de" not in res["error"]


def test_access_summary_road_at_border_vs_through_neighbors():
    from landcheck.location import access_summary

    nspd_road = {"roads": [{"name": "а/д Козельск—Вырск", "distance_m": 12}], "items": [1]}
    a = access_summary(nspd_road, {"elements": []}, 5)
    assert (
        a["level"] == "plus" and "Козельск—Вырск" in a["text"] and a["roads"][0]["distance_m"] == 12
    )
    osm_street = {
        "elements": [{"tags": {"highway": "residential", "name": "Приморская"}, "distance_m": 8}]
    }
    a = access_summary({"roads": [], "items": []}, osm_street, 5)
    assert a["level"] == "plus" and "улица «Приморская»" in a["text"]
    far = {"elements": [{"tags": {"highway": "residential"}, "distance_m": 140}]}
    a = access_summary({"roads": [], "items": [1]}, far, 7)
    assert a["level"] == "risk" and "сервитут" in a["text"] and a["roads"] == []
    a = access_summary(None, {"elements": [], "error": "timeout"}, 7)
    assert a["level"] == "neutral" and "не определён" in a["text"]
    a = access_summary({"roads": [], "items": []}, {"elements": []}, 0)
    assert a["level"] == "neutral" and "на месте" in a["text"]


def test_forest_summary_adjacent_near_and_absent():
    from landcheck.location import forest_summary

    def nb(cat, dist, cad):
        return {
            "properties": {"options": {"land_record_category_type": cat, "cad_num": cad}},
            "distance_m": dist,
        }

    adj = forest_summary(
        [nb("Земли лесного фонда", 0.0, "39:17:0:1"), nb("Земли населенных пунктов", 0.0, "x")]
    )
    assert adj["adjacent"] and adj["level"] == "check" and "30 м" in adj["text"]
    assert "39:17:0:1" in adj["text"] and "СП 4.13130" in adj["law"]
    near = forest_summary([nb("Земли лесного фонда", 60.0, "39:17:0:1")])
    assert near["adjacent"] is False and near["distance_m"] == 60.0 and near["level"] == "neutral"
    assert forest_summary([nb("Земли лесного фонда", 300.0, "y")]) is None
    assert forest_summary([nb("Земли населенных пунктов", 0.0, "x")]) is None


def test_location_summary_includes_access_and_forest(fixtures_dir):
    from landcheck.location import summarize_location

    raw = {
        "nspd": {
            "neighbors": [
                {
                    "properties": {
                        "options": {
                            "land_record_category_type": "Земли лесного фонда",
                            "cad_num": "50:09:0:7",
                        }
                    },
                    "distance_m": 1.0,
                }
            ],
            "constructions": [],
            "zouit": [],
            "pmt": [],
        },
        "osm": {"elements": []},
    }
    loc = summarize_location(
        raw, parcel_geojson=None, cad_num="50:09:0:1", category=None, cost_index=None
    )
    assert loc["forest"]["adjacent"] and loc["access"]["level"] == "risk"
