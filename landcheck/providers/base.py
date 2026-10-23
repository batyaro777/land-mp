"""Контракт провайдера данных по участку.

Сырой ответ (payload) — словарь одного формата для живого НСПД и для фикстур:

    {
      "schema_version": 1,
      "cad_num": "50:09:0060202:123",
      "fetched_at": "2026-10-10T08:00:00+00:00",
      "provider": "nspd" | "fixture",
      "found": true,
      "landplot": {GeoJSON Feature из слоя «Земельные участки из ЕГРН»} | null,
      "permission_types": ["..."] | null,      # вкладка ВРИ; null = не запрашивали / ошибка
      "zouit": [GeoJSON Feature, ...] | null,  # пересечение с ЗОУИТ; null = не проверяли
      "territorial_zones": [GeoJSON Feature, ...] | null,
      "errors": ["..."]
    }
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

SCHEMA_VERSION = 1


class ParcelProvider(Protocol):
    name: str

    def fetch_raw(self, cad_num: str) -> dict[str, Any]:
        """Вернуть payload по кадастровому номеру. Не бросать при «не найдено»."""
        ...


def empty_payload(cad_num: str, provider: str, *, found: bool = False) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "cad_num": cad_num,
        "fetched_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "found": found,
        "landplot": None,
        "permission_types": None,
        "zouit": None,
        "territorial_zones": None,
        "errors": [],
    }
