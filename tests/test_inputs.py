from pathlib import Path

from landcheck.inputs import dedupe, load_listings, parse_csv, parse_number, parse_text


def test_parse_number_variants():
    assert parse_number("1 200 000") == 1_200_000
    assert parse_number("1,2 млн") == 1_200_000
    assert parse_number("850 тыс") == 850_000
    assert parse_number("12 сот") == 12
    assert parse_number("abc") is None


def test_parse_text_cn_price_area():
    text = (
        "50:09:0060202:101; 2 500 000; 10\n"
        "50:09:0060202:102 1,5 млн 8 сот https://avito.ru/x\n"
        "50:09:0060202:103\n"
        "50 : 09 : 0060202 : 104; 12 сот\n"
        "строка без номера\n"
    )
    items = parse_text(text)
    assert [i.cad_num for i in items] == [
        "50:09:0060202:101",
        "50:09:0060202:102",
        "50:09:0060202:103",
        "50:09:0060202:104",
    ]
    assert items[0].price_rub == 2_500_000 and items[0].declared_area_m2 == 1000
    assert items[1].price_rub == 1_500_000 and items[1].declared_area_m2 == 800
    assert items[1].url == "https://avito.ru/x"
    assert items[2].price_rub is None and items[2].declared_area_m2 is None
    assert items[3].declared_area_m2 == 1200


def test_parse_csv_with_headers_units():
    text = "КН;Цена, тыс. руб;Площадь, м2\n50:09:0060202:101;2500;1000\n"
    items = parse_csv(text)
    assert items[0].price_rub == 2_500_000
    assert items[0].declared_area_m2 == 1000


def test_parse_csv_without_header_falls_back_to_text():
    text = "50:09:0060202:101;2500000;10\n50:09:0060202:102;1000000;8\n"
    items = parse_csv(text)
    assert len(items) == 2 and items[1].declared_area_m2 == 800


def test_load_listings_xlsx(tmp_path: Path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Кадастровый номер", "Цена", "Площадь (соток)"])
    ws.append(["50:09:0060202:101", 2_500_000, 10])
    ws.append(["50:09:0060202:102", "1,2 млн", 8.5])
    p = tmp_path / "in.xlsx"
    wb.save(p)
    items = load_listings(p)
    assert len(items) == 2
    assert items[1].price_rub == 1_200_000 and items[1].declared_area_m2 == 850


def test_dedupe_merges_and_keeps_first_values():
    items = parse_text(
        "50:09:0060202:101; 2500000; 10\n50:09:0060202:101; 2400000; 10\n50:09:0060202:102\n"
    )
    groups = dedupe(items)
    assert len(groups) == 2
    primary, dups = groups[0]
    assert primary.price_rub == 2_500_000 and len(dups) == 1
