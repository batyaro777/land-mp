"""API v1 (R10): для юристов, инженеров и интеграций. Ключ выпускается в профиле, передаётся
заголовком Authorization: Bearer <ключ> (или X-Api-Key). Проверка ставится в ту же очередь,
что и подборки из кабинета; результат — JSON, CSV или XLSX."""

from __future__ import annotations

import io
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from landcheck.inputs import load_listings
from landcheck.models import SOTKA_M2, Purpose
from landcheck.models import Request as CheckRequest
from landcheck.web import jobs
from landcheck.web.config import settings
from landcheck.web.db import Check, CheckItem, User, session
from landcheck.web.serialize import request_to_dict

router = APIRouter(prefix="/api/v1", tags=["api"])


def api_user(request: Request) -> User:
    auth = request.headers.get("authorization") or ""
    key = (
        auth[7:].strip()
        if auth.lower().startswith("bearer ")
        else request.headers.get("x-api-key", "")
    )
    if not key:
        raise HTTPException(
            401, "нужен ключ API: заголовок Authorization: Bearer <ключ> (выпускается в профиле)"
        )
    with session() as db:
        user = db.scalar(select(User).where(User.api_key == key))
    if user is None:
        raise HTTPException(401, "ключ API не найден или отозван")
    return user


ApiUser = Annotated[User, Depends(api_user)]


class CheckIn(BaseModel):
    numbers: list[str] | None = Field(default=None, description="кадастровые номера")
    text: str | None = Field(default=None, description="свободный текст: номера, цены, площади")
    purpose: str = Field(default="any", description="цель: " + ", ".join(p.value for p in Purpose))
    budget_rub: float | None = None
    min_area_sotka: float | None = None
    max_area_sotka: float | None = None
    title: str | None = None


def _base() -> str:
    return settings.base_url.rstrip("/")


def _zones(zs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {"kind": z.get("kind"), "name": z.get("name"), "reg_number": z.get("reg_number")}
        for z in zs or []
    ]


def item_json(check: Check, it: CheckItem) -> dict[str, Any]:
    d = it.data or {}
    p = d.get("parcel") or {}
    return {
        "cad_num": it.cad_num,
        "verdict": it.verdict,
        "score": it.score,
        "price_per_sotka": d.get("price_per_sotka"),
        "flags": [
            {
                "rule_id": f.get("rule_id"),
                "level": f.get("level"),
                "message": f.get("message"),
                "source": f.get("source"),
            }
            for f in d.get("flags") or []
        ],
        "parcel": {
            "found": p.get("found"),
            "address": p.get("address"),
            "status": p.get("status"),
            "category": p.get("category"),
            "permitted_use": p.get("permitted_use"),
            "area_m2": p.get("specified_area_m2") or p.get("area_m2") or p.get("declared_area_m2"),
            "has_borders": p.get("has_borders"),
            "cadastral_cost_rub": p.get("cadastral_cost_rub"),
            "ownership_type": p.get("ownership_type"),
            "zouit": _zones(p.get("zouit")),
            "territorial_zones": _zones(p.get("territorial_zones")),
            "fetched_at": p.get("fetched_at"),
        },
        "rights": (
            {
                k: (d.get("rights") or {}).get(k)
                for k in ("status", "level", "headline", "source", "as_of", "restrictions")
            }
            if d.get("rights")
            else None
        ),
        "report_url": f"{_base()}/checks/{check.id}/items/{it.cad_num}",
        "public_url": f"{_base()}/r/{it.token}" if it.token else None,
    }


def check_json(check: Check, *, with_items: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": check.id,
        "title": check.title,
        "status": check.status,
        "done": check.done,
        "total": check.total,
        "error": check.error,
        "created_at": check.created_at.isoformat() if check.created_at else None,
        "finished_at": check.finished_at.isoformat() if check.finished_at else None,
        "stats": check.stats or {},
        "url": f"{_base()}/api/v1/checks/{check.id}",
        "export": {
            "csv": f"{_base()}/api/v1/checks/{check.id}/export.csv",
            "xlsx": f"{_base()}/api/v1/checks/{check.id}/export.xlsx",
        },
    }
    if with_items and check.status == "done":
        out["items"] = [
            item_json(check, it) for it in sorted(check.items, key=lambda i: i.position)
        ]
    return out


@router.get("")
def api_index() -> dict[str, Any]:
    return {
        "service": "Land-MP API",
        "version": "v1",
        "auth": "Authorization: Bearer <ключ из профиля>",
        "endpoints": {
            "POST /api/v1/checks": (
                "проверка: {numbers: [...], purpose, budget_rub, min_area_sotka, max_area_sotka, "
                "title} или {text: '...'} → 202 {id, status}"
            ),
            "GET /api/v1/checks": "мои проверки (последние 50)",
            "GET /api/v1/checks/{id}": "статус и результаты (items — когда status=done)",
            "GET /api/v1/checks/{id}/export.csv|xlsx": "выгрузка таблицы",
            "GET /api/v1/parcels/{cad_num}": "последний результат по участку из моих проверок",
        },
        "purposes": [p.value for p in Purpose],
        "limits": {"max_items_per_check": settings.max_items},
    }


@router.post("/checks", status_code=202)
def api_create_check(user: ApiUser, body: CheckIn) -> dict[str, Any]:
    input_text = (body.text or "").strip() or "\n".join(
        n.strip() for n in body.numbers or [] if n.strip()
    )
    listings = load_listings(input_text)
    if not listings:
        raise HTTPException(400, "в запросе нет кадастровых номеров")
    if len(listings) > settings.max_items:
        raise HTTPException(400, f"не больше {settings.max_items} участков за одну проверку")
    req = CheckRequest()
    try:
        req.purpose = Purpose(body.purpose)
    except ValueError as exc:
        raise HTTPException(400, f"неизвестная цель «{body.purpose}»") from exc
    req.budget_rub = body.budget_rub
    req.min_area_m2 = body.min_area_sotka * SOTKA_M2 if body.min_area_sotka else None
    req.max_area_m2 = body.max_area_sotka * SOTKA_M2 if body.max_area_sotka else None
    title = (body.title or "").strip()[
        :200
    ] or f"API {datetime.now(UTC):%d.%m %H:%M} ({len(listings)})"
    with session() as db:
        check = Check(
            user_id=user.id,
            title=title,
            request={**request_to_dict(req), "api": True},
            input_text=input_text,
            total=len(listings),
        )
        db.add(check)
        db.commit()
        check_id = check.id
    jobs.enqueue(check_id)
    return {"id": check_id, "status": "queued", "url": f"{_base()}/api/v1/checks/{check_id}"}


def _own(db, user: User, check_id: int) -> Check:
    check = db.get(Check, check_id)
    if check is None or check.user_id != user.id:
        raise HTTPException(404, "проверка не найдена")
    return check


@router.get("/checks")
def api_list_checks(user: ApiUser) -> dict[str, Any]:
    with session() as db:
        rows = db.scalars(
            select(Check).where(Check.user_id == user.id).order_by(Check.id.desc()).limit(50)
        ).all()
        return {"checks": [check_json(c, with_items=False) for c in rows]}


@router.get("/checks/{check_id}")
def api_get_check(user: ApiUser, check_id: int) -> dict[str, Any]:
    with session() as db:
        check = _own(db, user, check_id)
        return check_json(check, with_items=True)


@router.get("/checks/{check_id}/export.{fmt}")
def api_export(user: ApiUser, check_id: int, fmt: str):
    from landcheck.export import to_csv, to_xlsx
    from landcheck.web.app import _batch_from_check

    with session() as db:
        check = _own(db, user, check_id)
        if check.status != "done":
            raise HTTPException(409, f"проверка ещё не завершена: {check.status}")
        batch = _batch_from_check(check)
        title = check.title
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title)[:60].strip() or "check"
    if fmt == "csv":
        return Response(
            to_csv(batch).encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe)}.csv"},
        )
    if fmt == "xlsx":
        buf = io.BytesIO()
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=True) as tmp:
            to_xlsx(batch, tmp.name)
            buf.write(Path(tmp.name).read_bytes())
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe)}.xlsx"},
        )
    raise HTTPException(404, "формат: csv или xlsx")


@router.get("/parcels/{cad_num}")
def api_parcel(user: ApiUser, cad_num: str) -> dict[str, Any]:
    from landcheck.web.watch import latest_item

    with session() as db:
        it = latest_item(db, user.id, cad_num)
        if it is None:
            raise HTTPException(404, "участок не проверялся: POST /api/v1/checks")
        return {"check_id": it.check_id, **item_json(it.check, it)}
