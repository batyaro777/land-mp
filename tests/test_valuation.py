"""R6: разбор объявлений, сбор по локации, оценка ориентира и вердикт."""

from __future__ import annotations

from landcheck.market_ads import collect_ads, parse_ad
from landcheck.web.content import valuation
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def test_parse_ad_prices_and_areas():
    assert parse_ad("Участок 10 сот. ИЖС, Белокуриха", "Цена 2 500 000 ₽") == {
        "price_rub": 2_500_000.0,
        "area_m2": 1000.0,
        "pps": 250_000.0,
    }
    a = parse_ad("Продам 1,2 га сельхоз", "1,8 млн руб, Чемальский район")
    assert a["area_m2"] == 12_000 and a["price_rub"] == 1_800_000 and round(a["pps"]) == 15_000
    assert parse_ad("Участок 12 соток", "торг") is None  # нет цены
    # дом с участком: берём землю (сотки), а не метры дома
    assert parse_ad("Дом 120 м² на участке 10 сот", "6 млн")["area_m2"] == 1000.0
    assert parse_ad("Участок", "850 тыс. руб, 6 сот") == {
        "price_rub": 850_000.0,
        "area_m2": 600.0,
        "pps": 141_666.66666666666,
    }


def test_collect_ads_filters_and_stats():
    def search_fn(q, max_results, news, recent=False):
        return {
            "results": [
                {
                    "title": "Участок 10 сот ИЖС Белокуриха",
                    "url": "https://avito.ru/1",
                    "snippet": "2 000 000 ₽",
                },
                {
                    "title": "Участок 8 сот Белокуриха",
                    "url": "https://cian.ru/2",
                    "snippet": "2,4 млн",
                },
                {
                    "title": "Участок 12 сот Белокуриха",
                    "url": "https://domclick.ru/3",
                    "snippet": "1 800 000 руб",
                },
                {
                    "title": "Участок 10 сот Белокуриха",
                    "url": "https://x.ru/4",
                    "snippet": "90 млн ₽",
                },  # выброс
                {
                    "title": "Участок 10 сот в Бийске",
                    "url": "https://x.ru/5",
                    "snippet": "1 млн",
                },  # не та локация
                {
                    "title": "Карточка участка",
                    "url": "https://egrp365.org/x",
                    "snippet": "Белокуриха 10 сот 1 млн",
                },
                {
                    "title": "Дом 150 м² Белокуриха, 10 сот",
                    "url": "https://x.ru/6",
                    "snippet": "9 млн",
                },
                {
                    "title": "Участок 10 сот ИЖС Белокуриха",
                    "url": "https://avito.ru/1",
                    "snippet": "дубль",
                },
            ],
            "error": None,
        }

    r = collect_ads("Белокуриха", "izhs", search_fn=search_fn)
    assert r["n"] == 3 and [a["host"] for a in r["ads"]] == ["domclick.ru", "avito.ru", "cian.ru"]
    assert r["median"] == 200_000 and r["p25"] == 175_000 and r["p75"] == 250_000
    assert r["error"] is None and len(r["queries"]) == 2
    empty = collect_ads(
        "Белокуриха", "izhs", search_fn=lambda *a, **k: {"results": [], "error": "down"}
    )
    assert empty["n"] == 0 and empty["error"] == "down"


def _item(pps=None, area=1000.0, cad_cost=1_500_000):
    return {
        "parcel": {"area_m2": area, "cadastral_cost_rub": cad_cost},
        "price_per_sotka": pps,
        "location": {
            "price": {"per_sotka": 180_000, "basis": "медиана ставки соседей (5 участков)"}
        },
    }


def test_valuation_range_verdict_and_confidence():
    corridor = {
        "n": 4,
        "median": 200_000,
        "p25": 170_000,
        "p75": 240_000,
        "where": "в этом районе",
        "days": 180,
    }
    ads = {"n": 5, "median": 210_000, "p25": 180_000, "p75": 250_000, "ads": []}
    v = valuation(_item(pps=200_000), corridor=corridor, ads=ads, median_batch=190_000)
    assert v["range"] == (170_000, 240_000) and v["range_source"] == "база Land-MP"
    assert v["verdict"].startswith("в рынке") and v["confidence"] == "средняя"
    assert v["total_range"] == (1_700_000, 2_400_000) and v["area_sotka"] == 10.0
    assert [a["label"] for a in v["anchors"]][:2] == [
        "кадастровая стоимость участка",
        "кадастровая ставка соседей",
    ]
    v = valuation(_item(pps=300_000), corridor=None, ads=ads, median_batch=None)
    assert (
        v["range_source"] == "объявления в локации"
        and "выше рынка" in v["verdict"]
        and "20%" in v["verdict"]
    )
    v = valuation(_item(pps=100_000), corridor=None, ads=ads, median_batch=None)
    assert "ниже рынка" in v["verdict"]
    v = valuation(_item(pps=250_000), corridor=None, ads={"n": 1, "ads": []}, median_batch=None)
    assert v["range"] is None and "не определён" in v["verdict"] and v["confidence"] == "низкая"
    assert valuation(_item(pps=None, area=1.0), corridor=None, ads=None, median_batch=None) is None
    v = valuation(_item(), corridor=None, ads=None, median_batch=None)
    assert v["verdict"] is None and len(v["anchors"]) == 2


def test_report_loads_ads_block_lazily_and_caches(client, monkeypatch):  # noqa: F811
    from landcheck import market_ads
    from tests.test_verification import _register

    _register(client, "val@example.com", role="broker")
    r = client.post(
        "/checks",
        data={"title": "t", "numbers": "50:09:0060202:101; 2 500 000; 10 сот", "purpose": "izhs"},
    )
    assert r.status_code == 303
    url = r.headers["location"]
    import time

    for _ in range(50):
        page = client.get(url).text
        if "Проверено за" in page:
            break
        time.sleep(0.1)
    report = client.get(url + "/items/50:09:0060202:101").text
    assert 'hx-get="' in report and "/items/50:09:0060202:101/ads" in report
    monkeypatch.setattr(
        market_ads,
        "collect_ads",
        lambda loc, kind, **k: {
            "ads": [
                {
                    "title": "Участок 10 сот",
                    "url": "https://avito.ru/1",
                    "host": "avito.ru",
                    "price_rub": 2_000_000.0,
                    "area_m2": 1000.0,
                    "pps": 200_000.0,
                }
            ]
            * 3,
            "n": 3,
            "median": 200_000.0,
            "p25": 200_000.0,
            "p75": 200_000.0,
            "queries": [],
            "fetched_at": "2026-10-21T00:00:00+00:00",
            "error": None,
        },
    )
    import landcheck.web.app as app_mod

    monkeypatch.setattr(app_mod, "collect_ads", market_ads.collect_ads, raising=False)
    block = client.get(url + "/items/50:09:0060202:101/ads").text
    assert "Оценка стоимости" in block and "Объявления в локации (3)" in block
    report = client.get(url + "/items/50:09:0060202:101").text
    assert (
        "Объявления в локации (3)" in report
        and "/ads" not in report.split("Оценка стоимости")[1][:200]
    )
