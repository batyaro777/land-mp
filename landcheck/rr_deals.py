"""Цены зарегистрированных сделок с земельными участками из открытых дата-сетов Росреестра —
фактический рынок рядом с лотом для анализатора торгов.

Источник: раздел «Государственные дата-сеты» (`SOURCE_URL`), набор «Данные о ценах
регистрируемых сделок (по отчуждению)…». Раз в квартал Росреестр выкладывает файл по всей
стране: CSV с разделителем «~», строка — группа одинаковых сделок в кадастровом квартале.
Проверено на файле за 3 квартал 2026 года (18.11.2026, 1 190 032 строки, из них 355 724 по
земельным участкам):

- кадастровых номеров и точных дат нет — только квартал и первое число отчётного квартала;
- `area` и `deal_price` в строке — суммы по группе, `deals_number` — число объектов. Цена за
  м² = `deal_price / area` при любом числе сделок (медиана за м² по группам 1 / 2 / 3–5 / 6+
  сделок почти одна и та же), площадь одного участка = `area / deals_number`;
- назначение — код классификатора видов использования земель в `purpose_code`;
- 11,6 % строк с нулевой ценой (с версии 2 они идут отдельными строками), 3 % ненулевых —
  дешевле рубля за метр; персональных данных нет.

Условия сайта Росреестра: информация «бесплатной и свободной для копирования, последующего
использования и распространения», при публикации в интернете нужна гиперссылка на страницу
с данными — она выводится рядом с каждой оценкой.

Чего ждать от цифр: цена в договоре бывает занижена, а среди сделок есть льготный выкуп
участка у муниципалитета. Поэтому это скорее нижняя граница рынка, а не его середина.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin

SOURCE_URL = "https://rosreestr.gov.ru/open-service/data-sety-rosreestra/"
LISTING_URL = "https://rosreestr.gov.ru/data-sets/"
LAND_CODE = "002001001000"
DATA_PATH = Path(__file__).resolve().parent / "data" / "rosreestr_purpose_codes.tsv"

PRICE_M2_MIN, PRICE_M2_MAX = 0.1, 1_000_000.0  # вне этих границ — описка, а не цена
AREA_BAND = 5.0  # сопоставимая площадь: от 1/5 до 5 площадей лота (как у торгов)
MIN_QUARTER = 3  # столько сделок в квартале хватает для оценки
MIN_DISTRICT = 10  # в целом районе участки разнороднее — сделок нужно больше
TRIM = 5.0  # цена за м² дальше медианы в пять раз — в расчёт не идёт
MAX_SHOWN = 15
# Версия разбора: файл, отклонённый этой версией, заново не качаем, пока она не сменится.
PARSER_REV = 1

# Группа назначения по коду классификатора видов использования земель (57_УНСИ). Группы те
# же, что у лотов (`torgi_scan.GROUPS`): цена за метр у поля и у участка под дом в одном
# квартале различается в десятки раз, поэтому чужое назначение в сравнение не берём.
_GROUP_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("141003", "housing"),  # личное подсобное хозяйство
    ("141004", "garden"),  # садоводство и огородничество
    ("141006", "garden"),  # дачное строительство
    ("141000", "agri"),
    ("141001", "agri"),
    ("141002", "agri"),
    ("141005", "agri"),
    ("141010", "agri"),
    ("141013", "agri"),
    ("141014", "agri"),
    ("142001010000", "housing"),  # индивидуальная жилая застройка
    ("142001000000", "housing"),
    ("142001020100", "housing"),  # малоэтажная
    ("142001020400", "housing"),
    ("142002110000", "garage"),
    ("142002150000", "garage"),
    ("142002160000", "garage"),
    ("142002000000", "commerce"),
    ("14200204", "commerce"),  # торговля
    ("142002050000", "commerce"),
    ("142002060000", "commerce"),
    ("142002080000", "commerce"),
    ("142002120000", "commerce"),
    ("142002130000", "commerce"),
    ("142002140000", "commerce"),
    ("142002170000", "commerce"),
    ("142005", "industry"),  # коммунальные и складские объекты
    ("143005", "other"),  # космос
    ("143006", "other"),  # оборона и граница
    ("143", "industry"),
    ("144001070000", "recreation"),  # санаторно-курортные
    ("144003", "recreation"),
)


@lru_cache(maxsize=1)
def purpose_names() -> dict[str, str]:
    """Код вида использования → наименование из справочника Росреестра."""
    out: dict[str, str] = {}
    for line in DATA_PATH.read_text(encoding="utf-8").splitlines():
        code, _, name = line.partition("\t")
        if code and name:
            out[code] = name
    return out


def purpose_group(code: str | None) -> str:
    """Группа назначения по коду; пустой или неизвестный код — «прочее»."""
    code = (code or "").strip()
    if code not in purpose_names():
        return "other"
    for prefix, group in _GROUP_BY_PREFIX:
        if code.startswith(prefix):
            return group
    return "other"


def quarter_key(quarter: str | None) -> tuple[str, str, int] | None:
    """`24:50:0100537` → («24», «50», 100537). Третья часть — числом: в разных источниках
    квартал пишут и с ведущим нулём, и без."""
    parts = (quarter or "").strip().split(":")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return None
    if not int(parts[0]) or not int(parts[2]):
        return None  # «0:0:0» и «24:29:0000000» — заглушки, не кварталы
    return parts[0].zfill(2), parts[1].zfill(2), int(parts[2])


def period_of(date_text: str | None) -> str | None:
    """«2026-07-01» → «2026Q3» (в файле — первое число отчётного квартала)."""
    m = re.match(r"(\d{4})-(\d{2})-\d{2}$", (date_text or "").strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return f"{m.group(1)}Q{(int(m.group(2)) - 1) // 3 + 1}"


def period_label(period: str) -> str:
    """«2026Q3» → «3 кв. 2026»."""
    return f"{period[5:]} кв. {period[:4]}" if len(period) == 6 else period


def _num(value: str | None) -> float | None:
    try:
        return float((value or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def parse_rows(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Строки файла сделок → сделки с земельными участками с ценой. Колонки берём по именам
    из заголовка: порядок в следующих выпусках может поменяться. Строка-результат:
    `region` (первые две цифры квартала), `district` («24:50»), `q` (номер квартала числом),
    `group`, `area` и `price` (суммы по группе), `n` (число сделок), `period`."""
    reader = csv.reader(lines, delimiter="~", quotechar='"')
    header = next(reader, None)
    if not header:
        return
    col = {name.strip().lstrip("﻿"): i for i, name in enumerate(header)}
    need = ("quarter_cad_number", "realestate_type_code", "area", "deal_price", "deals_number")
    if any(name not in col for name in need):
        raise ValueError("в файле сделок нет ожидаемых колонок: " + ", ".join(header)[:200])

    def cell(row: list[str], name: str) -> str:
        i = col.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    for row in reader:
        if cell(row, "realestate_type_code") != LAND_CODE:
            continue
        if not cell(row, "currency").lower().startswith("руб"):
            continue
        key = quarter_key(cell(row, "quarter_cad_number"))
        area, price = _num(cell(row, "area")), _num(cell(row, "deal_price"))
        n = _num(cell(row, "deals_number"))
        period = period_of(cell(row, "period_start_date"))
        if key is None or not area or not price or not n or period is None:
            continue
        if area <= 0 or n < 1 or not PRICE_M2_MIN <= price / area <= PRICE_M2_MAX:
            continue
        yield {
            "region": key[0],
            "district": f"{key[0]}:{key[1]}",
            "q": key[2],
            "group": purpose_group(cell(row, "purpose_code")),
            "area": area,
            "price": price,
            "n": int(n),
            "period": period,
        }


# --- где лежат файлы ---------------------------------------------------------------------------
_HREF_RE = re.compile(r'href="([^"?#]+)"', re.IGNORECASE)
_FOLDER_RE = re.compile(r"^([1-4])\s*квартал\s*(\d{4})", re.IGNORECASE)
_FILE_RE = re.compile(r"_y_(\d{4})_q_([1-4])")


def _name(href: str) -> str:
    """Имя из ссылки листинга: у Росреестра оно закодировано в cp1251, не в UTF-8."""
    raw = href.rstrip("/").rsplit("/", 1)[-1]
    try:
        return unquote(raw, encoding="cp1251", errors="strict")
    except UnicodeDecodeError:
        return unquote(raw)


def listing_folders(html: str, base: str = LISTING_URL) -> list[tuple[str, str]]:
    """Папки кварталов из листинга каталога: (период «2026Q3», адрес), новые первыми. Адрес
    собираем из ссылки как есть — перекодировать её нельзя, сервер ждёт cp1251."""
    out = {}
    for href in _HREF_RE.findall(html):
        if not href.endswith("/") or href.startswith(("/", "..")):
            continue
        m = _FOLDER_RE.match(_name(href))
        if m:
            out[f"{m.group(2)}Q{m.group(1)}"] = urljoin(base, href)
    return sorted(out.items(), reverse=True)


def listing_files(html: str, base: str, kind: str = "СДЕЛКИ") -> list[tuple[str, str]]:
    """Файлы набора в папке квартала: (период из имени файла, адрес). Квартал может лежать
    одним файлом на всю страну или несколькими по группам регионов."""
    out = []
    for href in _HREF_RE.findall(html):
        name = _name(href)
        m = _FILE_RE.search(name)
        if m and kind in name and name.lower().endswith(".csv.zip"):
            out.append((f"{m.group(1)}Q{m.group(2)}", urljoin(base, href)))
    return sorted(set(out))


# --- цена для лота -----------------------------------------------------------------------------
def _wquantile(pairs: list[tuple[float, int]], q: float) -> float:
    """Квантиль цены, где у каждой цены вес — число сделок в строке."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    target, acc = total * q, 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= target:
            return value
    return pairs[-1][0]


class DealIndex:
    """Сделки кадастрового округа (первые две цифры номера), разложенные по кварталам и
    районам и сразу по группам назначения: в районе тысячи строк, перебирать их все для
    каждого лота долго. Строка — (площадь одного участка, цена за м², число сделок, период)."""

    def __init__(self, rows: Iterable[tuple[str, int, str, float, float, int, str]]):
        self.by_quarter: dict[tuple[str, int, str], list[tuple]] = {}
        self.by_district: dict[tuple[str, str], list[tuple]] = {}
        self.size = 0
        self.periods: set[str] = set()
        for district, q, group, area, price, n, period in rows:
            item = (area / n, price / area, n, period)
            self.by_quarter.setdefault((district, q, group), []).append(item)
            self.by_district.setdefault((district, group), []).append(item)
            self.size += 1
            self.periods.add(period)


def deals_market(lot: dict[str, Any], index: DealIndex | None) -> dict[str, Any] | None:
    """Цена перепродажи лота по зарегистрированным сделкам — в том виде, какой ждёт
    `torgi_scan.market_for`: три сценария за м² (25 %, медиана, 75 % по сделкам) и подпись.

    Сопоставимые сделки: та же группа назначения, площадь участка от 1/5 до 5 площадей лота.
    Сначала свой кадастровый квартал (не меньше `MIN_QUARTER` сделок), потом район (не меньше
    `MIN_DISTRICT`). Цены дальше медианы в пять раз отбрасываются: это описки и символические
    суммы. Лот с назначением «прочее» не оцениваем — в этой группе участки несравнимы."""
    key = quarter_key(lot.get("quarter"))
    area, group = lot.get("area_m2"), lot.get("vri_group")
    if index is None or not index.size or key is None or not area or group in (None, "other"):
        return None
    district = f"{key[0]}:{key[1]}"
    lo, hi = area / AREA_BAND, area * AREA_BAND
    for level, rows, need in (
        ("quarter", index.by_quarter.get((district, key[2], group), ()), MIN_QUARTER),
        ("district", index.by_district.get((district, group), ()), MIN_DISTRICT),
    ):
        pool = [r for r in rows if lo <= r[0] <= hi]
        if sum(r[2] for r in pool) < need:
            continue
        med = _wquantile([(r[1], r[2]) for r in pool], 0.5)
        kept = [r for r in pool if med / TRIM <= r[1] <= med * TRIM]
        n = sum(r[2] for r in kept)
        if n < need:
            continue
        pairs = [(r[1], r[2]) for r in kept]
        periods = sorted({r[3] for r in kept})
        span = period_label(periods[0])
        if len(periods) > 1:
            span += " – " + period_label(periods[-1])
        where = "квартал" if level == "quarter" else "район"
        note = (
            "та же группа назначения, площадь от 1/5 до 5 площадей лота; цена в договоре "
            "бывает занижена, среди сделок есть льготный выкуп у муниципалитета — это скорее "
            "нижняя граница рынка"
        )
        dropped = sum(r[2] for r in pool) - n
        if dropped:
            note += f"; не учтено сделок с ценой дальше медианы в пять раз: {dropped}"
        return {
            "min_m2": _wquantile(pairs, 0.25),
            "mid_m2": _wquantile(pairs, 0.5),
            "max_m2": _wquantile(pairs, 0.75),
            "source": "rr",
            "source_label": f"сделки Росреестра: {where}, {n} за {span}",
            # квартал — свой микрорайон или село; район мешает сёла с райцентром
            "confidence": 2 if level == "quarter" else 1,
            "exposure_days": None,
            "liquid": None,
            "note": note,
            "deals_n": n,
            "deals_lo_m2": min(p for p, _ in pairs),
            "deals_hi_m2": max(p for p, _ in pairs),
            "deals_level": level,
            "deals_span": span,
            # для карточки лота: сами сделки, свежие первыми
            "deals": [
                {"period": period_label(r[3]), "area_m2": r[0], "pps": r[1] * 100, "n": r[2]}
                for r in sorted(kept, key=lambda r: (r[3], r[1]), reverse=True)[:MAX_SHOWN]
            ],
        }
    return None
