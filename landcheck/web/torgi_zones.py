"""Обременения участков из торгов: ЗОУИТ, ООПТ, лесопарк — без отдельной проверки участка.

Чат «МП Земля», 17.11.2026, №76 (эксперт по торгам): разобрать, почему торги не состоялись, в
том числе «есть обременения»; обещано к 19.11 — обременения у лотов «интересен» и «на грани»
в списке лотов и у завершённых торгов в их списке. Полный запрос НСПД с зонами — около шести
обращений на участок, поэтому спрашиваем только то, что показано на странице, фоном, одним
потоком; страница НСПД не ждёт. Ответ с зонами кладём в общий кэш участков — его же возьмёт
проверка участка.
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
from landcheck.web.db import TorgiLot, session

log = logging.getLogger(__name__)

RETRY_AFTER = timedelta(days=1)  # НСПД не ответил — повтор не раньше суток
PAUSE_S = 1.0
MAX_PER_RUN = 120
NOTE_LEN = 90

STATE_RU = {
    "noborders": "границы участка не установлены — зоны не определить",
    "notfound": "участок не найден в НСПД",
    "error": "НСПД не ответил, повторим завтра",
}

_lock = threading.Lock()
_running = False
_queue: dict[str, None] = {}  # лоты в очереди на проверку, по порядку постановки


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def summary(payload: dict[str, Any]) -> tuple[list[str] | None, str]:
    """Обременения из ответа НСПД: (строки, состояние). Строки None — зоны не проверены."""
    from landcheck.normalize import parcel_from_payload

    if not payload.get("found"):
        return None, ("error" if payload.get("errors") else "notfound")
    parcel = parcel_from_payload(payload)
    if not parcel.zouit_checked:
        return None, ("noborders" if not parcel.has_borders else "error")
    out = [z.listing_note(NOTE_LEN) for z in parcel.zouit]
    out += [f"ООПТ: {z.listing_note(NOTE_LEN)}" for z in parcel.oopt]
    out += [f"лесопарк: {z.listing_note(NOTE_LEN)}" for z in parcel.forest_park]
    return list(dict.fromkeys(out)), "ok"


def missing(lot_ids: Iterable[str | None], now: datetime | None = None) -> list[str]:
    """Лоты с кадастровым номером, у которых обременения не смотрели либо НСПД не ответил
    сутки назад и раньше."""
    now = now or datetime.now(UTC)
    todo = list(dict.fromkeys(i for i in lot_ids if i))
    out: list[str] = []
    with session() as db:
        for i in range(0, len(todo), 500):
            part = todo[i : i + 500]
            q = select(TorgiLot.id, TorgiLot.cad_num, TorgiLot.zones_state, TorgiLot.zones_at)
            rows = {r.id: r for r in db.execute(q.where(TorgiLot.id.in_(part)))}
            for lot_id in part:
                r = rows.get(lot_id)
                if r is None or not r.cad_num:
                    continue
                at = _aware(r.zones_at)
                if r.zones_state is None or (
                    r.zones_state == "error" and (at is None or now - at >= RETRY_AFTER)
                ):
                    out.append(lot_id)
    return out


def fill(
    lot_ids: Iterable[str | None],
    *,
    provider: Any = None,
    cache: Any = None,
    sleep=time.sleep,
    now: datetime | None = None,
) -> int:
    """Обременения для лотов без них (не больше `MAX_PER_RUN` за раз): сначала кэш участков
    (там полный ответ, если участок проверяли), иначе полный запрос НСПД."""
    todo = missing(lot_ids, now)[:MAX_PER_RUN]
    if not todo:
        return 0
    own_provider, own_cache = provider is None, cache is None
    if own_provider:
        from landcheck.providers.nspd import LiveNspdProvider

        provider = LiveNspdProvider(
            with_zones=True, proxy=settings.proxy, timeout=settings.nspd_timeout
        )
    if own_cache:
        from landcheck.cache import ParcelCache

        cache = ParcelCache(settings.cache_path, ttl_days=settings.cache_ttl_days)
    done = 0
    try:
        for lot_id in todo:
            with session() as db:
                lot = db.get(TorgiLot, lot_id)
                cad = lot.cad_num if lot is not None else None
            if not cad:
                continue
            payload = cache.get(cad, allow_stale=True) if cache is not None else None
            if payload is None or not payload.get("found") or payload.get("zouit") is None:
                try:
                    payload = provider.fetch_raw(cad)
                except Exception as exc:  # noqa: BLE001 — НСПД не ответил: повторим через сутки
                    payload = {"found": False, "errors": [f"{type(exc).__name__}: {exc}"]}
                if cache is not None and payload.get("found") and payload.get("zouit") is not None:
                    cache.put(cad, payload)
                sleep(PAUSE_S)
            zones, state = summary(payload)
            with session() as db:
                lot = db.get(TorgiLot, lot_id)
                if lot is not None:
                    lot.zones, lot.zones_state = zones, state
                    lot.zones_at = now or datetime.now(UTC)
                    db.commit()
            done += 1
    finally:
        if own_provider:
            provider.close()
        if own_cache:
            cache.close()
    return done


def start_fill(lot_ids: Iterable[str | None]) -> int:
    """Фоном, один поток на сервер. Лоты встают в общую очередь: страница, открытая во время
    чужой проверки, не теряет свои (прод, 17.11: запрос «не ушедших» во время проверки списка
    лотов отбрасывался). Возвращает, сколько лотов добавлено в очередь (0 — всё проверено,
    уже в очереди или демо-режим)."""
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
            except Exception as exc:  # noqa: BLE001 — фоновая проверка не должна ронять сервер
                log.warning("torgi zones: %s", exc)
            finally:
                with _lock:
                    for i in batch:
                        _queue.pop(i, None)

    threading.Thread(target=job, name="torgi-zones", daemon=True).start()
    return len(fresh)


def queued() -> int:
    """Сколько лотов ждут проверки обременений (для подписи на странице)."""
    with _lock:
        return len(_queue)


def label(lot: dict[str, Any]) -> str | None:
    """Строка для таблицы: «нет», перечень зон или почему не знаем; None — не смотрели."""
    state = lot.get("zones_state")
    if state is None:
        return None
    if state == "ok":
        zones = lot.get("zones") or []
        return "; ".join(zones) if zones else "нет"
    return STATE_RU.get(state, state)
