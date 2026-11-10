"""Анализатор торгов (решение зума 09.11; ТЗ v3, п. 9): строка лота, группы назначения,
ликвидность по району и кварталу, оценка идущего лота, вердикт и причины, выгрузки."""

from __future__ import annotations

from datetime import date

import pytest

from landcheck import torgi_scan as ts
from tests.test_torgi import _card, _notice

TODAY = date(2026, 11, 10)


def _lot(lot_id: str, cad: str, **kw) -> dict:
    """Строка хранилища из синтетической карточки + поля, которых в API нет."""
    extra = {
        k: kw.pop(k)
        for k in ("applied", "admitted", "cad_cost", "bidd_end", "has_gpzu", "price_step")
        if k in kw
    }
    row = ts.lot_row(_card(lot_id, cad, **kw))
    row.update(extra)
    return row


def _district(prefix: str = "40:13", *, sold: int = 6, single: int = 2, none: int = 2) -> list:
    """История района: `sold` состоявшихся (1000 м², 100 → 150 тыс., 150 ₽/м²), `single` с
    одной заявкой, `none` без заявок."""
    lots = []
    for i in range(sold):
        lots.append(
            _lot(f"S{i}", f"{prefix}:120101:{i + 1}", area=1000, start=100_000, fin=150_000)
            | {"applied": 3}
        )
    for i in range(single):
        lots.append(
            _lot(f"O{i}", f"{prefix}:120102:{i + 1}", fin=None, status="FAILED") | {"applied": 1}
        )
    for i in range(none):
        lots.append(
            _lot(f"N{i}", f"{prefix}:120103:{i + 1}", fin=None, status="FAILED") | {"applied": 0}
        )
    return lots


def test_lot_row_from_search_hit_and_card():
    hit = _card("L_1", "40:13:120101:22", area=1200, start=90_000, fin=None, status="PUBLISHED")
    hit["biddEndTime"] = "2026-11-30T06:00:00Z"
    row = ts.lot_row(hit)
    assert row["id"] == "L_1" and row["region"] == "40"
    assert row["district"] == "40:13" and row["quarter"] == "40:13:120101"
    assert row["kind"] == "sale" and row["status"] == "PUBLISHED" and row["form"] == "EA"
    assert row["area_m2"] == 1200 and row["price_start"] == 90_000 and row["price_fin"] is None
    assert row["bidd_end"] == "2026-11-30" and row["vri_group"] == "housing"
    assert row["has_gpzu"] is None and row["applied"] is None  # в поиске этого нет
    card = {
        **hit,
        "lotStatus": "SUCCEED",
        "priceFin": 180_000,
        "priceStep": 2_700,
        "lotAttachments": [{"fileId": "f1", "attachmentTypeCode": "Plan_lands"}],
        "protocols": [
            {
                "participantAdmissions": [
                    {"admissionStatus": "ADMITTED", "participantName": "Иванов Иван Иванович"},
                    {"admissionStatus": "ADMITTED"},
                    {"admissionStatus": "REJECTED"},
                ],
                "results": [
                    {"isFirst": True, "participantPrice": 180_000, "orgType": "INDIVIDUAL_PERSON"},
                    {"isSecond": True, "participantPrice": 177_300},
                ],
            }
        ],
    }
    full = ts.lot_row(card)
    assert full["price_fin"] == 180_000 and full["price_step"] == 2_700
    assert full["applied"] == 3 and full["admitted"] == 2 and full["second_price"] == 177_300
    assert full["winner_type"] == "физлицо" and full["has_gpzu"] is True
    assert "Иванов" not in str(full)  # имён участников в хранилище нет
    # пустое из поиска не затирает данные карточки, статус обновляется
    merged = ts.merge_row(full, ts.lot_row({**hit, "lotStatus": "SUCCEED"}))
    assert merged["price_fin"] == 180_000 and merged["applied"] == 3 and merged["has_gpzu"] is True
    assert ts.lot_row({"lotName": "без id"}) is None


@pytest.mark.parametrize(
    ("vri", "code", "group"),
    [
        ("Для индивидуального жилищного строительства", None, "housing"),
        (
            "для ведения личного подсобного хозяйства (приусадебный земельный участок)",
            None,
            "housing",
        ),
        ("Ведение садоводства", None, "garden"),
        ("ведение огородничества", "13.1", "garden"),
        ("Сенокошение", "1.19", "agri"),
        ("Для сельскохозяйственного производства", None, "agri"),
        ("Магазины", "4.4", "commerce"),
        ("Склады", "6.9", "industry"),
        ("Хранение автотранспорта", "2.7.1", "garage"),
        ("Размещение гаражей для собственных нужд", None, "garage"),
        ("Отдых (рекреация)", "5.0", "recreation"),
        ("что-то своё из местных правил", None, "other"),
    ],
)
def test_vri_group(vri, code, group):
    assert ts.vri_group(vri, code) == group


def test_vri_group_falls_back_to_category():
    assert ts.vri_group(None, None, "Земли сельскохозяйственного назначения") == "agri"


def test_liquidity_stats_and_index():
    lots = _district()
    s = ts.stats_of(lots)
    assert (s["closed"], s["sold"], s["single"], s["none"]) == (10, 6, 2, 2)
    assert s["sell_through"] == 0.8  # нашли хозяина 8 из 10: и борьба, и единственный участник
    assert s["growth_median"] == 1.5 and s["applied_median"] == 3.0
    assert s["sale"]["n"] == 6 and s["sale"]["median_m2"] == 150.0
    assert s["index"] >= 55 and s["label"] == "ликвид"
    # мёртвый район: все торги без заявок
    dead = ts.stats_of(_district(sold=0, single=0, none=6))
    assert dead["sell_through"] == 0 and dead["index"] == 0 and dead["label"] == "неликвид"
    # мало данных — индекс не считаем
    assert ts.stats_of(_district(sold=1, single=0, none=0))["label"] == "мало данных"


def test_unread_failed_cards_are_split_proportionally():
    lots = _district(sold=2, single=2, none=2)
    # ещё четыре «не состоялись» без прочитанной карточки: число заявок неизвестно
    for i in range(4):
        lots.append(_lot(f"U{i}", f"40:13:120104:{i + 1}", fin=None, status="FAILED"))
    s = ts.stats_of(lots)
    assert s["failed_unknown"] == 4 and s["closed"] == 10
    # известные поровну (2 одна заявка / 2 без заявок) → из четырёх неизвестных считаем 2
    assert s["sell_through"] == 0.6


def test_liquidity_table_by_district_and_quarter():
    lots = _district("40:13") + _district("40:10", sold=0, single=0, none=5)
    rows = ts.liquidity(lots)
    assert [r["key"] for r in rows] == ["40:13", "40:10"]  # ликвидные первыми
    quarters = {r["key"]: r for r in ts.liquidity(lots, level="quarter")}
    assert quarters["40:13:120101"]["sold"] == 6 and quarters["40:13:120103"]["none"] == 2
    assert ts.liquidity(lots, group="agri") == []  # в выборке только жильё


def test_score_go_when_limit_beats_expected_price():
    # Рынок района задан вручную: 60 тыс. ₽ за сотку → 10 соток = 600 тыс.
    layer = [{"scope": "40:13", "kind": "sale", "vri_group": "any", "price_mid": 60_000}]
    lot = _lot(
        "A1",
        "40:13:120101:77",
        area=1000,
        start=100_000,
        fin=None,
        status="APPLICATIONS_SUBMISSION",
        bidd_end="2026-11-25",
        price_step=3_000,
        has_gpzu=True,
    )
    stats = ts.index_by(_district())
    s = ts.score_lot(lot, stats, ts.ScanParams(target_roi=1.0), layer=layer, today=TODAY)
    assert s["sale"] == {"min": 510_000, "mid": 600_000, "max": 690_000}
    # предел при 100 %: 600 000 × 0,87 / 1,87 = 279 144
    assert s["limit"] == 279_144 and s["safe"] == 237_273
    assert s["market"]["source"] == "manual" and s["confidence"] == "высокая"
    # ожидаемая цена победителя: в квартале лота все шесть торгов прошли с борьбой, рост ×1,5
    assert s["expected_price"] == 150_000
    assert s["verdict"] == "go" and s["verdict_label"] == "интересен"
    assert s["steps_left"] == 59 and s["headroom"] == 2.79
    assert s["roi_start"] == 4.35 and s["days_left"] == 15
    assert any("ГПЗУ" in p for p in s["plus"]) and any("ликвидный" in p for p in s["plus"])
    assert not s["stop"]


def test_score_edge_and_no_and_unknown():
    stats = ts.index_by(_district())
    base = dict(area=1000, fin=None, status="PUBLISHED", bidd_end="2026-11-25")
    # ориентир — только итоги торгов района (150 ₽/м² → 150 тыс.): предел 69 786
    cheap = _lot("B1", "40:13:120105:1", start=60_000, **base)
    s = ts.score_lot(cheap, stats, ts.ScanParams(target_roi=1.0), today=TODAY)
    assert s["market"]["source"] == "auctions" and s["limit"] == 69_786
    # предел выше старта, но ниже ожидаемой цены победителя (87 тыс.) → на грани
    assert s["verdict"] == "edge" and any("слабой конкуренции" in m for m in s["minus"])
    # при пороге 40 % тот же лот проходит: предел 102 756 выше ожидаемых 87 тыс.
    s40 = ts.score_lot(cheap, stats, ts.ScanParams(target_roi=0.4), today=TODAY)
    assert s40["limit"] == 102_756 and s40["verdict"] == "go"
    # старт уже выше предела
    dear = _lot("B2", "40:13:120105:2", start=140_000, **base)
    s = ts.score_lot(dear, stats, ts.ScanParams(target_roi=1.0), today=TODAY)
    assert s["verdict"] == "no" and any("старт выше предельной" in m for m in s["minus"])
    # район без истории и без слоя рынка — вердикта нет, сказано, чего не хватает
    lone = _lot("B3", "40:99:000001:1", start=50_000, **base)
    s = ts.score_lot(lone, stats, today=TODAY)
    assert s["verdict"] == "unknown" and s["limit"] is None
    assert any("нет цены перепродажи" in a for a in s["ask"])


def test_score_stoppers_deadline_budget_annulled():
    stats = ts.index_by(_district())
    lot = _lot(
        "C1",
        "40:13:120105:3",
        area=1000,
        start=60_000,
        fin=None,
        status="PUBLISHED",
        bidd_end="2026-11-11",
    )
    s = ts.score_lot(lot, stats, ts.ScanParams(target_roi=0.4), today=TODAY)
    assert s["verdict"] == "no" and any("не успеть" in x for x in s["stop"])
    late = {**lot, "bidd_end": "2026-11-01"}
    assert "приём заявок закончен" in ts.score_lot(late, stats, today=TODAY)["stop"]
    ok = {**lot, "bidd_end": "2026-11-30"}
    s = ts.score_lot(ok, stats, ts.ScanParams(target_roi=0.4, budget=50_000), today=TODAY)
    assert s["verdict"] == "no" and any("выше бюджета" in x for x in s["stop"])
    s = ts.score_lot({**ok, "annulled": True}, stats, ts.ScanParams(target_roi=0.4), today=TODAY)
    assert s["verdict"] == "no" and any("аннулировано" in x for x in s["stop"])


def test_score_illiquid_district_and_repeats_and_cadastre():
    dead = _district("40:10", sold=0, single=0, none=6)
    # тот же участок уже дважды выставляли без заявок
    dead += [
        _lot(f"R{i}", "40:10:160500:5", fin=None, status="FAILED") | {"applied": 0}
        for i in range(2)
    ]
    lot = _lot(
        "D1",
        "40:10:160500:5",
        area=1000,
        start=40_000,
        fin=None,
        status="PUBLISHED",
        bidd_end="2026-11-30",
        cad_cost=100_000,
    )
    layer = [{"scope": "40:10", "kind": "sale", "vri_group": "any", "price_mid": 30_000}]
    r = ts.scan(dead + [lot], ts.ScanParams(target_roi=0.4), layer=layer, today=TODAY)
    assert r["total"] == 1 and r["counts"]["edge"] == 1
    s = r["rows"][0]["s"]
    # предел есть и выше старта, но район неликвидный и участок не берут — только «на грани»
    assert s["limit"] > 40_000 and s["verdict"] == "edge" and s["repeats"] == 2
    assert any("неликвидный" in m for m in s["minus"])
    assert any("без результата: 2 раз" in m for m in s["minus"])
    assert any("ниже кадастровой стоимости: 40%" in p for p in s["plus"])
    # без слоя рынка и без состоявшихся торгов остаётся кадастр — нижний ориентир, не «интересен»
    r = ts.scan(dead + [lot], ts.ScanParams(target_roi=0.4), today=TODAY)
    s = r["rows"][0]["s"]
    assert s["market"]["source"] == "cadastre" and s["base"] == "min" and s["verdict"] == "edge"


def test_rent_lot_needs_assignment_price():
    stats = ts.index_by(_district())
    lot = _lot(
        "E1",
        "40:13:120101:90",
        area=900,
        start=38_000,
        fin=None,
        kind="rent",
        status="APPLICATIONS_SUBMISSION",
        bidd_end="2026-11-30",
        cad_cost=400_000,
    )
    s = ts.score_lot(lot, stats, ts.ScanParams(target_roi=0.4), today=TODAY)
    assert s["verdict"] == "unknown" and any("цена переуступки" in a for a in s["ask"])
    assert any("9.5% кадастровой стоимости" in p for p in s["plus"])
    # итоги аренды на торгах — это годовая плата, а не цена переуступки: оценкой перепродажи
    # не становятся, но показывают, дёшев ли старт
    rented = [
        _lot(f"RT{i}", f"40:13:120101:{400 + i}", area=1000, start=30_000, fin=60_000, kind="rent")
        for i in range(4)
    ]
    stats_r = ts.index_by(_district() + rented)
    s = ts.score_lot(lot, stats_r, ts.ScanParams(target_roi=0.4), today=TODAY)
    assert s["verdict"] == "unknown" and s["market"] is None and s["limit"] is None
    assert any("аренда уходит по 6\u202f000 ₽" in p for p in s["plus"])
    # пример эксперта: аренда с торгов 38 тыс. в год → переуступка за 200 тыс.
    layer = [
        {
            "scope": "40:13",
            "kind": "rent",
            "vri_group": "housing",
            "price_mid": 22_222,
            "exposure_days": 30,
            "source": "deals",
        }
    ]
    s = ts.score_lot(lot, stats, ts.ScanParams(target_roi=0.4, tax=0.0), layer=layer, today=TODAY)
    assert s["sale"]["mid"] == 199_998 and s["limit"] == 142_856
    assert s["market"]["source"] == "deals" and s["hold_months"] == 3
    assert s["roi_start"] == 4.263 and s["verdict"] == "go"


def test_market_layer_prefers_quarter_and_manual_over_ads():
    lot = _lot("F1", "40:13:120101:5", area=1000, start=10_000, fin=None, status="PUBLISHED")
    layer = [
        {"scope": "40:13", "kind": "sale", "vri_group": "any", "price_mid": 50_000},
        {
            "scope": "40:13:120101",
            "kind": "sale",
            "vri_group": "any",
            "price_mid": 80_000,
            "source": "ads",
        },
        {
            "scope": "40:13:120101",
            "kind": "sale",
            "vri_group": "housing",
            "price_mid": 90_000,
            "price_min": 70_000,
            "price_max": 110_000,
            "liquid": "liquid",
        },
        {"scope": "40:13:120101", "kind": "sale", "vri_group": "agri", "price_mid": 5_000},
    ]
    m = ts.market_for(lot, {}, layer)
    assert m["mid_m2"] == 900 and m["min_m2"] == 700 and m["max_m2"] == 1100
    assert m["source"] == "manual" and "40:13:120101" in m["source_label"]
    # без записи по кварталу берётся район
    assert ts.market_for(lot, {}, layer[:1])["mid_m2"] == 500


def test_scan_filters_sorts_and_exports(tmp_path):
    lots = _district()
    layer = [{"scope": "40:13", "kind": "sale", "vri_group": "any", "price_mid": 60_000}]
    common = dict(fin=None, status="PUBLISHED", bidd_end="2026-11-30")
    lots += [
        _lot("G1", "40:13:120101:201", area=1000, start=100_000, **common)
        | {"lat": 54.5, "lon": 36.2},
        _lot("G2", "40:13:120101:202", area=1000, start=500_000, **common),
        _lot(
            "G3",
            "40:13:120101:203",
            area=50_000,
            start=90_000,
            vri="Для сельскохозяйственного производства",
            **common,
        ),
        _lot("G4", "40:13:120101:204", area=800, start=20_000, kind="rent", **common),
    ]
    r = ts.scan(lots, ts.ScanParams(target_roi=1.0), layer=layer, today=TODAY)
    assert r["total"] == 4
    assert [x["id"] for x in r["rows"]][0] == "G3"  # самый большой запас к пределу входа
    assert r["rows"][-1]["id"] == "G2" and r["rows"][-1]["s"]["verdict"] == "no"
    assert r["counts"]["unknown"] == 1  # аренда без цены переуступки
    only = ts.scan(
        lots,
        ts.ScanParams(kinds=("sale",), groups=("housing",), area_max_m2=2000),
        layer=layer,
        today=TODAY,
    )
    assert {x["id"] for x in only["rows"]} == {"G1", "G2"}
    rows = ts.xlsx_rows(r["rows"])
    assert len(rows[0]) == len(ts.XLSX_COLUMNS) and rows[0][1] == "40:13:120101:203"
    path = ts.to_xlsx(r["rows"], tmp_path / "torgi.xlsx")
    from openpyxl import load_workbook

    ws = load_workbook(path).active
    assert ws.max_row == 5 and ws["A1"].value == "Вердикт"
    kml = ts.to_kml(r["rows"])
    assert kml.count("<Placemark>") == 1 and "36.200000,54.500000" in kml  # только с координатами
    assert "<Folder><name>интересен</name>" in kml and "40:13:120101:201" in kml


def test_auction_comparables_same_purpose_and_similar_area():
    # В квартале продавали только участки под дом по 10 соток (150 ₽/м²).
    stats = ts.index_by(_district())
    common = dict(fin=None, status="PUBLISHED")
    field = _lot(
        "H1",
        "40:13:120101:300",
        area=50_000,
        start=90_000,
        vri="Для сельскохозяйственного производства",
        **common,
    )
    assert ts.market_for(field, stats) is None  # поле с домами не сравниваем
    huge = _lot("H2", "40:13:120101:301", area=20_000, start=90_000, **common)
    assert ts.market_for(huge, stats) is None  # площадь в 20 раз больше — не сопоставимо
    house = _lot("H3", "40:13:120101:302", area=1500, start=90_000, **common)
    m = ts.market_for(house, stats)
    assert m["source"] == "auctions" and m["mid_m2"] == 150 and m["confidence"] == 2
    assert m["source_label"] == "итоги торгов: квартал, 6 лот."
    # в другом квартале того же района — выборка по району
    other = _lot("H4", "40:13:999999:1", area=1000, start=90_000, **common)
    assert ts.market_for(other, stats)["source_label"] == "итоги торгов: район, 6 лот."
    # «рынок выше торгов в 1,3 раза» — множитель применяется и назван в пометке
    m = ts.market_for(house, stats, markup=1.3)
    assert m["mid_m2"] == 195 and "в 1.3 раза выше торгов" in m["note"]


# --- извещения о предоставлении (ст. 39.18 ЗК): не торги ------------------------------------
def test_notice_row_takes_project_area_and_quarter_from_text():
    row = ts.lot_row(_notice("I_1"))
    assert row["form"] == "IPS" and ts.is_notice(row)
    assert row["cad_num"] is None and row["price_start"] is None and row["region"] == "40"
    assert row["quarter"] == "40:13:120105" and row["district"] == "40:13"
    assert row["area_m2"] == 1500 and row["bidd_end"] == "2026-12-01"
    assert ts.bidd_label(row) == "извещение о предоставлении (ст. 39.18 ЗК)"
    # номер участка — не квартал; обычный аукцион подписи не получает
    assert ts.quarter_from_text({"lotName": "участок 40:13:120101:22"}) is None
    lot = _lot("A1", "40:13:120101:203")
    assert ts.bidd_label(lot) == "" and not ts.is_notice(lot)
    assert ts.bidd_label(lot | {"bidd_type": "229FZ"}) == "имущество должников"
    assert ts.bidd_label(lot | {"bidd_type": "XX", "bidd_type_name": "Иное"}) == "Иное"


def test_notices_stay_out_of_liquidity_and_default_scan():
    lots = _district()  # 6 с борьбой, 2 одному, 2 без заявок
    done = [ts.lot_row(_notice(f"IS_{i}", status="SUCCEED")) for i in range(40)]
    base, mixed = ts.stats_of(lots), ts.stats_of(lots + done)
    # 40 «состоявшихся» извещений не делают район ликвидным
    assert mixed["closed"] == base["closed"] == 10 and mixed["sold"] == 6
    assert mixed["sell_through"] == base["sell_through"] and mixed["index"] == base["index"]
    assert (base["notices"], mixed["notices"]) == (0, 40)
    assert ts.repeats_of(done) == {}

    active = _lot(
        "A1", "40:13:120101:203", area=1000, start=100_000, fin=None, status="PUBLISHED"
    ) | {"bidd_end": "2026-11-30"}
    live = ts.lot_row(_notice("I_live"))
    everything = lots + done + [active, live]
    r = ts.scan(everything, today=TODAY)
    assert [x["id"] for x in r["rows"]] == ["A1"]
    assert r["forms"] == {"auction": 1, "notice": 1} and r["counts"]["notice"] == 0

    only = ts.scan(everything, ts.ScanParams(forms="notice"), today=TODAY)
    assert [x["id"] for x in only["rows"]] == ["I_live"] and only["counts"]["notice"] == 1
    s = only["rows"][0]["s"]
    assert s["verdict"] == "notice" and s["verdict_label"] == "извещение 39.18"
    assert any("39.18" in t for t in s["ask"])
    assert not any("начальная цена" in t or "цены перепродажи" in t for t in s["ask"])

    both = ts.scan(everything, ts.ScanParams(forms="all"), today=TODAY)
    # при цели 100 % лот получает «нет» и уходит в конец; извещения — выше отказов
    assert [x["id"] for x in both["rows"]] == ["I_live", "A1"]
    assert both["rows"][1]["s"]["verdict"] == "no"
    table = ts.xlsx_rows(both["rows"])
    assert all(len(row) == len(ts.XLSX_COLUMNS) for row in table)
    assert table[0][-2].startswith("извещение о предоставлении") and table[1][-2] == ""
    # срок вышел — своё заявление уже не подать
    late = ts.score_lot(live | {"bidd_end": "2026-11-01"}, {}, today=TODAY)
    assert late["verdict"] == "no" and "приём заявок закончен" in late["stop"]


def test_rent_lot_priced_as_share_of_ownership_price_when_ratio_given():
    """Аренда под переуступку: цены переуступки в слое нет — берём долю от цены участка в
    собственности, если долю задали в критериях. Без доли аренда остаётся без оценки."""
    lots = _district()  # продажи района: 150 ₽/м² → участок 1000 м² = 150 тыс.
    rent = _lot(
        "R1", "40:13:120101:300", area=1000, start=10_000, fin=None, kind="rent", status="PUBLISHED"
    ) | {"bidd_end": "2026-11-30"}
    stats = ts.index_by(lots)
    plain = ts.score_lot(rent, stats, ts.ScanParams(target_roi=0.4), today=TODAY)
    assert plain["verdict"] == "unknown" and plain["sale"]["mid"] is None
    assert any("долю от цены участка в собственности" in t for t in plain["ask"])

    p = ts.ScanParams(target_roi=0.4, assign_ratio=0.5)
    s = ts.score_lot(rent, stats, p, today=TODAY)
    assert s["sale"]["mid"] == 75_000  # 150 тыс. × 50 %
    assert s["market"]["source"] == "auctions" and s["market"]["assign_ratio"] == 0.5
    assert s["market"]["source_label"].startswith("переуступка = 50% цены в собственности")
    assert s["limit"] is not None and s["limit"] > rent["price_start"]
    assert s["verdict"] in ("go", "edge")
    # своя цена переуступки в слое рынка важнее доли
    layer = [
        {
            "scope": "40:13",
            "kind": "rent",
            "vri_group": "any",
            "price_mid": 20_000,
            "source": "manual",
        }
    ]
    own = ts.score_lot(rent, stats, p, layer=layer, today=TODAY)
    assert own["sale"]["mid"] == 200_000 and own["market"]["source"] == "manual"
    # продаже доля не мешает; нет цены собственности — нет и оценки аренды
    sale = _lot("A1", "40:13:120101:203", area=1000, start=100_000, fin=None, status="PUBLISHED")
    assert ts.score_lot(sale, stats, p, today=TODAY)["sale"]["mid"] == 150_000
    assert ts.score_lot(rent, {}, p, today=TODAY)["verdict"] == "unknown"
    # только кадастр: нижний ориентир, вердикт не выше «на грани»
    low = ts.score_lot(rent | {"cad_cost": 300_000}, {}, p, today=TODAY)
    assert low["sale"] == {"min": 150_000, "mid": None, "max": None} and low["verdict"] == "edge"


def test_failed_auction_outcome_by_contract_price_and_card():
    """Не состоявшиеся торги (прод, Карелия: протокол есть у 20 лотов из 222, цена договора —
    у 88): цена договора значит «ушёл единственному участнику»; карточка прочитана и пуста —
    «не ушёл»; карточка не прочитана — «не знаем»."""
    base = _lot("F1", "40:13:120103:1", fin=None, status="FAILED")
    assert ts._outcome(base) == "failed"
    assert ts._outcome(base | {"card": True}) == "none"
    assert ts._outcome(base | {"price_fin": 100_000.0}) == "single"
    assert ts._outcome(base | {"card": True, "applied": 2, "admitted": 1}) == "single"
    assert ts._outcome(base | {"applied": 3, "admitted": 0}) == "none"
    lots = _district(sold=4, single=0, none=0) + [
        base | {"id": f"C{i}", "price_fin": 100_000.0, "card": True} for i in range(3)
    ]
    lots += [base | {"id": f"E{i}", "card": True} for i in range(3)]
    s = ts.stats_of(lots)
    assert (s["closed"], s["sold"], s["single"], s["none"], s["failed_unknown"]) == (10, 4, 3, 3, 0)
    assert s["sell_through"] == 0.7
    # в повторы идут только лоты, которые не ушли
    assert ts.repeats_of(lots) == {"40:13:120103:1": 3}


def test_expected_price_uses_growth_of_same_deal_kind_with_enough_lots():
    """Годовую плату на аукционах аренды разгоняют в разы, продажу — нет: рост берём только по
    той же сделке; меньше трёх торгов с ценой в квартале и районе — по региону; нет и там —
    роста не знаем и ждём старт."""
    rent = [
        _lot(f"R{i}", f"40:13:120101:{i + 1}", start=10_000, fin=80_000, kind="rent")
        for i in range(6)
    ]
    sale = [_lot(f"S{i}", f"40:13:120102:{i + 1}", start=100_000, fin=120_000) for i in range(3)]
    stats = ts.index_by(rent + sale)
    assert stats["40:13"]["rent"]["growth_median"] == 8.0 and stats["40:13"]["rent"]["sold"] == 6
    assert stats["40:13"]["sale"]["growth_median"] == 1.2 and stats["*"]["sale"]["growth_n"] == 3
    live_sale = _lot("A1", "40:13:120105:1", start=200_000, fin=None, status="PUBLISHED")
    live_rent = _lot(
        "A2", "40:13:120105:2", start=20_000, fin=None, kind="rent", status="PUBLISHED"
    )
    assert ts.growth_for(live_sale, stats) == (1.2, 1.0, "район")
    assert ts.growth_for(live_rent, stats) == (8.0, 1.0, "район")
    s = ts.score_lot(live_sale, stats, today=TODAY)
    assert s["expected_price"] == 240_000 and s["liquidity"]["growth_where"] == "район"
    assert ts.score_lot(live_rent, stats, today=TODAY)["expected_price"] == 160_000
    # другой район: своих торгов нет — берём регион; продаж в регионе меньше трёх — рост не знаем
    far = live_sale | {"district": "40:99", "quarter": "40:99:000001"}
    assert ts.growth_for(far, stats) == (1.2, 1.0, "регион")
    thin = ts.index_by(rent + sale[:2])
    assert ts.growth_for(live_sale, thin) == (None, None, None)
    assert ts.score_lot(live_sale, thin, today=TODAY)["expected_price"] == 200_000
    assert [ts.times_ru(n) for n in (1, 2, 4, 5, 11, 12, 22)] == [
        "1 раз",
        "2 раза",
        "4 раза",
        "5 раз",
        "11 раз",
        "12 раз",
        "22 раза",
    ]
