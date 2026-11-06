"""Выписка ЕГРН в XML: структурный разбор и сверка с подписью, свежестью, категорией и ВРИ."""

from __future__ import annotations

from datetime import UTC, datetime

from landcheck import egrn, egrn_xml

XML = """<?xml version="1.0" encoding="UTF-8"?>
<extract_about_property_land xmlns="http://rosreestr.ru/services/v0.18/TExtractAboutPropertyLand">
  <details_statement><group_top_requisites>
    <date_formation>{issued}</date_formation>
  </group_top_requisites></details_statement>
  <land_record>
    <object><common_data>
      <type><code>002001001000</code><value>Земельный участок</value></type>
      <cad_number>50:09:0060202:101</cad_number>
      <quarter_cad_number>50:09:0060202</quarter_cad_number>
    </common_data></object>
    <address_location><address><readable_address>Московская область, СНТ Виктория, уч. 5</readable_address></address></address_location>
    <params>
      <area><value>1000</value><inaccuracy>11</inaccuracy></area>
      <category><type><code>003002000000</code><value>{category}</value></type></category>
      <permitted_use><permitted_use_established><by_document>{vri}</by_document></permitted_use_established></permitted_use>
    </params>
  </land_record>
  <right_records><right_record>
    <record_info><registration_date>2019-03-15T00:00:00</registration_date></record_info>
    <right_data>
      <right_type><code>001001000000</code><value>Собственность</value></right_type>
      <right_number>50:09:0060202:101-50/001/2019-1</right_number>
    </right_data>
    <right_holders><right_holder><individual>
      <surname>Иванов</surname><name>Иван</name><patronymic>Иванович</patronymic>
    </individual></right_holder></right_holders>
  </right_record></right_records>
  {restrict}
</extract_about_property_land>
"""


def build(
    issued: str = "2026-10-23T10:00:00",
    category: str = "Земли населенных пунктов",
    vri: str = "Для индивидуального жилищного строительства",
    restrict: str = "",
) -> bytes:
    return XML.format(issued=issued, category=category, vri=vri, restrict=restrict).encode()


def test_parse_xml_fields():
    ex = egrn_xml.parse_xml_extract(build())
    assert ex is not None and ex.is_extract and ex.source == "xml"
    assert ex.cad_num == "50:09:0060202:101" and ex.area_m2 == 1000.0
    assert ex.address == "Московская область, СНТ Виктория, уч. 5"
    assert ex.category == "Земли населенных пунктов"
    assert ex.permitted_use == "Для индивидуального жилищного строительства"
    assert ex.holders == ["Иванов Иван Иванович"]
    assert ex.right_kind == "собственность" and ex.right_date == "15.03.2019"
    assert ex.right_number == "50:09:0060202:101-50/001/2019-1"
    assert ex.issued == "23.10.2026" and ex.issued_at == "2026-10-23T10:00:00"
    assert ex.encumbrances_known and ex.encumbrances == []


def test_parse_xml_restrictions_and_legal_entity():
    restrict = (
        "<restrict_records><restrict_record><restrictions_encumbrances_data>"
        "<restriction_encumbrance_type><code>022001000000</code><value>Ипотека</value>"
        "</restriction_encumbrance_type></restrictions_encumbrances_data></restrict_record>"
        "</restrict_records>"
    )
    individual = (
        "<individual>\n      <surname>Иванов</surname><name>Иван</name>"
        "<patronymic>Иванович</patronymic>\n    </individual>"
    )
    entity = (
        '<legal_entity><entity><resident><name>ООО "Ромашка"</name><inn>5044000000</inn>'
        "</resident></entity></legal_entity>"
    )
    text = build(restrict=restrict).decode()
    assert individual in text
    raw = text.replace(individual, entity).encode()
    ex = egrn_xml.parse_xml_extract(raw)
    assert ex is not None and ex.encumbrances == ["ипотека"]
    assert ex.holders == ['ООО "Ромашка"']


def test_parse_xml_rejects_garbage_and_dtd():
    assert egrn_xml.parse_xml_extract(b"%PDF-1.4") is None
    assert egrn_xml.parse_xml_extract(b"<a><b></a>") is None
    assert (
        egrn_xml.parse_xml_extract(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]><a>&e;</a>')
        is None
    )
    ex = egrn_xml.parse_xml_extract(b"<root><foo>bar</foo></root>")
    assert ex is not None and not ex.is_extract and ex.cad_num is None


def test_freshness_assumes_moscow_time():
    ex = egrn_xml.parse_xml_extract(build(issued="2026-10-23T10:00:00"))  # 10:00 МСК = 07:00 UTC
    early = egrn.compare(ex, cad_num="x", created_at=datetime(2026, 10, 23, 6, 30, tzinfo=UTC))
    late = egrn.compare(ex, cad_num="x", created_at=datetime(2026, 10, 23, 8, 0, tzinfo=UTC))
    assert {i.key: i.status for i in early.items}["fresh"] == "ok"
    by = {i.key: i for i in late.items}
    assert by["fresh"].status == "warn" and "23.10.2026 10:00 МСК" in by["fresh"].note


def test_compare_with_signature_freshness_category():
    ex = egrn_xml.parse_xml_extract(build())
    created = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)  # 09:00 МСК, выписка в 10:00 МСК
    res = egrn.compare(
        ex,
        cad_num="50:09:0060202:101",
        area_m2=1000.0,
        seller_name="Иван Иванов",
        today=created.date(),
        signature={"status": "ok", "signer": "Росреестр (ФГИС ЕГРН)"},
        created_at=created,
        category="Земли населённых пунктов",
        permitted_use="Для индивидуального жилищного строительства",
    )
    by = {i.key: i for i in res.items}
    assert res.verdict == "ok" and "подпись Росреестра проверена" in res.label
    assert by["sig"].status == "ok" and "Росреестр" in by["sig"].note
    assert by["fresh"].status == "ok" and "после создания" in by["fresh"].note
    assert by["category"].status == "ok" and by["vri"].status == "ok"
    assert [i.key for i in res.items][:2] == ["sig", "cad"]


def test_compare_bad_signature_stale_and_land_mismatch():
    ex = egrn_xml.parse_xml_extract(
        build(
            issued="2026-10-01",
            category="Земли сельскохозяйственного назначения",
            vri="Для ведения садоводства",
        )
    )
    created = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    res = egrn.compare(
        ex,
        cad_num="50:09:0060202:101",
        seller_name="Иван Иванов",
        today=created.date(),
        signature={"status": "bad", "detail": "content verify error"},
        created_at=created,
        category="Земли населённых пунктов",
        permitted_use="Для индивидуального жилищного строительства",
    )
    by = {i.key: i for i in res.items}
    assert res.verdict == "mismatch"
    assert (
        by["sig"].status == "mismatch"
        and by["category"].status == "mismatch"
        and by["vri"].status == "mismatch"
    )
    assert by["fresh"].status == "warn" and "закажите новую" in by["fresh"].note
    assert "подпись" in res.label and "категория" in res.label and "ври" in res.label
    # без подписи и без даты объявления — неизвестно, не ошибка
    res2 = egrn.compare(
        ex, cad_num="50:09:0060202:101", seller_name="Иван Иванов", today=created.date()
    )
    by2 = {i.key: i for i in res2.items}
    assert by2["sig"].status == "unknown" and by2["fresh"].status == "unknown"
    res3 = egrn.compare(
        ex, cad_num="x", today=created.date(),
        signature={"status": "chain_unverified", "signer": "Росреестр"},
    )  # fmt: skip
    assert {i.key: i.status for i in res3.items}["sig"] == "warn"
