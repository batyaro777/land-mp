"""Коридор цены за сотку по базе: тип участка, уровни локации, медиана и позиция."""

from __future__ import annotations

from datetime import UTC, datetime

from landcheck.web.content import land_kind, locality_keys, price_corridor, price_point


def test_land_kind_and_locality_levels():
    assert (
        land_kind("Земли населенных пунктов", "Для индивидуального жилищного строительства")
        == "izhs"
    )
    assert land_kind("Земли сельскохозяйственного назначения", "Ведение садоводства") == "snt"
    assert (
        land_kind(
            "Земли сельскохозяйственного назначения", "Для сельскохозяйственного производства"
        )
        == "agri"
    )
    assert land_kind("Земли промышленности", None) == "industry"
    assert land_kind(None, None) == "other"
    keys = locality_keys(
        "Российская Федерация, Республика Алтай, Чемальский район, с. Элекмонар, ул. Мира, з/у 5"
    )
    assert [lvl for lvl, _ in keys] == ["settlement", "district", "region"]
    assert keys[0][1] == "республика алтай, чемальский район, с. элекмонар"
    assert locality_keys("Алтайский край") == [("region", "алтайский край")]
    assert locality_keys(None) == []


def _pt(
    i,
    pps,
    address,
    kind=("Земли населенных пунктов", "Для индивидуального жилищного строительства"),
):
    area = 1000.0
    return price_point(
        f"04:05:0000{i:02d}:1",
        price_rub=pps * 10,
        area_m2=area,
        category=kind[0],
        vri=kind[1],
        address=address,
        when=datetime.now(UTC),
    )


def test_corridor_prefers_narrowest_level_with_enough_points():
    elek = "Республика Алтай, Чемальский район, с. Элекмонар, ул. Мира"
    chemal = "Республика Алтай, Чемальский район, с. Чемал, ул. Пчёлкина"
    pts = [_pt(1, 100_000, elek), _pt(2, 120_000, elek)]  # в селе только две цены
    pts += [_pt(3, 200_000, chemal), _pt(4, 250_000, chemal), _pt(5, 300_000, chemal)]
    c = price_corridor(
        pts, address=elek, category="Земли населенных пунктов", vri="Для ИЖС", pps=110_000
    )
    assert c["level"] == "district" and c["n"] == 5 and c["kind"] == "izhs"
    assert c["median"] == 200_000 and c["p25"] == 120_000 and c["p75"] == 250_000
    assert c["position_text"] == "дешевле медианы базы на 45%"
    pts.append(_pt(6, 130_000, elek))
    c = price_corridor(
        pts, address=elek, category="Земли населенных пунктов", vri="Для ИЖС", pps=115_000
    )
    assert c["level"] == "settlement" and c["n"] == 3 and c["position_text"] == "в коридоре базы"
    # другой тип участка не смешивается, сам участок исключается
    assert (
        price_corridor(
            pts,
            address=elek,
            category="Земли сельскохозяйственного назначения",
            vri="Ведение садоводства",
        )
        is None
    )
    assert (
        price_corridor(
            pts[:3],
            address=elek,
            category="Земли населенных пунктов",
            vri="Для ИЖС",
            exclude_cad="04:05:000001:1",
        )
        is None
    )
    assert (
        price_point(
            "x", price_rub=100, area_m2=1.0, category=None, vri=None, address=elek, when=None
        )
        is None
    )
