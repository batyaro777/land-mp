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
from landcheck.region import place_stem

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
# Справочники по кадастру и соцсети: стена группы vk.ru/public… — не объявление
_LOOKUP = (
    "vk.com",
    "vk.ru",
    "ok.ru",
    "t.me",
    "youtube.",
    "instagram.com",
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
    r"|\bс/п\b|сельское поселение"
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
# У страницы одного объявления в пути есть числовой id после «/» или «_» (cian /sale/suburban/312…/,
# domclick sale__land__216…, avito …_123…); код локации cian «-cheposh-01164480/» стоит после дефиса
_AD_ID_RE = re.compile(r"(?<=[/_])\d{6,}")
_LEAD_JUNK_RE = re.compile(r"^[^A-Za-zА-Яа-яЁё0-9]+")  # «ᐉ» (буква по \w), «➤», кавычки
# olan: в адресе страницы зашиты площадь и цена («…/163940237-11-0-sot-1799999-rub»); если они
# не совпадают со сниппетом — поиск показал соседнюю карточку из списка, не это объявление
_SLUG_AREA_RE = re.compile(r"(?<=[/-])(\d+)-(\d+)-sot(?=[-/]|$)")
_SLUG_PRICE_RE = re.compile(r"(?<=[/-])(\d{5,})-rub(?=[-/]|$)")
# Дом с участком — цена дома, а не земли: в оценку земли не берём
_HOUSE_RE = re.compile(
    r"(\bдом(?!клик)[а-я]*\b|коттедж|таунхаус|дуплекс|\bбаня\b|дача с домом)", re.IGNORECASE
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


def _list_like(snippet: str) -> bool:
    """Сниппет со списком карточек (olan, mirkvartir): несколько разных площадей и цен."""
    areas = {
        m.group(1)
        for m in _AREA_RE.finditer(snippet)
        if m.group(2).lower().startswith(("сот", "га")) and _area_m2(m)
    }
    prices = {m.group(1) for m in _PRICE_RE.finditer(snippet) if _price_rub(m)}
    return len(areas) >= 2 and len(prices) >= 2


def is_catalog(title: str, snippet: str, url: str) -> bool:
    """Страница списка объявлений, а не одного участка."""
    t = _LEAD_JUNK_RE.sub("", title)
    if _CATALOG_RE.search(t) or re.search(r"\d+\s*объявлени", snippet, re.IGNORECASE):
        return True
    if _list_like(snippet):
        return True
    path = urlparse(url).path.lower()
    if _AD_ID_RE.search(path):  # конкретное объявление: /card/sale__land__2165263839
        return False
    return bool(_CATALOG_START_RE.search(t)) or any(x in path for x in _CATALOG_PATHS)


def slug_mismatch(url: str, parsed: dict[str, float]) -> bool:
    """Площадь/цена в адресе страницы есть и расходятся с распарсенными из сниппета."""
    path = urlparse(url).path.lower()
    pm, am = _SLUG_PRICE_RE.search(path), _SLUG_AREA_RE.search(path)
    if pm and abs(float(pm.group(1)) - parsed["price_rub"]) > parsed["price_rub"] * 0.02:
        return True
    if am:
        sot = float(f"{am.group(1)}.{am.group(2)}") * SOTKA_M2
        if abs(sot - parsed["area_m2"]) > parsed["area_m2"] * 0.05:
            return True
    return False


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


# Аренда и переуступка права аренды, выставленные как продажа земли: в цену собственности не
# берём (зум 13.11, 24:48: объявления по 70 тыс. за сотку на Avito — аренда под видом продажи)
_LEASE_RE = re.compile(r"аренд|переуступ", re.IGNORECASE)
# Цена за сотку ниже медианы во столько раз и больше — подозрительно дёшево: в медиану не идёт,
# показывается отдельно для проверки глазами (зум 13.11, 25:20: «отсекать неадекватные цены»)
CHEAP_RATIO = 2.5


def build_queries(locality_short: str, kind: str) -> list[str]:
    what = KIND_QUERY.get(kind, KIND_QUERY["other"])
    return [f"{locality_short} {what} купить цена", f"{locality_short} {what} продажа соток"]


AREA_BAND = 5.0  # сопоставимая площадь: от 1/5 до 5 площадей участка


MIN_N = 3  # столько сопоставимых объявлений нужно для рыночного диапазона (web.content)


def district_fallback(locality: str | None) -> dict[str, str] | None:
    """Запасной охват для маленького села: район («Козельский район», основа «козельск»).
    Рынок деревни Вырск в выдаче есть только на уровне Козельска и района (Клавдий, 06.11)."""
    from landcheck.region import district_of, district_stem

    if not locality:
        return None
    d, stem = district_of(locality), district_stem(locality)
    if not d or not stem:
        return None
    name = re.sub(r"\s*(район|р-н)\s*", " ", d, flags=re.IGNORECASE).strip()
    return {"place": f"{name} район", "stem": stem, "label": f"{name} район"}


def collect_ads(
    locality_short: str,
    kind: str,
    *,
    area_m2: float | None = None,
    search_fn=None,
    max_results: int = 10,
    fallback: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Объявления в локации; при известной площади участка несопоставимые по площади
    (73 га против 10 соток ИЖС) уходят в off_scale и в медиану не входят.
    Если в населённом пункте сопоставимых меньше MIN_N, а задан fallback (район) — добираем
    объявлениями района с пометкой scope="district"."""
    from landcheck import websearch

    search_fn = search_fn or websearch.search
    seen: set[str] = set()
    ads: list[dict[str, Any]] = []
    off_scale: list[dict[str, Any]] = []
    leasehold: list[dict[str, Any]] = []
    errors: list[str] = []
    queries: list[str] = []

    def gather(place: str, stem: str | None, scope: str) -> None:
        for q in build_queries(place, kind):
            queries.append(q)
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
                if stem and stem not in f"{title} {snippet}".lower().replace("ё", "е"):
                    continue
                if _HOUSE_RE.search(title) or is_catalog(title, snippet, url):
                    continue
                parsed = parse_ad(title, snippet)
                if not parsed or slug_mismatch(url, parsed):
                    continue
                seen.add(url)
                row = {"title": title[:120], "url": url, "host": host, "scope": scope, **parsed}
                if _LEASE_RE.search(f"{title} {snippet}"):
                    leasehold.append(row)
                    continue
                if (
                    area_m2
                    and area_m2 >= 100
                    and not (area_m2 / AREA_BAND <= parsed["area_m2"] <= area_m2 * AREA_BAND)
                ):
                    off_scale.append(row)
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
                ads.append({**row, "date": it.get("date")})

    gather(locality_short, place_stem(locality_short), "locality")
    n_local = len(ads)
    scope, scope_label = "locality", locality_short
    if n_local < MIN_N and fallback:
        gather(fallback["place"], fallback.get("stem"), "district")
        if len(ads) > n_local:
            scope, scope_label = "district", fallback.get("label") or fallback["place"]
    # выбросы: цена за сотку дальше чем в 5 раз от медианы — скорее всего, не тот участок/единица
    cheap: list[dict[str, Any]] = []
    if len(ads) >= 4:
        med = _quantile([a["pps"] for a in ads], 0.5)
        ads = [a for a in ads if med / 5 <= a["pps"] <= med * 5]
        cheap = [a for a in ads if a["pps"] < med / CHEAP_RATIO]
        ads = [a for a in ads if a["pps"] >= med / CHEAP_RATIO]
    ads.sort(key=lambda a: a["pps"])
    ads = ads[:MAX_ADS]
    pps = [a["pps"] for a in ads]
    return {
        "ads": ads,
        "n": len(ads),
        "n_local": sum(1 for a in ads if a.get("scope") == "locality"),
        "scope": scope,
        "scope_label": scope_label,
        "off_scale": off_scale[:MAX_ADS],
        "leasehold": leasehold[:MAX_ADS],
        "cheap": sorted(cheap, key=lambda a: a["pps"])[:MAX_ADS],
        "median": _quantile(pps, 0.5) if pps else None,
        "p25": _quantile(pps, 0.25) if pps else None,
        "p75": _quantile(pps, 0.75) if pps else None,
        "queries": queries,
        "fetched_at": datetime.now(UTC).isoformat(),
        "error": "; ".join(dict.fromkeys(errors)) if errors and not ads else None,
    }
