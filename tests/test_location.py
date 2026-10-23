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
    labels = [g["label"] for g in groups]
    assert labels[0] == "санитарно-защитная зона предприятия" and groups[0]["severity"] == "risk"
    assert 250 <= groups[0]["min_distance_m"] <= 350
    resort = next(g for g in groups if g["severity"] == "plus")
    assert resort["min_distance_m"] == 0 and "курорт" in resort["label"]
    nets = next(g for g in groups if g["label"] == "охранная зона инженерных сетей")
    assert nets["count"] == 2


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
    assert osm["landfill"]["severity"] == "risk" and 1000 <= osm["landfill"]["distance_m"] <= 1200
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
    assert all(el["tags"] for el in slim["osm"]["elements"]) and len(slim["osm"]["elements"]) == 3
    loc = summarize_location(
        slim,
        parcel_geojson=PARCEL,
        cad_num="50:09:0060202:101",
        category=SETTLEMENT,
        cost_index=270.0,
    )
    assert loc["zouit"][0]["label"] == "санитарно-защитная зона предприятия"


def test_price_falls_back_to_dominant_category_when_parcel_category_unset():
    from landcheck.location import price_reference

    n = summarize_neighbors(
        "50:09:0060202:101", "Категория не установлена", None, raw_sample()["nspd"]["neighbors"]
    )
    p = price_reference(n, None, None)
    assert p is not None and p["per_sotka"] == 34_600 and "своей группы у участка нет" in p["basis"]
