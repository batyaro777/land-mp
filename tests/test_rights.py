"""Права и ограничения (ТЗ v3, п. 2): разбор текста ЛК Росреестра и выписки, сводка, флаг R18."""

from __future__ import annotations

from landcheck import egrn, rights
from landcheck.models import Purpose

LK_TEXT = """Справочная информация по объектам недвижимости в режиме online
Земельный участок
Кадастровый номер: 40:10:160500:1
Площадь, кв.м 368288
Категория земель Земли сельскохозяйственного назначения
Вид разрешенного использования для ведения сельскохозяйственной деятельности
Сведения о кадастровой стоимости
Кадастровая стоимость (руб) 962590.73
Дата определения 01.01.2022
Дата внесения 17.01.2023
Сведения о правах и ограничениях (обременениях)
Вид, номер и дата государственной регистрации права Собственность
№ 40-40-10/002/2013-603 от 07.03.2013
Ограничение прав и обременение объекта недвижимости: не зарегистрировано
Дата обновления информации: 28.10.2026
"""

LK_LEASE = """Сведения о правах и ограничениях (обременениях)
Вид, номер и дата государственной регистрации права: Собственность № 50:07:0070507:567-50/001/2019-1 от 12.02.2019
Правообладатель: Администрация Волоколамского городского округа
Ограничение прав и обременение объекта недвижимости
Вид: Аренда, № 50:07:0070507:567-50/001/2021-5 от 03.06.2021, срок действия: с 03.06.2021 по 02.06.2041
Лицо, в пользу которого установлено ограничение прав и обременение объекта недвижимости: Иванов Иван Иванович
"""

LK_ARREST = """Вид, номер и дата государственной регистрации права: Собственность № 23:49:0420002:1332-23/050/2020-3 от 15.09.2020
Ограничение прав и обременение объекта недвижимости: Запрещение регистрации, № 23:49:0420002:1332-23/050/2024-7 от 11.03.2024
Ипотека № 23:49:0420002:1332-23/050/2020-4 от 15.09.2020, срок действия: с 15.09.2020 на 20 лет
"""


def test_parse_lk_ownership_without_restrictions():
    r = rights.parse_lk_text(LK_TEXT)
    assert r.parsed and r.source == "lk" and r.as_of == "28.10.2026"
    assert [e.kind for e in r.rights] == ["ownership"]
    assert r.rights[0].number == "40-40-10/002/2013-603" and r.rights[0].date == "07.03.2013"
    assert r.restrictions == [] and r.restrictions_known
    s = rights.summarize([r], {"ownership_type": "Муниципальная"})
    # собственник не назван, по НСПД — муниципальная, обременений нет → свободен
    assert s["status"] == "public_free" and s["level"] == "ок" and "свободен" in s["headline"]
    f = rights.rights_flag(s)
    assert f["rule_id"] == "R18" and f["level"] == "ок" and "28.10.2026" in f["source"]


def test_parse_lk_lease_masks_person_and_marks_occupied():
    r = rights.parse_lk_text(LK_LEASE)
    assert r.rights[0].holder_kind == "public" and "Администрация" in r.rights[0].holder
    lease = [e for e in r.restrictions if e.kind == "lease"][0]
    assert lease.term == "с 03.06.2021 по 02.06.2041"
    assert lease.holder == "И***в И. И." and lease.holder_kind == "person"
    assert "Иванов" not in str(r.to_dict())
    s = rights.summarize([r], {"ownership_type": "Муниципальная"})
    assert s["status"] == "leased" and s["level"] == "проверить"
    assert "арендатором" in s["headline"] and "И***в" in s["headline"]


def test_parse_lk_arrest_is_stop_and_mortgage_listed():
    r = rights.parse_lk_text(LK_ARREST)
    kinds = {e.kind for e in r.restrictions}
    assert {"ban", "mortgage"} <= kinds
    s = rights.summarize([r], {"ownership_type": "Частная"})
    assert s["status"] == "arrest" and s["level"] == "стоп"
    f = rights.rights_flag(s)
    assert f["level"] == "стоп" and "запрет регистрационных действий" in f["message"]
    assert "ипотека" not in f["message"]  # при аресте остальное не важно


def test_unparsed_text_and_nspd_only():
    r = rights.parse_lk_text("просто какой-то текст без прав")
    assert not r.parsed and r.notes
    s = rights.summarize([r], {"ownership_type": "Муниципальная"})
    assert s["status"] == "public_unknown" and s["source"] == "nspd"
    assert rights.rights_flag(s) is None  # по НСПД работает R12
    s = rights.summarize([], {"ownership_type": "Частная собственность"})
    assert s["status"] == "private" and s["level"] == "проверить"


def test_from_extract_private_owner_with_mortgage():
    ex = egrn.Extract(
        is_extract=True,
        cad_num="50:09:0060202:101",
        holders=["Петров Пётр Петрович"],
        right_kind="собственность",
        right_date="01.02.2020",
        right_number="50:09:0060202:101-50/001/2020-1",
        issued="20.10.2026",
        encumbrances=["ипотека"],
        encumbrances_known=True,
        source="xml",
    )
    r = rights.from_extract(ex)
    assert r.source == "egrn_xml" and r.as_of == "20.10.2026"
    assert r.rights[0].holder == "П***в П. П." and r.rights[0].holder_kind == "person"
    assert [e.kind for e in r.restrictions] == ["mortgage"]
    s = rights.summarize([r], {"ownership_type": "Частная"})
    assert s["status"] == "private" and s["level"] == "проверить" and "ипотека" in s["headline"]
    # лучший источник побеждает ЛК
    lk = rights.parse_lk_text(LK_TEXT)
    s2 = rights.summarize([lk, r], {"ownership_type": "Частная"})
    assert s2["source"] == "egrn_xml"


def test_apply_to_item_recomputes_verdict():
    data = {
        "parcel": {"ownership_type": "Муниципальная", "found": True},
        "flags": [
            {"rule_id": "R12", "level": "проверить", "message": "м", "source": "s", "details": {}}
        ],
        "verdict": "проверить",
    }
    out = rights.apply_to_item(data, rights.parse_lk_text(LK_ARREST), Purpose.IZHS)
    assert out["verdict"] == "стоп" and out["rights"]["status"] == "arrest"
    # R12 (вопрос «свободен или в аренде?» по НСПД) закрыт справкой ЛК — остаётся только R18
    assert [f["rule_id"] for f in out["flags"]] == ["R18"]
    assert len(out["rights_sources"]) == 1
    # повторная загрузка того же источника заменяет, а не дублирует
    out2 = rights.apply_to_item(out, rights.parse_lk_text(LK_TEXT), Purpose.IZHS)
    assert len(out2["rights_sources"]) == 1 and out2["verdict"] == "проверить"
    assert out2["rights"]["status"] == "public_free"
