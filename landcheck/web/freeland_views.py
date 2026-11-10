"""Страницы «Свободная земля на территории» (ТЗ v3, п. 8, этапы 3–4; внутренний режим).

/tools/free-land        — новый поиск (квартал или окрестность участка) и прошлые поиски;
/tools/free-land/{id}   — результат: публичные участки без видимых препятствий, отметки
                          «на аукционе», «в проекте межевания», «был на торгах», карточки
                          «как оформить», выгрузка и полная проверка отмеченных.
Поиск идёт фоном: по кварталу это десятки запросов к НСПД.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from landcheck import freeland, torgi, torgi_scan
from landcheck.inputs import normalize_cad
from landcheck.models import Purpose
from landcheck.models import Request as CheckRequest
from landcheck.web import jobs
from landcheck.web.app import render
from landcheck.web.config import settings
from landcheck.web.db import Check, LandScan, TorgiLot, session
from landcheck.web.serialize import request_to_dict
from landcheck.web.torgi_views import GROUP_USE, TorgiDep

log = logging.getLogger("landcheck.web.freeland")
router = APIRouter()
MAX_CHECK_ITEMS = 30
STALE_MIN = 30  # «ищем» дольше — считаем, что процесс перезапускался
_running: set[int] = set()
_lock = threading.Lock()


def parse_scope(text: str) -> tuple[str, str] | None:
    """«40:10:160500» → ("quarter", номер); «40:10:160500:1» → ("around", номер участка)."""
    cad = normalize_cad(text)
    if cad:
        return "around", cad
    parts = [p for p in (text or "").strip().replace(" ", "").split(":") if p]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return "quarter", f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2]}"
    return None


def _provider():
    """Живой НСПД; в демо-режиме на фикстурах поиска по территории нет."""
    if settings.fixtures is not None:
        return None
    from landcheck.providers.nspd import LiveNspdProvider

    return LiveNspdProvider(with_zones=False, proxy=settings.proxy, timeout=settings.nspd_timeout)


def _center_of(cad_num: str, provider) -> tuple[float, float] | None:
    """Центр участка: из кэша, иначе запрос НСПД."""
    from landcheck.cache import ParcelCache
    from landcheck.location import centroid
    from landcheck.normalize import parcel_from_payload

    cache = ParcelCache(settings.cache_path, ttl_days=settings.cache_ttl_days)
    try:
        payload = cache.get(cad_num, allow_stale=True)
    finally:
        cache.close()
    if payload is None or not payload.get("found"):
        payload = provider.fetch_raw(cad_num)
    parcel = parcel_from_payload(payload)
    if not parcel.found or not parcel.geometry:
        return None
    return centroid(parcel.geometry)


def run_scan(scan_id: int, provider=None) -> None:
    """Фоновая задача: территория → участки НСПД → отбор. Ошибки — в запись поиска."""
    with session() as db:
        scan = db.get(LandScan, scan_id)
        if scan is None:
            return
        kind, scope, radius = scan.kind, scan.scope, scan.radius_m or 1000
        scan.state = "running"
        db.commit()
    error: str | None = None
    result: dict[str, Any] = {}
    try:
        provider = provider or _provider()
        if provider is None:
            raise RuntimeError("в демо-режиме поиск по территории недоступен: нужен живой НСПД")
        if kind == "quarter":
            raw = provider.fetch_area_raw(quarter=scope)
        else:
            center = _center_of(scope, provider)
            if center is None:
                raise RuntimeError("участок не найден в НСПД или у него нет границ")
            raw = provider.fetch_area_raw(center=center, radius_m=radius)
        if raw.get("errors") and not raw.get("landplots"):
            raise RuntimeError("; ".join(raw["errors"])[:300])
        result = freeland.analyze(
            raw.get("landplots") or [],
            raw.get("auction"),
            raw.get("pmt"),
            quarter=scope if kind == "quarter" else None,
        )
        result["errors"] = raw.get("errors") or []
        result["layers_truncated"] = raw.get("truncated") or []
        result["center"] = raw.get("center")
    except Exception as exc:  # noqa: BLE001 — фон: ошибка в запись, не наружу
        log.warning("free land %s %s: %s", kind, scope, exc)
        error = str(exc)[:400] or type(exc).__name__
    with session() as db:
        scan = db.get(LandScan, scan_id)
        if scan is not None:
            scan.state = "error" if error else "done"
            scan.error = error
            scan.result = result
            scan.finished_at = datetime.now(UTC)
            db.commit()
    with _lock:
        _running.discard(scan_id)


def start_scan(scan_id: int) -> None:
    with _lock:
        if scan_id in _running:
            return
        _running.add(scan_id)
    jobs.executor().submit(run_scan, scan_id)


def _own_scan(db, user, scan_id: int) -> LandScan:
    scan = db.get(LandScan, scan_id)
    if scan is None or (scan.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    return scan


def _state(scan: LandScan) -> str:
    """Состояние для страницы: «ищем» без живой задачи дольше получаса — прервано."""
    if scan.state in ("queued", "running") and scan.id not in _running:
        created = scan.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if (datetime.now(UTC) - created).total_seconds() > STALE_MIN * 60:
            return "interrupted"
    return scan.state


def _with_torgi(db, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Добавить к участкам последний лот из базы торгов: «сдан с торгов», «идут торги»."""
    cads = [i["cad_num"] for i in items]
    if not cads:
        return items
    lots: dict[str, dict[str, Any]] = {}
    for start in range(0, len(cads), 400):
        chunk = cads[start : start + 400]
        for lot in db.scalars(select(TorgiLot).where(TorgiLot.cad_num.in_(chunk))):
            d = lot.to_dict()
            prev = lots.get(lot.cad_num)
            if prev is None or (d.get("published") or "") > (prev.get("published") or ""):
                lots[lot.cad_num] = d
    return [
        {**i, "torgi": freeland.torgi_note(lots[i["cad_num"]]) if i["cad_num"] in lots else None}
        for i in items
    ]


def _num(value: str | None) -> float | None:
    try:
        v = float((value or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def _filtered(db, scan: LandScan, q) -> list[dict[str, Any]]:
    items = _with_torgi(db, (scan.result or {}).get("items") or [])
    amin, amax = _num(q.get("amin")), _num(q.get("amax"))
    return freeland.filter_items(
        items,
        how=q.get("how") if q.get("how") in freeland.HOWTO else None,
        group=q.get("group") if q.get("group") in torgi_scan.GROUPS else None,
        area_min_m2=amin * 100 if amin else None,
        area_max_m2=amax * 100 if amax else None,
        hide_busy=q.get("free") == "1",
        own=q.get("own") if q.get("own") in freeland.OWN_KIND_LABEL else None,
    )


@router.get("/tools/free-land", response_class=HTMLResponse)
def free_land_page(request: Request, user: TorgiDep, scope: str = "", error: str = ""):
    with session() as db:
        q = select(LandScan).order_by(LandScan.id.desc()).limit(30)
        if not user.is_admin:
            q = q.where(LandScan.user_id == user.id)
        scans = [
            {
                "id": s.id,
                "scope": s.scope,
                "kind": s.kind,
                "radius_m": s.radius_m,
                "state": _state(s),
                "created_at": s.created_at,
                "found": ((s.result or {}).get("counts") or {}).get("public_free"),
                "total": ((s.result or {}).get("counts") or {}).get("total"),
            }
            for s in db.scalars(q)
        ]
    return render(
        request,
        "free_land.html",
        scans=scans,
        scope=scope,
        error=error,
        radii=freeland.RADII_M,
        demo=settings.fixtures is not None,
        user=user,
    )


@router.post("/tools/free-land")
def free_land_start(
    user: TorgiDep,
    scope: Annotated[str, Form()] = "",
    radius: Annotated[str, Form()] = "1000",
):
    parsed = parse_scope(scope)
    if parsed is None:
        return RedirectResponse("/tools/free-land?error=scope", status_code=303)
    kind, value = parsed
    radius_m = int(radius) if radius.isdigit() and int(radius) in freeland.RADII_M else 1000
    with session() as db:
        scan = LandScan(
            user_id=user.id,
            scope=value,
            kind=kind,
            radius_m=radius_m if kind == "around" else None,
            state="queued",
            result={},
        )
        db.add(scan)
        db.commit()
        scan_id = scan.id
    start_scan(scan_id)
    return RedirectResponse(f"/tools/free-land/{scan_id}", status_code=303)


@router.get("/tools/free-land/{scan_id}", response_class=HTMLResponse)
def free_land_result(request: Request, user: TorgiDep, scan_id: int):
    q = request.query_params
    with session() as db:
        scan = _own_scan(db, user, scan_id)
        state = _state(scan)
        result = scan.result or {}
        items = _filtered(db, scan, q) if state == "done" else []
        ctx = {
            "scan": {
                "id": scan.id,
                "scope": scan.scope,
                "kind": scan.kind,
                "radius_m": scan.radius_m,
                "created_at": scan.created_at,
                "finished_at": scan.finished_at,
                "error": scan.error,
            },
        }
    hows = sorted({i["how"] for i in (result.get("items") or [])})
    points = [
        {
            "cad": i["cad_num"],
            "lat": i["point"][0],
            "lon": i["point"][1],
            "busy": bool(
                i.get("on_auction") or i.get("in_pmt") or (i.get("torgi") or {}).get("busy")
            ),
        }
        for i in items
        if i.get("point")
    ]
    return render(
        request,
        "free_land_result.html",
        state=state,
        result=result,
        items=items,
        points=points,
        hows=hows,
        howto=freeland.HOWTO,
        howto_label=freeland.HOWTO_LABEL,
        own_label=freeland.OWN_KIND_LABEL,
        own_kinds=sorted({freeland.own_kind(i) for i in (result.get("items") or [])}),
        groups=torgi_scan.GROUPS,
        q=q,
        qs=str(q),
        user=user,
        lot_url=torgi.LOT_URL,
        **ctx,
    )


@router.post("/tools/free-land/{scan_id}/retry")
def free_land_retry(user: TorgiDep, scan_id: int):
    with session() as db:
        scan = _own_scan(db, user, scan_id)
        scan.state, scan.error, scan.created_at = "queued", None, datetime.now(UTC)
        db.commit()
    start_scan(scan_id)
    return RedirectResponse(f"/tools/free-land/{scan_id}", status_code=303)


@router.post("/tools/free-land/{scan_id}/delete")
def free_land_delete(user: TorgiDep, scan_id: int):
    with session() as db:
        scan = _own_scan(db, user, scan_id)
        db.delete(scan)
        db.commit()
    return RedirectResponse("/tools/free-land", status_code=303)


@router.get("/tools/free-land/{scan_id}/export.xlsx")
def free_land_export(request: Request, user: TorgiDep, scan_id: int):
    with session() as db:
        scan = _own_scan(db, user, scan_id)
        if scan.state != "done":
            raise HTTPException(404)
        items = _filtered(db, scan, request.query_params)
        scope = scan.scope
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "free-land.xlsx"
        freeland.to_xlsx(items, path)
        body = path.read_bytes()
    name = f"free-land-{scope.replace(':', '-')}.xlsx"
    return Response(
        body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/tools/free-land/{scan_id}/check")
async def free_land_check(request: Request, user: TorgiDep, scan_id: int):
    """Полная проверка отмеченных участков: подборка с целью «получить у муниципалитета»."""
    form = await request.form()
    chosen = [str(v) for v in form.getlist("cad")][:MAX_CHECK_ITEMS]
    with session() as db:
        scan = _own_scan(db, user, scan_id)
        known = {i["cad_num"]: i for i in (scan.result or {}).get("items") or []}
        picked = [known[c] for c in chosen if c in known]
        if not picked:
            raise HTTPException(400, "не выбрано ни одного участка")
        req = CheckRequest(purpose=Purpose.MUNICIPAL)
        groups = {i.get("group") for i in picked}
        use = GROUP_USE.get(next(iter(groups)) or "") if len(groups) == 1 else None
        if use:
            req.use = use
        check = Check(
            user_id=user.id,
            title=f"Свободная земля {scan.scope}: {len(picked)} уч. · {datetime.now(UTC):%d.%m.%Y}",
            request=request_to_dict(req),
            input_text="\n".join(i["cad_num"] for i in picked),
            total=len(picked),
        )
        db.add(check)
        db.commit()
        check_id = check.id
    jobs.enqueue(check_id)
    return RedirectResponse(f"/checks/{check_id}", status_code=303)
