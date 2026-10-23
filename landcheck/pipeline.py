"""Пакетная проверка: ввод → кэш/НСПД → нормализация → правила → сортировка."""

from __future__ import annotations

import logging
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from landcheck.cache import ParcelCache
from landcheck.inputs import dedupe, is_valid_cad_num
from landcheck.models import (
    SOTKA_M2,
    BatchResult,
    CheckResult,
    Flag,
    Level,
    Listing,
    Parcel,
    Request,
)
from landcheck.normalize import parcel_from_payload
from landcheck.providers.base import ParcelProvider, empty_payload
from landcheck.rules import RuleContext, RulesConfig, evaluate
from landcheck.rules.engine import BatchStats
from landcheck.scoring import score, sort_key

Progress = Callable[[int, int, str], None]


log = logging.getLogger(__name__)


def fetch_payload(
    cad_num: str,
    provider: ParcelProvider,
    cache: ParcelCache | None,
    *,
    refresh: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Возвращает (payload, ошибки получения). При сбое отдаёт устаревший кэш, если есть."""
    errors: list[str] = []
    live = getattr(provider, "name", "unknown") != "fixture"
    if cache is not None and not refresh:
        cached = cache.get(cad_num)
        # Синтетический ответ фикстур никогда не выдаём за данные НСПД.
        if cached is not None and not (live and cached.get("provider") == "fixture"):
            return cached, errors
    try:
        payload = provider.fetch_raw(cad_num)
    except Exception as e:  # noqa: BLE001 — сеть; деградируем в кэш
        errors.append(f"{type(e).__name__}: {e}")
        log.warning("nspd %s: %s", cad_num, errors[-1])
        payload = None
    if (
        payload is not None
        and not payload.get("errors")
        and cache is not None
        and payload.get("provider") != "fixture"
    ):
        cache.put(cad_num, payload)
    if payload is not None and payload.get("errors"):
        errors.extend(payload["errors"])
    if payload is None or (not payload.get("found") and errors):
        stale = cache.get(cad_num, allow_stale=True) if cache is not None else None
        if stale is not None and live and stale.get("provider") == "fixture":
            stale = None
        if stale is not None:
            stale.setdefault("errors", []).extend(errors)
            stale["from_stale_cache"] = True
            log.warning(
                "nspd %s: показан кэш от %s, причина: %s",
                cad_num,
                stale.get("fetched_at"),
                "; ".join(errors)[:200],
            )
            return stale, errors
    if payload is None:
        payload = empty_payload(cad_num, getattr(provider, "name", "unknown"))
        payload["errors"] = errors
    return payload, errors


def _median_price_per_sotka(items: list[tuple[Listing, Parcel]]) -> tuple[float | None, int]:
    values: list[float] = []
    for lst, parcel in items:
        area = parcel.best_area_m2 or lst.declared_area_m2
        if lst.price_rub and area:
            values.append(lst.price_rub / (area / SOTKA_M2))
    if not values:
        return None, 0
    return statistics.median(values), len(values)


def check_batch(
    listings: list[Listing],
    request: Request,
    provider: ParcelProvider,
    cache: ParcelCache | None = None,
    *,
    refresh: bool = False,
    workers: int = 1,
    config: RulesConfig | None = None,
    progress: Progress | None = None,
) -> BatchResult:
    started = datetime.now(UTC)
    config = config or RulesConfig()
    groups = dedupe(listings)
    total = len(groups)

    fetched: dict[str, tuple[dict[str, Any], list[str]]] = {}
    to_fetch = [primary.cad_num for primary, _ in groups if is_valid_cad_num(primary.cad_num)]

    def _one(cn: str) -> tuple[str, tuple[dict[str, Any], list[str]]]:
        return cn, fetch_payload(cn, provider, cache, refresh=refresh)

    done = 0
    if workers > 1 and len(to_fetch) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for cn, res in pool.map(_one, to_fetch):
                fetched[cn] = res
                done += 1
                if progress:
                    progress(done, total, cn)
    else:
        for cn in to_fetch:
            fetched[cn] = fetch_payload(cn, provider, cache, refresh=refresh)
            done += 1
            if progress:
                progress(done, total, cn)

    pairs: list[tuple[Listing, Parcel, list[Listing], list[str]]] = []
    for primary, dups in groups:
        if primary.cad_num in fetched:
            payload, errs = fetched[primary.cad_num]
            parcel = parcel_from_payload(payload)
        else:
            parcel = Parcel(cad_num=primary.cad_num, found=False, fetched_at=started)
            errs = []
        pairs.append((primary, parcel, dups, errs))

    median, priced = _median_price_per_sotka([(lst, p) for lst, p, _, _ in pairs])
    stats = BatchStats(median_price_per_sotka=median, priced_count=priced)

    results: list[CheckResult] = []
    for lst, parcel, dups, errs in pairs:
        if not is_valid_cad_num(lst.cad_num):
            flags = [
                Flag(
                    "R01",
                    Level.STOP,
                    f"«{lst.raw_cad_num or lst.cad_num}» не похоже на кадастровый номер "
                    "(формат XX:XX:XXXXXXX:XX)",
                    "формат кадастрового номера",
                )
            ]
        else:
            ctx = RuleContext(
                listing=lst,
                parcel=parcel,
                request=request,
                stats=stats,
                config=config,
                duplicates=dups,
                fetch_errors=errs,
            )
            flags = evaluate(ctx)
        res = CheckResult(listing=lst, parcel=parcel, flags=flags, duplicates=dups)
        res.score = score(res, request)
        results.append(res)

    results.sort(key=sort_key)
    all_errors = sorted({e for _, _, _, errs in pairs for e in errs})
    return BatchResult(
        request=request,
        results=results,
        started_at=started,
        finished_at=datetime.now(UTC),
        median_price_per_sotka=median,
        errors=all_errors,
    )
