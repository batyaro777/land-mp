"""Границы кадастровых районов и кварталов для карты ликвидности.

Чат «МП Земля», 17.11.2026, №75 (эксперт по торгам): «карту кварталов сделать интерактивную;
где мне брать кварталы, если я не умею пользоваться кадастровой картой». Контур берём из НСПД
по номеру района или квартала один раз и храним в базе упрощённым (~20 м): страница карты
НСПД не ждёт, недостающие границы догружаются фоном одним потоком.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from landcheck.web.config import settings
from landcheck.web.db import TorgiPlace, session

log = logging.getLogger(__name__)

RETRY_AFTER = timedelta(days=1)  # не нашли или НСПД не ответил — повтор не раньше суток
PAUSE_S = 1.0
MAX_PER_RUN = 400
SIMPLIFY_DEG = 0.0002  # ~20 м: границу квартала на карте региона видно, а объём в разы меньше

_lock = threading.Lock()
_running = False
_queue: dict[str, None] = {}  # районы и кварталы в очереди, по порядку постановки


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _uniq(keys: Iterable[str | None]) -> list[str]:
    return list(dict.fromkeys(k for k in keys if k))


def shapes(keys: Iterable[str | None]) -> dict[str, dict[str, Any]]:
    """Что уже есть в базе по этим районам и кварталам."""
    todo = _uniq(keys)
    out: dict[str, dict[str, Any]] = {}
    with session() as db:
        for i in range(0, len(todo), 500):
            part = todo[i : i + 500]
            for row in db.scalars(select(TorgiPlace).where(TorgiPlace.key.in_(part))):
                out[row.key] = {
                    "geometry": row.geometry,
                    "lat": row.lat,
                    "lon": row.lon,
                    "error": row.error,
                    "fetched_at": _aware(row.fetched_at),
                }
    return out


def missing(keys: Iterable[str | None], now: datetime | None = None) -> list[str]:
    """Районы и кварталы без контура: ещё не спрашивали либо прошлый запрос не удался сутки
    назад и раньше."""
    now = now or datetime.now(UTC)
    todo = _uniq(keys)
    have = shapes(todo)
    out = []
    for key in todo:
        row = have.get(key)
        if row is None:
            out.append(key)
        elif (
            row["geometry"] is None and row["fetched_at"] and now - row["fetched_at"] >= RETRY_AFTER
        ):
            out.append(key)
    return out


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 5)
    if isinstance(value, (list, tuple)):
        return [_round(v) for v in value]
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    return value


def simplify(geometry: dict[str, Any]) -> tuple[dict[str, Any], float, float]:
    """Упрощённый контур и точка внутри него (для подписи и запасного кружка)."""
    from shapely.geometry import mapping, shape

    g = shape(geometry)
    point = g.representative_point()
    simple = g.simplify(SIMPLIFY_DEG, preserve_topology=True)
    if simple.is_empty:
        simple = g
    return _round(mapping(simple)), round(point.y, 6), round(point.x, 6)


def fill(
    keys: Iterable[str | None],
    *,
    provider: Any = None,
    sleep=time.sleep,
    now: datetime | None = None,
) -> int:
    """Догрузить недостающие контуры из НСПД (не больше `MAX_PER_RUN` за раз)."""
    todo = missing(keys, now)[:MAX_PER_RUN]
    if not todo:
        return 0
    own = provider is None
    if own:
        from landcheck.providers.nspd import LiveNspdProvider

        provider = LiveNspdProvider(
            with_zones=False, proxy=settings.proxy, timeout=settings.nspd_timeout
        )
    done = 0
    try:
        for key in todo:
            geom, lat, lon, err = None, None, None, None
            try:
                raw = provider.fetch_division_raw(key)
                if raw is None:
                    err = "в НСПД не найден"
                else:
                    geom, lat, lon = simplify(raw)
            except Exception as exc:  # noqa: BLE001 — НСПД не ответил: повторим через сутки
                err = f"{type(exc).__name__}: {exc}"[:300]
            with session() as db:
                row = db.get(TorgiPlace, key)
                if row is None:
                    row = TorgiPlace(key=key)
                    db.add(row)
                row.geometry, row.lat, row.lon, row.error = geom, lat, lon, err
                row.fetched_at = now or datetime.now(UTC)
                db.commit()
            done += 1
            sleep(PAUSE_S)
    finally:
        if own:
            provider.close()
    return done


def start_fill(keys: Iterable[str | None]) -> int:
    """Фоном, один поток на сервер: страница не ждёт НСПД. Районы и кварталы встают в общую
    очередь — открытые во время чужой загрузки не теряются (прод, 17.11: кварталы 24:50 не
    встали в загрузку, пока грузились районы). Возвращает, сколько добавлено в очередь
    (0 — всё есть, уже в очереди или демо-режим)."""
    global _running
    if settings.fixtures is not None:
        return 0
    todo = missing(keys)
    with _lock:
        fresh = [k for k in todo if k not in _queue]
        for k in fresh:
            _queue[k] = None
        if not _queue or _running:
            return len(fresh)
        _running = True

    def job() -> None:
        global _running
        while True:
            with _lock:
                batch = list(_queue)[:MAX_PER_RUN]
                if not batch:
                    _running = False
                    return
            try:
                fill(batch)
            except Exception as exc:  # noqa: BLE001 — фоновая догрузка не должна ронять сервер
                log.warning("torgi places: %s", exc)
            finally:
                with _lock:
                    for k in batch:
                        _queue.pop(k, None)

    threading.Thread(target=job, name="torgi-places", daemon=True).start()
    return len(fresh)
