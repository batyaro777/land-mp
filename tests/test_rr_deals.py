"""Сделки Росреестра: разбор квартального файла, каталог с именами в cp1251, цена лота по
сделкам квартала и района, загрузка в хранилище и показ на страницах анализатора торгов."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from landcheck import rr_deals, torgi_scan
from tests.test_torgi_web import FakeNspd, _admin, _seed, _sync
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

HEADER = (
    "deals_number~okato~region_code~district~city~quarter_cad_number~street~"
    "realestate_type_code~wall_material_code~year_build~floor~purpose_code~area~"
    "period_start_date~deal_price~currency~doc_type"
)
# строки файла за 3 квартал 2026 года как есть (прод, 18.11.2026)
REAL = [
    "1~04401371000~24~город Красноярск~Красноярск~24:50:0100537~Маерчака~002001001000~~~~141004000000~608~2026-07-01~770000~руб~ДКП",
    "1~04214846001~24~Емельяновский~Частоостровское~24:11:0120101~Энергетиков~002001001000~~~~141003000000~2000~2026-07-01~250000~руб~ДКП",
    "4~04214828001~24~город Красноярск~Минино~24:11:0340516~~002001001000~~~~141004000000~3633~2026-07-01~3401646~руб~ДКП",
    "3~04237501000~24~Назаровский~Назарово~24:54:0105004~Свободы~002001001000~~~~142001010000~3708~2026-07-01~402775~руб~ДКП",
    "1~84250892001~04~Шебалинский~Шебалино~04:04:110105~Радуги~002001001000~~~~142001010000~1388~2026-07-01~260000~руб~ДКП",
]
JUNK = [
    # квартира, нулевая цена, заглушка вместо квартала, дешевле 10 копеек за метр, чужая валюта
    "1~04401371000~24~г~Красноярск~24:50:0100537~~002001003000~~~~~60~2026-07-01~5000000~руб~ДКП",
    "1~04000000000~24~Абанский~~24:01:1302008~~002001001000~~~~141005000000~118109~2026-07-01~0.0~руб~ДКП",
    "1~04000000000~24~Б~~24:29:0000000~~002001001000~~~~141005000000~1000~2026-07-01~90000~руб~ДКП",
    "1~04000000000~24~Б~~0:0:0~~002001001000~~~~141005000000~1000~2026-07-01~90000~руб~ДКП",
    "1~04000000000~24~Б~~24:03:0100003~~002001001000~~~~141001000000~500000~2026-07-01~10000~руб~ДКП",
    "1~04000000000~24~Б~~24:03:0100003~~002001001000~~~~141001000000~1000~2026-07-01~90000~USD~ДКП",
    # улица в кавычках с разделителем внутри — наивная разбивка по «~» здесь ломается
    '2~04000000000~24~Б~Село~24:03:0100003~"ул. ""Новая""~1"~002001001000~~~~~1500~2026-07-01~300000~руб~ДКП',
]


def test_purpose_codes_map_to_lot_groups():
    names = rr_deals.purpose_names()
    assert len(names) == 189 and names["142001010000"] == "Для индивидуальной жилой застройки"
    group = rr_deals.purpose_group
    assert group("142001010000") == group("141003000000") == "housing"  # ИЖС и ЛПХ
    assert group("141004000000") == group("141006000000") == "garden"
    assert group("141001000000") == group("141010000000") == "agri"
    assert group("142002160000") == "garage" and group("142002040100") == "commerce"
    assert group("143003020300") == "industry" and group("142005000000") == "industry"
    assert group("144003010000") == "recreation" and group("144001070000") == "recreation"
    # многоквартирная застройка, оборона, пустой и неизвестный код — не сравниваем ни с чем
    assert group("142001020300") == group("143006010000") == group("") == group("999") == "other"
    assert all(group(code) in torgi_scan.GROUPS for code in names)
    # у лотов «транспорт» больше не попадает в «отдых» из-за «спорт» внутри слова
    assert torgi_scan.vri_group("Для размещения объектов транспорта") == "industry"
    assert torgi_scan.vri_group("Спорт") == "recreation"


def test_quarter_and_period_helpers():
    assert rr_deals.quarter_key("24:50:0100537") == ("24", "50", 100537)
    assert rr_deals.quarter_key("4:4:110105") == ("04", "04", 110105)
    assert rr_deals.quarter_key("24:50:100537") == rr_deals.quarter_key("24:50:0100537")
    for bad in (None, "", "24:50", "0:0:0", "24:29:0000000", "24:х:1"):
        assert rr_deals.quarter_key(bad) is None
    assert rr_deals.period_of("2026-07-01") == "2026Q3" and rr_deals.period_of("2025-12-01") == (
        "2025Q4"
    )
    assert rr_deals.period_of("01.07.2026") is None and rr_deals.period_of(None) is None
    assert rr_deals.period_label("2026Q3") == "3 кв. 2026"


def test_parse_rows_keeps_priced_land_deals_only():
    rows = list(rr_deals.parse_rows([HEADER, *REAL, *JUNK]))
    assert len(rows) == 6
    first = rows[0]
    assert first == {
        "region": "24",
        "district": "24:50",
        "q": 100537,
        "group": "garden",
        "area": 608.0,
        "price": 770000.0,
        "n": 1,
        "period": "2026Q3",
    }
    assert [r["n"] for r in rows] == [1, 1, 4, 3, 1, 2]
    assert rows[4]["region"] == "04" and rows[4]["district"] == "04:04"
    assert rows[5]["group"] == "other" and rows[5]["area"] == 1500.0  # строка с кавычками
    # порядок колонок берём из заголовка: переставленный файл читается так же
    cols = HEADER.split("~")
    swapped = [cols[-1], *cols[:-1]]
    line = REAL[0].split("~")
    moved = "~".join([line[-1], *line[:-1]])
    assert list(rr_deals.parse_rows(["~".join(swapped), moved])) == [first]
    assert list(rr_deals.parse_rows([])) == []
    # прежняя версия расчёта (до 3 кв. 2026): первая колонка number, валюта «рубль»
    old = HEADER.replace("deals_number", "number", 1)
    line = REAL[2].replace("2026-07-01", "2026-04-01").replace("~руб~", "~рубль~")
    (row,) = rr_deals.parse_rows([old, line])
    assert row["n"] == 4 and row["period"] == "2026Q2" and row["price"] == 3401646.0
    # файл с BOM в начале заголовка читается так же
    assert len(list(rr_deals.parse_rows(["\ufeff" + HEADER, REAL[0]]))) == 1
    with pytest.raises(ValueError, match="нет ожидаемых колонок"):
        list(rr_deals.parse_rows(["a~b~c", "1~2~3"]))


FOLDERS = (
    '<a href="../">../</a>\n'
    '<a href="2%20%EA%E2%E0%F0%F2%E0%EB%202025%E3./">x</a>   30-Jul-2025 11:27   -\n'
    '<a href="2%20%EA%E2%E0%F0%F2%E0%EB%202026%E3./">x</a>   30-Jul-2026 11:27   -\n'
    '<a href="3%20%EA%E2%E0%F0%F2%E0%EB%202026%E3%2C%20%E2%E5%F0%F1%E8%FF%202/">x</a>   -\n'
    '<a href="%C0%F0%F5%E8%E2%20%E4%EE%202023%E3.%20%E2%EA%EB%FE%F7%E8%F2%E5%EB%FC%ED%EE/">x</a>\n'
    '<a href="%CE%EF%E8%F1%E0%ED%E8%E5.pdf">x</a>\n'
)
Q3 = "https://rosreestr.gov.ru/data-sets/3%20%EA%E2%E0%F0%F2%E0%EB%202026%E3%2C%20%E2%E5%F0%F1%E8%FF%202/"
Q3_FILES = (
    '<a href="dataset_%D1%C4%C5%CB%CA%C8_r-r_01-92_y_2026_q_3_v2.csv.zip">x</a>   46486560\n'
    '<a href="dataset_%C0%D0%C5%CD%C4%C0_r-r_01-92_y_2026_q_3_v2.csv.zip">x</a>   1565365\n'
)


def test_listing_names_are_cp1251_and_links_are_kept_as_is():
    folders = rr_deals.listing_folders(FOLDERS)
    assert [p for p, _ in folders] == ["2026Q3", "2026Q2", "2025Q2"]  # новые первыми, без архива
    assert folders[0][1] == Q3  # адрес не перекодируется: сервер ждёт cp1251
    files = rr_deals.listing_files(Q3_FILES, Q3)
    assert files == [("2026Q3", Q3 + "dataset_%D1%C4%C5%CB%CA%C8_r-r_01-92_y_2026_q_3_v2.csv.zip")]
    rent = rr_deals.listing_files(Q3_FILES, Q3, kind="АРЕНДА")
    assert len(rent) == 1 and "%C0%D0%C5%CD%C4%C0" in rent[0][1]


def _idx(*rows) -> rr_deals.DealIndex:
    return rr_deals.DealIndex(rows)


def _deal(q, group, area, price, n=1, period="2026Q3", district="24:11"):
    return (district, q, group, float(area), float(price), n, period)


LOT = {"quarter": "24:11:0120101", "area_m2": 1000.0, "vri_group": "housing", "kind": "sale"}


def test_deals_market_uses_own_quarter_then_district():
    index = _idx(
        _deal(120101, "housing", 1000, 300_000),
        _deal(120101, "housing", 2400, 960_000, n=2, period="2026Q2"),  # два участка по 12 сот.
        _deal(120101, "garden", 1000, 9_000_000),  # другое назначение
        _deal(120101, "housing", 90_000, 900_000),  # поле в 90 раз больше лота
        _deal(120101, "housing", 1000, 5_000),  # символическая цена: дальше медианы в 5 раз
        *[_deal(340516, "housing", 1500, 150_000 + i * 15_000) for i in range(12)],
    )
    mk = rr_deals.deals_market(LOT, index)
    assert mk["source"] == "rr" and mk["confidence"] == 2 and mk["deals_level"] == "quarter"
    assert mk["source_label"] == "сделки Росреестра: квартал, 3 за 2 кв. 2026 – 3 кв. 2026"
    assert mk["deals_n"] == 3 and (mk["min_m2"], mk["mid_m2"], mk["max_m2"]) == (300, 400, 400)
    assert "не учтено сделок с ценой дальше медианы в пять раз: 1" in mk["note"]
    assert "нижняя граница рынка" in mk["note"]
    assert [d["n"] for d in mk["deals"]] == [1, 2] and mk["deals"][1]["area_m2"] == 1200.0
    assert mk["deals"][0]["period"] == "3 кв. 2026" and mk["deals"][0]["pps"] == 30_000

    # в своём квартале сделок мало — берём район, но надёжность ниже
    lot = {**LOT, "quarter": "24:11:0999999"}
    far = rr_deals.deals_market(lot, index)
    assert far["deals_level"] == "district" and far["confidence"] == 1
    assert far["source_label"].startswith("сделки Росреестра: район, 15 за 2 кв. 2026")
    # квартал с ведущим нулём и без — один и тот же
    assert rr_deals.deals_market({**LOT, "quarter": "24:11:120101"}, index)["deals_n"] == 3
    # мало сделок и в районе; нет площади, квартала, группы; «прочее» не оцениваем
    assert rr_deals.deals_market({**LOT, "vri_group": "agri"}, index) is None
    assert rr_deals.deals_market({**LOT, "area_m2": None}, index) is None
    assert rr_deals.deals_market({**LOT, "quarter": None}, index) is None
    assert rr_deals.deals_market({**LOT, "vri_group": "other"}, index) is None
    assert rr_deals.deals_market(LOT, None) is None and rr_deals.deals_market(LOT, _idx()) is None


def test_quarter_far_above_district_is_not_taken_as_market():
    """Прод, 19.11.2026: старт лота 87 тыс. ₽ за сотку, «рынок» по пяти сделкам квартала —
    3,49 млн; такие лоты стояли первыми по доходности. Квартал дороже района больше чем втрое —
    берём район, квартал остаётся справкой."""
    index = _idx(
        *[_deal(120101, "housing", 1000, 3_000_000 + i * 100_000) for i in range(5)],
        *[_deal(340516, "housing", 1500, 150_000 + i * 15_000) for i in range(20)],
    )
    mk = rr_deals.deals_market(LOT, index)
    assert mk["deals_level"] == "district" and mk["confidence"] == 1
    assert mk["mid_m2"] < 400
    assert mk["quarter_ref"]["n"] == 5 and mk["quarter_ref"]["mid_m2"] == 3200
    assert mk["quarter_ref"]["ratio"] > 3
    assert "взят район" in mk["note"]
    # квартал дороже района, но в пределах трёх раз — остаётся кварталом, район рядом для сверки
    near = _idx(
        *[_deal(120101, "housing", 1000, 400_000) for _ in range(4)],
        *[_deal(340516, "housing", 1500, 300_000) for _ in range(20)],
    )
    ok = rr_deals.deals_market(LOT, near)
    assert ok["deals_level"] == "quarter" and ok["district_mid_m2"] == 200
    assert "quarter_ref" not in ok


def test_weighted_quantiles_count_every_deal():
    # одна строка на 8 сделок по 200 ₽/м² и две одиночные по 600: медиана — 200, не 600
    index = _idx(
        _deal(120101, "housing", 8000, 1_600_000, n=8),
        _deal(120101, "housing", 1000, 600_000),
        _deal(120101, "housing", 1000, 600_000, period="2026Q2"),
    )
    mk = rr_deals.deals_market(LOT, index)
    assert mk["deals_n"] == 10 and mk["mid_m2"] == 200 and mk["max_m2"] == 200
    assert (mk["deals_lo_m2"], mk["deals_hi_m2"]) == (200, 600)


def test_pick_local_prefers_quarter_deals_then_ads_then_district_deals():
    quarter = {"mid_m2": 300.0, "source_label": "сделки: квартал", "deals_level": "quarter"}
    district = {"mid_m2": 250.0, "source_label": "сделки: район", "deals_level": "district"}
    ads = {"mid_m2": 500.0, "source_label": "объявления в радиусе 2 км: 6 шт."}
    pick = torgi_scan.pick_local
    best = pick(quarter, ads)
    assert best["source_label"] == "сделки: квартал"
    assert best["alt"] == {"label": "объявления в радиусе 2 км: 6 шт.", "mid_m2": 500.0}
    assert "alt" not in quarter  # исходную оценку не портим
    best = pick(district, ads)
    assert best["source_label"].startswith("объявления") and best["alt"]["label"] == "сделки: район"
    assert pick(district, None)["source_label"] == "сделки: район"
    assert "alt" not in pick(None, ads) and pick(None, None) is None
    assert pick({"mid_m2": None}, None) is None


def _zip(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dataset_v2.csv", "\n".join(lines).encode("utf-8"))
    return buf.getvalue()


def _kaluga(n: int, price: int, quarter: str = "40:13:120101", period: str = "2026-07-01"):
    return (
        f"{n}~29000000000~40~Малоярославецкий~Село~{quarter}~Новая~002001001000~~~~"
        f"142001010000~{1000 * n}~{period}~{price * n}~руб~ДКП"
    )


def test_store_sync_and_pages(client, monkeypatch):  # noqa: F811
    from landcheck.web import rr_sync

    assert rr_sync.enabled() is False and rr_sync.start_loop() is None  # демо на фикстурах
    assert rr_sync.market_fn() is None and rr_sync.status("40")["periods"] == []
    fake = _seed(client)
    _sync(fake, provider=FakeNspd())
    _admin(client)
    url = "/tools/torgi-scan?region=40&roi=40"
    assert "сделки Росреестра" not in client.get(url).text.split("Как определяется")[0]

    served: list[str] = []
    q2 = "https://rosreestr.gov.ru/data-sets/2%20%EA%E2%E0%F0%F2%E0%EB%202026%E3./"
    q2_file = '<a href="dataset_%D1%C4%C5%CB%CA%C8_r-r_01-92_y_2026_q_2.csv.zip">x</a>'
    archives = {
        "q_3_v2": _zip([HEADER, *REAL, _kaluga(2, 2_800_000), _kaluga(1, 3_000_000)]),
        # 2 квартал — в прежней версии: первая колонка называется number
        "q_2.csv": _zip(
            [
                HEADER.replace("deals_number", "number", 1),
                _kaluga(1, 3_200_000, period="2026-04-01"),
            ]
        ),
    }

    def handler(req: httpx.Request) -> httpx.Response:
        raw = str(req.url)
        served.append(raw)
        if raw == rr_deals.LISTING_URL:
            return httpx.Response(200, content=FOLDERS.encode("cp1251"))
        if raw == Q3:
            return httpx.Response(200, content=Q3_FILES.encode("cp1251"))
        if raw == q2:
            return httpx.Response(200, content=q2_file.encode("cp1251"))
        for key, body in archives.items():
            if key in raw and "%D1%C4%C5%CB%CA%C8" in raw:
                return httpx.Response(200, content=body)
        return httpx.Response(404)

    api = httpx.Client(transport=httpx.MockTransport(handler))
    before = rr_sync.version()
    done, problems = rr_sync.sync(api, sleep=lambda s: None, keep=2)
    assert done == {"2026Q3": 7, "2026Q2": 1} and problems == []
    assert rr_sync.version() == before + 2
    # ссылки каталога ушли на сервер как были в листинге — в cp1251, без перекодировки
    assert Q3 in served and any(
        "dataset_%D1%C4%C5%CB%CA%C8_r-r_01-92_y_2026_q_3_v2" in u for u in served
    )
    assert not any("%C0%D0%C5%CD%C4%C0" in u for u in served)  # аренду не качаем
    n = len(served)
    assert rr_sync.sync(api, sleep=lambda s: None, keep=2) == ({}, [])  # всё уже загружено
    assert len(served) == n + 3 and rr_sync.version() == before + 2  # только три листинга

    st = rr_sync.status("40")
    assert [(p["label"], p["deals"]) for p in st["periods"]] == [
        ("3 кв. 2026", 3),
        ("2 кв. 2026", 1),
    ]
    assert rr_sync.status("24")["periods"][0]["deals"] == 9
    lot = {"quarter": "40:13:120101", "area_m2": 1000.0, "vri_group": "housing"}
    mk = rr_sync.market(lot)
    assert mk["deals_n"] == 4 and mk["mid_m2"] == 2800 and mk["deals_level"] == "quarter"
    assert rr_sync.market({**lot, "quarter": None}) is None

    page = client.get(url).text  # расчёт списка устарел сам: у сделок своя версия
    assert "сделки Росреестра: квартал, 4 за 2 кв. 2026 – 3 кв. 2026" in page
    assert "Вся выборка, 4 сделок: от" in page and "итоги торгов: " not in page
    lot_page = client.get("/tools/torgi-lot/ACT_0?roi=40").text
    assert "Цена взята из сделок, зарегистрированных Росреестром в кадастровом квартале лота" in (
        lot_page
    )
    assert "Сделки из выборки: 3 строк, всего сделок 4" in lot_page
    assert f'href="{rr_deals.SOURCE_URL}"' in lot_page and "нижней границей рынка" in lot_page
    # оборот места (ТЗ v5, п. 6): счёт сделок попадает в лот, на «Горячие» и в сводку инвестору
    assert rr_sync.turnover(lot)["quarter_n"] == 4 and rr_sync.turnover(lot)["district_n"] == 4
    assert rr_sync.turnover({**lot, "quarter": None}) is None
    hot = client.get("/tools/torgi-hot?annual=10&cap=9000000&days=3&top=50").text
    # рынок здесь в 28 раз выше старта — лоты в списке «Проверить руками», оборот виден и там
    assert "Проверить руками: 12" in hot and "сделок в квартале: 4" in hot
    invest = client.get("/tools/torgi-lot/ACT_0/invest").text
    assert (
        "Оборот: за 2 кв. 2026 – 3 кв. 2026 Росреестр зарегистрировал <strong>4</strong>" in invest
    )
    assert "Сделки в квартале идут" in invest
    liq = client.get("/tools/torgi-liquidity?region=40").text
    assert (
        "Сделки Росреестра:</strong> загружены 3 кв. 2026 (3 сделок с участками в регионе)" in liq
    )
    assert f'href="{rr_deals.SOURCE_URL}"' in liq

    # файл перезалит с другими ценами — строки заменяются, а не копятся
    from pathlib import Path

    tmp = Path(rr_sync.store_path()).with_suffix(".zip")
    tmp.write_bytes(_zip([HEADER, _kaluga(3, 1_000_000)]))
    q3_url = Q3 + "dataset_%D1%C4%C5%CB%CA%C8_r-r_01-92_y_2026_q_3_v2.csv.zip"
    assert rr_sync.load_zip("2026Q3", q3_url, tmp) == 1
    mk = rr_sync.market(lot)
    assert mk["deals_n"] == 4 and mk["mid_m2"] == 1000  # 3 сделки из нового файла + 1 из 2 кв.

    # старые кварталы уходят: держим только последние
    assert rr_sync.sync(api, sleep=lambda s: None, keep=1) == ({}, [])
    assert [p["period"] for p in rr_sync.status("40")["periods"]] == ["2026Q3"]

    # квартал в другом формате или недоступная папка не мешают остальным
    archives["q_2.csv"] = _zip(["a~b~c", "1~2~3"])
    done, problems = rr_sync.sync(api, sleep=lambda s: None, keep=3)
    assert done == {} and len(problems) == 2
    assert problems[0].startswith("2 кв. 2026: в файле сделок нет ожидаемых колонок")
    assert problems[1] == "2 кв. 2025: HTTPStatusError"
    assert rr_sync.market(lot)["deals_n"] == 3  # загруженное на месте
    # отклонённый файл заново не качаем, пока версия разбора та же; недоступную папку — пробуем
    n = len(served)
    done, again = rr_sync.sync(api, sleep=lambda s: None, keep=3)
    assert done == {} and again == problems
    assert [u for u in served[n:] if u.endswith(".zip")] == []
    monkeypatch.setattr(rr_deals, "PARSER_REV", rr_deals.PARSER_REV + 1)
    rr_sync.sync(api, sleep=lambda s: None, keep=3)
    assert sum(1 for u in served if "q_2.csv.zip" in u) == 3  # загрузка, отказ, новая версия

    # битый архив: прежние строки остаются
    tmp.write_bytes(_zip(["a~b", "1~2"]))
    with pytest.raises(ValueError):
        rr_sync.load_zip("2026Q3", q3_url, tmp)
    assert rr_sync.market(lot)["deals_n"] == 3


def test_zone_shares_and_text():
    """Задача из чата (№87–89): доля площади участка под каждой ЗОУИТ, а не только «действует
    на участок»."""
    from landcheck.rules.catalog import share_text, zone_shares
    from tests.helpers import SQUARE

    def box(x0: float, x1: float) -> dict:
        ring = [[x0, 55.0], [x1, 55.0], [x1, 55.001], [x0, 55.001], [x0, 55.0]]
        return {"type": "Polygon", "coordinates": [ring]}

    parcel = {
        "geometry": SQUARE,  # 37.000–37.001
        "zouit_geometries": [
            {
                "reg_number": "50:09-6.1",
                "name": "Охранная зона ЛЭП",
                "geometry": box(37.0, 37.0003),
            },
            {"reg_number": "50:09-6.2", "name": "Водоохранная зона", "geometry": box(36.9, 37.1)},
            {"reg_number": "50:09-6.3", "name": "Газопровод", "geometry": box(37.001, 37.002)},
            {"reg_number": None, "name": "Зона без номера", "geometry": box(37.0, 37.00005)},
            {"reg_number": "50:09-6.5", "name": "Без контура", "geometry": None},
            {"reg_number": None, "name": None, "geometry": box(37.0, 37.001)},
        ],
    }
    shares = zone_shares(parcel)
    assert shares["50:09-6.1"] == pytest.approx(0.3, abs=0.001)
    assert shares["50:09-6.2"] == 1.0 and shares["50:09-6.3"] == 0.0
    assert shares["Зона без номера"] == pytest.approx(0.05, abs=0.001)
    assert "50:09-6.5" not in shares and "" not in shares
    assert zone_shares(None) == {} and zone_shares({"zouit_geometries": []}) == {}
    assert zone_shares({**parcel, "geometry": None}) == {}  # границ участка нет — доли нет
    assert share_text(0.3) == "30 % площади участка" and share_text(1.0) == "весь участок"
    assert share_text(0.997) == "весь участок" and share_text(0.004) == "меньше 1 % площади участка"
    assert share_text(0.0) == "только касается границы" and share_text(None) is None


def test_valuation_takes_rosreestr_deals_as_market_anchor():
    from landcheck.web.content import valuation

    item = {"parcel": {"area_m2": 1000.0, "cadastral_cost_rub": 1_500_000}, "price_per_sotka": None}
    index = _idx(*[_deal(120101, "housing", 1000, 200_000 + i * 50_000) for i in range(9)])
    deals = rr_deals.deals_market(LOT, index)
    assert deals["deals_n"] == 9 and deals["deals_level"] == "quarter"

    v = valuation(
        {**item, "price_per_sotka": 60_000}, corridor=None, ads=None, median_batch=None, deals=deals
    )
    labels = [a["label"] for a in v["market_anchors"]]
    assert labels == ["сделки Росреестра в кадастровом квартале"] and v["has_market"] is True
    assert (
        v["market_anchors"][0]["pps"] == 40_000
        and "9 сделок за 3 кв. 2026" in (v["market_anchors"][0]["note"])
    )
    # ни своей базы, ни объявлений — диапазоном становятся сделки квартала
    assert v["range_source"] == "сделки Росреестра в квартале" and v["range"] == (30_000, 50_000)
    assert (
        v["verdict"].startswith("выше цен сделок в квартале на 20%") and "ожидаемо" in v["verdict"]
    )
    assert v["confidence"] == "средняя" and v["coef"]["n"] == 9 and v["deals"] is deals
    assert v["deals_source"] == rr_deals.SOURCE_URL
    inside = valuation(
        {**item, "price_per_sotka": 45_000}, corridor=None, ads=None, median_batch=None, deals=deals
    )
    assert inside["verdict"].startswith("в диапазоне цен сделок")
    low = valuation(
        {**item, "price_per_sotka": 15_000}, corridor=None, ads=None, median_batch=None, deals=deals
    )
    assert low["verdict"].startswith("ниже цен сделок в квартале на 50%")

    # есть объявления — диапазон и вердикт по ним, сделки остаются ориентиром рядом
    ads = {"n": 5, "median": 210_000, "p25": 180_000, "p75": 250_000, "ads": []}
    v = valuation(
        {**item, "price_per_sotka": 200_000}, corridor=None, ads=ads, median_batch=None, deals=deals
    )
    assert v["range_source"] == "объявления в локации" and v["verdict"].startswith("в рынке")
    assert [a["label"] for a in v["market_anchors"]] == [
        "объявления в локации",
        "сделки Росреестра в кадастровом квартале",
    ]
    # сделки только по району — ориентир есть, диапазона и вердикта «по сделкам» нет
    far = rr_deals.deals_market(
        {**LOT, "quarter": "24:11:0999999"},
        _idx(*[_deal(120101, "housing", 1000, 300_000) for _ in range(12)]),
    )
    v = valuation(
        {**item, "price_per_sotka": 60_000}, corridor=None, ads=None, median_batch=None, deals=far
    )
    assert v["market_anchors"][0]["label"] == "сделки Росреестра в кадастровом районе"
    assert v["range"] is None and v["confidence"] == "низкая"
    # сделок нет — блок прежний
    v = valuation({**item, "price_per_sotka": 60_000}, corridor=None, ads=None, median_batch=None)
    assert v["deals"] is None and v["has_market"] is False


def test_report_shows_zone_share_and_deals_in_quarter(client):  # noqa: F811
    from landcheck.web import rr_sync
    from tests.test_web import login

    login(client)
    r = client.post(
        "/checks", data={"title": "Доли", "numbers": "50:09:0060202:104", "purpose": "izhs"}
    )
    assert r.status_code == 303
    url = r.headers["location"] + "/items/50:09:0060202:104"
    page = client.get(url).text
    # контур зоны в тестовых данных совпадает с участком
    assert "действует на участок" in page and "· под зоной весь участок" in page
    assert "Сделки в кадастровом" not in page

    def line(price: int, n: int = 1) -> str:
        return (
            f"{n}~46000000000~50~Солнечногорский~Село~50:09:0060202~Новая~002001001000~~~~"
            f"142001010000~{1000 * n}~2026-07-01~{price * n}~руб~ДКП"
        )

    rr_sync.load_lines(
        "2026Q3", "test://q3", [HEADER, line(2_000_000), line(2_500_000, 2), line(3_000_000)]
    )
    page = client.get(url).text
    assert "Сделки Росреестра в кадастровом квартале" in page
    assert "Сделки в кадастровом квартале: 4 за 3 кв. 2026" in page
    assert f'href="{rr_deals.SOURCE_URL}"' in page and "нижней границей рынка" in page
    assert "<td>3 кв. 2026</td>" in page


def test_turnover_counts_comparable_deals_in_quarter_and_district():
    """ТЗ v5, п. 6: оборот места — сколько сделок с участками того же назначения и похожей
    площади (от 1/5 до 5 площадей лота) прошло в квартале и районе за загруженные кварталы."""
    index = rr_deals.DealIndex(
        [
            ("40:13", 120101, "house", 1000.0, 400_000.0, 1, "2026-1"),
            ("40:13", 120101, "house", 2400.0, 900_000.0, 2, "2026-2"),  # два участка по 12 сот.
            ("40:13", 120101, "garden", 800.0, 100_000.0, 1, "2026-2"),  # другое назначение
            ("40:13", 120102, "house", 900.0, 300_000.0, 1, "2026-2"),
            ("40:13", 120102, "house", 90_000.0, 300_000.0, 1, "2026-2"),  # 9 га — не ровня
        ]
    )
    lot = {"quarter": "40:13:0120101", "area_m2": 1000, "vri_group": "house"}
    assert rr_deals.turnover(lot, index) == {
        "quarter_n": 3,
        "district_n": 4,
        "periods": 2,
        "span": "1 кв. 2026 – 2 кв. 2026",
    }
    # в соседнем квартале сделок не было, в районе — те же четыре
    other = rr_deals.turnover({**lot, "quarter": "40:13:120109"}, index)
    assert (other["quarter_n"], other["district_n"]) == (0, 4)
    # счёт поиском по отсортированным площадям совпадает с перебором
    assert index.count(("40:13", "house"), 200.0, 5000.0) == 4
    assert index.count(("40:13", "house"), 1000.0, 1000.0) == 1
    assert index.count(("40:13", 120101, "garden"), 0.0, 1e9) == 1
    assert index.count(("40:99", "house"), 0.0, 1e9) == 0
    # «прочее» и лот без квартала не оцениваем
    assert rr_deals.turnover({**lot, "vri_group": "other"}, index) is None
    assert rr_deals.turnover({**lot, "quarter": None}, index) is None
    assert rr_deals.turnover(lot, None) is None
