"""Автосверка выписки ЕГРН (R12): разбор печатной формы и сравнение с объявлением/продавцом."""

from __future__ import annotations

from datetime import date

from landcheck import egrn

EXTRACT = """Выписка из Единого государственного реестра недвижимости об основных характеристиках
и зарегистрированных правах на объект недвижимости
На основании запроса от 10.10.2026, сообщаем, что по состоянию на 10.10.2026 в ЕГРН содержатся сведения:
Земельный участок
Кадастровый номер: 50:09:0060202:101
Номер кадастрового квартала: 50:09:0060202
Дата присвоения кадастрового номера: 12.05.2005
Адрес: Московская область, Солнечногорский район, СНТ Виктория, уч. 5
Площадь: 1000 +/- 11 кв. м
Кадастровая стоимость, руб.: 1 500 000
Категория земель: Земли населённых пунктов
Виды разрешенного использования: Для индивидуального жилищного строительства
Сведения о зарегистрированных правах
1. Правообладатель (правообладатели): 1.1. Иванов Иван Иванович
2. Вид, номер и дата государственной регистрации права: 2.1. Собственность, № 50:09:0060202:101-50/001/2019-1 от 15.03.2019
3. Ограничение прав и обременение объекта недвижимости: не зарегистрировано
4. Договоры участия в долевом строительстве: данные отсутствуют
"""

TODAY = date(2026, 10, 20)


def test_parse_extract_fields():
    ex = egrn.parse_extract(EXTRACT)
    assert ex.is_extract
    assert ex.cad_num == "50:09:0060202:101"
    assert ex.area_m2 == 1000.0
    assert ex.address == "Московская область, Солнечногорский район, СНТ Виктория, уч. 5"
    assert ex.holders == ["Иванов Иван Иванович"]
    assert ex.right_kind == "собственность" and ex.right_date == "15.03.2019"
    assert ex.issued == "10.10.2026"
    assert ex.encumbrances_known and ex.encumbrances == []


def test_compare_ok_for_owner():
    ex = egrn.parse_extract(EXTRACT)
    res = egrn.compare(
        ex, cad_num="50:09:0060202:101", area_m2=1000.0, seller_name="Иван Иванов", today=TODAY
    )
    assert res.verdict == "ok"
    by = {i.key: i for i in res.items}
    assert by["cad"].status == "ok" and by["area"].status == "ok"
    assert by["holder"].status == "ok" and "И***в И. И." in by["holder"].note
    assert "Иванов Иван" not in by["holder"].note  # ФИО наружу не уходит
    assert by["right"].status == "ok" and by["date"].status == "ok" and by["enc"].status == "ok"
    d = res.to_dict()
    assert d["verdict"] == "ok" and len(d["items"]) == 6 and d["checked_at"] == "2026-10-20"


def test_compare_mismatch_cad_and_holder():
    ex = egrn.parse_extract(EXTRACT)
    res = egrn.compare(
        ex, cad_num="50:09:0060202:108", area_m2=1200.0, seller_name="Пётр Сидоров", today=TODAY
    )
    assert res.verdict == "mismatch"
    by = {i.key: i.status for i in res.items}
    assert by["cad"] == "mismatch" and by["area"] == "mismatch" and by["holder"] == "mismatch"
    assert "кадастровый номер" in res.label and "правообладатель" in res.label


def test_compare_agent_does_not_require_name_match():
    ex = egrn.parse_extract(EXTRACT)
    res = egrn.compare(
        ex,
        cad_num="50:09:0060202:101",
        seller_name="Агент Брокеров",
        proof_kind="agent",
        today=TODAY,
    )
    assert res.verdict == "ok" and "представител" in res.label
    assert {i.key: i.status for i in res.items}["holder"] == "unknown"


def test_company_holder_stale_date_and_encumbrance():
    text = EXTRACT.replace("1.1. Иванов Иван Иванович", '1.1. ООО "Ромашка", ИНН 5044000000')
    text = text.replace("по состоянию на 10.10.2026", "по состоянию на 01.03.2026")
    text = text.replace(
        "обременение объекта недвижимости: не зарегистрировано",
        "обременение объекта недвижимости: Ипотека, № 50:09:0060202:101-50/001/2021-2 от 01.02.2021",
    )
    ex = egrn.parse_extract(text)
    assert ex.encumbrances == ["ипотека"]
    res = egrn.compare(ex, cad_num="50:09:0060202:101", seller_name="Иван Иванов", today=TODAY)
    by = {i.key: i for i in res.items}
    assert by["holder"].status == "unknown" and "юрлицо" in by["holder"].note
    assert by["date"].status == "warn" and "233 дн." in by["date"].note
    assert by["enc"].status == "warn" and "ипотека" in by["enc"].note
    assert res.verdict == "unknown" and "внимание" in res.label


def test_non_extract_document():
    res = egrn.compare(
        egrn.parse_extract("Договор купли-продажи. Стороны договорились."), cad_num="x"
    )
    assert res.verdict == "unknown" and "не похож на выписку" in res.label and res.items == []
    assert egrn.parse_extract("").is_extract is False


def test_name_match_and_mask():
    assert egrn.name_match("Иванов Иван Иванович", "Иванов Иван Иванович") == "ok"
    assert egrn.name_match("Иван Иванов", "Иванов И. И.") == "ok"
    assert egrn.name_match("Иванов Пётр", "Иванов И. И.") == "mismatch"
    assert egrn.name_match("Сидоров Пётр", "Иванов Иван Иванович") == "mismatch"
    assert egrn.name_match("Иван Иванов", "Администрация городского округа") == "unknown"
    assert egrn.name_match("Иван", "Петров Иван Сергеевич") == "unknown"  # одно слово, не фамилия
    assert egrn.name_match("Иванов", "Иванов Иван Иванович") == "ok"
    assert egrn.name_match("Петр Иванов", "Иванов Пётр Сергеевич") == "ok"
    assert egrn.name_match("", "Иванов Иван") == "unknown"
    assert egrn.mask_name("Иванов Иван Иванович") == "И***в И. И."
    assert egrn.mask_name("Ли") == "Л***"


def test_best_extract_and_summary():
    best = egrn.best_extract(["просто текст", EXTRACT])
    assert best is not None and best.cad_num == "50:09:0060202:101"
    full = {"verdict": "ok", "label": "x", "checked_at": "2026-10-20", "items": [{"key": "holder"}]}
    assert egrn.summary_only(full) == {"verdict": "ok", "label": "x", "checked_at": "2026-10-20"}
    assert egrn.summary_only(None) is None
