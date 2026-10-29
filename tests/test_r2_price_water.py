"""ТЗ v2, релиз R2: «цена к ориентиру», разделы «Вода» и «Негативные факторы»."""

from __future__ import annotations

from pathlib import Path

from landcheck.export import COLUMNS, to_csv
from landcheck.location import negative_factors, water_summary
from landcheck.web.content import batch_median, price_position
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета


def _item(pps: float | None, cost: float | None = 1_500_000, area: float = 1000.0, loc=None):
    d = {
        "price_per_sotka": pps,
        "parcel": {"cadastral_cost_rub": cost, "area_m2": area},
    }
    if loc is not None:
        d["location"] = loc
    return d


def test_price_position_labels_and_parts():
    # кадастровая 1,5 млн за 10 сот = 150 000 за сотку; цена 300 000 → ×2.0
    pp = price_position(_item(300_000), median_pps=250_000)
    assert pp["vs_cadastral"] == 2.0 and pp["label"] == "дороже подборки на 20%"
    assert pp["parts"] == ["×2.0 к кадастровой"]
    assert pp["text"] == "дороже подборки на 20% · ×2.0 к кадастровой"
    assert price_position(_item(240_000), 250_000)["label"] == "в медиане подборки"
    assert price_position(_item(150_000), 250_000)["label"] == "дешевле подборки на 40%"
    # без медианы и с соседями из «Локации»
    pp = price_position(_item(300_000, loc={"price": {"listing_vs_gko": 1.4}}), None)
    assert pp["label"] is None and pp["vs_neighbors"] == 1.4
    assert pp["text"] == "×2.0 к кадастровой · ×1.4 к ставке соседей"
    assert price_position(_item(None), 250_000) is None
    assert price_position(_item(300_000, cost=None), None) is None
    # площадь ЕГРН 1 м² (ошибка данных) — ориентир не считаем, чтобы не показать ×14615
    assert price_position(_item(500_000_000, area=1.0), 250_000) is None


def test_price_reference_ignores_micro_area():
    from landcheck.location import price_reference

    nb = {"same_category": {"median_rub_m2": 346.0, "count": 10}, "categories": []}
    assert "listing_vs_gko" not in price_reference(nb, 5_000_000, 1.0)
    assert price_reference(nb, 5_000_000, 1000.0)["listing_vs_gko"] == 14.45


def test_batch_median_needs_three_prices():
    stats = {"median_price_per_sotka": 200_000}
    assert batch_median(stats, [_item(1), _item(2)]) is None
    assert batch_median(stats, [_item(1), _item(2), _item(3)]) == 200_000
    assert batch_median(None, [_item(1), _item(2), _item(3)]) is None


def test_export_has_ratio_columns():
    assert COLUMNS[10:12] == ["Цена к кадастровой, ×", "Цена к медиане подборки, %"]


LOC = {
    "fetched_at": "2026-10-19T10:00:00+00:00",
    "radius_m": {"nspd": 1000, "neighbors": 400, "osm": 3000},
    "zouit": [
        {
            "label": "зона затопления",
            "severity": "risk",
            "note": "паводковый риск",
            "count": 1,
            "min_distance_m": 0,
            "nearest_name": "Зона затопления р. Катунь",
            "source": "НСПД",
        },
        {
            "label": "санитарно-защитная зона предприятия",
            "severity": "risk",
            "note": "СЗЗ",
            "count": 2,
            "min_distance_m": 630,
            "nearest_name": "СЗЗ котельной",
            "source": "НСПД",
        },
        {
            "label": "водоохранная зона",
            "severity": "neutral",
            "note": "",
            "count": 1,
            "min_distance_m": 0,
            "nearest_name": None,
            "source": "НСПД",
        },
    ],
    "osm": [
        {
            "kind": "landfill",
            "label": "свалка",
            "severity": "risk",
            "distance_m": 1015,
            "name": None,
            "count": 1,
        },
        {
            "kind": "water",
            "label": "водоём / река",
            "severity": "plus",
            "distance_m": 128,
            "name": "Катунь",
            "count": 3,
        },
    ],
}


def test_water_summary_levels():
    w = water_summary(LOC)
    assert w["level"] == "risk" and "в зоне затопления" in w["text"]
    assert [i["label"] for i in w["items"]] == [
        "зона затопления",
        "водоохранная зона",
        "водоём или река",
    ]
    # только водоохранная зона и вода рядом
    loc2 = {"zouit": [LOC["zouit"][2]], "osm": [LOC["osm"][1]]}
    w2 = water_summary(loc2)
    assert w2["level"] == "neutral" and "водоохранной зоне" in w2["text"]
    w3 = water_summary({"zouit": [], "osm": [LOC["osm"][1]]})
    assert w3["level"] == "plus" and "Катунь" in w3["text"]
    # зона затопления в 200 м — участок вне зоны: факт, не риск (зум 28.10, п. 1.2)
    w4 = water_summary({"zouit": [LOC["zouit"][0] | {"min_distance_m": 200}], "osm": None})
    assert w4["level"] == "neutral" and "вне зон затопления" in w4["text"] and "200 м" in w4["text"]
    assert w4["items"][0]["severity"] == "neutral"
    # граница зоны в 30 м — риск
    w5 = water_summary({"zouit": [LOC["zouit"][0] | {"min_distance_m": 30}], "osm": None})
    assert w5["level"] == "risk" and "в 30 м" in w5["text"]
    # вода рядом и зона затопления далеко: «плюс» с честной фразой про зону
    w6 = water_summary(
        {"zouit": [LOC["zouit"][0] | {"min_distance_m": 512}], "osm": [LOC["osm"][1]]}
    )
    assert w6["level"] == "plus" and "вне зон затопления" in w6["text"] and "512 м" in w6["text"]
    assert water_summary({"zouit": [], "osm": []})["level"] == "none"


def test_negative_factors_merges_nspd_osm_region():
    region = {
        "fetched_at": "2026-10-19T11:00:00+00:00",
        "sections": [
            {"title": "Дороги и инфраструктура", "items": ["мост"]},
            {
                "title": "Негативные факторы",
                "items": ["данных не найдено", "Планируется полигон ТКО в 5 км (2027)"],
            },
        ],
    }
    # СЗЗ в 630 м и свалка в 1 015 м — окружение, не риск; новости региона остаются в блоке
    # «Регион» и в риски участка не попадают (зум 28.10, п. 1.3)
    n = negative_factors(LOC, region)
    assert [x["label"] for x in n] == ["зона затопления"]
    loc2 = {"zouit": LOC["zouit"], "osm": [LOC["osm"][0] | {"distance_m": 300}]}
    n2 = negative_factors(loc2, region)
    assert [x["label"] for x in n2] == ["зона затопления", "свалка"]
    assert n2[1]["source"] == "OpenStreetMap"
    assert negative_factors({"zouit": [], "osm": None}, {"error": "x", "sections": []}) == []


def test_check_table_and_report_show_price_position(client, sample_csv: Path):  # noqa: F811
    login(client)
    r = client.post(
        "/checks",
        data={
            "title": "Цены",
            "numbers": sample_csv.read_text(encoding="utf-8"),
            "purpose": "izhs",
        },
    )
    url = r.headers["location"]
    page = client.get(url).text
    assert "Цена к ориентиру" in page and "к кадастровой" in page and "подборки" in page
    csv_text = client.get(url + "/export.csv").text
    assert "Цена к кадастровой" in csv_text.splitlines()[0]
    rep = client.get(url + "/items/50:09:0060202:101").text
    assert "Цена к ориентиру:" in rep
    # локация на фикстурах: блок грузится, разделы «Вода» и «Негативные факторы» есть
    loc = client.get(url + "/items/50:09:0060202:101/location").text
    assert "Вода" in loc and "Негативные факторы" in loc


def test_to_csv_ratio_values(client, sample_csv: Path):  # noqa: F811
    from landcheck.web.app import _batch_from_check
    from landcheck.web.db import Check, session

    login(client)
    r = client.post("/checks", data={"numbers": sample_csv.read_text(encoding="utf-8")})
    check_id = int(r.headers["location"].rsplit("/", 1)[1])
    with session() as db:
        batch = _batch_from_check(db.get(Check, check_id))
    rows = [line.split(";") for line in to_csv(batch).splitlines()]
    head = rows[0]
    i_ratio, i_pct = head.index("Цена к кадастровой, ×"), head.index("Цена к медиане подборки, %")
    first = next(row for row in rows[1:] if row[1] == "50:09:0060202:101")
    # 2 500 000 за 10 сот = 250 000/сот; кадастровая 1 500 000 → 150 000/сот → ×1.67
    assert first[i_ratio] == "1.67" and first[i_pct] != ""
