"""Рельеф участка по открытой цифровой модели местности (Copernicus DEM 90 м).

Открытые API высот: Open-Meteo (бесплатно для некоммерческого использования, 10 000 вызовов
в день, до 100 точек за вызов) и OpenTopoData (публичный экземпляр или свой на VPS, тот же
формат ответа). Адрес задаётся `LANDCHECK_ELEVATION_URL`. Считаем уклон, перепад и положение
участка относительно окружения; вместе с водой и зоной затопления — риск подтопления.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from shapely.geometry import shape

OPEN_METEO_URL = "https://api.open-meteo.com/v1/elevation"
RING_M = 300.0  # кольцо вокруг участка для оценки «низина / возвышенность»
SLOPE_STEEP_PCT = 15.0
SLOPE_NOTABLE_PCT = 8.0
LOWLAND_MAX_SLOPE_PCT = 3.0  # «низина» и подтопление имеют смысл только на ровной местности
FLAT_DROP_M = 1.5  # перепад меньше точности модели — участок ровный (зум 28.10, п. 1.5)
LOWLAND_M = -3.0
HILL_M = 3.0


def elevation_url() -> str | None:
    """Адрес API высот; `LANDCHECK_ELEVATION_URL=off` выключает рельеф (тесты, оффлайн)."""
    url = os.environ.get("LANDCHECK_ELEVATION_URL")
    if url and url.strip().lower() == "off":
        return None
    return url or OPEN_METEO_URL


def _offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / 111_320.0
    dlon = east_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.05))
    return (lat + dlat, lon + dlon)


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (b[0] - a[0]) * 111_320.0
    dx = (b[1] - a[1]) * 111_320.0 * math.cos(lat)
    return math.hypot(dx, dy)


def sample_points(parcel_geojson: dict[str, Any], ring_m: float = RING_M) -> dict[str, Any]:
    """9 точек по участку (центр, углы и середины сторон bbox) и 8 точек кольца вокруг."""
    geom = shape(parcel_geojson)
    c = geom.centroid
    minx, miny, maxx, maxy = geom.bounds
    xs = (minx, (minx + maxx) / 2, maxx)
    ys = (miny, (miny + maxy) / 2, maxy)
    parcel = [(y, x) for y in ys for x in xs]
    ring = [
        _offset(c.y, c.x, ring_m * math.cos(math.radians(a)), ring_m * math.sin(math.radians(a)))
        for a in range(0, 360, 45)
    ]
    return {
        "center": (c.y, c.x),
        "parcel": parcel,
        "ring": ring,
        "diag_m": _dist_m((miny, minx), (maxy, maxx)),
    }


def fetch_elevations(
    points: list[tuple[float, float]], *, url: str | None = None, timeout: float = 10.0
) -> list[float | None]:
    """Высоты в метрах для точек (lat, lon): Open-Meteo или OpenTopoData по формату URL."""
    url = url or elevation_url()
    if not points or not url:
        return []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        if "open-meteo" in url:
            r = client.get(
                url,
                params={
                    "latitude": ",".join(f"{p[0]:.6f}" for p in points),
                    "longitude": ",".join(f"{p[1]:.6f}" for p in points),
                },
            )
            r.raise_for_status()
            vals = r.json().get("elevation") or []
            return [float(v) if v is not None else None for v in vals]
        r = client.get(
            url, params={"locations": "|".join(f"{p[0]:.6f},{p[1]:.6f}" for p in points)}
        )
        r.raise_for_status()
        return [
            (float(x["elevation"]) if x.get("elevation") is not None else None)
            for x in r.json().get("results") or []
        ]


def terrain_summary(
    parcel_geojson: dict[str, Any] | None,
    *,
    water_distance_m: float | None = None,
    flood_zone: bool = False,
    water_zone: bool = False,
    fetch: Callable[[list[tuple[float, float]]], list[float | None]] | None = None,
) -> dict[str, Any] | None:
    """Сводка рельефа: высота, уклон, положение относительно окружения, уровень риска."""
    if not parcel_geojson or (fetch is None and elevation_url() is None):
        return None
    pts = sample_points(parcel_geojson)
    fetch = fetch or fetch_elevations
    try:
        vals = fetch(pts["parcel"] + pts["ring"])
    except Exception as exc:  # noqa: BLE001 — рельеф не должен ронять отчёт
        return {"error": f"высоты недоступны: {exc}", "fetched_at": datetime.now(UTC).isoformat()}
    n = len(pts["parcel"])
    parcel = [v for v in vals[:n] if v is not None]
    ring = [v for v in vals[n:] if v is not None]
    if len(parcel) < 3:
        return {"error": "высоты недоступны", "fetched_at": datetime.now(UTC).isoformat()}
    elev = sum(parcel) / len(parcel)
    drop = max(parcel) - min(parcel)
    # Для участка меньше ~60 м по диагонали модель 90 м не различает уклон внутри — берём
    # уклон местности по кольцу 300 м.
    if pts["diag_m"] >= 60 and drop > 0:
        slope = drop / pts["diag_m"] * 100.0
        slope_scope = "по участку"
    elif ring:
        slope = (max(ring) - min(ring)) / (2 * RING_M) * 100.0
        slope_scope = "по местности вокруг"
    else:
        slope, slope_scope = 0.0, "по участку"
    relief = (elev - sum(ring) / len(ring)) if ring else 0.0
    notes: list[str] = []
    level = "neutral"
    if slope >= SLOPE_STEEP_PCT:
        level = "risk"
        notes.append(
            f"крутой склон {slope:.0f} % ({slope_scope}): дороже фундамент, подпорные стены, "
            "подъезд"
        )
    elif slope >= SLOPE_NOTABLE_PCT:
        notes.append(
            f"заметный уклон {slope:.0f} % ({slope_scope}): учесть в проекте дома и дренаже"
        )
    elif drop < FLAT_DROP_M:
        notes.append(f"ровный участок: перепад {drop:.1f} м ({slope_scope}, точность модели ±1 м)")
    else:
        notes.append(f"ровно: уклон {slope:.0f} % ({slope_scope})")
    near_water = water_distance_m is not None and water_distance_m <= 300
    # На склоне «низина» и «подтопление» не имеют смысла: Красная Поляна при уклоне 26 %
    # получала «низина, риск подтопления» (Влад, зум 28.10). Про подтопление — только на
    # ровной местности; зона затопления по ЗОУИТ — факт при любом рельефе.
    flat = slope < SLOPE_NOTABLE_PCT
    if flat and relief <= LOWLAND_M:
        if slope <= LOWLAND_MAX_SLOPE_PCT and (flood_zone or near_water or water_zone):
            level = "risk"
            if flood_zone:
                why = "участок в зоне затопления"
            elif near_water:
                why = f"вода в {water_distance_m:.0f} м"
            else:
                why = "участок в водоохранной зоне или прибрежной полосе, вода рядом"
            notes.append(
                f"низина: на {abs(relief):.0f} м ниже окружения в радиусе {RING_M:.0f} м, {why} — "
                "риск подтопления, уточнить отметки паводка и дренаж"
            )
        else:
            notes.append(
                f"ниже окружения на {abs(relief):.0f} м — проверить сток воды и грунтовые воды"
            )
    elif flat and relief >= HILL_M:
        if level != "risk":
            level = "plus"
        notes.append(
            f"возвышенность: на {relief:.0f} м выше окружения — сухо, вид, сток от участка"
        )
    elif flood_zone:
        level = "risk"
        notes.append("участок в зоне затопления по ЗОУИТ — риск подтопления независимо от рельефа")
    lat, lon = pts["center"]
    return {
        "elevation_m": round(elev, 1),
        "drop_m": round(drop, 1),
        "slope_pct": round(slope, 1),
        "slope_scope": slope_scope,
        "relief_m": round(relief, 1),
        "level": level,
        "notes": notes,
        "source": "Copernicus DEM 90 м"
        + (" (Open-Meteo)" if "open-meteo" in (elevation_url() or "") else " (OpenTopoData)"),
        # Наклонный спутниковый вид: так Андрей смотрит далёкие участки (зум 28.10, 1:16).
        "earth_url": earth_url(lat, lon),
        "fetched_at": datetime.now(UTC).isoformat(),
        "error": None,
    }


def earth_url(lat: float, lon: float, *, distance_m: float = 600.0, tilt_deg: float = 60.0) -> str:
    """Ссылка на Google Earth (web) с наклоном камеры — рельеф и застройка видны объёмно."""
    return f"https://earth.google.com/web/@{lat:.6f},{lon:.6f},0a,{distance_m:.0f}d,35y,0h,{tilt_deg:.0f}t,0r"
