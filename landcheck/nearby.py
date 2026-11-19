"""Факторы спроса у участка: что рядом и как добраться — по OpenStreetMap.

ТЗ v5, п. 7 (зум 18.11.2026, 01:08: «доступность, инфраструктура, коммуникации, дороги»).
Для лота-кандидата один запрос к карте вокруг его точки: ближайший населённый пункт, город,
дорога с твёрдым покрытием, линии электропередачи, газопровод, вода, школа и магазин и то, что
портит место (свалка, кладбище, промзона, карьер). Здесь только разбор ответа и выводы; сеть —
в `landcheck.web.torgi_nearby`.

Карта неполна: распределительные линии и газопроводы в сёлах нанесены не везде. Поэтому
найденный объект — факт, а отсутствие на карте — не доказательство, что его нет; на этом
построены и подписи, и итог.
"""

from __future__ import annotations

import math
from typing import Any

FAR_M = 60_000  # города ищем в часе езды
NEAR_M = 5_000  # сёла, школа, магазин, станция
ROAD_M = 3_000  # дорога с твёрдым покрытием
UTIL_M = 2_000  # ЛЭП, газ, вода
BAD_M = 3_000  # что портит место (как в отчёте по участку)
CITY_POPULATION = 50_000  # с такого населения город тянет спрос на землю вокруг

QUERY = """
[out:json][timeout:40];
(
  node["place"~"^(city|town)$"](around:{far},{lat},{lon});
  node["place"~"^(village|hamlet|suburb|neighbourhood)$"](around:{near},{lat},{lon});
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary)$"](around:{road},{lat},{lon});
  way["highway"~"^(unclassified|residential|living_street)$"]["surface"~"^(asphalt|concrete|paved|paving_stones|sett)"](around:{road},{lat},{lon});
  way["power"~"^(line|minor_line)$"](around:{util},{lat},{lon});
  way["man_made"="pipeline"]["substance"~"gas"](around:{util},{lat},{lon});
  way["natural"="water"](around:{util},{lat},{lon});
  way["waterway"~"^(river|canal)$"](around:{util},{lat},{lon});
  way["landuse"~"^(industrial|landfill|cemetery|quarry)$"](around:{bad},{lat},{lon});
  nwr["amenity"="grave_yard"](around:{bad},{lat},{lon});
  node["amenity"~"^(school|kindergarten)$"](around:{near},{lat},{lon});
  node["shop"~"^(supermarket|convenience)$"](around:{near},{lat},{lon});
  node["railway"~"^(station|halt)$"](around:{near},{lat},{lon});
);
out geom tags;
"""

PLACE_RU = {
    "city": "город",
    "town": "город",
    "village": "село",
    "hamlet": "деревня",
    "suburb": "район города",
    "neighbourhood": "микрорайон",
}
ROAD_RU = {
    "motorway": "автомагистраль",
    "trunk": "трасса",
    "primary": "региональная дорога",
    "secondary": "дорога",
    "tertiary": "дорога",
    "unclassified": "местная дорога с покрытием",
    "residential": "улица с покрытием",
    "living_street": "улица с покрытием",
}
BAD_RU = {
    "landfill": "свалка",
    "cemetery": "кладбище",
    "industrial": "промзона",
    "quarry": "карьер",
}
# ближе этого расстояния сосед заметно портит место; дальше — только упоминаем
BAD_NEAR_M = {"landfill": 2000, "cemetery": 500, "industrial": 500, "quarry": 1000}


def query(lat: float, lon: float) -> str:
    return QUERY.format(
        far=FAR_M, near=NEAR_M, road=ROAD_M, util=UTIL_M, bad=BAD_M, lat=lat, lon=lon
    )


def _xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Метры от точки отсчёта: на десятках километров плоской проекции хватает."""
    return (
        math.radians(lon - lon0) * 6_371_000 * math.cos(math.radians(lat0)),
        math.radians(lat - lat0) * 6_371_000,
    )


def _seg(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if not dx and not dy:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def distance_m(lat: float, lon: float, el: dict[str, Any]) -> float | None:
    """Расстояние от точки лота до объекта карты: до точки, до ближайшего отрезка линии."""
    if el.get("type") == "node" and "lat" in el:
        return math.hypot(*_xy(el["lat"], el["lon"], lat, lon))
    pts = [_xy(p["lat"], p["lon"], lat, lon) for p in el.get("geometry") or [] if p]
    if not pts and el.get("members"):  # отношение: берём контуры его частей
        pts = [
            _xy(p["lat"], p["lon"], lat, lon)
            for m in el["members"]
            for p in m.get("geometry") or []
            if p
        ]
    if not pts:
        center = el.get("center")
        return math.hypot(*_xy(center["lat"], center["lon"], lat, lon)) if center else None
    if len(pts) == 1:
        return math.hypot(*pts[0])
    return min(_seg(0.0, 0.0, a, b) for a, b in zip(pts, pts[1:], strict=False))


def _population(tags: dict[str, str]) -> int | None:
    digits = "".join(ch for ch in tags.get("population") or "" if ch.isdigit())
    return int(digits) if digits else None


def _kind(tags: dict[str, str]) -> str | None:
    if tags.get("place") in PLACE_RU:
        return "place"
    if tags.get("highway") in ROAD_RU:
        return "road"
    if tags.get("power") == "line":
        return "hv_line"
    if tags.get("power") == "minor_line":
        return "power"
    if tags.get("man_made") == "pipeline":
        return "gas"
    if tags.get("natural") == "water" or tags.get("waterway") in ("river", "canal"):
        return "water"
    if tags.get("landuse") in BAD_RU:
        return tags["landuse"]
    if tags.get("amenity") == "grave_yard":
        return "cemetery"
    if tags.get("amenity") in ("school", "kindergarten"):
        return "school"
    if tags.get("shop") in ("supermarket", "convenience"):
        return "shop"
    if tags.get("railway") in ("station", "halt"):
        return "rail"
    return None


def summarize(lat: float, lon: float, elements: list[dict[str, Any]]) -> dict[str, Any]:
    """Ближайшие объекты каждого вида и расстояния до них в метрах. Ключи, которых нет, —
    объект на карте в радиусе поиска не найден."""
    best: dict[str, dict[str, Any]] = {}

    def keep(key: str, dist: float, **extra: Any) -> None:
        cur = best.get(key)
        if cur is None or dist < cur["m"]:
            best[key] = {"m": round(dist), **{k: v for k, v in extra.items() if v}}

    for el in elements:
        tags = el.get("tags") or {}
        kind = _kind(tags)
        dist = distance_m(lat, lon, el) if kind else None
        if dist is None:
            continue
        name = tags.get("name")
        if kind == "place":
            place, people = tags["place"], _population(tags)
            if place in ("city", "town"):
                keep("town", dist, name=name, population=people)
                if place == "city" or (people or 0) >= CITY_POPULATION:
                    keep("city", dist, name=name, population=people)
            # ближайший населённый пункт любого размера, в том числе сам город
            if dist <= NEAR_M:
                keep("settlement", dist, name=name, kind=PLACE_RU[place])
        elif kind == "road":
            keep("road", dist, kind=ROAD_RU[tags["highway"]], name=tags.get("ref") or name)
        elif kind in BAD_RU:
            keep(kind, dist, name=name)
        else:
            keep(kind, dist, name=name)
    bad = [{"kind": k, "label": BAD_RU[k], **best.pop(k)} for k in list(best) if k in BAD_RU]
    best["bad"] = sorted(bad, key=lambda x: x["m"])
    return best


def km(m: float | None) -> str:
    if m is None:
        return "—"
    if m < 950:
        return f"{max(50, round(m / 50) * 50)} м" if m >= 75 else "рядом"
    return f"{m / 1000:.1f} км".replace(".", ",") if m < 9950 else f"{round(m / 1000)} км"


def access(facts: dict[str, Any]) -> dict[str, Any]:
    """Итог по доступности места: `good`, `mid` или `weak` — и из чего он сложен.

    Правила намеренно простые и проверяемые глазами:
    - слабая — на карте нет дороги с твёрдым покрытием в 3 км либо нет населённого пункта в
      5 км: до такого участка трудно доехать, покупателей на него мало;
    - хорошая — населённый пункт не дальше 1 км, дорога с покрытием не дальше 1 км и город от
      50 тыс. жителей не дальше 30 км;
    - иначе средняя."""
    road, place, city = facts.get("road"), facts.get("settlement"), facts.get("city")
    why: list[str] = []
    if road is None:
        why.append(f"на карте нет дороги с твёрдым покрытием в {ROAD_M // 1000} км")
    if place is None:
        why.append(f"нет населённого пункта в {NEAR_M // 1000} км")
    if why:
        return {"level": "weak", "label": "слабая", "why": why}
    good = road["m"] <= 1000 and place["m"] <= 1000 and city is not None and city["m"] <= 30_000
    if good:
        return {
            "level": "good",
            "label": "хорошая",
            "why": [
                f"{place.get('kind') or 'населённый пункт'} {km(place['m'])}",
                f"дорога с покрытием {km(road['m'])}",
                f"{city.get('name') or 'город'} {km(city['m'])}",
            ],
        }
    if road["m"] > 1000:
        why.append(f"до дороги с покрытием {km(road['m'])}")
    if place["m"] > 1000:
        why.append(f"до населённого пункта {km(place['m'])}")
    if city is None:
        why.append(f"города от {CITY_POPULATION // 1000} тыс. жителей в {FAR_M // 1000} км нет")
    elif city["m"] > 30_000:
        why.append(f"до города {km(city['m'])}")
    return {"level": "mid", "label": "средняя", "why": why}


def nuisances(facts: dict[str, Any]) -> list[str]:
    """Что портит место ближе порога для своего вида (свалка — 2 км, кладбище и промзона —
    500 м, карьер — 1 км); высоковольтная линия ближе 50 м — охранная зона."""
    out = [
        f"{b['label']} {km(b['m'])}"
        for b in facts.get("bad") or []
        if b["m"] <= BAD_NEAR_M[b["kind"]]
    ]
    hv = facts.get("hv_line")
    if hv and hv["m"] <= 50:
        out.append("высоковольтная линия над участком или вплотную")
    return out


def lines(facts: dict[str, Any]) -> list[tuple[str, str]]:
    """Строки для сводки инвестору: (что, значение). Только найденное на карте."""

    def named(x: dict[str, Any], default: str = "") -> str:
        name = x.get("name") or default
        return f"{name}, {km(x['m'])}" if name else km(x["m"])

    out: list[tuple[str, str]] = []
    place, town, city = facts.get("settlement"), facts.get("town"), facts.get("city")
    if place:
        out.append(
            ("Ближайший населённый пункт", f"{place.get('kind', '')} {named(place)}".strip())
        )
    if town and (not place or town.get("name") != place.get("name")):
        out.append(("Ближайший город", named(town)))
    if city and (not town or city.get("name") != town.get("name")):
        people = city.get("population")
        tail = f" ({round(people / 1000)} тыс. жителей)" if people else ""
        out.append(("Крупный город", named(city) + tail))
    road = facts.get("road")
    if road:
        out.append(("Дорога с твёрдым покрытием", f"{road.get('kind', 'дорога')}, {km(road['m'])}"))
    if facts.get("power"):
        out.append(("Линия электропередачи 0,4–10 кВ", km(facts["power"]["m"])))
    if facts.get("hv_line"):
        out.append(("Высоковольтная линия", km(facts["hv_line"]["m"])))
    if facts.get("gas"):
        out.append(("Газопровод", km(facts["gas"]["m"])))
    if facts.get("water"):
        out.append(("Вода (река, озеро, пруд)", named(facts["water"])))
    for key, label in (
        ("school", "Школа или детский сад"),
        ("shop", "Магазин"),
        ("rail", "Станция"),
    ):
        if facts.get(key):
            out.append((label, named(facts[key])))
    for b in facts.get("bad") or []:
        out.append((b["label"].capitalize(), named(b)))
    return out
