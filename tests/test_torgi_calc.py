"""Калькулятор входа в торги (ТЗ v3, п. 9 этап 4 / п. 13.7)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import update

from landcheck import torgi_calc
from landcheck.web.db import User, session
from landcheck.web.serialize import fmt_money
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета


def test_limit_price_matches_spec_example():
    # Пример из таблицы Андрея: продажа 120 000, без расходов и налога, ROI 40 % → 85 714
    assert round(torgi_calc.limit_price(120_000, 0, 0, 0.4)) == 85_714
    # с расходами и налогом: прибыль после налога равна r × (P + E)
    p = torgi_calc.limit_price(1_000_000, 100_000, 0.13, 0.4)
    profit = (1_000_000 - p - 100_000) * (1 - 0.13)
    assert abs(profit - 0.4 * (p + 100_000)) < 1e-6
    assert torgi_calc.limit_price(0, 0, 0, 0.4) is None


def test_compute_scenarios_safe_benchmark_and_steps():
    r = torgi_calc.compute(
        {
            "area": "900",
            "start": "38 000",
            "step": "2000",
            "extra": "",
            "tax": "0",
            "roi": "40",
            "smin": "120000",
            "smid": "200000",
            "smax": "250000",
            "steps": "30",
        }
    )
    assert r["limits"] == {"min": 85_714, "mid": 142_857, "max": 178_571, "safe": 85_714}
    assert r["entry"]["per_m2"] == 42.2
    rows = r["rows"]
    assert rows[0]["price"] == 38_000 and rows[0]["status"] == "в пределах по всем"
    assert r["last_safe_step"] == 23 and rows[23]["price"] == 84_000 and rows[23]["within_safe"]
    assert not rows[24]["within_safe"] and rows[24]["status"] == "только MEDIUM, MAX"
    assert rows[24]["scenarios"]["min"]["roi"] == round((120_000 - 86_000) / 86_000, 3)
    assert "после шага 23" in r["verdict"] and fmt_money(84_000) in r["verdict"]


def test_compute_rent_payback_tax_and_verdicts():
    r = torgi_calc.compute(
        {"start": "1000000", "step": "50000", "rent": "10000", "tax": "13", "smin": "1200000"}
    )
    assert r["inputs"]["tax"] == 0.13 and r["inputs"]["roi"] == torgi_calc.DEFAULT_ROI
    assert r["entry"]["rent_year"] == 120_000 and r["entry"]["payback_years"] == 8.3
    assert r["limits"]["mid"] is None and r["limits"]["safe"] == r["limits"]["min"]
    assert "уже начальная цена выше" in r["verdict"]  # предельная 788 679 < старт 1 000 000
    empty = torgi_calc.compute({"start": "500000"})
    assert empty["limits"]["safe"] is None and "хотя бы один сценарий" in empty["verdict"]
    assert torgi_calc.compute({})["verdict"] == "задайте хотя бы один сценарий цены продажи"


def test_defaults_from_valuation_chain():
    d = torgi_calc.defaults_from_valuation
    assert d(None) == {}
    assert d({"total_range": None}) == {}
    v = d({"total_range": (1_000_000.4, 1_500_000.0)})
    assert v["smin"] == 1_000_000 and v["smid"] == 1_250_000 and v["smax"] == 1_500_000
    assert "рыночного диапазона" in v["_note"]
    # нет рынка → итоги торгов продажи в районе (p25 / медиана / p75 × площадь)
    au = {
        "sale": {"with_price": 2, "median_rub_m2": 300.0, "p25_rub_m2": 250.0, "p75_rub_m2": 350.0}
    }
    v = d({"total_range": None}, auctions=au, area_m2=1000)
    assert (v["smin"], v["smid"], v["smax"]) == (250_000, 300_000, 350_000)
    assert "итогов торгов" in v["_note"] and "250 / 300 / 350" in v["_note"]
    assert d(None, auctions={"sale": {"with_price": 0}}, area_m2=1000) == {}
    # единственный рыночный якорь → только MEDIUM
    val = {
        "area_sotka": 10.0,
        "market_anchors": [{"label": "медиана вашей подборки", "pps": 120_000}],
        "cadastral_anchors": [{"label": "кадастровая стоимость участка", "pps": 30_000}],
    }
    v = d(val)
    assert v == {"smid": 1_200_000, "_note": v["_note"]} and "медиана вашей подборки" in v["_note"]
    # только кадастр → MIN
    v = d({"area_sotka": 10.0, "cadastral_anchors": [{"label": "кадастровая", "pps": 30_000}]})
    assert v["smin"] == 300_000 and "smid" not in v and "кадастровая" in v["_note"]


def test_torgi_calc_pages_admin_only(client, fixtures_dir: Path):  # noqa: F811
    login(client)
    assert client.get("/tools/torgi-calc").status_code == 404
    r = client.post("/checks", data={"numbers": "50:09:0060202:101; 2 500 000; 10 сот"})
    base = r.headers["location"] + "/items/50:09:0060202:101"
    assert client.get(base + "/torgi-calc").status_code == 404
    with session() as db:
        db.execute(update(User).values(is_admin=True))
        db.commit()
    page = client.get(
        "/tools/torgi-calc?start=38000&step=2000&area=900&smin=120000&smid=200000&smax=250000&tax=0"
    ).text
    assert "Калькулятор входа в торги" in page and fmt_money(85_714) in page
    assert "после шага 23" in page and 'name="roi" inputmode="decimal" value="40"' in page
    # по участку: площадь из ЕГРН подставляется сама, сценарии — из ориентира стоимости
    page = client.get(base + "/torgi-calc").text
    assert "50:09:0060202:101" in page and 'name="area" inputmode="decimal" value="' in page
    assert 'value=""' not in page.split('name="area"')[1].split(">")[0]
    # параметры запроса (ссылка из блока «Торги рядом») перекрывают подстановку
    page = client.get(
        base + "/torgi-calc?start=900000&area=1000&kind=продажа"
        "&lot_url=https%3A%2F%2Ftorgi.gov.ru%2Fnew%2Fpublic%2Flots%2Flot%2FX_1&smin=1200000"
    ).text
    assert 'name="area" inputmode="decimal" value="1000"' in page
    assert "https://torgi.gov.ru/new/public/lots/lot/X_1" in page and "Предельные цены" in page
