"""Конструктор синтетических payload в формате провайдера. Данные вымышленные."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[37.0, 55.0], [37.001, 55.0], [37.001, 55.001], [37.0, 55.001], [37.0, 55.0]]],
}


def make_payload(
    cad_num: str,
    *,
    found: bool = True,
    category: str | None = "Земли населенных пунктов",
    vri: str | None = "Для индивидуального жилищного строительства",
    vri_tab: list[str] | None = None,
    area: float | None = 1000.0,
    specified_area: float | None = 1000.0,
    declared_area: float | None = None,
    cost: float | None = 1_500_000.0,
    status: str | None = "Учтенный",
    ownership: str | None = "Частная собственность",
    borders: bool = True,
    zouit: list[dict[str, Any]] | None = None,
    zouit_checked: bool = True,
    tz: list[dict[str, Any]] | None = None,
    tz_checked: bool = True,
    fetched_at: datetime | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    fetched = (fetched_at or datetime.now(UTC)).isoformat()
    if not found:
        return {
            "schema_version": 1,
            "cad_num": cad_num,
            "fetched_at": fetched,
            "provider": "fixture",
            "found": False,
            "landplot": None,
            "permission_types": None,
            "zouit": None,
            "territorial_zones": None,
            "errors": errors or [],
            "_synthetic": True,
        }
    options = {
        "cad_num": cad_num,
        "readable_address": "Синтетический адрес, тестовые данные",
        "status": status,
        "land_record_type": "Земельный участок",
        "land_record_subtype": "Землепользование",
        "land_record_reg_date": "2015-06-01T00:00:00",
        "land_record_category_type": category,
        "permitted_use_established_by_document": vri,
        "area": area,
        "specified_area": specified_area,
        "declared_area": declared_area,
        "cost_value": cost,
        "cost_index": (cost / area) if (cost and area) else None,
        "ownership_type": ownership,
        "geocoderObject": not borders,
        "objdocId": 1,
        "registersId": 1,
    }
    landplot = {
        "type": "Feature",
        "id": 123456,
        "geometry": SQUARE if borders else {"type": "Point", "coordinates": [37.0005, 55.0005]},
        "properties": {
            "category": 36368,
            "categoryName": "Земельные участки из ЕГРН",
            "options": options,
            "systemInfo": None,
        },
    }
    return {
        "schema_version": 1,
        "cad_num": cad_num,
        "fetched_at": fetched,
        "provider": "fixture",
        "found": True,
        "landplot": landplot,
        "permission_types": vri_tab,
        "zouit": (zouit or []) if zouit_checked else None,
        "territorial_zones": (tz or []) if tz_checked else None,
        "errors": errors or [],
        "_synthetic": True,
    }


def zone(
    layer: str,
    reg: str,
    kind: str | None = None,
    name: str | None = None,
    restrictions: str | None = None,
) -> dict[str, Any]:
    """Структура как в реальном ответе НСПД (type_zone, name_by_doc, reg_numb_border)."""
    return {
        "type": "Feature",
        "id": 1,
        "geometry": SQUARE,
        "properties": {
            "category": 36940,
            "categoryName": layer,
            "options": {
                "reg_numb_border": reg,
                "type_zone": kind,
                "name_by_doc": name,
                "content_restrict_encumbrances": restrictions,
            },
        },
    }
