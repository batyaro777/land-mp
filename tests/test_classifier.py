"""Классификатор ВРИ П/0412 (ТЗ v3, п. 3): код по формулировке, старые записи, «что можно/нельзя»."""

from __future__ import annotations

from landcheck.models import Purpose
from landcheck.rules import classifier as c
from landcheck.rules.purpose import vri_match


def test_exact_names_codes_and_case_forms():
    assert c.match("Для индивидуального жилищного строительства").code == "2.1"
    m = c.match("Для ведения личного подсобного хозяйства")
    assert (m.code, m.how, m.legacy) == ("2.2", "name", False)
    assert c.match("Магазины (4.4)").how == "code"
    assert c.match("код 13.2").code == "13.2"
    assert c.match("Ведение садоводства").how == "name"
    og = c.match("для ведения огородничества")
    assert og.code == "13.1" and not og.legacy
    agri = c.match("Для сельскохозяйственного использования")
    assert agri.code == "1.0" and agri.how == "name"
    assert c.match("Склады").code == "6.9" and not c.match("Склады").legacy


def test_legacy_wordings_mapped_with_notes():
    m = c.match("Для сельскохозяйственного производства")
    assert (m.code, m.legacy, m.how) == ("1.0", True, "legacy")
    assert c.match("под жилую застройку индивидуальную").code == "2.1"
    k = c.match("для ведения крестьянского (фермерского) хозяйства")
    assert k.code == "1.0" and "КФХ" in k.note
    s = c.match("Садоводство")
    assert s.code == "13.2" and "1.5" in s.note
    assert c.match("для дачного строительства").code == "13.2"
    assert c.match("Жилой дом (многоэтажная застройка)").code == "2.6"
    assert c.match("Для размещения объектов торговли").code == "4.4"
    assert c.match("ЭКСПЛУАТАЦИИ ПОМЕЩЕНИЙ ПОД ШАШЛЫЧНУЮ-КАФЕ-БАР").code == "4.6"
    assert c.match("абракадабра") is None and c.match("") is None


def test_limits_fall_back_to_group():
    assert c.limits_for("4.4")["source"].endswith("4.0")
    assert "многоквартирный" in c.limits_for("2.1")["cannot"]
    assert c.limits_for("13.1")["cannot"].startswith("садовый")
    assert c.limits_for("1.19")["source"].startswith("ЗК РФ ст. 77")


def test_describe_and_codes_for_multiple_wordings():
    d = c.describe(
        "Для индивидуального жилищного строительства",
        [
            "Для индивидуального жилищного строительства",
            "Для ведения личного подсобного хозяйства",
            "что-то местное",
        ],
    )
    assert d["codes"] == ["2.1", "2.2"] and d["unmatched"] == ["что-то местное"]
    assert d["main"]["code"] == "2.1" and not d["legacy"] and "П/0412" in d["source"]
    assert c.codes_for("Для сельскохозяйственного производства | сенокошение") == ["1.0", "1.19"]
    assert c.describe(None, []) is None


def test_data_anchors_and_code_boundaries():
    by = c.by_code()
    assert by["2.1"].name == "Для индивидуального жилищного строительства"
    assert by["13.2"].name == "Ведение садоводства" and by["13.1"].name == "Ведение огородничества"
    assert by["12.0"].name.startswith("Земельные участки (территории) общего")
    assert by["4.9.1.1"].name == "Заправка транспортных средств"
    assert len(by) >= 170 and "П/0412" in c.source()
    # код 2.1 не ловится внутри 2.1.1 и 12.1
    assert vri_match(Purpose.IZHS, "код 2.1.1 малоэтажная многоквартирная") != "match"
    assert vri_match(Purpose.IZHS, "код 2.1") == "match"
