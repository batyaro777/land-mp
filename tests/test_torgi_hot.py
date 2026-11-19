"""«Горячие предложения» (ТЗ v5, зум 18.11.2026): отбор, три сценария, сверка цены рынка."""

from __future__ import annotations

from landcheck import torgi_hot as th
from landcheck import torgi_scan as ts
from tests.test_torgi_scan import TODAY, _district, _lot

# Рынок района задан вручную: 80 тыс. ₽ за сотку → участок 10 соток стоит 800 тыс.
LAYER = [{"scope": "40:13", "kind": "sale", "vri_group": "any", "price_mid": 80_000}]


def _active(lot_id: str = "A1", *, start: int = 200_000, bidd_end: str = "2026-11-25", **kw):
    lot = _lot(
        lot_id,
        f"40:13:120101:{70 + len(lot_id)}",
        area=1000,
        start=start,
        fin=None,
        status="APPLICATIONS_SUBMISSION",
        bidd_end=bidd_end,
    )
    lot.update({"zones_state": "ok", "zones": []})
    lot.update(kw)
    return lot


def _rows(*lots, layer=LAYER, params: ts.ScanParams | None = None) -> list[dict]:
    return ts.scan([*_district(), *lots], params, layer=layer, today=TODAY)["rows"]


def test_hot_counts_yield_from_expected_winner_price_not_from_start():
    rows = _rows(_active())
    res = th.select(rows)
    assert [i["row"]["id"] for i in res["hot"]] == ["A1"] and not res["check"]
    item = res["hot"][0]
    sc = item["sc"]
    # в квартале лота все торги шли с борьбой, цена росла в полтора раза: вход 300, а не 200 тыс.
    assert sc["expected"]["entry"] == 300_000 and sc["alone"]["entry"] == 200_000
    # (800 − 300) × 0,87 = 435 тыс. на 300 вложенных → 145 % за 12 месяцев
    assert sc["expected"]["profit"] == 435_000 and sc["expected"]["annual"] == 1.45
    assert sc["alone"]["annual"] == 2.61 and sc["hold"] == 12
    # осторожно: продаём по низу рынка (680 тыс.) и в полтора раза дольше
    assert sc["cautious"]["sale"] == 680_000 and sc["cautious"]["months"] == 18
    assert 0 < sc["cautious"]["annual"] < sc["expected"]["annual"]
    # цена отсечения: 800 000 × 0,87 / 1,87 — выше неё 100 % годовых уже не выходит
    assert item["cutoff"] == 372_193
    assert item["cls"] == "A" and item["cls_why"] == []
    assert res["classes"] == {"A": 1, "B": 0, "C": 0}
    left = {f["key"]: f["left"] for f in res["funnel"]}
    assert left["type"] == 1 and left["price"] == 1 and "kind" not in left and res["total"] == 1


def test_implausible_market_goes_to_manual_check_not_to_the_top():
    """Прод, 19.11.2026: у первых 15 лотов по доходности «рынок» был выше старта в 4–40 раз."""
    cheap = _active("A2", start=100_000)  # рынок 80 тыс. за сотку против старта 10 тыс.
    by_cad = _active("A33", cad_cost=100_000)  # кадастровая 10 тыс. за сотку, рынок — 80
    res = th.select(_rows(_active(), cheap, by_cad))
    assert [i["row"]["id"] for i in res["hot"]] == ["A1"]
    check = {i["row"]["id"]: i["fail"] for i in res["check"]}
    assert set(check) == {"A2", "A33"}
    assert "в 8 раз выше старта" in check["A2"][0] and "несопоставимых" in check["A2"][0]
    assert "в 8 раз выше кадастровой стоимости" in check["A33"][0]
    # пока цена не подтверждена, это гипотеза: класс C, причина — первой
    assert {i["cls"] for i in res["check"]} == {"C"}
    assert all(i["cls_why"][0] == i["fail"][0] for i in res["check"])
    left = {f["key"]: f["left"] for f in res["funnel"]}
    assert left["zones"] == 3 and left["price"] == 1


def test_threshold_cap_deadline_and_hint_for_lower_threshold():
    layer = [{"scope": "40:13", "kind": "sale", "vri_group": "any", "price_mid": 60_000}]
    rows = _rows(_active(), layer=layer)  # (600 − 300) × 0,87 / 300 = 87 % годовых
    res = th.select(rows)
    assert not res["hot"] and res["relaxed"] == {0.7: 1, 0.5: 1, 0.3: 1}
    assert {f["key"]: f["left"] for f in res["funnel"]}["annual"] == 0
    assert len(th.select(rows, params=th.HotParams(min_annual=0.7))["hot"]) == 1

    rows = _rows(_active())
    # лимит сравнивается с ожидаемой ценой победителя (300 тыс.), а не со стартом (200 тыс.)
    capped = th.select(rows, params=th.HotParams(price_cap=250_000))
    assert not capped["hot"] and {f["key"]: f["left"] for f in capped["funnel"]}["cap"] == 0
    assert len(th.select(rows, params=th.HotParams(price_cap=None))["hot"]) == 1
    # расходы на сделку входят в цену входа
    sp = ts.ScanParams(extra=60_000)
    costly = th.select(_rows(_active(), params=sp), sp, th.HotParams(price_cap=350_000))
    assert not costly["hot"]
    # прибыль не меньше заданной
    assert not th.select(rows, params=th.HotParams(min_profit=500_000))["hot"]
    assert th.select(rows, params=th.HotParams(min_profit=400_000))["hot"]
    # до конца приёма заявок два дня — не берём
    late = th.select(_rows(_active(bidd_end="2026-11-12")))
    assert not late["hot"] and {f["key"]: f["left"] for f in late["funnel"]}["time"] == 0
    # только аренда — продажа отсеяна своей строкой воронки
    rent_only = th.select(rows, params=th.HotParams(kinds=("rent",)))
    assert not rent_only["hot"] and {f["key"] for f in rent_only["funnel"]} >= {"kind"}


def test_class_depends_on_zones_and_blocking_zones_remove_the_lot():
    unchecked = _active("A2", zones_state=None, zones=None)
    with_zone = _active("A33", zones=["охранная зона ЛЭП 10 кВ"])
    oopt = _active("A444", zones=["ООПТ: памятник природы"])
    res = th.select(_rows(_active(), unchecked, with_zone, oopt))
    cls = {i["row"]["id"]: (i["cls"], i["cls_why"]) for i in res["hot"]}
    assert set(cls) == {"A1", "A2", "A33"} and res["hot"][0]["row"]["id"] == "A1"
    assert cls["A2"] == ("B", ["обременения ещё не проверены"])
    assert cls["A33"][0] == "B" and "охранная зона ЛЭП" in cls["A33"][1][0]
    left = {f["key"]: f["left"] for f in res["funnel"]}
    assert left["zones"] == 3  # участок в ООПТ не прошёл


def test_forest_lots_have_no_yield_and_are_hidden_by_default():
    """Зум 18.11, 01:12: 2 382 % годовых у лесничества; на проде — до 58 730 %."""
    forest = _active("F1", bidd_type="200FZ")
    lots = [*_district(), _active(), forest]
    result = ts.scan(lots, layer=LAYER, today=TODAY)
    assert [r["id"] for r in result["rows"]] == ["A1"] and result["hidden"]["forest"] == 1
    shown = ts.scan(lots, ts.ScanParams(forest=True), layer=LAYER, today=TODAY)
    s = next(r["s"] for r in shown["rows"] if r["id"] == "F1")
    assert s["verdict"] == "unknown" and s["market"] is None and s["annual_start"] is None
    assert s["why"] == [ts.FOREST_TEXT]
    # и в «горячие» лесной лот не попадает, даже если его показать
    assert [i["row"]["id"] for i in th.select(shown["rows"])["hot"]] == ["A1"]


def test_price_check_for_rent_compares_ownership_price_with_cadastre():
    row = {
        "kind": "rent",
        "price_start": 3_000,  # годовая плата: с ценой переуступки не сверяется
        "area_m2": 1000.0,
        "cad_cost": 100_000,
        "s": {
            "market": {
                "mid_m2": 300.0,
                "assign_ratio": 0.5,
                "source": "rr",
                "deals_n": 3,
                "source_label": "переуступка = 50 % цены в собственности; сделки Росреестра",
                "quarter_ref": {"mid_m2": 3200.0, "n": 5, "ratio": 4.2},
                "alt": {"label": "объявления: 6 в радиусе 5 км", "mid_m2": 900.0},
            }
        },
    }
    fail, warn = th.price_check(row)
    # участок в собственности 600 ₽/м² против кадастровых 100 — в 6 раз
    assert len(fail) == 1 and "в 6 раз выше кадастровой" in fail[0]
    assert any("взят район" in w for w in warn)
    assert any("расходятся в 3.0 раза" in w for w in warn)
    assert any("меньше пяти" in w for w in warn)
    row["cad_cost"] = 200_000
    assert th.price_check(row)[0] == []
