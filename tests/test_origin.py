"""Происхождение участка из выписки (ТЗ v3, п. 2.4.4): исходные номера, дата присвоения, флаг R24."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from landcheck import egrn, egrn_xml, rights
from landcheck.models import Purpose
from tests.test_egrn import EXTRACT
from tests.test_egrn_xml import build

ASC_LINE = (
    "Кадастровые номера объектов недвижимости, из которых образован объект недвижимости: "
    "50:09:0060202:1, 50:09:0060202:2\n"
)


def test_text_extract_origin():
    ex = egrn.parse_extract(EXTRACT.replace("Адрес:", ASC_LINE + "Адрес:"))
    assert ex.assigned_date == "12.05.2005"
    assert ex.ascendants == ["50:09:0060202:1", "50:09:0060202:2"]
    assert ex.cad_num == "50:09:0060202:101"  # основной номер не перепутан с исходными
    plain = egrn.parse_extract(EXTRACT)
    assert plain.assigned_date == "12.05.2005" and plain.ascendants == []


def test_xml_extract_origin():
    links = (
        "<cad_links><ascendant_cad_numbers><ascendant_cad_number>"
        "<cad_number>50:09:0060202:7</cad_number></ascendant_cad_number>"
        "</ascendant_cad_numbers></cad_links>"
    )
    ex = egrn_xml.parse_xml_extract(build(restrict=links))
    assert ex.ascendants == ["50:09:0060202:7"] and ex.assigned_date is None
    assert egrn_xml.parse_xml_extract(build()).ascendants == []


def test_origin_flag_fresh_vs_old():
    ex = egrn.parse_extract(EXTRACT.replace("Адрес:", ASC_LINE + "Адрес:"))
    r = rights.from_extract(ex)
    assert r.origin == {
        "ascendants": ["50:09:0060202:1", "50:09:0060202:2"],
        "assigned": "12.05.2005",
    }
    summary = rights.summarize([r], {"ownership_type": "Частная собственность"}, Purpose.IZHS)
    assert summary["origin"]["assigned"] == "12.05.2005"
    old = rights.origin_flag(summary)
    assert (
        old["rule_id"] == "R24"
        and old["level"] == "ок"
        and "история образования длинная" in old["message"]
    )
    # молодой участок — проверить историю
    fresh = (datetime.now(UTC) - timedelta(days=400)).strftime("%d.%m.%Y")
    summary["origin"]["assigned"] = fresh
    f = rights.origin_flag(summary)
    assert (
        f["level"] == "проверить"
        and "ООПТ или лесфонда" in f["message"]
        and f["details"]["years"] < 2
    )
    # без исходных — флага нет
    assert rights.origin_flag({"origin": {"ascendants": [], "assigned": fresh}}) is None
    assert rights.origin_flag({}) is None


def test_apply_to_item_adds_r24_and_drops_duplicates():
    ex = egrn.parse_extract(EXTRACT.replace("Адрес:", ASC_LINE + "Адрес:"))
    r = rights.from_extract(ex)
    data = {
        "parcel": {"ownership_type": "Частная собственность", "found": True},
        "flags": [],
        "verdict": "ок",
    }
    out = rights.apply_to_item(data, r, Purpose.IZHS)
    ids = [f["rule_id"] for f in out["flags"]]
    assert "R18" in ids and ids.count("R24") == 1
    out2 = rights.apply_to_item(out, r, Purpose.IZHS)  # повторное применение не дублирует
    assert [f["rule_id"] for f in out2["flags"]].count("R24") == 1
    assert out2["rights"]["origin"]["ascendants"] == ["50:09:0060202:1", "50:09:0060202:2"]
