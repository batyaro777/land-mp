"""Объявления о продаже земли из ads-api.ru: клиент ленты, разбор объявления и цена рынка
рядом с участком.

Зачем: «интересен» анализатор торгов ставит только при рыночной цене перепродажи, а её почти
нигде нет (прод, 17.11.2026: цена рынка у 3 лотов из 398). Поиск объявлений через открытую
выдачу нестабилен. Лента ads-api.ru отдаёт объявления Авито, Циан, Яндекс Недвижимости с
ценой и координатами (документация /api и /apiorder прочитана 18.11.2026).

Из объявления берём цену, площадь, категорию земли, точку, заголовок, адрес и ссылку.
Телефон, имя продавца и текст описания не читаем и не храним.
"""

from __future__ import annotations

import math
import ssl
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from landcheck import market_ads, ruproxy
from landcheck.models import SOTKA_M2
from landcheck.regions import CADASTRAL_REGIONS

API_URL = "https://ads-api.ru/main/api"
CATEGORY_LAND = 5  # «Земельные участки»
DEAL_SALE = 1  # nedvigimost_type: «Продам»
MIN_INTERVAL_S = 5.5  # сервис: между запросами не меньше 5 секунд, иначе 429
PAGE_LIMIT = 1000  # платный доступ; тестовый отдаёт не больше 50
MSK = timezone(timedelta(hours=3))  # время в ленте московское
TIME_FMT = "%Y-%m-%d %H:%M:%S"
TRANSPORT_RETRY_S = (10.0, 30.0)
RATE_RETRY = 2

# Названия регионов, как их принимает параметр `city` (список со страницы /api, дословно).
REGION_NAMES = frozenset(
    (
        "Москва, Санкт-Петербург, Севастополь, Адыгея, Алтай, Алтайский край, Амурская область, "
        "Архангельская область, Астраханская область, Башкортостан, Белгородская область, "
        "Брянская область, Бурятия, Владимирская область, Волгоградская область, "
        "Вологодская область, Воронежская область, Дагестан, Еврейская автономная область, "
        "Забайкальский край, Ивановская область, Ингушетия, Иркутская область, "
        "Кабардино-Балкария, Калининградская область, Калмыкия, Калужская область, "
        "Камчатский край, Карачаево-Черкесия, Карелия, Кемеровская область, Кировская область, "
        "Коми, Костромская область, Краснодарский край, Красноярский край, Крым, "
        "Курганская область, Курская область, Ленинградская область, Липецкая область, "
        "Магаданская область, Марий Эл, Мордовия, Московская область, Мурманская область, "
        "Нижегородская область, Новгородская область, Новосибирская область, Омская область, "
        "Оренбургская область, Орловская область, Пензенская область, Пермский край, "
        "Приморский край, Псковская область, Ростовская область, Рязанская область, "
        "Самарская область, Саратовская область, Саха (Якутия), Сахалинская область, "
        "Свердловская область, Северная Осетия, Смоленская область, Ставропольский край, "
        "Тамбовская область, Татарстан, Тверская область, Томская область, Тульская область, "
        "Тыва, Тюменская область, Удмуртия, Ульяновская область, Хабаровский край, Хакасия, "
        "Ханты-Мансийский АО, Челябинская область, Чеченская Республика, Чувашия, Чукотский АО, "
        "Ямало-Ненецкий АО, Ярославская область"
    ).split(", ")
)
# Где название ленты не получается из нашего отбрасыванием слова «Республика».
_NAME_FIX = {
    "07": "Кабардино-Балкария",
    "09": "Карачаево-Черкесия",
    "18": "Удмуртия",
    "20": "Чеченская Республика",
    "21": "Чувашия",
    "86": "Ханты-Мансийский АО",
    "87": "Чукотский АО",
    "89": "Ямало-Ненецкий АО",
}


def region_name(code: str | None) -> str | None:
    """Название региона для ленты по кадастровому коду; None — такого региона в ленте нет
    (Ненецкий автономный округ отдельно не выделен)."""
    if not code:
        return None
    name = _NAME_FIX.get(code) or (CADASTRAL_REGIONS.get(code) or "").removeprefix("Республика ")
    return name if name in REGION_NAMES else None


class AdsApiError(RuntimeError):
    """Отказ ленты: код из ответа и текст. Адрес запроса сюда не попадает — в нём ключ."""

    def __init__(self, code: int, text: str):
        super().__init__(f"{code}: {text}" if code else text)
        self.code = code
        self.text = text


# Ключ неверный, срок доступа истёк, учётная запись закрыта или тариф не даёт этот метод:
# повторять запрос бессмысленно, нужен человек.
FATAL_CODES = frozenset({401, 402, 403, 452, 453, 454, 455})
CODE_RU = {
    401: "неверный логин или ключ",
    402: "срок доступа закончился — нужна оплата",
    403: "учётная запись заблокирована",
    429: "слишком частые запросы",
    453: "тариф открывает только недвижимость",
    454: "тариф открывает только один город",
    455: "метод доступен только на платном тарифе",
}


def http_client(timeout: float = 60.0) -> httpx.Client:
    """Через RU-шлюз, если он настроен: сервис обещает работу только с российского адреса.
    Сертификаты — системные корни и, если задан, набор шлюза; проверку не отключаем."""
    ctx = ssl.create_default_context()
    bundle = ruproxy.ca_bundle()
    if bundle:
        ctx.load_verify_locations(cafile=bundle)
    return httpx.Client(
        proxy=ruproxy.proxy_url(),
        verify=ctx,
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": ruproxy.UA, "Accept": "application/json"},
    )


def msk_now() -> datetime:
    return datetime.now(MSK).replace(tzinfo=None)


def _shift(stamp: str, seconds: int) -> str:
    return (datetime.strptime(stamp, TIME_FMT) + timedelta(seconds=seconds)).strftime(TIME_FMT)


class AdsApiClient:
    """Лента объявлений о продаже земли по региону. Один запрос — одна страница; между
    запросами пауза, которую требует сервис."""

    def __init__(
        self,
        user: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        limit: int = PAGE_LIMIT,
        interval: float = MIN_INTERVAL_S,
        transport_retry: tuple[float, ...] = TRANSPORT_RETRY_S,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._user, self._token = user, token
        self._own = client is None
        self._c = client or http_client()
        self.limit = max(1, int(limit))
        self._interval = interval
        self._transport_retry = transport_retry
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None
        self.requests = 0

    def close(self) -> None:
        if self._own:
            self._c.close()

    def _get(self, params: dict[str, Any]) -> httpx.Response:
        for pause in (*self._transport_retry, None):
            if self._last is not None:
                left = self._interval - (self._clock() - self._last)
                if left > 0:
                    self._sleep(left)
            self._last = self._clock()
            self.requests += 1
            try:
                return self._c.get(API_URL, params=params)
            except httpx.HTTPError as exc:
                # текст исключения httpx содержит адрес с ключом — наружу только имя ошибки
                if pause is None:
                    raise AdsApiError(0, f"нет связи с лентой: {type(exc).__name__}") from None
                self._sleep(pause)
        raise AssertionError("unreachable")

    def page(self, region: str, date1: str, date2: str) -> list[dict[str, Any]]:
        """Объявления региона, поступившие в ленту с `date1` по `date2` (московское время),
        новые первыми, не больше `limit`."""
        params = {
            "user": self._user,
            "token": self._token,
            "category_id": CATEGORY_LAND,
            "nedvigimost_type": DEAL_SALE,
            "city": region,
            "date1": date1,
            "date2": date2,
            "withcoords": 1,
            "withphone": 0,  # без него лента не отдаёт Авито и Юлу
            "limit": self.limit,
        }
        for attempt in range(RATE_RETRY + 1):
            resp = self._get(params)
            try:
                body = resp.json()
            except ValueError:
                raise AdsApiError(
                    resp.status_code,
                    "ответ не в формате ленты (возможно, запрос ушёл не с российского адреса)",
                ) from None
            code = body.get("code") if isinstance(body, dict) else None
            if code == 200 and isinstance(body.get("data"), list):
                return [x for x in body["data"] if isinstance(x, dict)]
            if code == 429 and attempt < RATE_RETRY:
                self._sleep(self._interval * 2)
                continue
            code = code if isinstance(code, int) else resp.status_code
            text = CODE_RU.get(code) or str((body or {}).get("error") or "отказ ленты")[:200]
            raise AdsApiError(code, text)
        raise AssertionError("unreachable")

    def pages(
        self, region: str, lo: str, hi: str, *, max_pages: int
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        """Окно [lo, hi] от новых к старым. Отдаёт страницу и время, с которого продолжать
        (None — окно пройдено). Постраничного вывода у ленты нет: если пришла полная страница,
        следующий запрос — до времени самого раннего объявления в ней. Секунду прибавляем,
        чтобы не потерять объявления той же секунды; повторы снимаются по номеру."""
        cursor = hi
        for _ in range(max_pages):
            items = self.page(region, lo, cursor)
            times = [t for x in items if isinstance(t := x.get("time"), str) and len(t) == 19]
            if len(items) < self.limit or not times:
                yield items, None
                return
            oldest = min(times)
            nxt = _shift(oldest, 1)
            if nxt >= cursor:  # вся страница — в последней секунде окна
                nxt = oldest if oldest < cursor else _shift(cursor, -1)
            if nxt <= lo:
                yield items, None
                return
            cursor = nxt
            yield items, cursor


# --- разбор объявления -----------------------------------------------------------------------
LAND_CLASSES = {
    "izhs": "земли поселений (ИЖС)",
    "snt": "сельхозназначение (СНТ, ДНП)",
    "prom": "промназначение",
}
# Какие объявления сопоставимы с лотом: категория земли в объявлениях — всего три значения.
# Сад и поле попадают в одну категорию, их разводит площадь (от 1/5 до 5 площадей лота).
CLASS_OF_GROUP = {
    "housing": "izhs",
    "garden": "snt",
    "agri": "snt",
    "industry": "prom",
    "commerce": "prom",
}
PRICE_MIN, PRICE_MAX = 10_000, 5_000_000_000
UNIT_TOLERANCE = 0.05


def land_class(item: dict[str, Any]) -> str | None:
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    text = str(params.get("Категория земель") or item.get("title") or "").lower()
    if "ижс" in text or "поселен" in text:
        return "izhs"
    if "снт" in text or "днп" in text or "сельхоз" in text:
        return "snt"
    if "пром" in text:
        return "prom"
    return None


def _title_area_m2(title: str) -> float | None:
    """Площадь из заголовка («Участок 10 сот. (ИЖС)»): сотки и гектары раньше метров."""
    matches = [m for m in market_ads._AREA_RE.finditer(title) if market_ads._area_m2(m)]
    land = [m for m in matches if m.group(2).lower().startswith(("сот", "га"))]
    return market_ads._area_m2((land or matches)[0]) if (land or matches) else None


def _num(v: Any) -> float | None:
    try:
        out = float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def param_unit(title_m2: float | None, param: float | None) -> str | None:
    """В чём записано поле «Площадь» объявления, если сверить с заголовком. Документация
    единицу не называет, поэтому площадь берём только из заголовка, а здесь копим сверку."""
    if not title_m2 or not param or param <= 0:
        return None
    for unit, k in (("сот", SOTKA_M2), ("м2", 1.0), ("га", 10_000.0)):
        if abs(title_m2 / (param * k) - 1) <= UNIT_TOLERANCE:
            return unit
    return "другое"


def parse_ad(item: dict[str, Any]) -> dict[str, Any] | None:
    """Объявление ленты → строка для базы. `skip` — почему в цену не годится (None — годится).
    None — у объявления нет номера."""
    try:
        ad_id = int(item.get("id"))
    except (TypeError, ValueError):
        return None
    title = str(item.get("title") or "").strip()
    coords = item.get("coords") if isinstance(item.get("coords"), dict) else {}
    lat, lon = _num(coords.get("lat")), _num(coords.get("lng"))
    if not lat or not lon or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        lat = lon = None
    price = _num(item.get("price"))
    metric = str(item.get("price_metric") or "").strip()
    area = _title_area_m2(title)
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    stamp = item.get("time") if isinstance(item.get("time"), str) else None
    skip = None
    if market_ads._LEASE_RE.search(title):
        skip = "lease"  # аренда под видом продажи (зум 13.11, 24:48)
    elif market_ads._HOUSE_RE.search(title):
        skip = "house"  # цена дома, а не земли
    elif lat is None:
        skip = "no_coords"
    elif not area:
        skip = "no_area"
    elif metric not in ("", "₽"):
        skip = "metric"  # «₽ за сутки», «₽ в месяц» — не цена участка
    elif not price or not (PRICE_MIN <= price <= PRICE_MAX):
        skip = "no_price"  # тестовый доступ отдаёт цену 0
    elif not stamp or len(stamp) != 19:
        skip = "no_time"
    return {
        "id": ad_id,
        "source": str(item.get("source") or "")[:24],
        "url": str(item.get("url") or "")[:400],
        "title": title[:200],
        "address": str(item.get("address") or item.get("city") or "")[:300],
        "region_name": str(item.get("region") or ""),
        "cls": land_class(item),
        "price": price,
        "area_m2": area,
        "lat": lat,
        "lon": lon,
        "posted": stamp,
        "metric": metric,
        "unit": param_unit(area, _num(params.get("Площадь"))),
        "skip": skip,
    }


# --- цена рынка рядом с участком -------------------------------------------------------------
RADII_KM = (2.0, 5.0, 10.0, 20.0)
MIN_ADS = 5  # меньше сопоставимых объявлений в радиусе — берём радиус шире
FRESH_DAYS = 180
AREA_BAND = 5.0  # как у торгов: от 1/5 до 5 площадей лота
MAX_SHOWN = 10
KM_PER_DEG = 111.2
_CELL_LAT, _CELL_LON = 0.05, 0.1  # клетка около 5,5 км по широте


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / _CELL_LAT), math.floor(lon / _CELL_LON)


class AdIndex:
    """Объявления региона, разложенные по клеткам: поиск «что продаётся в радиусе» без
    перебора всего региона. Строка — (id, lat, lon, цена, площадь м², категория, время)."""

    def __init__(self, rows: Iterable[tuple[int, float, float, float, float, str | None, str]]):
        self._cells: dict[tuple[int, int], list[tuple]] = {}
        self.size = 0
        for row in rows:
            self._cells.setdefault(_cell(row[1], row[2]), []).append(row)
            self.size += 1

    def around(self, lat: float, lon: float, radius_km: float) -> list[tuple[float, tuple]]:
        """Объявления не дальше `radius_km` от точки: (расстояние в км, строка)."""
        k_lon = KM_PER_DEG * max(math.cos(math.radians(lat)), 0.05)
        d_lat, d_lon = radius_km / KM_PER_DEG, radius_km / k_lon
        a0, b0 = _cell(lat - d_lat, lon - d_lon)
        a1, b1 = _cell(lat + d_lat, lon + d_lon)
        out = []
        for a in range(a0, a1 + 1):
            for b in range(b0, b1 + 1):
                for row in self._cells.get((a, b), ()):
                    dist = math.hypot((row[1] - lat) * KM_PER_DEG, (row[2] - lon) * k_lon)
                    if dist <= radius_km:
                        out.append((dist, row))
        return out


# Объявление — цена, которую просят, а не цена сделки. Замер на проде 20.11.2026, в первый
# час работы ленты: у 1 470 идущих лотов, где рядом есть и объявления, и сделки Росреестра в
# том же квартале, середина объявлений выше середины сделок в 1,93 раза (четверть лотов — до
# 1,51, три четверти — до 2,85; Московская область — 2,05). «Горячих» с ценой по объявлениям
# на истории 46 % остались без заявок против 15–16 % у оценённых по сделкам, а купленные ушли
# за 0,82 ожидаемой цены. Поэтому в расчёт цена объявлений идёт приведённой к уровню сделок.
ASK_TO_DEAL = 0.52  # 1 / 1,93
ASK_NOTE = (
    "в расчёт взята цена объявлений × 0,52: там, где есть и объявления, и сделки Росреестра, "
    "объявления в середине в 1,9 раза выше цен сделок (замер 20.11.2026, 1 470 лотов)"
)


def deal_level(mk: dict[str, Any] | None) -> dict[str, Any] | None:
    """Оценку по объявлениям привести к уровню цен сделок. Цены предложения остаются рядом
    (`ask_min_m2`, `ask_mid_m2`, `ask_max_m2`): эксперт видит и то, что просят, и то, что
    взято в расчёт."""
    if mk is None:
        return None
    out = dict(mk)
    for key in ("min_m2", "mid_m2", "max_m2"):
        out[f"ask_{key}"] = mk[key]
        out[key] = mk[key] * ASK_TO_DEAL
    out["source_label"] = f"{mk['source_label']}, с поправкой на торг"
    out["note"] = f"{mk['note']}; {ASK_NOTE}"
    return out


def near_market(
    point: tuple[float, float] | None,
    area_m2: float | None,
    group: str | None,
    index: AdIndex | None,
    *,
    now: datetime | None = None,
    approx: bool = False,
) -> dict[str, Any] | None:
    """Цена перепродажи участка по объявлениям вокруг него — в том виде, какой ждёт
    `torgi_scan.market_for`: три сценария за м² (25 %, медиана, 75 %) и подпись источника.

    Сопоставимые объявления: та же категория земли, площадь от 1/5 до 5 площадей лота, не
    старше полугода. Радиус растёт с 2 до 20 км, пока не наберётся `MIN_ADS`. Двойники (одно
    объявление на двух площадках) считаются один раз; цены вне медианы в 5 раз отбрасываются,
    подозрительно дешёвые (в 2,5 раза ниже медианы) в расчёт не идут — как в поиске
    объявлений (`market_ads`). `approx` — точка взята по кварталу, а не по самому участку."""
    cls = CLASS_OF_GROUP.get(group or "")
    if index is None or not index.size or not point or not area_m2 or cls is None:
        return None
    since = ((now or msk_now()) - timedelta(days=FRESH_DAYS)).strftime(TIME_FMT)
    lat, lon = point
    for radius in RADII_KM:
        seen: set[tuple] = set()
        ads = []
        for dist, row in sorted(index.around(lat, lon, radius), key=lambda x: x[0]):
            _id, a_lat, a_lon, price, a_area, a_cls, posted = row
            if a_cls != cls or posted < since:
                continue
            if not (area_m2 / AREA_BAND <= a_area <= area_m2 * AREA_BAND):
                continue
            twin = (round(price), round(a_area), round(a_lat, 2), round(a_lon, 2))
            if twin in seen:
                continue
            seen.add(twin)
            ads.append((price / a_area, dist, _id))
        if len(ads) < MIN_ADS:
            continue
        med = market_ads._quantile([a[0] for a in ads], 0.5)
        ads = [a for a in ads if med / 5 <= a[0] <= med * 5]
        cheap = [a for a in ads if a[0] < med / market_ads.CHEAP_RATIO]
        ads = [a for a in ads if a[0] >= med / market_ads.CHEAP_RATIO]
        if len(ads) < MIN_ADS:
            continue
        pps = [a[0] for a in ads]
        note = (
            f"{LAND_CLASSES[cls]}, площадь от 1/5 до 5 площадей лота, не старше полугода; "
            "это цены предложений, торг обычно снижает их"
        )
        if approx:
            note += "; точка лота взята по кадастровому кварталу"
        if cheap:
            note += f"; подозрительно дешёвых не учтено: {len(cheap)}"
        return {
            "min_m2": market_ads._quantile(pps, 0.25),
            "mid_m2": market_ads._quantile(pps, 0.5),
            "max_m2": market_ads._quantile(pps, 0.75),
            "source": "ads",
            "source_label": f"объявления в радиусе {radius:g} км: {len(ads)} шт.",
            "confidence": 2,
            "exposure_days": None,
            "liquid": None,
            "note": note,
            "ads_n": len(ads),
            "ads_lo_m2": min(pps),
            "ads_hi_m2": max(pps),
            "radius_km": radius,
            "near": True,
            # ближайшие — для карточки лота: эксперт проверяет выборку глазами
            "ad_dist": {a[2]: round(a[1], 1) for a in ads[:MAX_SHOWN]},
            "cheap_dist": {a[2]: round(a[1], 1) for a in cheap[:MAX_SHOWN]},
        }
    return None
