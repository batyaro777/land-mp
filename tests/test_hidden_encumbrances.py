"""Скрытые обременения (ТЗ v3, п. 2.4): участок в границах ООПТ (кейс Влада — вырезан из нацпарка),
лесопарковый пояс, наложение лесфонда по контурам (кейс Андрея)."""

from __future__ import annotations

from landcheck.actions import action_plan
from landcheck.location import forest_summary
from landcheck.models import CheckResult, Level, Listing, Purpose, Request
from landcheck.normalize import parcel_from_payload
from landcheck.rules import RuleContext, evaluate
from landcheck.rules.engine import BatchStats
from landcheck.web.serialize import result_from_dict, result_to_dict
from tests.helpers import CORNER, SLIVER, SQUARE, boundary, make_payload

OOPT = "Особо охраняемые природные территории"
PARK = "Граница лесопарка"


def run(payload, purpose=Purpose.IZHS):
    parcel = parcel_from_payload(payload)
    ctx = RuleContext(
        listing=Listing(cad_num=payload["cad_num"]),
        parcel=parcel,
        request=Request(purpose=purpose),
        stats=BatchStats(),
    )
    return parcel, {f.rule_id: f for f in evaluate(ctx)}


AGRI = "Земли сельскохозяйственного назначения"


def test_oopt_inside_is_stop_even_with_clean_category():
    # сельхозка внутри нацпарка (кейс Влада: 5 га в Красной Поляне) — стоп
    parcel, flags = run(
        make_payload(
            "23:49:0420002:1332",
            category=AGRI,
            vri="Для сельскохозяйственного производства",
            oopt=[boundary(OOPT, "23:49-6.123")],
        )
    )
    assert parcel.oopt_checked and len(parcel.oopt) == 1
    f = flags["R22"]
    assert f.level is Level.STOP and "23:49-6.123" in f.message and "100%" in f.message
    assert "в частную не передаются" in f.message and "оспариваются" in f.message
    assert f.details["share"] == 1.0
    item = result_to_dict(
        CheckResult(listing=Listing(cad_num="x"), parcel=parcel, flags=list(flags.values()))
    )
    plan = action_plan(item)
    r22 = next(s for s in plan["steps"] if s["rule_id"] == "R22")
    texts = " ".join(a["what"] + a["where"] + a["next"] for a in r22["actions"])
    assert "История образования" in texts and "дирекции ООПТ" in texts and "300 млн" in texts
    # сериализация туда-обратно сохраняет ООПТ
    back = result_from_dict(item)
    assert back.parcel.oopt_checked and back.parcel.oopt[0].reg_number == "23:49-6.123"


def test_oopt_settlement_inside_park_is_check_with_505fz():
    # деревня внутри нацпарка «Угра» (приёмка 5f6123a): участки законно частные — проверить
    _, flags = run(make_payload("40:04:180201:66", oopt=[boundary(OOPT, "40:00-9.6")]))
    f = flags["R22"]
    assert f.level is Level.CHECK and "населённого пункта" in f.message
    assert "ст. 3.1 33-ФЗ" in f.message and "505-ФЗ" in f.source


def test_oopt_touch_and_protected_category_are_check():
    _, flags = run(make_payload("x", category=AGRI, oopt=[boundary(OOPT, "r1", SLIVER)]))
    assert flags["R22"].level is Level.CHECK and "задевает" in flags["R22"].message
    assert "1% площади" in flags["R22"].message
    _, flags = run(make_payload("x", category=AGRI, oopt=[boundary(OOPT, "r1", CORNER)]))
    assert flags["R22"].level is Level.STOP and "25%" in flags["R22"].message
    from landcheck.rules.catalog import _pct

    assert _pct(0.004) == "<1%" and _pct(0.0) == "0%" and _pct(0.25) == "25%"
    _, flags = run(
        make_payload(
            "x",
            category="Земли особо охраняемых территорий и объектов",
            oopt=[boundary(OOPT, "r1")],
        )
    )
    assert flags["R22"].level is Level.CHECK and "режимом" in flags["R22"].message
    # слой не проверялся или пересечений нет — флага нет
    assert "R22" not in run(make_payload("x"))[1]
    assert "R22" not in run(make_payload("x", oopt=[]))[1]


def test_forest_park_check_and_touch():
    _, flags = run(make_payload("x", forest_park=[boundary(PARK, "fp-1")]))
    assert flags["R23"].level is Level.CHECK and "лесопаркового" in flags["R23"].message
    assert "R23" not in run(make_payload("x", forest_park=[boundary(PARK, "fp-1", SLIVER)]))[1]
    assert "R23" not in run(make_payload("x", forest_park=[]))[1]


def _forest_neighbor(cad: str, geometry: dict, distance: float | None = 0.0) -> dict:
    return {
        "type": "Feature",
        "geometry": geometry,
        "distance_m": distance,
        "properties": {
            "options": {"cad_num": cad, "land_record_category_type": "Земли лесного фонда"}
        },
    }


def test_forest_overlap_by_contours():
    # лесной участок заходит на четверть участка — наложение (кейс Андрея)
    fr = forest_summary([_forest_neighbor("50:09:0000000:7", CORNER)], SQUARE)
    assert fr["overlap"] and fr["share"] == 0.25 and fr["level"] == "check"
    assert "наложение земель лесного фонда" in fr["text"] and "25%" in fr["text"]
    assert "280-ФЗ" in fr["text"] and "50:09:0000000:7" in fr["text"]
    # лес рядом, но контуры не пересекаются — прежняя логика смежности
    far = {
        "type": "Polygon",
        "coordinates": [
            [[37.002, 55.0], [37.003, 55.0], [37.003, 55.001], [37.002, 55.001], [37.002, 55.0]]
        ],
    }
    fr = forest_summary([_forest_neighbor("50:09:0000000:8", far, 3.0)], SQUARE)
    assert not fr.get("overlap") and fr["adjacent"] and "граничит" in fr["text"]
    # без геометрии участка — только смежность
    fr = forest_summary([_forest_neighbor("50:09:0000000:7", CORNER)], None)
    assert not fr.get("overlap") and fr["adjacent"]
    assert forest_summary([], SQUARE) is None
