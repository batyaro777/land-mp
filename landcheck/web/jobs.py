"""Фоновое выполнение проверок: пул потоков в процессе, прогресс в БД."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

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
        cache = ParcelCache(settings.cache_path, ttl_days=settings.cache_ttl_days)

        def progress(done: int, total: int, _cn: str) -> None:
            _set(check_id, done=done, total=total)

        try:
            batch = check_batch(
                listings, request, provider, cache, workers=settings.workers, progress=progress
            )
        finally:
            cache.close()
            close = getattr(provider, "close", None)
            if close:
                close()
        with session() as db:
            check = db.get(Check, check_id)
            assert check is not None
            db.query(CheckItem).filter(CheckItem.check_id == check_id).delete()
            for pos, r in enumerate(batch.results, start=1):
                db.add(
                    CheckItem(
                        check_id=check_id,
                        position=pos,
                        cad_num=r.listing.cad_num,
                        verdict=r.verdict.value,
                        score=r.score,
                        data=result_to_dict(r),
                    )
                )
            check.status = "done"
            check.done = check.total = len(batch.results)
            check.stats = stats_of(batch)
            check.finished_at = datetime.now(UTC)
            db.commit()
    except Exception as e:  # noqa: BLE001 — любая ошибка должна попасть в карточку проверки
        log.exception("check %s failed", check_id)
        _set(check_id, status="error", error=f"{type(e).__name__}: {e}")
