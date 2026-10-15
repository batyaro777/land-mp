"""Цель покупателя как действие и три уровня проверки: категория × ВРИ × терзона (запрос Антона М., 13.10)."""

from landcheck.intent import detect_purpose
from landcheck.models import CheckResult, Level, Listing, Purpose, Request
from landcheck.normalize import parcel_from_payload
from landcheck.rules import RuleContext, evaluate
from landcheck.rules.engine import BatchStats
from landcheck.rules.zones import zone_class
from landcheck.web.content import goal_summary
from landcheck.web.serialize import result_to_dict
from tests.helpers import make_payload, zone

SETTLEMENT = "Земли населенных пунктов"
AGRI = "Земли сельскохозяйственного назначения"


def run(payload, purpose):
    parcel = parcel_from_payload(payload)
    ctx = RuleContext(
        listing=Listing(cad_num=payload["cad_num"]),
        parcel=parcel,
        request=Request(purpose=purpose),
        stats=BatchStats(),
    )
    flags = evaluate(ctx)
    return parcel, {f.rule_id: f for f in flags}


# --- ВРИ: один и тот же ВРИ значит разное в зависимости от категории ----------------------
def test_lph_in_settlement_allows_house_with_note():
    _, flags = run(
        make_payload("x", category=SETTLEMENT, vri="Для ведения личного подсобного хозяйства"),
        Purpose.IZHS,
    )
    assert flags["R05"].level is Level.OK and "приусадебный" in flags["R05"].message
    assert "R04" not in flags


def test_lph_on_agri_land_stops_house():
    _, flags = run(
        make_payload("x", category=AGRI, vri="Для ведения личного подсобного хозяйства"),
        Purpose.IZHS,
    )
    assert flags["R05"].level is Level.STOP and "полевой" in flags["R05"].message
    assert flags["R04"].level is Level.STOP


def test_garden_vri_for_house_is_check_with_217fz():
    _, flags = run(make_payload("x", category=AGRI, vri="Ведение садоводства"), Purpose.IZHS)
    assert flags["R05"].level is Level.CHECK and "217-ФЗ" in flags["R05"].source
    assert flags["R04"].level is Level.CHECK  # сельхозземля, но садовый ВРИ — не «стоп»


def test_vegetable_garden_stops_any_house():
    _, flags = run(
        make_payload("x", category=SETTLEMENT, vri="Ведение огородничества"), Purpose.IZHS
    )
    assert flags["R05"].level is Level.STOP
    _, flags = run(
        make_payload("x", category=SETTLEMENT, vri="Ведение огородничества"), Purpose.SNT
    )
    assert flags["R05"].level is Level.CHECK and "садовый" in flags["R05"].message


def test_farming_on_agri_production_vri_is_clean():
    _, flags = run(
        make_payload("x", category=AGRI, vri="Для сельскохозяйственного производства"),
        Purpose.FARMING,
    )
    assert "R04" not in flags and "R05" not in flags


def test_farming_on_izhs_vri_conflicts():
    _, flags = run(make_payload("x", category=SETTLEMENT), Purpose.FARMING)
    assert flags["R05"].level is Level.STOP


def test_multifamily_on_izhs_vri_is_stop_with_grk_source():
    _, flags = run(make_payload("x", category=SETTLEMENT), Purpose.MULTIFAMILY)
    assert flags["R05"].level is Level.STOP and "ГрК" in flags["R05"].source


def test_lph_goal_on_izhs_vri_is_check_about_animals():
    _, flags = run(make_payload("x", category=SETTLEMENT), Purpose.LPH)
    assert flags["R05"].level is Level.CHECK and "скота" in flags["R05"].message


# --- терзона: класс по названию, семейство по коду, честный «проверить» без данных ----------
def test_zone_class_by_name_not_by_code():
    tz_mo = zone("Территориальные зоны", "50:09-7.1", "Ж-1", "Зона многоквартирной жилой застройки")
    tz_other = zone(
        "Территориальные зоны", "66:41-7.1", "Ж-1", "Зона индивидуальной жилой застройки"
    )
    p1 = parcel_from_payload(make_payload("x", tz=[tz_mo]))
    p2 = parcel_from_payload(make_payload("x", tz=[tz_other]))
    assert zone_class(p1.territorial_zones[0]) == "zh_high"
    assert zone_class(p2.territorial_zones[0]) == "zh_ind"


def test_zone_code_only_gives_family():
    p = parcel_from_payload(make_payload("x", tz=[zone("Территориальные зоны", "1", "СХ-1")]))
    assert zone_class(p.territorial_zones[0]) == "sx_use"
    p = parcel_from_payload(make_payload("x", tz=[zone("Территориальные зоны", "1", "П-2")]))
    assert zone_class(p.territorial_zones[0]) == "prom"


def test_house_in_multistorey_zone_is_check_and_in_individual_zone_is_ok():
    tz = [zone("Территориальные зоны", "1", "Ж-1", "Зона многоквартирной жилой застройки")]
    _, flags = run(make_payload("x", tz=tz), Purpose.IZHS)
    assert flags["R11"].level is Level.CHECK and "многоэтажной" in flags["R11"].message
    tz = [zone("Территориальные зоны", "1", "Ж-2", "Зона застройки индивидуальными жилыми домами")]
    _, flags = run(make_payload("x", tz=tz), Purpose.IZHS)
    assert flags["R11"].level is Level.OK
    # та же зона для многоквартирного дома — стоп
    _, flags = run(make_payload("x", tz=tz), Purpose.MULTIFAMILY)
    assert flags["R11"].level is Level.STOP


def test_house_in_agricultural_lands_zone_is_stop_with_grk36():
    tz = [zone("Территориальные зоны", "1", "СХ-1", "Зона сельскохозяйственных угодий")]
    _, flags = run(
        make_payload("x", category=AGRI, vri="Для сельскохозяйственного производства", tz=tz),
        Purpose.IZHS,
    )
    assert flags["R11"].level is Level.STOP and "ст. 36" in flags["R11"].source
    # а для фермы та же зона — ок
    _, flags = run(
        make_payload("x", category=AGRI, vri="Для сельскохозяйственного производства", tz=tz),
        Purpose.FARMING,
    )
    assert flags["R11"].level is Level.OK


def test_zone_without_name_asks_to_check_pzz():
    tz = [zone("Территориальные зоны", "77:05-7.5337")]
    _, flags = run(make_payload("x", tz=tz), Purpose.IZHS)
    assert flags["R11"].level is Level.CHECK and "ПЗЗ" in flags["R11"].message
    # без цели — просто справочно
    _, flags = run(make_payload("x", tz=tz), Purpose.ANY)
    assert flags["R11"].level is Level.OK


def test_parcel_in_two_zones_takes_the_worst():
    tz = [
        zone("Территориальные зоны", "1", "Ж-2", "Зона индивидуальной жилой застройки"),
        zone("Территориальные зоны", "2", "П-1", "Производственная зона"),
    ]
    _, flags = run(make_payload("x", tz=tz), Purpose.IZHS)
    assert flags["R11"].level is Level.STOP and "индивидуальной" in flags["R11"].message


# --- цель словами --------------------------------------------------------------------------
def test_detect_purpose_from_plain_wishes():
    assert detect_purpose("хочу построить дачу недалеко от Истры") is Purpose.SNT
    assert detect_purpose("построить дом для семьи и жить круглый год") is Purpose.IZHS
    assert detect_purpose("пасти коз и держать кур") is Purpose.LPH
    assert detect_purpose("выращивать картофель, небольшая ферма") is Purpose.FARMING
    assert detect_purpose("построить завод") is Purpose.INDUSTRY
    assert detect_purpose("построить пятиэтажный дом, застройщик") is Purpose.MULTIFAMILY
    assert detect_purpose("глэмпинг на берегу") is Purpose.RECREATION


# --- сводка «можно ли здесь» ---------------------------------------------------------------
def test_goal_summary_three_rows_and_worst_level():
    tz = [zone("Территориальные зоны", "1", "СХ-2", "Зона садоводства и дачного хозяйства")]
    payload = make_payload("x", category=AGRI, vri="Ведение садоводства", tz=tz)
    parcel, flags = run(payload, Purpose.IZHS)
    result = CheckResult(listing=Listing(cad_num="x"), parcel=parcel, flags=list(flags.values()))
    g = goal_summary(result_to_dict(result), Purpose.IZHS)
    assert g["activity"].startswith("Построить жилой дом")
    assert [r["rule_id"] for r in g["rows"]] == ["R04", "R05", "R11"]
    # сельхозземля под дом — исключение только благодаря садовому ВРИ (217-ФЗ): «проверить», не «стоп»
    assert g["rows"][0]["level"] == "проверить" and "217-ФЗ" in g["rows"][0]["source"]
    assert g["rows"][1]["level"] == "проверить" and g["rows"][2]["level"] == "проверить"
    assert g["level"] == "проверить"
    assert goal_summary(result_to_dict(result), Purpose.ANY) is None


# --- ввод: подборка из 30 номеров с CRLF не должна приниматься за путь к файлу --------------
def test_long_multiline_input_is_text_not_path():
    from landcheck.inputs import load_listings

    numbers = [f"04:05:0{i:05d}:{100 + i}" for i in range(32)]
    text = "\r\n".join(numbers) + " "
    assert len(text) > 255
    listings = load_listings(text)
    assert [item.cad_num for item in listings] == numbers
    # короткий путь к несуществующему файлу тоже читается как текст
    assert load_listings("50:09:0060202:101; 2 500 000; 10 сот")[0].price_rub == 2_500_000


def test_public_use_vri_is_stop_for_any_goal():
    for purpose in (Purpose.IZHS, Purpose.COMMERCE, Purpose.FARMING):
        _, flags = run(
            make_payload("x", category=SETTLEMENT, vri="Земли общего пользования"), purpose
        )
        assert flags["R05"].level is Level.STOP and "ст. 85" in flags["R05"].source
