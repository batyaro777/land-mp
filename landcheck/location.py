"""Локация участка: окружение и ценовой ориентир по открытым данным.

Запрос брокера (Антон М., 15.10.2026): «в рынке ли участок» и «что будет с локацией».
Честный ответ на данных, которые реально есть:
- НСПД вокруг участка: ЗОУИТ с расстоянием (промзона, курорт, затопление, аэродром),
  проекты межевания территории (единственный в НСПД сигнал «что здесь будут строить»),
  соседние участки с кадастровой стоимостью (групповая ставка ГКО, не рынок);
- OpenStreetMap в радиусе 3 км (ТЗ М2.2): свалки, кладбища, промзоны, трассы, ЛЭП, вода.

Сырой ответ (`fetch_*`) и сводка (`summarize_location`) разделены: сводка чистая и тестируется
на синтетике, сырой формат кладётся в кэш как есть.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime
from typing import Any

import numpy as np
import shapely
from shapely.geometry import box, shape

from landcheck.models import SOTKA_M2

SCHEMA_VERSION = 1
MIN_AREA_M2 = 50  # площадь ЕГРН меньше полусотки — ошибка данных, цену за м² по ней не считаем
NSPD_RADIUS_M = 1000  # ЗОУИТ и проекты межевания
NEIGHBORS_RADIUS_M = 400  # соседи по кадастровой стоимости: ближний круг, иначе сотни участков
CONSTRUCTIONS_RADIUS_M = 300  # сооружения ЕГРН: сети, трубопроводы, дороги (зум 28.10, п. 1.6)
AT_BORDER_M = 20  # сооружение ближе — «по границе участка»
OSM_RADIUS_M = 3000  # ТЗ М2.2
MIN_NEIGHBORS = 5  # ориентир по одному соседу — не ориентир (зум 28.10, п. 1.12)

SRC_NSPD_AROUND = "НСПД, объекты в радиусе от контура участка"
SRC_GKO = "НСПД, удельный показатель кадастровой стоимости соседних участков (ГКО, не рынок)"
SRC_OSM = "OpenStreetMap (Overpass API); полнота данных зависит от региона"
SRC_PMT = "НСПД, слой «ЗУ, образуемые по проекту межевания территории»"
SRC_CONSTR = "НСПД, слой «Сооружения» (ЕГРН), в радиусе от контура участка"

# --- геометрия --------------------------------------------------------------------------
_M_PER_DEG = 111_320.0


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = radius_m / _M_PER_DEG
    dlon = radius_m / (_M_PER_DEG * max(math.cos(math.radians(lat)), 0.2))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def bbox_polygon(lat: float, lon: float, radius_m: float):
    return box(*bbox_around(lat, lon, radius_m))


def _local(geom, lat0: float, lon0: float):
    """Равнопромежуточная проекция в метры вокруг участка — для расстояний до 10 км хватает."""
    kx = _M_PER_DEG * math.cos(math.radians(lat0))

    def proj(coords: np.ndarray) -> np.ndarray:
        out = np.empty_like(coords, dtype=float)
        out[:, 0] = (coords[:, 0] - lon0) * kx
        out[:, 1] = (coords[:, 1] - lat0) * _M_PER_DEG
        return out

    return shapely.transform(geom, proj)


def distance_m(parcel_geojson: dict[str, Any], other_geojson: dict[str, Any]) -> float | None:
    try:
        parcel = shape(parcel_geojson)
        other = shape(other_geojson)
    except Exception:  # noqa: BLE001 — кривая геометрия в данных
        return None
    if parcel.is_empty or other.is_empty:
        return None
    c = parcel.centroid
    return round(_local(parcel, c.y, c.x).distance(_local(other, c.y, c.x)))


def centroid(parcel_geojson: dict[str, Any]) -> tuple[float, float]:
    c = shape(parcel_geojson).centroid
    return (c.y, c.x)


# --- сводка -----------------------------------------------------------------------------
def _get(d: dict[str, Any], *keys: str, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


# (подстрока в type_zone/name, подпись, оценка для жилья: risk | neutral | plus, пояснение)
ZOUIT_KINDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "санитарно-защитн",
        "санитарно-защитная зона предприятия",
        "risk",
        "рядом производство или объект с СЗЗ: шум, запах, ограничения для жилья",
    ),
    ("кладбищ", "санитарная зона кладбища", "risk", "кладбище рядом снижает спрос на жильё"),
    ("затоплен", "зона затопления", "risk", "паводковый риск, страховка и стройка дороже"),
    ("подтоплен", "зона подтопления", "risk", "высокая вода, дренаж и фундамент дороже"),
    ("приаэродром", "приаэродромная территория", "risk", "шум и согласования строительства"),
    # Округ санитарной охраны курорта накрывает весь Сочи или всю Белокуриху: на участок
    # не влияет, это факт, а не риск и не «плюс» (Влад, зум 28.10, п. 1.4).
    (
        "лечебно-оздоровительн",
        "округ санитарной охраны курорта",
        "neutral",
        "действует на весь курорт, на конкретный участок не влияет",
    ),
    (
        "горно-санитарн",
        "округ санитарной охраны курорта",
        "neutral",
        "действует на весь курорт, на конкретный участок не влияет",
    ),
    (
        "курорт",
        "округ санитарной охраны курорта",
        "neutral",
        "действует на весь курорт, на конкретный участок не влияет",
    ),
    (
        "культурного наследия",
        "зона охраны объектов культурного наследия",
        "neutral",
        "облик и высотность согласуются",
    ),
    ("водоохран", "водоохранная зона", "neutral", "вода рядом: плюс к спросу, минус к стокам"),
    ("прибрежн", "прибрежная защитная полоса", "neutral", "берег рядом, застройка ограничена"),
    ("газопровод", "зона газопровода", "neutral", "газ рядом, минимальные расстояния"),
    ("электросет", "охранная зона электросетей", "neutral", "ЛЭП рядом"),
    ("электроэнергет", "охранная зона электросетей", "neutral", "ЛЭП рядом"),
    ("инженерных коммуникаций", "охранная зона инженерных сетей", "neutral", "сети рядом"),
    ("публичн", "публичный сервитут", "neutral", "проход или сети через участки"),
)


# Риск от зоны — только когда она задевает участок. «Зона затопления в 151 м» читалась как
# «участок в зоне затопления» (Андрей, зум 28.10, п. 1.2–1.3). Порог в метрах от контура.
ZOUIT_RISK_M: dict[str, int] = {
    "зона затопления": 50,
    "зона подтопления": 0,
    "санитарно-защитная зона предприятия": 0,
    "санитарная зона кладбища": 300,
    "приаэродромная территория": 0,
}


def _zouit_kind(type_zone: str, name: str) -> tuple[str, str, str]:
    """Сначала по виду зоны (type_zone), и только если он не распознан — по названию."""
    for text in (type_zone.lower(), name.lower()):
        for needle, label, severity, note in ZOUIT_KINDS:
            if needle in text:
                return label, severity, note
    return "иная ЗОУИТ", "neutral", ""


def _feature_distance(
    feature: dict[str, Any], parcel_geojson: dict[str, Any] | None
) -> float | None:
    """Расстояние из кэша (посчитано при сохранении) или по геометрии, если она ещё есть."""
    dist = feature.get("distance_m")
    if dist is not None:
        return dist
    if parcel_geojson and feature.get("geometry"):
        return distance_m(parcel_geojson, feature["geometry"])
    return None


def summarize_zouit(
    parcel_geojson: dict[str, Any] | None, features: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Группы ЗОУИТ вокруг участка: ближайшее расстояние и число зон."""
    groups: dict[str, dict[str, Any]] = {}
    for f in features:
        opts = (f.get("properties") or {}).get("options") or {}
        label, severity, note = _zouit_kind(
            str(_get(opts, "type_zone", default="")), str(_get(opts, "name_by_doc", default=""))
        )
        dist = _feature_distance(f, parcel_geojson)
        g = groups.setdefault(
            label,
            {
                "label": label,
                "severity": severity,
                "note": note,
                "count": 0,
                "min_distance_m": None,
                "nearest_name": None,
                "source": SRC_NSPD_AROUND,
            },
        )
        g["count"] += 1
        if dist is not None and (g["min_distance_m"] is None or dist < g["min_distance_m"]):
            g["min_distance_m"] = dist
            g["nearest_name"] = _get(opts, "name_by_doc", "type_zone")
    for g in groups.values():
        if g["severity"] == "risk":
            limit = ZOUIT_RISK_M.get(g["label"], 0)
            if g["min_distance_m"] is None or g["min_distance_m"] > limit:
                g["severity"] = "neutral"
                g["far"] = True
                g["note"] = "вне участка" + (
                    f", ближайшая в {g['min_distance_m']} м" if g["min_distance_m"] else ""
                )
    order = {"risk": 0, "plus": 1, "neutral": 2}
    return sorted(
        groups.values(),
        key=lambda g: (order[g["severity"]], g["min_distance_m"] is None, g["min_distance_m"] or 0),
    )


def summarize_plans(
    parcel_geojson: dict[str, Any] | None, features: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Проекты межевания рядом: по документу — какие ВРИ образуются и как близко."""
    docs: dict[str, dict[str, Any]] = {}
    for f in features:
        opts = (f.get("properties") or {}).get("options") or {}
        name = _get(opts, "document_name", "doc_name", "name_doc", "legal_act_document_name")
        number = _get(opts, "document_number", "doc_number", "legal_act_document_number")
        date = _get(opts, "document_date", "doc_date", "legal_act_document_date")
        issuer = _get(opts, "document_issuer", "doc_issuer", "legal_act_document_issuer")
        vri = _get(opts, "land_use_text", "permitted_use", "permitted_use_established_by_document")
        area = _to_float(_get(opts, "area", "specified_area", "declared_area"))
        date = _short_date(date)
        key = f"{name}|{number}|{date}"
        d = docs.setdefault(
            key,
            {
                "document": " ".join(str(x) for x in (name, "№", number, "от", date) if x)
                or "проект межевания",
                "issuer": issuer,
                "date": date,
                "parcels": 0,
                "uses": {},
                "area_m2": 0.0,
                "min_distance_m": None,
                "source": SRC_PMT,
            },
        )
        d["parcels"] += 1
        if vri:
            d["uses"][str(vri)] = d["uses"].get(str(vri), 0) + 1
        if area:
            d["area_m2"] += area
        dist = _feature_distance(f, parcel_geojson)
        if dist is not None and (d["min_distance_m"] is None or dist < d["min_distance_m"]):
            d["min_distance_m"] = dist
    out = []
    for d in docs.values():
        d["uses"] = sorted(d["uses"].items(), key=lambda kv: -kv[1])
        out.append(d)
    out.sort(key=lambda d: (d["min_distance_m"] is None, d["min_distance_m"] or 0))
    return out


def _to_float(v: Any) -> float | None:
    """НСПД отдаёт площадь ПМТ строкой («135358»)."""
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _short_date(v: Any) -> str | None:
    """«2022-04-28T00:00:00» → «28.04.2022»; «13.10.2020» остаётся как есть."""
    if not v:
        return None
    text = str(v)
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return f"{text[8:10]}.{text[5:7]}.{text[0:4]}"
    return text


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = (len(s) - 1) * q
    lo, hi = math.floor(idx), math.ceil(idx)
    return round(s[lo] + (s[hi] - s[lo]) * (idx - lo), 1)


def summarize_neighbors(
    target_cad_num: str,
    target_category: str | None,
    target_cost_index: float | None,
    features: list[dict[str, Any]],
) -> dict[str, Any]:
    """Кадастровая стоимость соседей: по категориям и ВРИ, место участка среди своих."""
    by_cat: dict[str, dict[str, Any]] = {}
    by_vri: dict[str, list[float]] = {}
    ownership: dict[str, int] = {}
    total = 0
    for f in features:
        opts = (f.get("properties") or {}).get("options") or {}
        if opts.get("cad_num") == target_cad_num:
            continue
        total += 1
        cat = _get(opts, "land_record_category_type", default="категория не указана")
        cost = opts.get("cost_index")
        area = _get(opts, "specified_area", "declared_area", "area", "land_record_area")
        vri = _get(opts, "permitted_use_established_by_document", default="ВРИ не указан")
        own = _get(opts, "ownership_type", default="не указана")
        ownership[str(own)] = ownership.get(str(own), 0) + 1
        c = by_cat.setdefault(
            str(cat), {"category": str(cat), "count": 0, "costs": [], "areas": []}
        )
        c["count"] += 1
        if isinstance(cost, (int, float)) and cost > 0:
            c["costs"].append(float(cost))
            by_vri.setdefault(str(vri), []).append(float(cost))
        if isinstance(area, (int, float)) and area > 0:
            c["areas"].append(float(area))
    categories = []
    for c in sorted(by_cat.values(), key=lambda c: -c["count"]):
        categories.append(
            {
                "category": c["category"],
                "count": c["count"],
                "median_rub_m2": _median(c["costs"]),
                "p25_rub_m2": _quantile(c["costs"], 0.25),
                "p75_rub_m2": _quantile(c["costs"], 0.75),
                "median_area_m2": _median(c["areas"]),
            }
        )
    uses = sorted(
        (
            {"vri": v, "count": len(costs), "median_rub_m2": _median(costs)}
            for v, costs in by_vri.items()
        ),
        key=lambda u: -u["count"],
    )[:6]
    same = next((c for c in categories if c["category"] == target_category), None)
    position = None
    if same and same["median_rub_m2"] and target_cost_index:
        ratio = target_cost_index / same["median_rub_m2"]
        if ratio < 0.9:
            position = f"ниже медианы соседей на {1 - ratio:.0%}"
        elif ratio > 1.1:
            position = f"выше медианы соседей на {ratio - 1:.0%}"
        else:
            position = "на уровне соседей"
    return {
        "total": total,
        "categories": categories,
        "uses": uses,
        "ownership": sorted(ownership.items(), key=lambda kv: -kv[1]),
        "same_category": same,
        "target_rub_m2": round(target_cost_index, 1) if target_cost_index else target_cost_index,
        "position": position,
        "source": SRC_GKO,
        "note": (
            "Удельный показатель кадастровой стоимости — групповая ставка государственной "
            "кадастровой оценки по району и виду использования, не рыночная цена. "
            "Информативна разница между группами и место участка в своей группе."
        ),
    }


def price_reference(
    neighbors: dict[str, Any], listing_price_rub: float | None, area_m2: float | None
) -> dict[str, Any] | None:
    """Ориентир за сотку и гектар по кадастровой ставке своей группы; цена объявления к нему."""
    same = neighbors.get("same_category")
    basis = "медиана кадастровой ставки соседей той же категории"
    if not same or not same.get("median_rub_m2") or same.get("count", 0) < MIN_NEIGHBORS:
        # Категория участка не установлена, у своей группы нет ставки или соседей меньше пяти —
        # берём самую многочисленную группу соседей и говорим об этом прямо.
        candidates = [
            c
            for c in neighbors.get("categories") or []
            if c.get("median_rub_m2") and c.get("count", 0) >= MIN_NEIGHBORS
        ]
        if not candidates:
            return None
        same = max(candidates, key=lambda c: c["count"])
        basis = f"медиана ставки соседей категории «{same['category']}», своей группы у участка нет"
    med = same["median_rub_m2"]
    out: dict[str, Any] = {
        "basis": f"{basis} ({same['count']} участков)",
        "per_sotka": round(med * SOTKA_M2),
        "per_10_sotka": round(med * SOTKA_M2 * 10),
        "per_ha": round(med * 10_000),
        "source": SRC_GKO,
    }
    if listing_price_rub and area_m2 and area_m2 >= MIN_AREA_M2:
        listing_rub_m2 = listing_price_rub / area_m2
        out["listing_rub_m2"] = round(listing_rub_m2)
        out["listing_vs_gko"] = round(listing_rub_m2 / med, 2)
    return out


# --- OSM --------------------------------------------------------------------------------
# (класс, подпись, оценка, фильтр тегов)
OSM_CLASSES: tuple[tuple[str, str, str], ...] = (
    ("landfill", "свалка / полигон отходов", "risk"),
    ("industrial", "промзона", "risk"),
    ("quarry", "карьер", "risk"),
    ("cemetery", "кладбище", "risk"),
    ("military", "военный объект", "risk"),
    ("aerodrome", "аэродром", "risk"),
    ("motorway", "автомагистраль / трасса", "neutral"),
    ("railway", "железная дорога", "neutral"),
    ("power_line", "ЛЭП", "neutral"),
    ("water", "водоём / река", "plus"),
    ("nature_reserve", "природная территория", "plus"),
    ("resort", "курорт / санаторий / отель", "plus"),
    ("school", "школа / детсад", "plus"),
    ("hospital", "больница / поликлиника", "plus"),
    ("shop", "магазин", "plus"),
)
_OSM_LABEL = {k: (label, sev) for k, label, sev in OSM_CLASSES}


# Риск от объекта OSM — только вблизи; дальше это «окружение». Красная Поляна получала
# «свалка/полигон ×32 в 353 м» и «промзона ×31» из контейнерных площадок и котельных
# (зум 28.10, п. 1.3, 1.11). Пороги — метры от контура; None — никогда не риск.
OSM_RISK_M: dict[str, int | None] = {
    "landfill": 500,
    "quarry": 500,
    "cemetery": 300,
    "industrial": 0,
    "military": None,
    "aerodrome": None,
}
OSM_MIN_AREA_M2 = {"landfill": 10_000, "industrial": 20_000}  # 1 га и 2 га


def osm_class(tags: dict[str, str]) -> str | None:
    lu = tags.get("landuse", "")
    if lu == "landfill":
        return "landfill"
    if lu == "industrial":
        return "industrial"
    if lu == "quarry":
        return "quarry"
    if lu == "cemetery" or tags.get("amenity") == "grave_yard":
        return "cemetery"
    if lu == "military" or tags.get("military"):
        return "military"
    if tags.get("aeroway") == "aerodrome":
        return "aerodrome"
    if tags.get("highway") in ("motorway", "trunk", "primary"):
        return "motorway"
    if tags.get("railway") == "rail":
        return "railway"
    if tags.get("power") == "line":
        return "power_line"
    if tags.get("natural") == "water" or tags.get("waterway") in ("river", "stream", "canal"):
        return "water"
    if tags.get("leisure") == "nature_reserve" or tags.get("boundary") == "national_park":
        return "nature_reserve"
    if tags.get("tourism") in ("resort", "hotel", "spa_resort", "guest_house"):
        return "resort"
    if tags.get("amenity") in ("school", "kindergarten"):
        return "school"
    if tags.get("amenity") in ("hospital", "clinic"):
        return "hospital"
    if tags.get("shop") in ("supermarket", "convenience"):
        return "shop"
    return None


def _osm_geojson(el: dict[str, Any]) -> dict[str, Any] | None:
    if el.get("type") == "node" and "lat" in el:
        return {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
    geom = el.get("geometry")
    if isinstance(geom, list) and geom:
        coords = [[p["lon"], p["lat"]] for p in geom if "lat" in p]
        if len(coords) >= 2:
            return {"type": "LineString", "coordinates": coords}
    members = el.get("members")
    if isinstance(members, list):
        lines = []
        for m in members:
            g = m.get("geometry")
            if isinstance(g, list):
                coords = [[p["lon"], p["lat"]] for p in g if "lat" in p]
                if len(coords) >= 2:
                    lines.append(coords)
        if lines:
            return {"type": "MultiLineString", "coordinates": lines}
    center = el.get("center")
    if isinstance(center, dict) and "lat" in center:
        return {"type": "Point", "coordinates": [center["lon"], center["lat"]]}
    return None


def _osm_area_m2(el: dict[str, Any], parcel_geojson: dict[str, Any] | None) -> float | None:
    """Площадь замкнутого контура OSM в м² (для фильтра мелких промзон и свалок)."""
    if el.get("area_m2") is not None:
        return el["area_m2"]
    geom = el.get("geometry")
    if not isinstance(geom, list) or len(geom) < 4:
        return None
    coords = [(p["lon"], p["lat"]) for p in geom if "lat" in p]
    if len(coords) < 4 or coords[0] != coords[-1]:
        return None
    try:
        poly = shapely.Polygon(coords)
        lat0 = poly.centroid.y if not parcel_geojson else centroid(parcel_geojson)[0]
        return round(abs(_local(poly, lat0, poly.centroid.x).area))
    except Exception:  # noqa: BLE001 — кривой контур
        return None


def _osm_too_small(cls: str, el: dict[str, Any], parcel_geojson: dict[str, Any] | None) -> bool:
    limit = OSM_MIN_AREA_M2.get(cls)
    if limit is None:
        return False
    area = _osm_area_m2(el, parcel_geojson)
    if area is None:
        # Точка «промзона» (без контура) — это гараж или котельная, не промзона.
        return cls == "industrial"
    return area < limit


def summarize_osm(
    parcel_geojson: dict[str, Any] | None, elements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ближайший объект каждого класса в радиусе OSM."""
    best: dict[str, dict[str, Any]] = {}
    for el in elements:
        tags = el.get("tags") or {}
        cls = osm_class(tags)
        if cls is None or _osm_too_small(cls, el, parcel_geojson):
            continue
        dist = el.get("distance_m")
        if dist is None:
            g = _osm_geojson(el)
            dist = distance_m(parcel_geojson, g) if (parcel_geojson and g) else None
        name = tags.get("name") or tags.get("ref")
        label, severity = _OSM_LABEL[cls]
        if severity == "risk":
            limit = OSM_RISK_M.get(cls)
            if limit is None or dist is None or dist > limit:
                severity = "neutral"
        cur = best.get(cls)
        if cur is None or (
            dist is not None and (cur["distance_m"] is None or dist < cur["distance_m"])
        ):
            best[cls] = {
                "kind": cls,
                "label": label,
                "severity": severity,
                "distance_m": dist,
                "name": name,
                "count": (cur["count"] + 1) if cur else 1,
                "source": SRC_OSM,
            }
        else:
            cur["count"] += 1
    order = {"risk": 0, "neutral": 1, "plus": 2}
    return sorted(
        best.values(),
        key=lambda o: (order[o["severity"]], o["distance_m"] is None, o["distance_m"] or 0),
    )


def summarize_location(
    raw: dict[str, Any],
    *,
    parcel_geojson: dict[str, Any] | None,
    cad_num: str,
    category: str | None,
    cost_index: float | None,
    listing_price_rub: float | None = None,
    area_m2: float | None = None,
) -> dict[str, Any]:
    """Сводка для отчёта из сырого ответа `fetch_location_raw` (+ OSM)."""
    nspd = raw.get("nspd") or {}
    osm = raw.get("osm")
    neighbors = summarize_neighbors(cad_num, category, cost_index, nspd.get("neighbors") or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": raw.get("fetched_at"),
        "radius_m": {
            "nspd": raw.get("nspd_radius_m", NSPD_RADIUS_M),
            "neighbors": raw.get("neighbors_radius_m", NEIGHBORS_RADIUS_M),
            "osm": raw.get("osm_radius_m", OSM_RADIUS_M),
        },
        "zouit": summarize_zouit(parcel_geojson, nspd.get("zouit") or []),
        "plans": summarize_plans(parcel_geojson, nspd.get("pmt") or []),
        "constructions": summarize_constructions(parcel_geojson, nspd.get("constructions")),
        "auction_parcels": len(nspd.get("auction") or []),
        "neighbors": neighbors,
        "price": price_reference(neighbors, listing_price_rub, area_m2),
        "osm": summarize_osm(parcel_geojson, (osm or {}).get("elements") or []) if osm else None,
        "osm_error": (osm or {}).get("error") if osm else "OSM не запрашивался",
        "access": access_summary(
            summarize_constructions(parcel_geojson, nspd.get("constructions")),
            osm,
            neighbors["total"],
        ),
        "forest": forest_summary(nspd.get("neighbors") or []),
        "errors": list(nspd.get("errors") or []),
        # Тексты исключений — в лог и в кэш, клиенту — одна фраза (зум 28.10, п. 1.10).
        "errors_public": (
            "часть запросов к НСПД не ответила, нажмите «обновить»" if nspd.get("errors") else None
        ),
        "disclaimer": (
            "Окружение и ориентир стоимости собраны автоматически из НСПД и OpenStreetMap. "
            "Это не отчёт об оценке и не анализ генплана: документы терпланирования в НСПД "
            "не публикуются, сверяйте с генпланом и ПЗЗ муниципалитета."
        ),
    }


# --- подъезд и лесфонд (ТЗ v3, п. 5) -------------------------------------------------------
ACCESS_HIGHWAYS = (
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "service",
    "track",
    "living_street",
    "road",
    "motorway",
    "trunk",
    "primary",
)
ACCESS_ROAD_M = 25  # дорога ближе — считаем, что съезд к границе есть
FOREST_ADJ_M = 5  # сосед-лесфонд ближе — «граничит»
FOREST_NEAR_M = 100
SRC_ACCESS = "НСПД «Сооружения» и OpenStreetMap: дороги ближе 25 м к границе участка"
SRC_FOREST = "НСПД, соседние участки категории «земли лесного фонда» (по контурам ЕГРН)"
SRC_SP_4 = (
    "СП 4.13130.2013 п. 4.14: от домов ИЖС и садовых до лесных насаждений не менее 30 м; "
    "280-ФЗ от 29.07.2017 («лесная амнистия») — наложение границ лесфонда устраняется по заявлению"
)
_HW_LABEL = {
    "motorway": "автомагистраль",
    "trunk": "трасса",
    "primary": "дорога регионального значения",
    "secondary": "дорога",
    "tertiary": "дорога",
    "unclassified": "дорога местного значения",
    "residential": "улица",
    "service": "проезд",
    "track": "полевая/лесная дорога",
    "living_street": "жилая улица",
    "road": "дорога",
}


def access_summary(
    constructions: dict[str, Any] | None, osm: dict[str, Any] | None, neighbors_total: int
) -> dict[str, Any]:
    """Подъезд к участку: дорога общего пользования у границы (НСПД «Сооружения» или OSM)
    → плюс; дорог у границы нет, а соседи есть → риск «проезд через соседние участки:
    проверить сервитут» (Андрей, Калининград: сервис писал «возможно, есть сервитут» там,
    где его нет, а съезды есть у каждого участка)."""
    roads: list[dict[str, Any]] = []
    for r in (constructions or {}).get("roads") or []:
        d = r.get("distance_m")
        if d is not None and d <= ACCESS_ROAD_M:
            roads.append(
                {
                    "label": "дорога по ЕГРН",
                    "name": r.get("name"),
                    "distance_m": d,
                    "source": "НСПД «Сооружения»",
                }
            )
    osm_ok = bool(osm) and not (osm or {}).get("error")
    for el in (osm or {}).get("elements") or []:
        tags = el.get("tags") or {}
        hw = tags.get("highway")
        d = el.get("distance_m")
        if hw in ACCESS_HIGHWAYS and d is not None and d <= ACCESS_ROAD_M:
            roads.append(
                {
                    "label": _HW_LABEL.get(hw, "дорога"),
                    "name": tags.get("name") or tags.get("ref"),
                    "distance_m": d,
                    "source": "OpenStreetMap",
                }
            )
    roads.sort(key=lambda r: r["distance_m"])
    if roads:
        names = ", ".join(
            dict.fromkeys(
                f"{r['label']}" + (f" «{r['name']}»" if r.get("name") else "") for r in roads
            )
        )
        level, text = "plus", f"съезд с дороги общего пользования у границы есть: {names}"
    elif not osm_ok and not (constructions or {}).get("items"):
        level, text = "neutral", "подъезд по картам не определён: данные о дорогах не получены"
    elif neighbors_total:
        level, text = (
            "risk",
            "дорога общего пользования к границе участка по картам не подходит: проезд, "
            "вероятно, через соседние участки — до сделки проверить сервитут или право "
            "проезда (выписка ЕГРН, раздел 7 ГПЗУ), иначе к участку «никто не подъедет»",
        )
    else:
        level, text = "neutral", "дорог у границы по картам нет; проверить подъезд на месте"
    return {"level": level, "text": text, "roads": roads[:5], "source": SRC_ACCESS}


def forest_summary(neighbors: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Смежность с землями лесного фонда (Андрей: участок граничит с лесфондом — 30 м от леса
    не строить; раньше было наложение, лесфонд границу скорректировал). None — лесфонда
    в ближнем круге нет."""
    forest: list[tuple[float | None, str | None]] = []
    for f in neighbors:
        opts = (f.get("properties") or {}).get("options") or {}
        cat = str(_get(opts, "land_record_category_type", default="") or "").lower()
        if "лесного фонда" in cat:
            forest.append((f.get("distance_m"), opts.get("cad_num")))
    if not forest:
        return None
    known = [x for x in forest if x[0] is not None]
    adjacent = [x for x in known if x[0] <= FOREST_ADJ_M]
    if adjacent:
        cads = ", ".join(c for _, c in adjacent[:3] if c)
        return {
            "level": "check",
            "adjacent": True,
            "count": len(adjacent),
            "cad_nums": [c for _, c in adjacent if c],
            "text": (
                "граничит с землями лесного фонда"
                + (f" ({cads})" if cads else "")
                + ": дом ИЖС или садовый — не ближе 30 м к лесу (противопожарный разрыв); "
                "запросить в лесничестве сведения о наложении границ лесфонда на участок — "
                "по ЕГРН наложения не видно, но история корректировок границ в выписке "
                "не отражается"
            ),
            "source": SRC_FOREST,
            "law": SRC_SP_4,
        }
    nearest = min(known, key=lambda x: x[0]) if known else None
    if nearest and nearest[0] <= FOREST_NEAR_M:
        return {
            "level": "neutral",
            "adjacent": False,
            "distance_m": nearest[0],
            "cad_nums": [nearest[1]] if nearest[1] else [],
            "text": (
                f"земли лесного фонда в {nearest[0]:.0f} м: на участок по ЕГРН не накладываются; "
                "при стройке у леса — отступ 30 м"
            ),
            "source": SRC_FOREST,
            "law": SRC_SP_4,
        }
    return None


# --- разделы «Вода» и «Негативные факторы» поверх сводки (ТЗ v2, М2.2–2.3) ----------------
WATER_RISK_LABELS = ("зона затопления", "зона подтопления")
WATER_NEUTRAL_LABELS = ("водоохранная зона", "прибрежная защитная полоса")
FLOOD_NEAR_M = 50  # граница зоны затопления ближе — риск; дальше участок «вне зоны» (п. 1.2)
WATER_NEAR_M = 1000
_WATER_LOCATIVE = {
    "зона затопления": "зоне затопления",
    "зона подтопления": "зоне подтопления",
    "водоохранная зона": "водоохранной зоне",
    "прибрежная защитная полоса": "прибрежной защитной полосе",
}


def water_summary(loc: dict[str, Any]) -> dict[str, Any]:
    """Вода одним разделом: зоны затопления и подтопления, водоохранные зоны, водоёмы по OSM.

    Антон М. смотрит их первыми после проверки категории и ВРИ. Уровень: risk — участок в зоне
    затопления или она рядом; neutral — водоохранная зона; plus — вода рядом без зон риска;
    none — ничего не найдено.
    """
    items: list[dict[str, Any]] = []
    for g in loc.get("zouit") or []:
        label = g.get("label")
        if label in WATER_RISK_LABELS or label in WATER_NEUTRAL_LABELS:
            dist = g.get("min_distance_m")
            near = dist is not None and dist <= ZOUIT_RISK_M.get(label, FLOOD_NEAR_M)
            items.append(
                {
                    "label": label,
                    "distance_m": dist,
                    "count": g.get("count", 1),
                    "severity": "risk" if (label in WATER_RISK_LABELS and near) else "neutral",
                    "flood": label in WATER_RISK_LABELS,
                    "name": g.get("nearest_name"),
                    "note": g.get("note"),
                    "source": g.get("source"),
                }
            )
    for o in loc.get("osm") or []:
        if o.get("kind") == "water":
            items.append(
                {
                    "label": "водоём или река",
                    "distance_m": o.get("distance_m"),
                    "count": o.get("count", 1),
                    "severity": "plus",
                    "name": o.get("name"),
                    "note": None,
                    "source": o.get("source"),
                }
            )
    items.sort(key=lambda i: (i["distance_m"] is None, i["distance_m"] or 0))

    def near(sev: str, limit: float | None) -> list[dict[str, Any]]:
        return [
            i
            for i in items
            if i["severity"] == sev
            and i["distance_m"] is not None
            and (limit is None or i["distance_m"] <= limit)
        ]

    flood_in = [i for i in near("risk", 0)]
    flood_near = [i for i in near("risk", FLOOD_NEAR_M) if i["distance_m"] > 0]
    flood_far = [i for i in items if i.get("flood") and i["severity"] != "risk" and i["distance_m"]]
    protect_in = [i for i in near("neutral", 0) if not i.get("flood")]
    water_near = near("plus", WATER_NEAR_M)
    outside = (
        f"участок вне зон затопления и подтопления (ближайшая в {flood_far[0]['distance_m']} м)"
        if flood_far
        else "зон затопления и подтопления рядом по НСПД нет"
    )
    if flood_in:
        level = "risk"
        text = (
            f"участок в {_WATER_LOCATIVE[flood_in[0]['label']]}: паводковый риск, "
            "страховка, дренаж и фундамент дороже; запросить отметки и историю паводков"
        )
    elif flood_near:
        level = "risk"
        text = (
            f"граница {_WATER_LOCATIVE[flood_near[0]['label']].replace('зоне', 'зоны')} "
            f"в {flood_near[0]['distance_m']} м от участка: уточнить отметки относительно "
            "уреза воды и историю паводков"
        )
    elif protect_in:
        level = "neutral"
        where = _WATER_LOCATIVE[protect_in[0]["label"]]
        text = (
            f"участок в {where}: берег рядом (плюс к спросу), ограничения на стоки, "
            f"стоянку техники и застройку у воды; {outside}"
        )
    elif water_near:
        w = water_near[0]
        level = "plus"
        text = (
            f"вода в {w['distance_m']} м{(' (' + w['name'] + ')') if w.get('name') else ''}: "
            f"плюс к спросу; {outside}"
        )
    elif items:
        level = "neutral"
        text = outside
    else:
        level = "none"
        text = "зон затопления, водоохранных зон и водоёмов рядом не найдено"
    return {"level": level, "text": text, "items": items}


def negative_factors(
    loc: dict[str, Any], region: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Свод негативных факторов с расстояниями: ЗОУИТ-риски из НСПД (только задевающие
    участок) и объекты риска из OSM в пределах порогов. Новости региона сюда не идут:
    «полигон Ядрово, пятеро госпитализированы» в рисках участка пугает клиента и не про
    участок (Антон М, зум 28.10, п. 1.3) — они остаются в блоке «Регион» со ссылками."""
    out: list[dict[str, Any]] = []
    for g in loc.get("zouit") or []:
        dist = g.get("min_distance_m")
        # Сводки, собранные до порогов (кэш на проде), тоже проходят через пороги.
        near = dist is not None and dist <= ZOUIT_RISK_M.get(g.get("label"), 0)
        if g.get("severity") == "risk" and near:
            out.append(
                {
                    "label": g["label"],
                    "distance_m": g.get("min_distance_m"),
                    "count": g.get("count", 1),
                    "name": g.get("nearest_name"),
                    "note": g.get("note"),
                    "source": "НСПД",
                    "date": loc.get("fetched_at"),
                }
            )
    for o in loc.get("osm") or []:
        limit = OSM_RISK_M.get(o.get("kind"))
        dist = o.get("distance_m")
        if o.get("severity") == "risk" and limit is not None and dist is not None and dist <= limit:
            out.append(
                {
                    "label": o["label"],
                    "distance_m": o.get("distance_m"),
                    "count": o.get("count", 1),
                    "name": o.get("name"),
                    "note": None,
                    "source": "OpenStreetMap",
                    "date": loc.get("fetched_at"),
                }
            )
    out.sort(key=lambda i: (i["distance_m"] is None, i["distance_m"] or 0))
    return out


# --- сооружения ЕГРН рядом: сети, трубопроводы, дороги (зум 28.10, п. 1.6) -------------------
# (класс, подпись, ключи в наименовании/назначении)
CONSTRUCTION_KINDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("gas", "газопровод", ("газ",)),
    ("water", "водопровод", ("водопровод", "водовод", "водоснабж", "скважин", "водонапор")),
    ("sewer", "канализация", ("канализац", "водоотвед", "очистн")),
    (
        "power",
        "электросети",
        ("электр", "лэп", "вл-", "вл ", "кл-", "кл ", "трансформат", "подстанц", "тп-", "тп "),
    ),
    ("heat", "теплосети", ("тепло", "котельн")),
    ("telecom", "связь", ("связ", "волс", "кабел")),
    ("road", "дорога, проезд", ("дорог", "проезд", "улиц", "7.4")),
)
_CONSTR_LABEL = {k: label for k, label, _ in CONSTRUCTION_KINDS}
COMM_KINDS = ("gas", "water", "sewer", "power", "heat", "telecom")


def construction_kind(name: str | None, purpose: str | None) -> str:
    text = f"{name or ''} {purpose or ''}".lower()
    for kind, _label, keys in CONSTRUCTION_KINDS:
        if any(k in text for k in keys):
            return kind
    return "other"


def summarize_constructions(
    parcel_geojson: dict[str, Any] | None, features: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """Сооружения ЕГРН в радиусе: что подведено к границе и что рядом. None — не запрашивались
    (кэш старого формата). По участку в Красной Поляне НСПД показывал газопровод, воду и
    электричество по границе, а сервис писал «коммуникаций не найдено»."""
    if features is None:
        return None
    items: list[dict[str, Any]] = []
    for f in features:
        opts = (f.get("properties") or {}).get("options") or {}
        name = _get(opts, "params_name")
        purpose = _get(opts, "params_purpose")
        kind = construction_kind(str(name or ""), str(purpose or ""))
        dist = _feature_distance(f, parcel_geojson)
        items.append(
            {
                "kind": kind,
                "label": _CONSTR_LABEL.get(kind, "иное сооружение"),
                "name": name,
                "purpose": purpose,
                "cad_num": _get(opts, "cad_number"),
                "length_m": _to_float(_get(opts, "params_extension")),
                "distance_m": dist,
                "at_border": dist is not None and dist <= AT_BORDER_M,
                "source": SRC_CONSTR,
            }
        )
    items.sort(key=lambda i: (i["distance_m"] is None, i["distance_m"] or 0, i["kind"]))
    at_border = [i for i in items if i["at_border"] and i["kind"] in COMM_KINDS]
    nearby = [i for i in items if not i["at_border"] and i["kind"] in COMM_KINDS]
    roads = [i for i in items if i["kind"] == "road"]
    comm_labels = list(dict.fromkeys(i["label"] for i in at_border))
    if comm_labels:
        text = "по границе участка: " + ", ".join(comm_labels)
    elif nearby:
        n = nearby[0]
        text = f"по границе сетей нет; ближайшее — {n['label']} в {n['distance_m']} м"
    else:
        text = f"сетей и трубопроводов в радиусе {CONSTRUCTIONS_RADIUS_M} м в ЕГРН не найдено"
    text += (
        "; подключение — по техусловиям сетевой организации"
        if comm_labels or nearby
        else "; в деревне с домами электричество обычно есть — ТУ в сетевой организации"
    )
    return {
        "items": items,
        "at_border": at_border,
        "nearby": nearby,
        "roads": roads,
        "text": text,
        "radius_m": CONSTRUCTIONS_RADIUS_M,
        "source": SRC_CONSTR,
    }


_KEEP_ZONE_KEYS = ("reg_numb_border", "type_zone", "name_by_doc", "subcategory")
_KEEP_PMT_KEYS = (
    "nominal_number",
    "area",
    "land_use_text",
    "document_name",
    "document_number",
    "document_date",
    "document_issuer",
)
_KEEP_CONSTR_KEYS = (
    "cad_number",
    "params_name",
    "params_purpose",
    "params_extension",
    "address_readable_address",
    "ownership_type",
    "object_type_value",
)
_KEEP_PLOT_KEYS = (
    "cad_num",
    "land_record_category_type",
    "permitted_use_established_by_document",
    "cost_index",
    "cost_value",
    "specified_area",
    "declared_area",
    "area",
    "land_record_area",
    "ownership_type",
    "status",
)


def slim_feature(
    feature: dict[str, Any], keep: tuple[str, ...], *, geometry: bool
) -> dict[str, Any]:
    """Оставить от GeoJSON-объекта НСПД только нужное: кэш окружения иначе ~1 МБ на участок."""
    opts = (feature.get("properties") or {}).get("options") or {}
    out: dict[str, Any] = {
        "type": "Feature",
        "properties": {"options": {k: opts.get(k) for k in keep if opts.get(k) not in (None, "")}},
    }
    if geometry:
        out["geometry"] = feature.get("geometry")
    return out


def _with_distance(
    feature: dict[str, Any], keep: tuple[str, ...], parcel_geojson: dict[str, Any] | None
) -> dict[str, Any]:
    """Расстояние считается один раз при сохранении, геометрия зоны в кэш не идёт
    (266 ЗОУИТ Белокурихи с контурами газопроводов весили 3,7 МБ на участок)."""
    out = slim_feature(feature, keep, geometry=False)
    dist = feature.get("distance_m")
    if dist is None and parcel_geojson and feature.get("geometry"):
        dist = distance_m(parcel_geojson, feature["geometry"])
    if dist is not None:
        out["distance_m"] = dist
    return out


def slim_raw(raw: dict[str, Any], parcel_geojson: dict[str, Any] | None = None) -> dict[str, Any]:
    nspd = raw.get("nspd") or {}
    nspd["zouit"] = [
        _with_distance(f, _KEEP_ZONE_KEYS, parcel_geojson) for f in nspd.get("zouit") or []
    ]
    nspd["pmt"] = [_with_distance(f, _KEEP_PMT_KEYS, parcel_geojson) for f in nspd.get("pmt") or []]
    if nspd.get("constructions") is not None:
        nspd["constructions"] = [
            _with_distance(f, _KEEP_CONSTR_KEYS, parcel_geojson) for f in nspd["constructions"]
        ]
    nspd["neighbors"] = [
        _with_distance(f, _KEEP_PLOT_KEYS, parcel_geojson) for f in nspd.get("neighbors") or []
    ]
    nspd["auction"] = [
        slim_feature(f, _KEEP_PLOT_KEYS, geometry=False) for f in nspd.get("auction") or []
    ]
    osm = raw.get("osm")
    if osm and osm.get("elements"):
        slim_elements = []
        for el in osm["elements"]:
            tags = el.get("tags") or {}
            if osm_class(tags) is None and tags.get("highway") not in ACCESS_HIGHWAYS:
                continue
            keep = {k: el.get(k) for k in ("type", "lat", "lon", "center") if el.get(k) is not None}
            g = _osm_geojson(el)
            area = _osm_area_m2(el, parcel_geojson)
            if area is not None:
                keep["area_m2"] = area
            if parcel_geojson and g:
                keep["distance_m"] = distance_m(parcel_geojson, g)
            elif el.get("geometry") is not None:
                keep["geometry"] = el["geometry"]
            keep["tags"] = {
                k: v
                for k, v in tags.items()
                if k
                in (
                    "name",
                    "ref",
                    "landuse",
                    "amenity",
                    "aeroway",
                    "highway",
                    "railway",
                    "power",
                    "natural",
                    "waterway",
                    "leisure",
                    "boundary",
                    "tourism",
                    "shop",
                    "military",
                )
            }
            slim_elements.append(keep)
        osm["elements"] = slim_elements
    return raw


def empty_location_raw(cad_num: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "cad_num": cad_num,
        "fetched_at": datetime.now(UTC).isoformat(),
        "nspd_radius_m": NSPD_RADIUS_M,
        "neighbors_radius_m": NEIGHBORS_RADIUS_M,
        "osm_radius_m": OSM_RADIUS_M,
        "nspd": {
            "zouit": [],
            "pmt": [],
            "neighbors": [],
            "auction": [],
            "constructions": [],
            "errors": [],
        },
        "osm": None,
    }
