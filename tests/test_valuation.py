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

    def search_dupes(q, max_results, news, recent=False):
        return {
            "results": [
                {
                    "title": "Купить земельный участок в с. Чепош — 37 объявлений",
                    "url": "https://domclick.ru/cat",
                    "snippet": "8 млн, 11,6 сот",
                },
                {
                    "title": "Участок 24 сот Чепош",
                    "url": "https://move.ru/objects/123456/",
                    "snippet": "5 000 000 ₽",
                },
                {
                    "title": "Участок 24 сот., Чепош",
                    "url": "https://realty.yandex.ru/offer/1",
                    "snippet": "5 000 000 ₽",
                },
            ],
            "error": None,
        }

    r2 = collect_ads("Чепош", "izhs", search_fn=search_dupes)
    assert r2["n"] == 1 and r2["ads"][0]["host"] == "move.ru"
    assert r2["ads"][0]["also"] == ["realty.yandex.ru"]
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
    assert v["range"] is None and "рыночных данных" in v["verdict"] and v["confidence"] == "низкая"
    assert v["has_market"] is False and v["coef"] is None
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
    assert "Рынок: в рынке ли цена" in block and "Объявления в локации (3)" in block
    report = client.get(url + "/items/50:09:0060202:101").text
    assert (
        "Объявления в локации (3)" in report
        and "/ads" not in report.split("Рынок: в рынке ли цена")[1][:200]
    )


def test_is_catalog_by_text_and_url():
    from landcheck.market_ads import is_catalog

    # одиночные объявления (реальные заголовки с прода): «Продам …» — не каталог
    assert not is_catalog(
        "Продам земельный участок Белокуриха Чехова ул",
        "10 сот, 1,2 млн",
        "https://altay.sibdom.ru/sale/land/123456/",
    )
    assert not is_catalog(
        "Продам земельный участок ижс село Чепош",
        "15 соток, 3 млн ₽",
        "https://olan.ru/ad/zemlya-chepos",
    )
    # «Купить …» с числовым id в пути — страница одного объявления
    assert not is_catalog(
        "Купить участок 10 сот. в селе Чепош",
        "2,1 млн ₽",
        "https://domclick.ru/card/sale__land__2165263839",
    )
    # каталоги: счётчик, значок перед заголовком, каталожный путь, множественное число
    assert is_catalog(
        "Купить земельный участок в с. Чепош — 37 объявлений", "", "https://domclick.ru/x"
    )
    assert is_catalog(
        "ᐉ Купить участок ИЖС в Белокурихе",
        "цены от 500 000",
        "https://barnaul.etagi.com/belokurikha/realty_zem/",
    )
    assert is_catalog(
        "Участок ИЖС Чепош", "по 37 объявлениям цены от 1 млн", "https://cian.ru/sale/suburban/1/"
    )
    assert is_catalog("Земельные участки в Чепоше", "", "https://x.ru/a")
    assert is_catalog("Участок Чепош", "", "https://www.cian.ru/kupit-zemelnyj-uchastok-chepos/")
    assert is_catalog("Участок Чепош", "", "https://domclick.ru/altai/pokupka/uchastki/chepos")


def test_collect_ads_off_scale_area():
    def search_fn(q, max_results, news, recent=False):
        return {
            "results": [
                {
                    "title": "Продам земельный участок Белокуриха, 73 га",
                    "url": "https://altay.sibdom.ru/sale/land/123456/",
                    "snippet": "30 млн руб",
                },
                {
                    "title": "Участок 10 сот ИЖС Белокуриха",
                    "url": "https://avito.ru/x_123456789",
                    "snippet": "2 млн ₽",
                },
            ],
            "error": None,
        }

    r = collect_ads("Белокуриха", "izhs", area_m2=1000, search_fn=search_fn)
    assert r["n"] == 1 and r["ads"][0]["host"] == "avito.ru" and r["median"] == 200_000
    assert [a["host"] for a in r["off_scale"]] == ["altay.sibdom.ru"]
    assert r["off_scale"][0]["area_m2"] == 730_000
    # без площади участка (или с мусорной площадью из ЕГРН) фильтра нет
    assert collect_ads("Белокуриха", "izhs", search_fn=search_fn)["n"] == 2
    assert collect_ads("Белокуриха", "izhs", area_m2=0.5, search_fn=search_fn)["n"] == 2


def test_is_catalog_cian_code_olan_list_and_domclick_suffix():
    from landcheck.market_ads import is_catalog

    # код локации cian после дефиса — не id объявления, путь /kupit- каталожный
    assert is_catalog(
        "Купить участок в Чепоше",
        "",
        "https://www.cian.ru/kupit-zemelnyj-uchastok-cheposh-01164480/",
    )
    # olan: id в адресе, но сниппет — список карточек с разными площадями и ценами
    assert is_catalog(
        "Купить земельный участок с/п Аносинское в Чемальском",
        "11 сот, 1 799 999 руб · 11,98 сот, 1 000 000 руб · агент",
        "https://olan.ru/zemlya/chemal/1234567",
    )

    # одиночное объявление domclick с припиской «— Домклик» — не дом и не каталог
    def search_fn(q, max_results, news, recent=False):
        return {
            "results": [
                {
                    "title": "Купить участок, 24 сот., ИЖС — Домклик",
                    "url": "https://domclick.ru/card/sale__land__2165263839",
                    "snippet": "3 000 000 ₽, два смежных участка по 12 соток, Чепош",
                }
            ],
            "error": None,
        }

    r = collect_ads("Чепош", "izhs", search_fn=search_fn)
    assert r["n"] == 1 and r["ads"][0]["host"] == "domclick.ru"


def test_collect_ads_matches_village_by_settlement_stem():
    def search_fn(q, max_results, news, recent=False):
        return {
            "results": [
                {
                    "title": "Участок 10 сот ИЖС, село Узнезя",
                    "url": "https://avito.ru/x_123456789",
                    "snippet": "1 200 000 ₽, Чемальский район",
                }
            ],
            "error": None,
        }

    assert collect_ads("Узнезинское", "izhs", search_fn=search_fn)["n"] == 1


def test_slug_mismatch_social_walls_and_settlement_titles():
    from landcheck.market_ads import is_catalog, slug_mismatch

    parsed = {"price_rub": 1_000_000.0, "area_m2": 1198.0, "pps": 83_472.0}
    # olan: в адресе 11 сот и 1 799 999 ₽, в сниппете 11,98 сот и 1 000 000 — соседняя карточка
    assert slug_mismatch("https://olan.ru/zemlya/chemal/163940237-11-0-sot-1799999-rub", parsed)
    assert not slug_mismatch(
        "https://olan.ru/zemlya/chemal/163940237-11-98-sot-1000000-rub", parsed
    )
    assert not slug_mismatch("https://avito.ru/x_123456789", parsed)
    assert is_catalog(
        "Купить земельный участок с/п Аносинское в Чемальском", "", "https://olan.ru/1234567"
    )

    def search_fn(q, max_results, news, recent=False):
        return {
            "results": [
                {
                    "title": "Участок 10 сот и дом Чепош",
                    "url": "https://vk.ru/public151862844",
                    "snippet": "59 м², 10 сот, 7 600 000 ₽",
                },
                {
                    "title": "Участок 12 сот ИЖС Чепош",
                    "url": "https://olan.ru/zemlya/chemal/163940237-11-0-sot-1799999-rub",
                    "snippet": "1 000 000 ₽, 11,98 сот",
                },
            ],
            "error": None,
        }

    assert collect_ads("Чепош", "izhs", search_fn=search_fn)["n"] == 0


def test_collect_ads_district_fallback_for_small_village():
    """Маленькая деревня (Вырск, прод 06.11): объявлений с названием нет — добираем по району."""
    from landcheck.market_ads import district_fallback

    calls: list[str] = []

    def search_fn(q, max_results, news, recent=False):
        calls.append(q)
        if q.startswith("Вырск"):
            return {"results": [], "error": None}
        return {
            "results": [
                {
                    "title": "Участок 10 сот. ИЖС, Козельск",
                    "url": "https://www.avito.ru/kozelsk/zemelnye_uchastki/uch_1234567890",
                    "snippet": "Цена 500 000 ₽",
                },
                {
                    "title": "Продам участок 12 соток, Козельский район",
                    "url": "https://domclick.ru/card/sale__land__2165263839",
                    "snippet": "650 тыс. руб",
                },
                {
                    "title": "Участок 8 сот, д. Сосенка Козельского района",
                    "url": "https://cian.ru/sale/suburban/312345678/",
                    "snippet": "420 000 руб",
                },
                {
                    "title": "Участок 10 сот, Людиново",  # чужой район — не про «козельск»
                    "url": "https://cian.ru/sale/suburban/312345999/",
                    "snippet": "300 000 руб",
                },
            ],
            "error": None,
        }

    fb = district_fallback("Калужская обл., р-н Козельский, д. Вырск.")
    assert fb == {"place": "Козельский район", "stem": "козельск", "label": "Козельский район"}
    r = collect_ads("Вырск", "izhs", area_m2=1000, search_fn=search_fn, fallback=fb)
    assert r["n"] == 3 and r["scope"] == "district" and r["n_local"] == 0
    assert r["scope_label"] == "Козельский район"
    assert all(a["scope"] == "district" for a in r["ads"])
    assert any(q.startswith("Козельский район") for q in calls)
    # без запасного охвата — честный ноль, как раньше
    assert collect_ads("Вырск", "izhs", search_fn=search_fn)["n"] == 0
    assert district_fallback("Московская область") is None and district_fallback(None) is None


def test_collect_ads_fallback_not_used_when_village_has_enough():
    calls: list[str] = []

    def search_fn(q, max_results, news, recent=False):
        calls.append(q)
        return {
            "results": [
                {
                    "title": f"Участок {n} сот, Чепош",
                    "url": f"https://x.ru/sale/{n}0000001/",
                    "snippet": f"{n * 100} тыс. руб",
                }
                for n in (10, 11, 12)
            ],
            "error": None,
        }

    fb = {"place": "Чемальский район", "stem": "чемальск", "label": "Чемальский район"}
    r = collect_ads("Чепош", "izhs", search_fn=search_fn, fallback=fb)
    assert r["n"] == 3 and r["scope"] == "locality" and r["n_local"] == 3
    assert not any("Чемальский" in q for q in calls)


def test_valuation_labels_district_ads():
    ads = {
        "n": 4,
        "median": 50_000,
        "p25": 40_000,
        "p75": 60_000,
        "ads": [],
        "scope": "district",
        "scope_label": "Козельский район",
        "n_local": 1,
    }
    v = valuation(_item(pps=50_000), corridor=None, ads=ads, median_batch=None)
    assert v["range_source"] == "объявления в районе" and v["has_market"]
    note = v["market_anchors"][-1]["note"]
    assert "нашлось 1" in note and "Козельский район" in note


def test_plain_summary_levels_and_documents_merge():
    from landcheck.summary import merge_documents, plain_summary

    base = {"parcel": {"found": True}, "price_per_sotka": 50_000}
    stop = {
        **base,
        "verdict": "стоп",
        "flags": [
            {
                "rule_id": "R04",
                "level": "стоп",
                "message": "на землях сельхозназначения дом нельзя",
            },
            {"rule_id": "R10", "level": "проверить", "message": "ЗОУИТ"},
        ],
    }
    goal = {"level": "стоп", "activity": "Построить жилой дом", "rows": []}
    s = plain_summary(stop, goal=goal, valuation=None)
    assert s["headline"].startswith("Для цели «построить жилой дом» участок не подходит")
    assert s["reasons"][0].startswith("категория земли не подходит")
    assert any("другой участок" in st for st in s["steps"])

    check = {
        **base,
        "verdict": "проверить",
        "flags": [
            {"rule_id": "R10", "level": "проверить", "message": "ЗОУИТ"},
            {"rule_id": "R99", "level": "проверить", "message": "новое правило: длинное пояснение"},
        ],
    }
    val = {"has_market": True, "range": (40_000, 45_000), "range_source": "объявления в районе"}
    s = plain_summary(check, goal={"level": "ок", "activity": "x"}, valuation=val)
    assert "закрыть 2 вопроса" in s["headline"] and "новое правило" in s["reasons"][1]
    assert "выше рынка на 11%" in s["price"] and "в районе" in s["price"]
    assert any("ГПЗУ" in st for st in s["steps"])
    near = plain_summary(check, goal=None, valuation={**val, "range": (40_000, 48_000)})
    assert "у верхней границы рынка" in near["price"]

    ok = plain_summary({**base, "verdict": "ок", "flags": []}, goal=None, valuation=None)
    assert ok["headline"].startswith("По открытым данным") and ok["reasons"] == []
    assert plain_summary({"parcel": {"found": False}}, goal=None, valuation=None) is None

    blocks = [
        {
            "title": "Документы, которые запросить у продавца",
            "items": ["Выписка ЕГРН об объекте не старше 30 дней: собственник"],
        }
    ]
    docs = [
        {"name": "Выписка ЕГРН об объекте не старше 30 дней", "where": "Госуслуги"},
        {"name": "ГПЗУ — градостроительный план участка", "where": "администрация"},
    ]
    merged = merge_documents(blocks, docs)
    assert merged[0]["items"][-1] == "ГПЗУ — градостроительный план участка — администрация"
    assert len(merged[0]["items"]) == 2 and len(blocks[0]["items"]) == 1  # исходник не тронут
    assert merge_documents([], docs)[0]["title"] == "Документы, которые запросить"


def test_collect_ads_drops_leasehold_and_flags_suspiciously_cheap():
    """Зум 13.11, 24:48: объявления по 70 тыс. за сотку на Avito — аренда, выставленная как
    продажа; в цену собственности их не берём, а заниженные в разы показываем отдельно."""

    def ad(n: int, title: str, price: str, area: str = "10 сот") -> dict:
        return {
            "title": f"{title} Белокуриха, {area}",
            "url": f"https://avito.ru/x_12345678{n}",
            "snippet": price,
        }

    def search_fn(q, max_results, news, recent=False):
        return {
            "results": [
                ad(1, "Участок ИЖС", "2 млн ₽"),
                ad(2, "Участок ИЖС у реки", "2,2 млн ₽"),
                ad(3, "Участок под ИЖС", "1,8 млн ₽"),
                ad(4, "Участок, вид на горы", "2,4 млн ₽"),
                ad(5, "Участок недорого", "700 тыс. ₽"),
                ad(6, "Переуступка права аренды, участок", "300 тыс. ₽"),
                ad(7, "Участок в аренде на 49 лет", "350 тыс. ₽"),
            ],
            "error": None,
        }

    r = collect_ads("Белокуриха", "izhs", area_m2=1000, search_fn=search_fn)
    assert r["n"] == 4 and r["median"] == 210_000
    assert sorted(a["pps"] for a in r["leasehold"]) == [30_000, 35_000]
    assert [a["pps"] for a in r["cheap"]] == [70_000]
