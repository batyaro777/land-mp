from datetime import UTC, datetime, timedelta

from landcheck.models import Level, Listing, Purpose, Request
from landcheck.normalize import parcel_from_payload
from landcheck.rules import RuleContext, evaluate
from landcheck.rules.engine import BatchStats
from landcheck.rules.purpose import vri_match
from tests.helpers import make_payload, zone


def run(payload, listing=None, request=None, stats=None, errors=None):
    parcel = parcel_from_payload(payload)
    ctx = RuleContext(
        listing=listing or Listing(cad_num=payload["cad_num"]),
        parcel=parcel,
        request=request or Request(purpose=Purpose.IZHS),
        stats=stats or BatchStats(),
        fetch_errors=errors or [],
    )
    return {f.rule_id: f for f in evaluate(ctx)}


def test_clean_izhs_parcel_has_no_stop_or_check():
    flags = run(make_payload("50:09:0060202:101", tz=[zone("Территориальные зоны", "1", "Ж-1")]))
    assert all(f.level is Level.OK for f in flags.values()), flags


def test_not_found_is_stop():
    flags = run(make_payload("50:09:0060202:999", found=False))
    assert flags["R01"].level is Level.STOP


def test_fetch_error_without_data_is_check_not_stop():
    flags = run(make_payload("50:09:0060202:999", found=False), errors=["timeout"])
    assert "R01" not in flags and flags["R00"].level is Level.CHECK


def test_archived_status_is_stop():
    flags = run(make_payload("50:09:0060202:105", status="Архивный"))
    assert flags["R02"].level is Level.STOP


def test_temporary_status_is_check():
    assert run(make_payload("x", status="Временный"))["R02"].level is Level.CHECK


def test_no_borders_is_stop_with_487fz_source():
    flags = run(make_payload("50:09:0060202:103", borders=False))
    assert flags["R03"].level is Level.STOP and "487-ФЗ" in flags["R03"].source


def test_agri_land_for_izhs_is_stop():
    flags = run(
        make_payload(
            "x",
            category="Земли сельскохозяйственного назначения",
            vri="Для сельскохозяйственного производства",
        )
    )
    assert flags["R04"].level is Level.STOP
    assert flags["R05"].level is Level.STOP


def test_agri_land_for_snt_is_ok_when_vri_matches():
    flags = run(
        make_payload(
            "x", category="Земли сельскохозяйственного назначения", vri="Ведение садоводства"
        ),
        request=Request(purpose=Purpose.SNT),
    )
    assert "R04" not in flags and "R05" not in flags


def test_resort_district_alone_is_fact_not_risk():
    # Округ санитарной охраны курорта накрывает весь Сочи: не риск (Влад, зум 28.10, п. 1.4)
    p = make_payload(
        "x",
        zouit=[
            zone(
                "Иные ЗОУИТ",
                "0:0-6.3",
                "Округ санитарной (горно-санитарной) охраны лечебно-оздоровительных местностей, курортов и природных лечебных ресурсов",
                "Граница округа санитарной охраны лечебного климата курорта федерального значения Сочи",
            )
        ],
    )
    flags = run(p)
    assert flags["R10"].level is Level.OK and "на весь курорт" in flags["R10"].message
    assert flags["R10"].details["wide"] and flags["R10"].details["zouit"] == []
    # вместе с реальной зоной — флаг по реальной зоне, курорт в details
    p = make_payload(
        "x",
        zouit=[
            zone("Иные ЗОУИТ", "1", "Округ санитарной охраны курорта", "Сочи"),
            zone(
                "ЗОУИТ объектов энергетики",
                "2",
                "Охранная зона инженерных коммуникаций",
                "ЛЭП 10 кВ",
            ),
        ],
    )
    f = run(p)["R10"]
    assert f.level is Level.CHECK and "ЗОУИТ (1)" in f.message and "ЛЭП" in f.message


def test_vri_from_tab_is_used():
    flags = run(
        make_payload("x", vri=None, vri_tab=["Для индивидуального жилищного строительства (2.1)"])
    )
    assert "R05" not in flags


def test_vri_unknown_is_check_and_conflict_is_stop():
    assert (
        run(make_payload("x", vri="Для размещения объектов неизвестного типа"))["R05"].level
        is Level.CHECK
    )
    assert run(make_payload("x", vri="Магазины"))["R05"].level is Level.STOP


def test_vri_code_matching_does_not_confuse_12_1_with_2_1():
    assert vri_match(Purpose.IZHS, "Код 12.1 — ритуальная деятельность") == "conflict"
    assert vri_match(Purpose.IZHS, "код 2.1") == "match"


def test_area_mismatch_levels():
    p = make_payload("x", area=1000, specified_area=1000)
    assert "R06" not in run(p, Listing(cad_num="x", declared_area_m2=1050))
    assert run(p, Listing(cad_num="x", declared_area_m2=1150))["R06"].level is Level.CHECK
    assert run(p, Listing(cad_num="x", declared_area_m2=1400))["R06"].level is Level.STOP


def test_declared_only_area_is_check():
    p = make_payload("x", specified_area=None, declared_area=1000, area=1000)
    assert run(p)["R07"].level is Level.CHECK


def test_price_vs_cadastral():
    p = make_payload("x", cost=1_500_000)
    assert run(p, Listing(cad_num="x", price_rub=300_000))["R08"].level is Level.CHECK
    assert run(p, Listing(cad_num="x", price_rub=9_000_000))["R08"].level is Level.OK
    assert "R08" not in run(p, Listing(cad_num="x", price_rub=2_000_000))


def test_price_vs_median_requires_samples():
    p = make_payload("x", area=1000, specified_area=1000)
    cheap = Listing(cad_num="x", price_rub=500_000)  # 50 000 ₽/сот.
    assert "R09" not in run(
        p, cheap, stats=BatchStats(median_price_per_sotka=250_000, priced_count=3)
    )
    f = run(p, cheap, stats=BatchStats(median_price_per_sotka=250_000, priced_count=8))["R09"]
    assert f.level is Level.CHECK


def test_zouit_is_check_and_absent_tz_is_check():
    p = make_payload(
        "x",
        zouit=[
            zone("ЗОУИТ объектов энергетики, связи, транспорта", "50:09-6.1", "Охранная зона ЛЭП")
        ],
        tz=[],
    )
    flags = run(p)
    assert flags["R10"].level is Level.CHECK and "ЛЭП" in flags["R10"].message
    assert flags["R11"].level is Level.CHECK


def test_zones_not_checked_produce_no_flags():
    flags = run(make_payload("x", zouit_checked=False, tz_checked=False))
    assert "R10" not in flags and "R11" not in flags


def test_public_ownership_is_check():
    assert (
        run(make_payload("x", ownership="Собственность публично-правовых образований"))["R12"].level
        is Level.CHECK
    )
    assert run(make_payload("x", ownership=None))["R12"].level is Level.OK
    assert "R12" not in run(make_payload("x", ownership="Частная собственность"))


def test_budget_and_area_range_are_info():
    p = make_payload("x", area=600, specified_area=600)
    req = Request(purpose=Purpose.IZHS, budget_rub=2_000_000, min_area_m2=800)
    flags = run(p, Listing(cad_num="x", price_rub=2_500_000), request=req)
    assert flags["R14"].level is Level.OK and flags["R15"].level is Level.OK


def test_stale_data_is_info():
    p = make_payload("x", fetched_at=datetime.now(UTC) - timedelta(days=30))
    assert run(p)["R16"].level is Level.OK


def test_sanitary_zone_is_stop_for_housing_but_check_for_industry():
    p = make_payload(
        "x",
        zouit=[
            zone(
                "Зоны с особыми условиями использования территории",
                "63:01-6.5119",
                "Санитарно-защитная зона предприятий, сооружений и иных объектов",
                "СЗЗ АО «Пример»",
            )
        ],
        tz=[zone("Территориальные зоны", "63:01-7.1")],
    )
    assert run(p)["R10"].level is Level.STOP
    assert run(p, request=Request(purpose=Purpose.INDUSTRY))["R10"].level is Level.CHECK
    assert "СанПиН" in run(p)["R10"].source
