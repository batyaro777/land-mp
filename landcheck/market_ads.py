"""Объявления о продаже участков в локации (R6): цены предложений с досок через поиск.

Берём только результаты, где в заголовке или сниппете есть и цена, и площадь: считаем ₽/сот,
отсекаем выбросы, отдаём список со ссылками и медиану. Это цены предложений, не сделки.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from landcheck.models import SOTKA_M2

KIND_QUERY = {
    "izhs": "участок ИЖС",
    "snt": "участок СНТ дача",
    "agri": "земля сельхозназначения участок",
    "industry": "участок промназначения",
    "other": "земельный участок",
}
MAX_ADS = 15
# «2 500 000 ₽», «2,5 млн», «850 тыс. руб», «1.2 млн ₽»
_PRICE_RE = re.compile(
    r"(?<![\d:])(\d{1,3}(?:[ \u00a0\u202f]\d{3}){1,3}|\d+(?:[.,]\d+)?)\s*(млн|тыс\.?|₽|руб)",
    re.IGNORECASE,
)
# «10 сот», «12,5 соток», «1,2 га», «1200 м²», «1200 кв. м»
_AREA_RE = re.compile(
    r"(?<![\d:])(\d+(?:[.,]\d+)?)\s*(сот[а-я.]*|га\b|м²|м2|кв\.?\s*м)", re.IGNORECASE
)
_LOOKUP = (
    "egrp365",
    "rosreestr",
    "pkk",
    "kadastr",
    "nspd.gov",
    "zemelyabot",
    "geo-risk",
    "zembaza",
)
# Страницы-каталоги агрегаторов («Купить земельный участок в с. Чепош — 37 объявлений»):
# цена там первая попавшаяся из списка, а не конкретного участка. Признаки: счётчик
# объявлений, множественное число в начале заголовка, каталожный путь без id объявления.
# «Продам …» — наоборот, типичное одиночное объявление.
_CATALOG_RE = re.compile(
    r"\d+\s*(объявлени|предложени|вариант)|каталог|все объявления|по цене от"
    r"|^(купить|продажа|продаю?тся)?\s*(земельн\w+\s+)?участк(и|ов)\b",
    re.IGNORECASE,
)
_CATALOG_START_RE = re.compile(r"^(купить|продажа)\b", re.IGNORECASE)
_CATALOG_PATHS = (
    "/pokupka/",
    "/kupit-",
    "/prodazha-",
    "/zemelnye-uchastki",
    "/zemelnye_uchastki/prodam-",
)
_AD_ID_RE = re.compile(r"\d{6,}")  # у страницы одного объявления в пути есть числовой id
_LEAD_JUNK_RE = re.compile(r"^[^A-Za-zА-Яа-яЁё0-9]+")  # «ᐉ» (буква по \w), «➤», кавычки
# Дом с участком — цена дома, а не земли: в оценку земли не берём
_HOUSE_RE = re.compile(
    r"(\bдом[а-я]*\b|коттедж|таунхаус|дуплекс|\bбаня\b|дача с домом)", re.IGNORECASE
)


def _price_rub(m: re.Match) -> float | None:
    num = m.group(1).replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    if "," in num and "." not in num:
        num = num.replace(",", ".")
    try:
        v = float(num)
    except ValueError:
        return None
    unit = m.group(2).lower()
    if unit.startswith("млн"):
        v *= 1_000_000
    elif unit.startswith("тыс"):
        v *= 1_000
    return v if 50_000 <= v <= 5_000_000_000 else None


def _area_m2(m: re.Match) -> float | None:
    v = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit.startswith("га"):
        v *= 10_000
    elif unit.startswith("сот"):
        v *= SOTKA_M2
    return v if 100 <= v <= 5_000_000 else None


def is_catalog(title: str, snippet: str, url: str) -> bool:
    """Страница списка объявлений, а не одного участка."""
    t = _LEAD_JUNK_RE.sub("", title)
    if _CATALOG_RE.search(t) or re.search(r"\d+\s*объявлени", snippet, re.IGNORECASE):
        return True
    path = urlparse(url).path.lower()
    if _AD_ID_RE.search(path):  # конкретное объявление: /card/sale__land__2165263839
        return False
    return bool(_CATALOG_START_RE.search(t)) or any(x in path for x in _CATALOG_PATHS)


def parse_ad(title: str, snippet: str) -> dict[str, float] | None:
    """Цена и площадь из текста объявления; None, если чего-то нет."""
    text = f"{title} {snippet}"
    price = next((p for m in _PRICE_RE.finditer(text) if (p := _price_rub(m))), None)
    # Площадь земли — сотки или гектары; «м²» берём, только если соток/га в тексте нет
    # (в «дом 120 м² на участке 10 сот» нужна земля).
    matches = [m for m in _AREA_RE.finditer(text) if _area_m2(m)]
    land = [m for m in matches if m.group(2).lower().startswith(("сот", "га"))]
    area = _area_m2((land or matches)[0]) if (land or matches) else None
    if not price or not area:
        return None
    return {"price_rub": price, "area_m2": area, "pps": price / (area / SOTKA_M2)}


def _quantile(values: list[float], q: float) -> float:
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def build_queries(locality_short: str, kind: str) -> list[str]:
    what = KIND_QUERY.get(kind, KIND_QUERY["other"])
    return [f"{locality_short} {what} купить цена", f"{locality_short} {what} продажа соток"]


AREA_BAND = 5.0  # сопоставимая площадь: от 1/5 до 5 площадей участка


def collect_ads(
    locality_short: str,
    kind: str,
    *,
    area_m2: float | None = None,
    search_fn=None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Объявления в локации; при известной площади участка несопоставимые по площади
    (73 га против 10 соток ИЖС) уходят в off_scale и в медиану не входят."""
    from landcheck import websearch

    search_fn = search_fn or websearch.search
    seen: set[str] = set()
    ads: list[dict[str, Any]] = []
    off_scale: list[dict[str, Any]] = []
    errors: list[str] = []
    queries = build_queries(locality_short, kind)
    stem = locality_short.lower()[:-1] if len(locality_short) > 5 else locality_short.lower()
    for q in queries:
        res = search_fn(q, max_results=max_results, news=False, recent=True)
        if res.get("error"):
            errors.append(res["error"])
            continue
        for it in res.get("results") or []:
            url = (it.get("url") or "").split("#")[0]
            host = urlparse(url).netloc.lower().removeprefix("www.")
            if not url or url in seen or any(x in host for x in _LOOKUP):
                continue
            title, snippet = it.get("title") or "", it.get("snippet") or ""
            if stem and stem not in f"{title} {snippet}".lower():
                continue
            if _HOUSE_RE.search(title) or is_catalog(title, snippet, url):
                continue
            parsed = parse_ad(title, snippet)
            if not parsed:
                continue
            seen.add(url)
            if (
                area_m2
                and area_m2 >= 100
                and not (area_m2 / AREA_BAND <= parsed["area_m2"] <= area_m2 * AREA_BAND)
            ):
                off_scale.append({"title": title[:120], "url": url, "host": host, **parsed})
                continue
            twin = next(
                (
                    a
                    for a in ads
                    if abs(a["price_rub"] - parsed["price_rub"]) < 1
                    and abs(a["area_m2"] - parsed["area_m2"]) < 1
                ),
                None,
            )
            if twin is not None:  # то же объявление на другой площадке
                twin.setdefault("also", []).append(host)
                continue
            ads.append(
                {"title": title[:120], "url": url, "host": host, "date": it.get("date"), **parsed}
            )
    # выбросы: цена за сотку дальше чем в 5 раз от медианы — скорее всего, не тот участок/единица
    if len(ads) >= 4:
        med = _quantile([a["pps"] for a in ads], 0.5)
        ads = [a for a in ads if med / 5 <= a["pps"] <= med * 5]
    ads.sort(key=lambda a: a["pps"])
    ads = ads[:MAX_ADS]
    pps = [a["pps"] for a in ads]
    return {
        "ads": ads,
        "n": len(ads),
        "off_scale": off_scale[:MAX_ADS],
        "median": _quantile(pps, 0.5) if pps else None,
        "p25": _quantile(pps, 0.25) if pps else None,
        "p75": _quantile(pps, 0.75) if pps else None,
        "queries": queries,
        "fetched_at": datetime.now(UTC).isoformat(),
        "error": "; ".join(dict.fromkeys(errors)) if errors and not ads else None,
    }
