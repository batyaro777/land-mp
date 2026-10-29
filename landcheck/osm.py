"""Окружение по OpenStreetMap через Overpass API (ТЗ М2.2: радиус 3 км, расстояния)."""

from __future__ import annotations

import logging
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

import httpx

log = logging.getLogger(__name__)

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
  nwr["amenity"="grave_yard"](around:{r},{lat},{lon});
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


def default_timeout() -> float:
    """Секунды на ответ зеркала; `LANDCHECK_OSM_TIMEOUT`. Клавдий (прод 29.10): с 45 с все
    четыре зеркала отваливались по очереди (до 3 минут на блок), с 90 с ответ пришёл за 46 с."""
    try:
        return float(os.environ.get("LANDCHECK_OSM_TIMEOUT") or 90.0)
    except ValueError:
        return 90.0


def _query_mirror(url: str, query: str, timeout: float) -> dict[str, Any]:
    r = httpx.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json()
    return {"elements": data.get("elements") or [], "url": url}


def fetch_osm(
    lat: float, lon: float, radius_m: int, *, timeout: float | None = None
) -> dict[str, Any]:
    """{"elements": [...]} или {"elements": [], "error": "..."} — без исключений наружу.

    Зеркала опрашиваются параллельно, берётся первый успешный ответ: последовательный перебор
    четырёх зеркал с таймаутами растягивал «Локацию» до 2–3 минут.
    """
    query = _QUERY.format(r=int(radius_m), lat=lat, lon=lon)
    timeout = timeout or default_timeout()
    failures: list[str] = []
    pool = ThreadPoolExecutor(max_workers=len(OVERPASS_URLS), thread_name_prefix="overpass")
    futures = {pool.submit(_query_mirror, url, query, timeout): url for url in OVERPASS_URLS}
    try:
        pending = set(futures)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                url = futures[fut]
                try:
                    return fut.result()
                except Exception as e:  # noqa: BLE001 — сеть; окружение не должно валить отчёт
                    failures.append(f"{_host(url)}: {type(e).__name__}: {e}"[:120])
    finally:
        # Остальные запросы не ждём: пул закрывается в фоне, потоки завершатся по таймауту.
        pool.shutdown(wait=False, cancel_futures=True)
    # Хосты и коды ошибок — в лог; клиенту в отчёте — одна понятная фраза (зум 28.10, п. 1.10).
    log.warning("overpass: все зеркала отказали: %s", "; ".join(failures))
    return {
        "elements": [],
        "error": "серверы OpenStreetMap не ответили, нажмите «обновить» позже",
        "error_detail": "; ".join(failures),
    }


def _host(url: str) -> str:
    return url.split("/")[2]
