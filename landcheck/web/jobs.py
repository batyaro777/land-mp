"""Фоновое выполнение проверок: пул потоков в процессе, прогресс в БД."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from landcheck.cache import ParcelCache
from landcheck.inputs import dedupe, load_listings
from landcheck.models import BatchResult
from landcheck.pipeline import check_batch
from landcheck.providers.base import ParcelProvider
from landcheck.providers.fixtures import FixtureProvider
from landcheck.web.config import settings
from landcheck.web.db import Check, CheckItem, session
from landcheck.web.serialize import request_from_dict, result_to_dict

log = logging.getLogger("landcheck.web.jobs")
_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def build_provider() -> ParcelProvider:
    if settings.fixtures is not None:
        return FixtureProvider(settings.fixtures)
    from landcheck.providers.nspd import LiveNspdProvider

    return LiveNspdProvider(with_zones=True, proxy=settings.proxy, timeout=settings.nspd_timeout)


def executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="landcheck-job")
        return _executor


def enqueue(check_id: int) -> None:
    executor().submit(run_check, check_id)


def _set(check_id: int, **fields) -> None:
    with session() as db:
        check = db.get(Check, check_id)
        if check is None:
            return
        for k, v in fields.items():
            setattr(check, k, v)
        db.commit()


def stats_of(batch: BatchResult) -> dict:
    return {
        "counts": batch.counts,
        "median_price_per_sotka": batch.median_price_per_sotka,
        "seconds": round((batch.finished_at - batch.started_at).total_seconds(), 1),
        "errors": batch.errors[:20],
        "purpose": batch.request.purpose.label,
    }


def _history_for(db, check: Check, cad_num: str, verdict: str, had_history: bool) -> dict:
    """М1.7: новый ли номер для брокера и изменился ли вердикт с прошлой проверки."""
    prev = db.execute(
        select(CheckItem.verdict, Check.id, Check.created_at, Check.title)
        .join(Check, CheckItem.check_id == Check.id)
        .where(
            Check.user_id == check.user_id,
            Check.id != check.id,
            Check.status == "done",
            CheckItem.cad_num == cad_num,
        )
        .order_by(Check.created_at.desc())
        .limit(1)
    ).first()
    if prev is None:
        return {"seen_before": False, "is_new": had_history}
    prev_verdict, prev_id, prev_at, prev_title = prev
    return {
        "seen_before": True,
        "is_new": False,
        "prev_check_id": prev_id,
        "prev_title": prev_title,
        "prev_at": prev_at.isoformat() if prev_at else None,
        "prev_verdict": prev_verdict,
        "changed": prev_verdict != verdict,
    }


def resume_pending() -> int:
    """После рестарта процесса дочитать незавершённые проверки: работа шла только в памяти."""
    with session() as db:
        ids = list(
            db.scalars(select(Check.id).where(Check.status.in_(["queued", "running"]))).all()
        )
    for cid in ids:
        enqueue(cid)
    return len(ids)


def run_check(check_id: int) -> None:
    with session() as db:
        check = db.get(Check, check_id)
        if check is None:
            return
        input_text, request_d = check.input_text, dict(check.request)
    try:
        listings = load_listings(input_text)
        if not listings:
            _set(
                check_id, status="error", error="Во входе не найдено ни одного кадастрового номера"
            )
            return
        groups = dedupe(listings)
        _set(check_id, status="running", total=len(groups), done=0)
        request = request_from_dict(request_d)
        provider = build_provider()
        # В демо-режиме на фикстурах кэш НСПД не трогаем: demo и live не смешиваются.
        cache = (
            None
            if settings.fixtures is not None
            else ParcelCache(settings.cache_path, ttl_days=settings.cache_ttl_days)
        )
        refresh = bool(request_d.pop("refresh", False))

        def progress(done: int, total: int, _cn: str) -> None:
            _set(check_id, done=done, total=total)

        try:
            batch = check_batch(
                listings,
                request,
                provider,
                cache,
                refresh=refresh,
                workers=settings.workers,
                progress=progress,
            )
        finally:
            if cache is not None:
                cache.close()
            close = getattr(provider, "close", None)
            if close:
                close()
        with session() as db:
            check = db.get(Check, check_id)
            assert check is not None
            db.query(CheckItem).filter(CheckItem.check_id == check_id).delete()
            had_history = bool(
                db.scalar(
                    select(Check.id).where(
                        Check.user_id == check.user_id,
                        Check.id != check_id,
                        Check.status == "done",
                    )
                )
            )
            for pos, r in enumerate(batch.results, start=1):
                data = result_to_dict(r)
                data["history"] = _history_for(
                    db, check, r.listing.cad_num, r.verdict.value, had_history
                )
                db.add(
                    CheckItem(
                        check_id=check_id,
                        position=pos,
                        cad_num=r.listing.cad_num,
                        verdict=r.verdict.value,
                        score=r.score,
                        data=data,
                    )
                )
            check.request = {k: v for k, v in dict(check.request).items() if k != "refresh"}
            check.status = "done"
            check.done = check.total = len(batch.results)
            check.stats = stats_of(batch)
            check.finished_at = datetime.now(UTC)
            db.commit()
    except Exception as e:  # noqa: BLE001 — любая ошибка должна попасть в карточку проверки
        log.exception("check %s failed", check_id)
        _set(check_id, status="error", error=f"{type(e).__name__}: {e}")


# --- модуль «Локация»: окружение по запросу из отчёта -----------------------------------
def fetch_location(
    cad_num: str,
    parcel_geojson: dict[str, Any] | None,
    *,
    refresh: bool = False,
    with_osm: bool = True,
) -> dict[str, Any]:
    """Сырой ответ окружения: кэш → провайдер → OSM. Ошибки внутри, исключений наружу нет."""
    from landcheck.location import empty_location_raw, slim_raw

    cache = ParcelCache(settings.cache_path, ttl_days=settings.cache_ttl_days)
    provider = build_provider()
    try:
        if not refresh:
            cached = cache.get_location(cad_num)
            if cached is not None:
                # НСПД-часть берём из кэша, а OSM, если тогда отказали зеркала, пробуем ещё раз.
                if with_osm and _osm_failed(cached) and parcel_geojson is not None:
                    cached["osm"] = _fetch_osm_safe(parcel_geojson)
                    if not _osm_failed(cached):
                        cache.put_location(cad_num, slim_raw(cached, parcel_geojson))
                return cached
        if parcel_geojson is None:
            raw = empty_location_raw(cad_num)
            raw["nspd"]["errors"].append("у участка нет контура, окружение не проверить")
            return raw
        fetch = getattr(provider, "fetch_location_raw", None)
        if fetch is None:
            raw = empty_location_raw(cad_num)
            raw["nspd"]["errors"].append("провайдер не умеет окружение")
            return raw
        raw = fetch(cad_num, parcel_geojson)
        if with_osm and raw.get("osm") is None and provider.name != "fixture":
            raw["osm"] = _fetch_osm_safe(parcel_geojson)
        if provider.name != "fixture" and not raw["nspd"].get("errors"):
            cache.put_location(cad_num, slim_raw(raw, parcel_geojson))
        return raw
    finally:
        cache.close()
        close = getattr(provider, "close", None)
        if close:
            close()


def _osm_failed(raw: dict[str, Any]) -> bool:
    osm = raw.get("osm")
    return osm is None or bool(osm.get("error"))


def _fetch_osm_safe(parcel_geojson: dict[str, Any]) -> dict[str, Any]:
    from landcheck.location import OSM_RADIUS_M, centroid
    from landcheck.osm import fetch_osm

    try:
        lat, lon = centroid(parcel_geojson)
        return fetch_osm(lat, lon, OSM_RADIUS_M)
    except Exception as e:  # noqa: BLE001
        return {"elements": [], "error": f"{type(e).__name__}: {e}"}


# --- модуль «Регион»: один запрос на локацию и цель, кэш 30 дней ------------------------
def fetch_region(
    locality: str, purpose, *, refresh: bool = False, cad_num: str | None = None
) -> dict[str, Any]:
    from landcheck.models import Purpose
    from landcheck.region import locality_key, research_region

    if not settings.region_enabled:
        return {
            "sections": [],
            "sources": [],
            "grounded": False,
            "error": "модуль выключен: нет ключа ни одного провайдера LLM "
            "(бесплатно: GROQ_API_KEY) и нет ключа Gemini",
            "model": None,
            "fetched_at": datetime.now(UTC).isoformat(),
        }
    purpose = purpose if isinstance(purpose, Purpose) else Purpose(purpose)
    key = f"{locality_key(locality)}|{purpose.value}"
    cache = ParcelCache(settings.cache_path, ttl_days=settings.cache_ttl_days)
    try:
        if not refresh:
            cached = cache.get_region(key)
            if cached is not None and not cached.get("error"):
                return cached
        result = research_region(
            locality,
            purpose,
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            cad_num=cad_num,
        )
        if not result.get("error"):
            cache.put_region(key, result)
        return result
    finally:
        cache.close()
