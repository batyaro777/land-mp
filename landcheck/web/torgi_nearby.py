"""Что рядом с участком из торгов: доступность и инфраструктура по OpenStreetMap.

ТЗ v5, п. 7 (зум 18.11.2026, 01:08: «доступность, инфраструктура, коммуникации, дороги»).
Запрос к карте медленный (до минуты), поэтому спрашиваем только лоты-кандидаты, которые
показаны на странице «Горячие», фоном и одним потоком; страница карту не ждёт. Разбор ответа
и выводы — в `landcheck.nearby`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from landcheck import nearby, osm
from landcheck.web.config import settings
from landcheck.web.db import TorgiLot, session

log = logging.getLogger(__name__)

RETRY_AFTER = timedelta(days=1)  # карта не ответила — повтор не раньше суток
REFRESH_AFTER = timedelta(days=90)  # карта меняется медленно
PAUSE_S = 5.0  # публичные серверы карты: не чаще запроса в несколько секунд
MAX_PER_RUN = 40

_lock = threading.Lock()
_running = False
_queue: dict[str, None] = {}  # лоты в очереди, по порядку постановки


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def missing(lot_ids: Iterable[str | None], now: datetime | None = None) -> list[str]:
    """Лоты, по которым карту не спрашивали, спрашивали давно либо она не ответила сутки
    назад и раньше. Лот без точки на карте тоже берём: отметим, что спросить не о чем."""
    now = now or datetime.now(UTC)
    todo = list(dict.fromkeys(i for i in lot_ids if i))
    out: list[str] = []
    with session() as db:
        for i in range(0, len(todo), 500):
            part = todo[i : i + 500]
            q = select(TorgiLot.id, TorgiLot.nearby_state, TorgiLot.nearby_at)
            rows = {r.id: r for r in db.execute(q.where(TorgiLot.id.in_(part)))}
            for lot_id in part:
                r = rows.get(lot_id)
                if r is None:
                    continue
                age = now - at if (at := _aware(r.nearby_at)) is not None else None
                if (
                    r.nearby_state is None
                    or age is None
                    or (r.nearby_state == "ok" and age >= REFRESH_AFTER)
                    or (r.nearby_state != "ok" and age >= RETRY_AFTER)
                ):
                    out.append(lot_id)
    return out


def fill(
    lot_ids: Iterable[str | None],
    *,
    fetch: Callable[[str], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> int:
    """Спросить карту по лотам без ответа (не больше `MAX_PER_RUN` за раз). Возвращает,
    сколько лотов обработано."""
    fetch = fetch or osm.run_query
    done = 0
    for lot_id in missing(lot_ids, now)[:MAX_PER_RUN]:
        with session() as db:
            lot = db.get(TorgiLot, lot_id)
            point = (lot.lat, lot.lon) if lot is not None else (None, None)
        if lot is None:
            continue
        facts: dict[str, Any] | None = None
        if point[0] is None or point[1] is None:
            state = "nopoint"
        else:
            raw = fetch(nearby.query(point[0], point[1]))
            if raw.get("error"):
                state = "error"
            else:
                state, facts = "ok", nearby.summarize(point[0], point[1], raw.get("elements") or [])
            sleep(PAUSE_S)
        with session() as db:
            lot = db.get(TorgiLot, lot_id)
            if lot is not None:
                lot.nearby, lot.nearby_state = facts, state
                lot.nearby_at = now or datetime.now(UTC)
                db.commit()
        done += 1
    return done


def start_fill(lot_ids: Iterable[str | None]) -> int:
    """Фоном, один поток на сервер; лоты встают в общую очередь. Возвращает, сколько лотов
    добавлено (0 — всё спрошено, уже в очереди или демо-режим)."""
    global _running
    if settings.fixtures is not None:
        return 0
    todo = missing(lot_ids)
    with _lock:
        fresh = [i for i in todo if i not in _queue]
        for i in fresh:
            _queue[i] = None
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
            except Exception as exc:  # noqa: BLE001 — фоновый сбор не должен ронять сервер
                log.warning("torgi nearby: %s", exc)
            finally:
                with _lock:
                    for i in batch:
                        _queue.pop(i, None)

    threading.Thread(target=job, name="torgi-nearby", daemon=True).start()
    return len(fresh)


def queued() -> int:
    with _lock:
        return len(_queue)
