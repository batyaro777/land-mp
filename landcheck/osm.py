"""Окружение по OpenStreetMap через Overpass API (ТЗ М2.2: радиус 3 км, расстояния)."""

from __future__ import annotations

from typing import Any

import httpx

# Проверено с VPS 15.10.2026: overpass-api.de и kumi отвечали 406/502/500, зеркало mail.ru
# отдавало данные за ~25 с. Перебираем по порядку, первый успешный — в ответе.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "landcheck/0.1 (land-mp.dnai.engineering; broker land check)"

_QUERY = """
[out:json][timeout:25];
(
  way["landuse"~"^(industrial|landfill|cemetery|quarry|military)$"](around:{r},{lat},{lon});
  relation["landuse"~"^(industrial|landfill|cemetery|quarry)$"](around:{r},{lat},{lon});
  nwr["amenity"~"^(grave_yard|waste_disposal)$"](around:{r},{lat},{lon});
  nwr["aeroway"="aerodrome"](around:{r},{lat},{lon});
  way["highway"~"^(motorway|trunk|primary)$"](around:{r},{lat},{lon});
  way["railway"="rail"](around:{r},{lat},{lon});
  way["power"="line"](around:{r},{lat},{lon});
  way["natural"="water"](around:{r},{lat},{lon});
  way["waterway"~"^(river|canal)$"](around:{r},{lat},{lon});
  nwr["leisure"="nature_reserve"](around:{r},{lat},{lon});
  nwr["tourism"~"^(resort|hotel|spa_resort)$"](around:{r},{lat},{lon});
  node["amenity"~"^(school|kindergarten|hospital|clinic)$"](around:{r},{lat},{lon});
  node["shop"~"^(supermarket|convenience)$"](around:{r},{lat},{lon});
);
out geom tags;
"""


def fetch_osm(lat: float, lon: float, radius_m: int, *, timeout: float = 45.0) -> dict[str, Any]:
    """{"elements": [...]} или {"elements": [], "error": "..."} — без исключений наружу."""
    query = _QUERY.format(r=int(radius_m), lat=lat, lon=lon)
    last_error = "Overpass недоступен"
    for url in OVERPASS_URLS:
        try:
            r = httpx.post(
                url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
            if r.status_code != 200:
                last_error = f"Overpass {url}: HTTP {r.status_code}"
                continue
            data = r.json()
            return {"elements": data.get("elements") or [], "url": url}
        except Exception as e:  # noqa: BLE001 — сеть; окружение не должно валить отчёт
            last_error = f"Overpass {url}: {type(e).__name__}: {e}"
    return {"elements": [], "error": last_error}
