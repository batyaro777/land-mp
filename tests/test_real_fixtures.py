"""Реальные ответы НСПД (сняты 10.10.2026 через туннель, геометрия сжата до bbox).

Фиксируют фактический формат: имена полей ЗОУИТ, статус «Ранее учтенный»,
пустую форму собственности, слой «ПКК. Территориальные зоны» с одним реестровым номером.
"""

from pathlib import Path

from landcheck.inputs import load_listings
from landcheck.models import Level, Purpose, Request
from landcheck.pipeline import check_batch
from landcheck.providers.fixtures import FixtureProvider
from landcheck.rules.purpose import category_class

REAL = Path(__file__).parent / "fixtures" / "real"


def test_moscow_parcel_normalizes_and_flags():
    prov = FixtureProvider(REAL, strict=True)
    batch = check_batch(load_listings("77:05:0001005:19"), Request(purpose=Purpose.IZHS), prov)
    r = batch.results[0]
    p = r.parcel
    assert p.found and p.status == "Ранее учтенный"
    assert category_class(p.category) == "settlement"
    assert p.best_area_m2 == 1434.0 and p.has_borders
    assert p.cadastral_cost_rub == 176626023.78
    assert p.ownership_type is None
    # ЗОУИТ: вид из type_zone, название из name_by_doc, текст ограничений отдельно
    kinds = {z.kind for z in p.zouit}
    assert kinds == {
        "Зоны охраны объектов культурного наследия",
        "Охранная зона инженерных коммуникаций",
    }
    assert any(z.name and z.name.startswith("Охранная зона КЛ 110 кВ") for z in p.zouit)
    assert all(z.restrictions for z in p.zouit)
    assert all(len(z.describe()) <= 120 for z in p.zouit)
    assert p.territorial_zones[0].reg_number == "77:05-7.5337"
    flags = {f.rule_id: f for f in r.flags}
    assert flags["R05"].level is Level.CHECK  # ВРИ «под кафе» не подтверждает ИЖС
    assert flags["R10"].level is Level.CHECK
    assert flags["R12"].level is Level.OK  # пустая форма собственности — справочно
    assert "R03" not in flags
    # НСПД отдаёт по терзоне только реестровый номер — честно просим сверить с ПЗЗ
    assert flags["R11"].level is Level.CHECK and "ПЗЗ" in flags["R11"].message
    assert flags["R11"].details["classes"] == [None]
    assert r.verdict is Level.CHECK


def test_samara_parcel_sanitary_zone_stops_housing():
    prov = FixtureProvider(REAL, strict=True)
    izhs = check_batch(load_listings("63:01:0810003:510"), Request(purpose=Purpose.IZHS), prov)
    r = izhs.results[0]
    p = r.parcel
    assert p.found and p.best_area_m2 == 921.0 and len(p.zouit) == 4
    assert p.territorial_zone_checked and p.territorial_zones == []
    flags = {f.rule_id: f for f in r.flags}
    assert flags["R10"].level is Level.STOP and "санитарно-защитной" in flags["R10"].message
    assert flags["R11"].level is Level.CHECK  # терзона в НСПД не опубликована
    assert flags["R05"].level is Level.STOP  # «многоэтажная застройка» несовместима с ИЖС
    assert r.verdict is Level.STOP
    # Для производства та же СЗЗ — «проверить», а не «стоп»
    ind = check_batch(load_listings("63:01:0810003:510"), Request(purpose=Purpose.INDUSTRY), prov)
    assert {f.rule_id: f for f in ind.results[0].flags}["R10"].level is Level.CHECK
