"""Сбор торгов региона в базу (анализатор торгов; ТЗ v3, п. 9).

torgi.gov.ru пропускает один запрос в 6 секунд и отдаёт по 10 лотов на страницу, поэтому регион
собирается фоном в отдельном потоке и хранится в таблице `torgi_lots`: страница анализатора
открывается мгновенно, а данные обновляются раз в сутки. Один проход:

1. `active` — все идущие лоты региона (страницы поиска целиком), затем карточки новых
   идущих лотов: шаг аукциона, задаток, дата торгов и документы (ГПЗУ) есть только там;
2. `finished` — лоты, которые в прошлый раз шли, а теперь из поиска пропали: читаем карточку,
   получаем итог;
3. `history` — завершённые за год (состоялись и не состоялись). Первый раз — все страницы
   (место запоминается: рестарт продолжит), дальше — только новые сверху;
4. `cards` — карточки завершённых лотов: итоговая цена и число заявок есть только там.
   За проход — не больше `settings.torgi_scan_cards`, новые первыми; остальное дочитает
   следующий проход;
5. `nspd` — по идущим лотам кадастровая стоимость и точка на карте из НСПД (лёгкий запрос
   без зон; в общий кэш участков не пишем, чтобы проверка не получила урезанный ответ).

Любая фаза идемпотентна: лоты пишутся по ключу, повтор ничего не портит. Имена участников
торгов не сохраняются нигде — только число заявок и тип победителя.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from landcheck import ruproxy, torgi, torgi_scan
from landcheck.web.config import settings
from landcheck.web.db import TorgiLot, TorgiSync, session

log = logging.getLogger("landcheck.web.torgi_sync")

HISTORY_STATUSES = (torgi.STATUS_SUCCEED, torgi.STATUS_FAILED)
MAX_ACTIVE_PAGES = 300  # 3000 идущих лотов на регион — с запасом
MAX_HISTORY_PAGES = 800
MAX_FINISHED = 150  # карточек «пропавших из идущих» за проход
SHALLOW_PAGES = 5  # после первого полного сбора — столько страниц истории сверху
STALE_RUNNING_MIN = 20  # нет отметок прогресса дольше — считаем, что процесс умер
NSPD_PAUSE_S = 1.0
ERROR_RETRY_HOURS = 1.0

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_queued: set[str] = set()


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="torgi-sync")
        return _executor


# --- запись лотов --------------------------------------------------------------------------
def upsert(
    db, row: dict[str, Any], now: datetime, *, card: bool = False
) -> tuple[TorgiLot, str | None]:
    """Лот по ключу: новое поверх старого, пустое из поиска не затирает данные карточки.
    Второе значение: `"new"` — лота не было, `"changed"` — сменился статус, иначе None."""
    lot = db.get(TorgiLot, row["id"])
    if lot is None:
        lot = TorgiLot(id=row["id"], first_seen_at=now)
        db.add(lot)
        what: str | None = "new"
        merged = row
    else:
        merged = torgi_scan.merge_row(lot.to_dict(), row)
        what = "changed" if merged.get("status") != lot.status else None
    for key in TorgiLot.FIELDS:
        if key in ("id", "cad_cost", "lat", "lon"):
            continue
        value = merged.get(key)
        if key in ("annulled", "stopped", "appeals"):
            value = bool(value)
        if isinstance(value, str):
            limit = getattr(TorgiLot, key).type.length if key != "id" else None
            if limit:
                value = value[:limit]
        setattr(lot, key, value)
    lot.seen_at = now
    if card:
        lot.card_at = now
    elif what == "changed" and lot.status not in torgi.ACTIVE:
        # лот завершился, а карточку читали, пока он шёл: итог и заявки нужно дочитать
        lot.card_at = None
    return lot, what


def lots_of(region: str, *, months: int | None = None) -> list[dict[str, Any]]:
    """Все лоты региона из базы (идущие и завершённые за `months`) — вход для расчётов."""
    since = None
    if months:
        since = (utcnow() - timedelta(days=30 * months)).date().isoformat()
    with session() as db:
        q = select(TorgiLot).where(TorgiLot.region == region)
        rows = [lot.to_dict() for lot in db.scalars(q).all()]
    if since:
        rows = [
            r
            for r in rows
            if r.get("status") in torgi.ACTIVE or not r.get("published") or r["published"] >= since
        ]
    return rows


STORE_FRESH_DAYS = 3  # сводку района берём из базы, если регион собирали не раньше


def auctions_from_store(cad_num: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Сводка «Торги рядом за год» по кадастровому району из собранной базы региона — в том
    же виде, что отдаёт `torgi.nearby_auctions`, но мгновенно и по всем лотам района, а не по
    12 последним карточкам. None — регион не собран целиком или данные устарели: тогда
    работает прежний сбор по району."""
    district, region = torgi.district_of(cad_num), torgi.region_of(cad_num)
    if district is None or region is None:
        return None
    now = now or utcnow()
    with session() as db:
        st = db.get(TorgiSync, region)
        if st is None or not st.backfill_done or st.finished_at is None:
            return None
        finished = _aware(st.finished_at)
    if now - finished > timedelta(days=STORE_FRESH_DAYS):
        return None
    lots = [
        lot
        for lot in lots_of(region, months=torgi.MONTHS)
        if lot.get("district") == district and lot.get("form") == "EA"
    ]
    result = torgi.empty_result(cad_num)
    by_status: dict[str, list[dict[str, Any]]] = {"SUCCEED": [], "FAILED": [], "ACTIVE": []}
    for lot in lots:
        if lot.get("status") in by_status:
            by_status[lot["status"]].append(lot)
        elif lot.get("status") in torgi.ACTIVE:
            by_status["ACTIVE"].append(lot)
    items = []
    for lot in by_status["SUCCEED"]:
        area, fin, start = lot.get("area_m2"), lot.get("price_fin"), lot.get("price_start")
        items.append(
            {
                "id": lot["id"],
                "url": torgi.LOT_URL.format(lot_id=lot["id"]),
                "cad_num": lot.get("cad_num"),
                "kind": "аренда" if lot.get("kind") == "rent" else "продажа",
                "status": lot.get("status"),
                "area_m2": area,
                "price_start": start,
                "price_fin": fin,
                "rub_m2": round(fin / area, 1) if fin and area else None,
                "ratio": round(fin / start, 2) if fin and start else None,
                "date": lot.get("auction_date") or lot.get("bidd_end") or lot.get("published"),
                "address": lot.get("address"),
                "vri": lot.get("vri"),
                "category": lot.get("category"),
                "form": lot.get("form"),
            }
        )
    items.sort(key=lambda i: i.get("date") or "", reverse=True)
    result["succeed_total"] = len(by_status["SUCCEED"])
    result["failed_total"] = len(by_status["FAILED"])
    result["active_total"] = len(by_status["ACTIVE"])
    result["ea_total"] = result["succeed_total"] + result["failed_total"] + result["active_total"]
    closed = result["succeed_total"] + result["failed_total"]
    result["liquidity"] = round(result["succeed_total"] / closed, 2) if closed else None
    result["sale"] = torgi._stats([i for i in items if i["kind"] == "продажа"])
    result["rent"] = torgi._stats([i for i in items if i["kind"] == "аренда"])
    result["items"] = items[:15]
    priced = sum(1 for i in items if i.get("price_fin"))
    notes = []
    if priced < len(items):
        notes.append(f"итоговая цена прочитана у {priced} состоявшихся аукционов из {len(items)}")
    result["sample_note"] = "; ".join(notes) or None
    result["source"] = torgi.SOURCE + f"; из собранной базы торгов региона на {finished:%d.%m.%Y}"
    result["as_of"] = finished.isoformat(timespec="seconds")
    result["from_store"] = True
    result["liquidity_text"] = torgi.liquidity_text(result)
    return result


# --- состояние -----------------------------------------------------------------------------
def _state(db, region: str) -> TorgiSync:
    st = db.get(TorgiSync, region)
    if st is None:
        st = TorgiSync(region=region, state="idle", progress={}, stats={})
        db.add(st)
        db.flush()
    return st


def _update(region: str, **fields: Any) -> None:
    with session() as db:
        st = _state(db, region)
        progress = fields.pop("progress", None)
        for k, v in fields.items():
            setattr(st, k, v)
        if progress is not None:
            st.progress = {**(st.progress or {}), **progress, "at": utcnow().isoformat()}
        db.commit()


def state_of(region: str) -> dict[str, Any]:
    """Состояние сбора для страницы: что собрано, идёт ли сбор, сколько лотов в базе."""
    with session() as db:
        st = db.get(TorgiSync, region)
        lots = db.execute(
            select(TorgiLot.status, TorgiLot.card_at, TorgiLot.price_fin, TorgiLot.nspd_at).where(
                TorgiLot.region == region
            )
        ).all()
    active = sum(1 for s, *_ in lots if s in torgi.ACTIVE)
    succeed = [row for row in lots if row[0] == "SUCCEED"]
    failed = [row for row in lots if row[0] == "FAILED"]
    out: dict[str, Any] = {
        "region": region,
        "state": "never",
        "phase": None,
        "progress": {},
        "backfill_done": False,
        "auto": True,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "stats": {},
        "lots": len(lots),
        "active": active,
        "succeed": len(succeed),
        "failed": len(failed),
        "succeed_priced": sum(1 for row in succeed if row[2]),
        "cards_left": sum(1 for row in succeed + failed if row[1] is None),
        "gateway": ruproxy.enabled(),
    }
    if st is not None:
        out.update(
            state=st.state,
            phase=st.phase,
            progress=st.progress or {},
            backfill_done=st.backfill_done,
            auto=st.auto,
            started_at=_aware(st.started_at),
            finished_at=_aware(st.finished_at),
            error=st.error,
            stats=st.stats or {},
        )
    if out["state"] in ("running", "queued") and region not in _queued:
        # процесс перезапускался: работа шла только в памяти
        at = (out["progress"] or {}).get("at")
        try:
            last = datetime.fromisoformat(at) if at else out["started_at"]
        except ValueError:
            last = out["started_at"]
        if last is None or utcnow() - _aware(last) > timedelta(minutes=STALE_RUNNING_MIN):
            out["state"] = "interrupted"
    return out


# --- один проход ---------------------------------------------------------------------------
def _store_page(
    region: str, hits: list[dict[str, Any]], now: datetime, stats: dict[str, int]
) -> tuple[list[str], int]:
    """Записать страницу поиска. Возвращает (id лотов страницы, сколько из них новых или
    сменивших статус)."""
    ids: list[str] = []
    fresh = 0
    with session() as db:
        for hit in hits:
            row = torgi_scan.lot_row(hit)
            if row is None:
                continue
            row["region"] = row.get("region") or region
            lot, what = upsert(db, row, now)
            ids.append(lot.id)
            if what:
                stats[what] += 1
                fresh += 1
        db.commit()
    return ids, fresh


def _pages(
    api: torgi.TorgiClient,
    region: str,
    statuses: str,
    pub_from: str,
    *,
    start_page: int = 0,
    max_pages: int,
    on_page,
) -> tuple[int, bool]:
    """Листаем поиск. `on_page(page, total_pages, hits)` возвращает False — остановиться.
    Возвращает (следующая страница, дошли ли до конца)."""
    page, total_pages = start_page, start_page + 1
    while page < total_pages and page < max_pages:
        data = api.search(region=region, statuses=statuses, pub_from=pub_from, page=page, form=None)
        total_pages = int(data.get("totalPages") or 0)
        hits = data.get("content") or []
        go_on = on_page(page, total_pages, hits)
        page += 1
        if not hits:  # пустая страница — дальше ничего нет
            return page, True
        if go_on is False:
            break
    return page, page >= total_pages


def sync_region(
    region: str,
    *,
    client=None,
    now: datetime | None = None,
    interval: float = torgi.REQUEST_INTERVAL_S,
    sleep=time.sleep,
    provider=None,
    max_cards: int | None = None,
    max_nspd: int | None = None,
    max_active_cards: int | None = None,
) -> dict[str, Any]:
    """Полный проход по региону (см. описание модуля). Ошибка сети прерывает проход, уже
    записанное остаётся; исключения наружу не выходят — результат в `torgi_sync`."""
    now = now or utcnow()
    max_cards = settings.torgi_scan_cards if max_cards is None else max_cards
    if max_active_cards is None:
        max_active_cards = settings.torgi_scan_active_cards
    max_nspd = settings.torgi_scan_nspd if max_nspd is None else max_nspd
    if client is None and not ruproxy.enabled():
        _update(
            region,
            state="error",
            error="RU-шлюз не настроен (LANDCHECK_RU_PROXY) — torgi.gov.ru из-за рубежа закрыт",
            finished_at=now,
        )
        return state_of(region)
    api = torgi.TorgiClient(client, interval=interval, sleep=sleep)
    pub_from = (now - timedelta(days=30 * settings.torgi_scan_months)).date().isoformat()
    with session() as db:
        st = _state(db, region)
        backfill_done = st.backfill_done
        cursors = dict((st.progress or {}).get("cursors") or {})
        db.commit()
    _update(region, state="running", phase="active", started_at=now, error=None)
    stats = {"new": 0, "changed": 0, "cards": 0, "nspd": 0, "requests": 0}
    try:
        # 1. идущие ------------------------------------------------------------------------
        seen: set[str] = set()

        def on_active(page: int, total: int, hits: list[dict[str, Any]]) -> None:
            ids, _ = _store_page(region, hits, now, stats)
            seen.update(ids)
            _update(region, progress={"page": page + 1, "pages": total, "phase": "active"})

        _, active_done = _pages(
            api,
            region,
            torgi.STATUS_ACTIVE,
            pub_from,
            max_pages=MAX_ACTIVE_PAGES,
            on_page=on_active,
        )

        # 1б. карточки идущих: шаг, задаток, дата торгов и ГПЗУ есть только в карточке ---------
        with session() as db:
            fresh_active = db.execute(
                select(TorgiLot.id, TorgiLot.published).where(
                    TorgiLot.region == region,
                    TorgiLot.status.in_(sorted(torgi.ACTIVE)),
                    TorgiLot.card_at.is_(None),
                )
            ).all()
        fresh_active.sort(key=lambda r: r[1] or "", reverse=True)
        _read_cards(
            api,
            region,
            [r[0] for r in fresh_active[:max_active_cards]],
            now,
            stats,
            "active_cards",
        )

        # 2. пропали из идущих ---------------------------------------------------------------
        # Только если идущие дочитаны до конца: иначе «пропавшими» окажутся непросмотренные.
        gone: list[str] = []
        if active_done:
            with session() as db:
                gone = [
                    lot_id
                    for lot_id in db.scalars(
                        select(TorgiLot.id).where(
                            TorgiLot.region == region, TorgiLot.status.in_(sorted(torgi.ACTIVE))
                        )
                    ).all()
                    if lot_id not in seen
                ][:MAX_FINISHED]
        _read_cards(api, region, gone, now, stats, "finished")

        # 3. завершённые за год ----------------------------------------------------------------
        _update(region, phase="history")
        complete = True
        for statuses in HISTORY_STATUSES:
            on_history = _history_handler(region, statuses, now, stats, cursors, backfill_done)
            _, done = _pages(
                api,
                region,
                statuses,
                pub_from,
                start_page=0 if backfill_done else int(cursors.get(statuses, 0)),
                max_pages=SHALLOW_PAGES if backfill_done else MAX_HISTORY_PAGES,
                on_page=on_history,
            )
            complete = complete and (done or backfill_done)
        if complete and not backfill_done:
            backfill_done = True
            _update(region, backfill_done=True, progress={"cursors": {}})

        # 4. карточки завершённых --------------------------------------------------------------
        _update(region, phase="cards")
        with session() as db:
            need = db.execute(
                select(TorgiLot.id, TorgiLot.status, TorgiLot.published).where(
                    TorgiLot.region == region,
                    TorgiLot.status.in_(HISTORY_STATUSES),
                    TorgiLot.card_at.is_(None),
                )
            ).all()
        # сначала состоявшиеся (нужна итоговая цена), внутри — новые первыми
        need.sort(key=lambda r: r[2] or "", reverse=True)
        need.sort(key=lambda r: r[1] != "SUCCEED")
        _read_cards(api, region, [r[0] for r in need[:max_cards]], now, stats, "cards")

        # 5. НСПД по идущим ----------------------------------------------------------------------
        _update(region, phase="nspd")
        stats["nspd"] = enrich_nspd(region, now, provider=provider, limit=max_nspd, sleep=sleep)

        stats["requests"] = api.requests
        _update(
            region,
            state="idle",
            phase=None,
            finished_at=utcnow(),
            stats=stats,
            progress={"page": 0, "pages": 0, "phase": "done"},
        )
    except Exception as exc:  # noqa: BLE001 — фоновая задача: ошибка в состояние, не наружу
        log.warning("torgi sync %s: %s", region, exc)
        stats["requests"] = api.requests
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        _update(
            region,
            state="error",
            error=f"сбор прерван: {reason} — уже собранное сохранено, следующий запуск продолжит",
            finished_at=utcnow(),
            stats=stats,
        )
    finally:
        if client is None:
            api.close()
    return state_of(region)


def _history_handler(
    region: str,
    statuses: str,
    now: datetime,
    stats: dict[str, int],
    cursors: dict[str, int],
    backfill_done: bool,
):
    """Обработчик страницы истории: пишет лоты и запоминает место. После первого полного сбора
    листаем, только пока на странице есть новое."""

    def on_history(page: int, total: int, hits: list[dict[str, Any]]) -> bool:
        _, fresh = _store_page(region, hits, now, stats)
        cursors[statuses] = page + 1
        _update(
            region,
            progress={
                "page": page + 1,
                "pages": total,
                "phase": f"history:{statuses}",
                "cursors": dict(cursors),
            },
        )
        return not (backfill_done and fresh == 0)

    return on_history


MAX_CARD_FAILURES = 5  # столько сбоев подряд — сайт или шлюз лежит, проход прерываем


def _read_cards(
    api: torgi.TorgiClient,
    region: str,
    lot_ids: list[str],
    now: datetime,
    stats: dict[str, int],
    phase: str,
) -> None:
    """Прочитать карточки лотов с отметками прогресса. Одна неудача прохода не роняет, но
    несколько подряд значат, что сайт недоступен: бросаем, уже записанное остаётся."""
    _update(region, phase=phase, progress={"page": 0, "pages": len(lot_ids), "phase": phase})
    failures = 0
    for n, lot_id in enumerate(lot_ids, 1):
        if _read_card(api, region, lot_id, now, stats):
            failures = 0
        else:
            failures += 1
            if failures >= MAX_CARD_FAILURES:
                raise RuntimeError("torgi.gov.ru не отвечает на запросы карточек")
        if n % 5 == 0 or n == len(lot_ids):
            _update(region, progress={"page": n, "pages": len(lot_ids), "phase": phase})


def _read_card(
    api: torgi.TorgiClient, region: str, lot_id: str, now: datetime, stats: dict[str, int]
) -> bool:
    """Карточка лота в базу. False — сайт не ответил (сеть, 5xx); «лота нет» (404) — это
    ответ: помечаем и больше не спрашиваем."""
    try:
        card = api.lot(lot_id)
    except Exception as exc:  # noqa: BLE001 — одна карточка не должна ронять проход
        log.info("torgi card %s: %s", lot_id, exc)
        if getattr(getattr(exc, "response", None), "status_code", None) != 404:
            return False
        with session() as db:  # лот убрали с площадки
            lot = db.get(TorgiLot, lot_id)
            if lot is not None:
                lot.card_at = now
                if lot.status in torgi.ACTIVE:
                    lot.status = "CANCELED"
                db.commit()
        return True
    row = torgi_scan.lot_row({"id": lot_id, **card})
    if row is None:
        return True
    row["region"] = row.get("region") or region
    with session() as db:
        upsert(db, row, now, card=True)
        db.commit()
    stats["cards"] += 1
    return True


def enrich_nspd(
    region: str, now: datetime, *, provider=None, limit: int = 300, sleep=time.sleep
) -> int:
    """Кадастровая стоимость и точка на карте для идущих лотов: из кэша участков, иначе лёгкий
    запрос НСПД (без зон). Урезанный ответ в общий кэш не пишем."""
    if limit <= 0 or (provider is None and settings.fixtures is not None):
        return 0
    from landcheck.cache import ParcelCache
    from landcheck.normalize import parcel_from_payload

    with session() as db:
        need = list(
            db.scalars(
                select(TorgiLot.id).where(
                    TorgiLot.region == region,
                    TorgiLot.status.in_(sorted(torgi.ACTIVE)),
                    TorgiLot.cad_num.is_not(None),
                    TorgiLot.nspd_at.is_(None),
                )
            ).all()
        )[:limit]
    if not need:
        return 0
    if provider is None:
        from landcheck.providers.nspd import LiveNspdProvider

        provider = LiveNspdProvider(
            with_zones=False, proxy=settings.proxy, timeout=settings.nspd_timeout
        )
    cache = ParcelCache(settings.cache_path, ttl_days=30)
    done = 0
    try:
        for lot_id in need:
            with session() as db:
                lot = db.get(TorgiLot, lot_id)
                if lot is None or not lot.cad_num:
                    continue
                cad = lot.cad_num
            payload = cache.get(cad, allow_stale=True)
            if payload is None or not payload.get("found"):
                try:
                    payload = provider.fetch_raw(cad)
                except Exception as exc:  # noqa: BLE001 — НСПД не ответил: попробуем в следующий раз
                    log.info("torgi nspd %s: %s", cad, exc)
                    sleep(NSPD_PAUSE_S)
                    continue
                sleep(NSPD_PAUSE_S)
            parcel = parcel_from_payload(payload)
            with session() as db:
                lot = db.get(TorgiLot, lot_id)
                if lot is None:
                    continue
                lot.nspd_at = now
                if parcel.found:
                    lot.cad_cost = parcel.cadastral_cost_rub
                    if lot.area_m2 is None and parcel.best_area_m2:
                        lot.area_m2 = parcel.best_area_m2
                    if parcel.geometry:
                        try:
                            from landcheck.location import centroid

                            lot.lat, lot.lon = centroid(parcel.geometry)
                        except Exception:  # noqa: BLE001 — кривая геометрия: без точки
                            pass
                db.commit()
            done += 1
    finally:
        cache.close()
    return done


# --- запуск --------------------------------------------------------------------------------
def start_sync(region: str) -> bool:
    """Поставить сбор региона в очередь (один поток на все регионы). False — уже в очереди."""
    with _lock:
        if region in _queued:
            return False
        _queued.add(region)
    _update(region, state="queued", error=None, progress={"phase": "queued"})

    def job() -> None:
        try:
            sync_region(region)
        finally:
            with _lock:
                _queued.discard(region)

    executor().submit(job)
    return True


def resume_interrupted() -> list[str]:
    """После рестарта: регионы, чей сбор оборвался, ставим в очередь снова."""
    with session() as db:
        regions = list(
            db.scalars(
                select(TorgiSync.region).where(TorgiSync.state.in_(["running", "queued"]))
            ).all()
        )
    if not ruproxy.enabled() or settings.fixtures is not None:
        for r in regions:
            _update(r, state="idle")
        return []
    return [r for r in regions if start_sync(r)]


def due_regions(now: datetime | None = None) -> list[str]:
    """Регионы с автосбором, которые не обновлялись дольше `settings.torgi_scan_hours`."""
    hours = settings.torgi_scan_hours
    if hours <= 0:
        return []
    now = now or utcnow()
    with session() as db:
        rows = db.execute(
            select(TorgiSync.region, TorgiSync.finished_at, TorgiSync.state).where(
                TorgiSync.auto.is_(True)
            )
        ).all()
    out = []
    for region, finished, state in rows:
        if state in ("running", "queued"):
            continue
        finished = _aware(finished)
        # после сбоя (шлюз, сайт) пробуем снова через час, а не через сутки
        wait = timedelta(hours=min(hours, ERROR_RETRY_HOURS) if state == "error" else hours)
        if finished is None or now - finished >= wait:
            out.append(region)
    return out


def start_auto_loop(*, first_delay_s: float = 120.0, period_s: float = 300.0):
    """Раз в пять минут смотрим, какие регионы пора пересобрать (один запрос к базе).
    Без RU-шлюза и в демо-режиме на фикстурах не запускается."""
    if settings.torgi_scan_hours <= 0 or settings.fixtures is not None or not ruproxy.enabled():
        return None

    def loop() -> None:
        time.sleep(first_delay_s)
        while True:
            try:
                for region in due_regions():
                    start_sync(region)
            except Exception as exc:  # noqa: BLE001 — цикл не должен умереть от одной ошибки
                log.warning("torgi auto: %s", exc)
            time.sleep(period_s)

    t = threading.Thread(target=loop, name="torgi-auto", daemon=True)
    t.start()
    return t
