"""CheckResult ↔ JSON для хранения в БД и отрисовки страниц."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from landcheck.models import SOTKA_M2, CheckResult, Flag, Level, Parcel, Purpose, Request, ZoneHit


def request_to_dict(r: Request) -> dict[str, Any]:
    return {
        "purpose": r.purpose.value,
        "budget_rub": r.budget_rub,
        "min_area_m2": r.min_area_m2,
        "max_area_m2": r.max_area_m2,
        "region_hint": r.region_hint,
    }


def request_from_dict(d: dict[str, Any]) -> Request:
    return Request(
        purpose=Purpose(d.get("purpose", "any")),
        budget_rub=d.get("budget_rub"),
        min_area_m2=d.get("min_area_m2"),
        max_area_m2=d.get("max_area_m2"),
        region_hint=d.get("region_hint"),
    )


def _zone(z: ZoneHit) -> dict[str, Any]:
    return {
        "layer": z.layer,
        "reg_number": z.reg_number,
        "kind": z.kind,
        "name": z.name,
        "restrictions": z.restrictions,
        "describe": z.describe(),
        "extra": {k: v for k, v in z.extra.items() if k != "content_restrict_encumbrances"},
    }


def _zone_geometries(parcel: Parcel) -> list[dict[str, Any]]:
    """Геометрии ЗОУИТ из сырого payload для карты (GeoJSON, EPSG:4326)."""
    out = []
    for f in parcel.raw.get("zouit") or []:
        g = f.get("geometry")
        opts = (f.get("properties") or {}).get("options") or {}
        if g and g.get("type") in ("Polygon", "MultiPolygon"):
            out.append(
                {
                    "geometry": g,
                    "kind": opts.get("type_zone"),
                    "name": opts.get("name_by_doc"),
                    "reg_number": opts.get("reg_numb_border"),
                }
            )
    return out


def result_to_dict(r: CheckResult) -> dict[str, Any]:
    p, lst = r.parcel, r.listing
    return {
        "listing": {
            "cad_num": lst.cad_num,
            "price_rub": lst.price_rub,
            "declared_area_m2": lst.declared_area_m2,
            "url": lst.url,
            "note": lst.note,
            "raw_cad_num": lst.raw_cad_num,
        },
        "duplicates": [
            {"price_rub": d.price_rub, "declared_area_m2": d.declared_area_m2, "url": d.url}
            for d in r.duplicates
        ],
        "parcel": {
            "cad_num": p.cad_num,
            "found": p.found,
            "fetched_at": p.fetched_at.isoformat(),
            "source": p.source,
            "address": p.address,
            "status": p.status,
            "category": p.category,
            "permitted_use": p.permitted_use,
            "permitted_use_tab": p.permitted_use_tab,
            "area_m2": p.best_area_m2,
            "specified_area_m2": p.specified_area_m2,
            "declared_area_m2": p.declared_area_m2,
            "cadastral_cost_rub": p.cadastral_cost_rub,
            "cost_index_rub_m2": p.cost_index_rub_m2,
            "ownership_type": p.ownership_type,
            "land_record_type": p.land_record_type,
            "land_record_subtype": p.land_record_subtype,
            "reg_date": p.reg_date.isoformat() if p.reg_date else None,
            "has_borders": p.has_borders,
            "geometry": p.geometry,
            "zouit": [_zone(z) for z in p.zouit],
            "zouit_checked": p.zouit_checked,
            "zouit_geometries": _zone_geometries(p),
            "territorial_zones": [_zone(z) for z in p.territorial_zones],
            "territorial_zone_checked": p.territorial_zone_checked,
            "errors": list(p.raw.get("errors") or []),
        },
        "flags": [
            {
                "rule_id": f.rule_id,
                "level": f.level.value,
                "message": f.message,
                "source": f.source,
                "details": f.details,
            }
            for f in r.flags
        ],
        "verdict": r.verdict.value,
        "score": r.score,
        "price_per_sotka": r.price_per_sotka,
        "area_sotka": (r.effective_area_m2 / SOTKA_M2) if r.effective_area_m2 else None,
    }


LEVEL_RANK = {Level.STOP.value: 2, Level.CHECK.value: 1, Level.OK.value: 0}


def flags_by_level(item: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"стоп": [], "проверить": [], "ок": []}
    for f in item.get("flags", []):
        out.setdefault(f["level"], []).append(f)
    return out


def fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f} ₽".replace(",", " ")


def fmt_sotka(m2: float | None) -> str:
    if not m2:
        return "—"
    return f"{m2 / SOTKA_M2:.2f} сот."


def fmt_date(s: str | datetime | None) -> str:
    if not s:
        return "—"
    if isinstance(s, datetime):
        return s.strftime("%d.%m.%Y %H:%M")
    try:
        return datetime.fromisoformat(s).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return s


def fmt_day(s: str | datetime | None) -> str:
    if not s:
        return "—"
    if isinstance(s, datetime):
        return s.strftime("%d.%m.%Y")
    try:
        return datetime.fromisoformat(s).strftime("%d.%m.%Y")
    except ValueError:
        return s


def flag_obj(d: dict[str, Any]) -> Flag:
    return Flag(d["rule_id"], Level(d["level"]), d["message"], d["source"], d.get("details") or {})


def result_from_dict(d: dict[str, Any]) -> CheckResult:
    """Обратная сборка для экспорта XLSX/CSV из сохранённого JSON."""
    from datetime import UTC

    from landcheck.models import Listing

    ld = d["listing"]
    listing = Listing(
        cad_num=ld["cad_num"],
        price_rub=ld.get("price_rub"),
        declared_area_m2=ld.get("declared_area_m2"),
        url=ld.get("url"),
        note=ld.get("note"),
        raw_cad_num=ld.get("raw_cad_num"),
    )
    pd = d["parcel"]
    fetched = (
        datetime.fromisoformat(pd["fetched_at"]) if pd.get("fetched_at") else datetime.now(UTC)
    )
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)

    def zones(items: list[dict[str, Any]]) -> list[ZoneHit]:
        return [
            ZoneHit(
                layer=z.get("layer") or "",
                reg_number=z.get("reg_number"),
                kind=z.get("kind"),
                name=z.get("name"),
                restrictions=z.get("restrictions"),
                extra=z.get("extra") or {},
            )
            for z in items
        ]

    parcel = Parcel(
        cad_num=pd.get("cad_num") or ld["cad_num"],
        found=bool(pd.get("found")),
        fetched_at=fetched,
        source=pd.get("source") or "НСПД",
        address=pd.get("address"),
        status=pd.get("status"),
        category=pd.get("category"),
        permitted_use=pd.get("permitted_use"),
        permitted_use_tab=list(pd.get("permitted_use_tab") or []),
        area_m2=pd.get("area_m2"),
        specified_area_m2=pd.get("specified_area_m2"),
        declared_area_m2=pd.get("declared_area_m2"),
        cadastral_cost_rub=pd.get("cadastral_cost_rub"),
        cost_index_rub_m2=pd.get("cost_index_rub_m2"),
        ownership_type=pd.get("ownership_type"),
        land_record_type=pd.get("land_record_type"),
        land_record_subtype=pd.get("land_record_subtype"),
        reg_date=datetime.fromisoformat(pd["reg_date"]) if pd.get("reg_date") else None,
        has_borders=bool(pd.get("has_borders")),
        geometry=pd.get("geometry"),
        zouit=zones(pd.get("zouit") or []),
        zouit_checked=bool(pd.get("zouit_checked")),
        territorial_zones=zones(pd.get("territorial_zones") or []),
        territorial_zone_checked=bool(pd.get("territorial_zone_checked")),
        raw={"errors": pd.get("errors") or []},
    )
    duplicates = [
        Listing(
            cad_num=ld["cad_num"],
            price_rub=x.get("price_rub"),
            declared_area_m2=x.get("declared_area_m2"),
            url=x.get("url"),
        )
        for x in d.get("duplicates") or []
    ]
    res = CheckResult(
        listing=listing,
        parcel=parcel,
        flags=[flag_obj(f) for f in d.get("flags", [])],
        duplicates=duplicates,
    )
    res.score = float(d.get("score") or 0.0)
    return res
