"""payload провайдера → Parcel с прослеживаемостью."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from landcheck.models import Parcel, ZoneHit

_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _get(d: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    """Первое непустое значение по списку ключей (snake и camel варианты)."""
    if not d:
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
        camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), k)
        if camel in d and d[camel] not in (None, ""):
            return d[camel]
        snake = _SNAKE_RE.sub("_", k).lower()
        if snake in d and d[snake] not in (None, ""):
            return d[snake]
    return default


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, int | float):
        return float(v)
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _to_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:25], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _zone_hit(feature: dict[str, Any]) -> ZoneHit:
    props = feature.get("properties") or {}
    opts = props.get("options") or {}
    layer = _get(props, "category_name", default="ЗОУИТ")
    reg = _get(opts, "reg_numb_border", "brd_nmb", "registration_number", "reg_number")
    kind = _get(opts, "type", "obj_kind_value", "zone_type", "type_zone", "kind")
    name = _get(
        opts, "name", "name_zone", "zone_name", "description", "content_restrict_encumbrances"
    )
    if name is None:
        name = _get(props, "descr", "label")
    extra = {
        k: v for k, v in opts.items() if k not in ("geocoderObject", "objdocId", "registersId")
    }
    return ZoneHit(layer=str(layer), reg_number=reg, kind=kind, name=name, extra=extra)


def parcel_from_payload(payload: dict[str, Any]) -> Parcel:
    cad_num = payload.get("cad_num", "")
    fetched_at = _to_dt(
        payload.get("_cache_fetched_at") or payload.get("fetched_at")
    ) or datetime.now(UTC)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)

    landplot = payload.get("landplot")
    if not payload.get("found") or not landplot:
        return Parcel(cad_num=cad_num, found=False, fetched_at=fetched_at, raw=payload)

    props = landplot.get("properties") or {}
    opts = props.get("options") or {}
    geometry = landplot.get("geometry")
    no_coords = bool(_get(opts, "geocoderObject", "no_coords", default=False))
    gtype = (geometry or {}).get("type")
    has_borders = (not no_coords) and gtype in ("Polygon", "MultiPolygon")

    zouit_raw = payload.get("zouit")
    tz_raw = payload.get("territorial_zones")

    parcel = Parcel(
        cad_num=_get(opts, "cad_num", default=cad_num),
        found=True,
        fetched_at=fetched_at,
        address=_get(opts, "readable_address"),
        status=_get(opts, "status"),
        category=_get(opts, "land_record_category_type"),
        permitted_use=_get(opts, "permitted_use_established_by_document"),
        permitted_use_tab=[str(x) for x in (payload.get("permission_types") or []) if x],
        area_m2=_to_float(_get(opts, "area")),
        specified_area_m2=_to_float(_get(opts, "specified_area")),
        declared_area_m2=_to_float(_get(opts, "declared_area")),
        cadastral_cost_rub=_to_float(_get(opts, "cost_value")),
        cost_index_rub_m2=_to_float(_get(opts, "cost_index")),
        ownership_type=_get(opts, "ownership_type"),
        land_record_type=_get(opts, "land_record_type"),
        land_record_subtype=_get(opts, "land_record_subtype"),
        reg_date=_to_dt(_get(opts, "land_record_reg_date")),
        has_borders=has_borders,
        geometry=geometry if has_borders else None,
        zouit=[_zone_hit(f) for f in (zouit_raw or [])],
        zouit_checked=zouit_raw is not None,
        territorial_zones=[_zone_hit(f) for f in (tz_raw or [])],
        territorial_zone_checked=tz_raw is not None,
        raw=payload,
    )
    return parcel
