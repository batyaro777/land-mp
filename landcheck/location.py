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
NSPD_RADIUS_M = 1000  # ЗОУИТ и проекты межевания
NEIGHBORS_RADIUS_M = 400  # соседи по кадастровой стоимости: ближний круг, иначе сотни участков
OSM_RADIUS_M = 3000  # ТЗ М2.2

SRC_NSPD_AROUND = "НСПД, объекты в радиусе от контура участка"
SRC_GKO = "НСПД, удельный показатель кадастровой стоимости соседних участков (ГКО, не рынок)"
SRC_OSM = "OpenStreetMap (Overpass API); полнота данных зависит от региона"
SRC_PMT = "НСПД, слой «ЗУ, образуемые по проекту межевания территории»"

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
    (
        "лечебно-оздоровительн",
        "округ санитарной охраны курорта",
        "plus",
        "курортная локация: спрос на рекреацию выше, режим застройки строже",
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
        "target_rub_m2": target_cost_index,
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
    if not same or not same.get("median_rub_m2"):
        # Категория участка не установлена или у своей группы нет ставки — берём самую
        # многочисленную группу соседей и говорим об этом прямо.
        candidates = [c for c in neighbors.get("categories") or [] if c.get("median_rub_m2")]
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
    if listing_price_rub and area_m2:
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


def osm_class(tags: dict[str, str]) -> str | None:
    lu = tags.get("landuse", "")
    if lu == "landfill" or tags.get("amenity") == "waste_disposal":
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


def summarize_osm(
    parcel_geojson: dict[str, Any] | None, elements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ближайший объект каждого класса в радиусе OSM."""
    best: dict[str, dict[str, Any]] = {}
    for el in elements:
        tags = el.get("tags") or {}
        cls = osm_class(tags)
        if cls is None:
            continue
        dist = el.get("distance_m")
        if dist is None:
            g = _osm_geojson(el)
            dist = distance_m(parcel_geojson, g) if (parcel_geojson and g) else None
        name = tags.get("name") or tags.get("ref")
        label, severity = _OSM_LABEL[cls]
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
        "auction_parcels": len(nspd.get("auction") or []),
        "neighbors": neighbors,
        "price": price_reference(neighbors, listing_price_rub, area_m2),
        "osm": summarize_osm(parcel_geojson, (osm or {}).get("elements") or []) if osm else None,
        "osm_error": (osm or {}).get("error") if osm else "OSM не запрашивался",
        "errors": list(nspd.get("errors") or []),
        "disclaimer": (
            "Окружение и ориентир стоимости собраны автоматически из НСПД и OpenStreetMap. "
            "Это не отчёт об оценке и не анализ генплана: документы терпланирования в НСПД "
            "не публикуются, сверяйте с генпланом и ПЗЗ муниципалитета."
        ),
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
    nspd["neighbors"] = [
        slim_feature(f, _KEEP_PLOT_KEYS, geometry=False) for f in nspd.get("neighbors") or []
    ]
    nspd["auction"] = [
        slim_feature(f, _KEEP_PLOT_KEYS, geometry=False) for f in nspd.get("auction") or []
    ]
    osm = raw.get("osm")
    if osm and osm.get("elements"):
        slim_elements = []
        for el in osm["elements"]:
            tags = el.get("tags") or {}
            if osm_class(tags) is None:
                continue
            keep = {k: el.get(k) for k in ("type", "lat", "lon", "center") if el.get(k) is not None}
            g = _osm_geojson(el)
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
        "nspd": {"zouit": [], "pmt": [], "neighbors": [], "auction": [], "errors": []},
        "osm": None,
    }
