"""Объявления о продаже земли из ленты ads-api.ru: загрузка по собранным регионам, хранение и
цена рынка рядом с лотом для анализатора торгов (18.11.2026).

Один фоновый поток на сервер: лента требует паузу между запросами. Регион берём окном по
времени поступления объявлений, от новых к старым; незаконченное окно хранится в базе и после
рестарта продолжается с места остановки. Первый сбор — за `settings.adsapi_days` дней, дальше
догрузка раз в `settings.adsapi_hours` часов.

Ключ ленты живёт только в окружении и в адресе запроса; в базу, журнал и на страницы не
попадает. Отказ «ключ неверный / срок истёк» останавливает запросы до кнопки или рестарта:
долбить ленту с плохим ключом незачем.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert

from landcheck import ads_api
from landcheck.web import db as dbmod
from landcheck.web.config import settings
from landcheck.web.db import AdsSync, MarketAd, TorgiPlace, TorgiSync, session

log = logging.getLogger("landcheck.web.ads_sync")

LAG_MIN = 30  # лента отстаёт от текущего времени на полчаса
OVERLAP_MIN = 60  # догрузка захватывает час до прошлой границы; повторы снимаются по номеру
MAX_PAGES_PER_RUN = 300  # около получаса на регион, потом очередь следующего
KEEP_DAYS = 365
ERROR_RETRY_MIN = 30
LOOP_PAUSE_S = 60.0
STAT_KEYS_MAX = 12
BUMP_EVERY_PAGES = 20  # при долгой загрузке расчёты лотов обновляются пачками

_lock = threading.Lock()
_version: dict[str, int] = {}
_index: dict[str, tuple[tuple[int, int], ads_api.AdIndex | None]] = {}
_wanted: dict[str, None] = {}
_fatal: str | None = None
_thread: threading.Thread | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def enabled() -> bool:
    """Логин и ключ заданы, и это не демо на фикстурах."""
    return bool(settings.adsapi_user and settings.adsapi_token) and settings.fixtures is None


def version(region: str) -> int:
    """Растёт, когда объявления региона изменились: по нему устаревает расчёт списка лотов."""
    return _version.get(region, 0)


def _bump(region: str) -> None:
    with _lock:
        _version[region] = _version.get(region, 0) + 1


# --- хранение -------------------------------------------------------------------------------
def _count(stats: dict[str, Any], group: str, key: str) -> None:
    box = stats.setdefault(group, {})
    if key in box or len(box) < STAT_KEYS_MAX:
        box[key] = box.get(key, 0) + 1


def store(
    region: str,
    name: str,
    items: list[dict[str, Any]],
    stats: dict[str, Any] | None = None,
    *,
    bump: bool = True,
) -> int:
    """Страница ленты → база. Возвращает, сколько объявлений годится в расчёт цены. В `stats`
    копится, что пришло и почему отброшено, — по этим счётчикам видно, верно ли мы читаем
    ленту (единицы площади, доля объявлений с точкой и ценой). `bump=False` — расчёты лотов
    пока не трогать (идёт длинная загрузка, пересчитаем пачкой)."""
    stats = stats if stats is not None else {}
    rows = []
    for item in items:
        stats["got"] = stats.get("got", 0) + 1
        ad = ads_api.parse_ad(item)
        if ad is None:
            _count(stats, "skip", "no_id")
            continue
        _count(stats, "sources", ad["source"] or "—")
        _count(stats, "metrics", ad["metric"] or "—")
        if ad["unit"]:
            _count(stats.setdefault("units", {}), ad["source"] or "—", ad["unit"])
        for key in ("lat", "area_m2", "cls"):
            if ad[key]:
                _count(stats, "has", key)
        skip = ad["skip"]
        if skip is None and ad["region_name"] and ad["region_name"] != name:
            skip = "other_region"  # «Алтай» не должен принести Алтайский край
        if skip is None and ad["cls"] is None:
            skip = "no_class"
        if skip:
            _count(stats, "skip", skip)
            continue
        rows.append(
            {
                "id": ad["id"],
                "region": region,
                "source": ad["source"],
                "url": ad["url"],
                "title": ad["title"],
                "address": ad["address"],
                "cls": ad["cls"],
                "price": ad["price"],
                "area_m2": ad["area_m2"],
                "lat": ad["lat"],
                "lon": ad["lon"],
                "posted": ad["posted"],
                "seen_at": utcnow(),
            }
        )
    stats["kept"] = stats.get("kept", 0) + len(rows)
    if rows:
        with session() as db:
            for i in range(0, len(rows), 400):
                stmt = insert(MarketAd).values(rows[i : i + 400])
                fresh = {c: stmt.excluded[c] for c in rows[0] if c != "id"}
                db.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=fresh))
            db.commit()
        if bump:
            _bump(region)
    return len(rows)


def index_of(region: str) -> ads_api.AdIndex | None:
    """Объявления региона в памяти (пересобираются, когда база по региону изменилась)."""
    key = (dbmod.generation, version(region))
    hit = _index.get(region)
    if hit and hit[0] == key:
        return hit[1]
    with session() as db:
        rows = db.execute(
            select(
                MarketAd.id,
                MarketAd.lat,
                MarketAd.lon,
                MarketAd.price,
                MarketAd.area_m2,
                MarketAd.cls,
                MarketAd.posted,
            ).where(MarketAd.region == region)
        ).all()
    index = ads_api.AdIndex(tuple(r) for r in rows) if rows else None
    with _lock:
        _index[region] = (key, index)
    return index


def near_fn(
    region: str, lots: list[dict[str, Any]]
) -> Callable[[dict[str, Any]], dict[str, Any] | None] | None:
    """Функция «цена рынка по объявлениям рядом с лотом» для региона; None — объявлений нет.
    У лота без точки на карте берём середину его кадастрового квартала: контур из НСПД, а
    если его нет — среднее по лотам квартала с точкой."""
    index = index_of(region)
    if index is None:
        return None
    sums: dict[str, list[float]] = {}
    for lot in lots:
        if lot.get("lat") and lot.get("lon") and lot.get("quarter"):
            s = sums.setdefault(lot["quarter"], [0.0, 0.0, 0.0])
            s[0] += lot["lat"]
            s[1] += lot["lon"]
            s[2] += 1
    quarters = {q: (s[0] / s[2], s[1] / s[2]) for q, s in sums.items()}
    with session() as db:
        for key, lat, lon in db.execute(
            select(TorgiPlace.key, TorgiPlace.lat, TorgiPlace.lon).where(
                TorgiPlace.key.like(f"{region}:%:%"), TorgiPlace.lat.is_not(None)
            )
        ):
            if lon is not None:
                quarters[key] = (lat, lon)
    now = ads_api.msk_now()

    def fn(lot: dict[str, Any]) -> dict[str, Any] | None:
        if lot.get("lat") and lot.get("lon"):
            point, approx = (lot["lat"], lot["lon"]), False
        else:
            point, approx = quarters.get(lot.get("quarter") or ""), True
        # в расчёт лота цена объявлений идёт приведённой к уровню сделок (`ads_api.deal_level`)
        return ads_api.deal_level(
            ads_api.near_market(
                point, lot.get("area_m2"), lot.get("vri_group"), index, now=now, approx=approx
            )
        )

    return fn


def rows_for(dist: dict[int, float] | None) -> list[dict[str, Any]]:
    """Объявления по номерам с расстоянием до лота — для карточки (ближние первыми)."""
    if not dist:
        return []
    with session() as db:
        ads = db.scalars(select(MarketAd).where(MarketAd.id.in_(list(dist)))).all()
    out = [
        {
            "title": a.title,
            "url": a.url if (a.url or "").startswith("https://") else None,
            "source": a.source,
            "address": a.address,
            "price": a.price,
            "area_m2": a.area_m2,
            "pps": a.price / a.area_m2 * 100,
            "posted": (a.posted or "")[:10],
            "km": dist[a.id],
        }
        for a in ads
    ]
    return sorted(out, key=lambda x: x["km"])


# --- загрузка -------------------------------------------------------------------------------
def _fmt(dt: datetime) -> str:
    return dt.strftime(ads_api.TIME_FMT)


def sync_region(
    region: str,
    client: ads_api.AdsApiClient,
    *,
    now: datetime | None = None,
    max_pages: int = MAX_PAGES_PER_RUN,
) -> dict[str, Any]:
    """Один проход по региону: продолжить незаконченное окно либо начать новое (догрузка с
    прошлой границы; первый раз — за `settings.adsapi_days` дней). Возвращает счётчики окна.
    Отказ ленты записывается в состояние региона и пробрасывается дальше."""
    name = ads_api.region_name(region)
    now_msk = now or ads_api.msk_now()
    with session() as db:
        st = db.get(AdsSync, region)
        if st is None:
            st = AdsSync(region=region, stats={})
            db.add(st)
        if name is None:
            st.state, st.error = "error", "этого региона нет в справочнике ленты"
            st.finished_at = utcnow()
            db.commit()
            return {}
        win = dict(st.pending) if st.pending else None
        stats: dict[str, Any] = dict(st.stats or {}) if win else {}
        if win is None:
            hi = _fmt(now_msk - timedelta(minutes=LAG_MIN))
            if st.newest:
                lo = ads_api._shift(st.newest, -OVERLAP_MIN * 60)
            else:
                lo = _fmt(now_msk - timedelta(days=settings.adsapi_days))
            win = {"lo": lo, "hi": hi, "cursor": hi}
            stats = {"window": {"lo": lo, "hi": hi}}
        st.state, st.started_at, st.error, st.pending = "running", utcnow(), None, win
        db.commit()
    done = False
    error: str | None = None
    before = client.requests
    try:
        pages = client.pages(name, win["lo"], win["cursor"], max_pages=max_pages)
        for n, (items, cursor) in enumerate(pages, 1):
            store(region, name, items, stats, bump=False)
            if n % BUMP_EVERY_PAGES == 0:
                _bump(region)
            stats["requests"] = stats.get("requests", 0) + client.requests - before
            before = client.requests
            done = cursor is None
            with session() as db:
                st = db.get(AdsSync, region)
                st.pending = None if done else {**win, "cursor": cursor}
                st.stats = dict(stats)
                db.commit()
    except ads_api.AdsApiError as exc:
        error = str(exc)
        raise
    finally:
        with session() as db:
            st = db.get(AdsSync, region)
            st.stats = dict(stats)
            if error:
                st.state, st.error = "error", error
            else:
                st.state = "idle"
            if done:
                st.newest = win["hi"]
                old = _fmt(now_msk - timedelta(days=KEEP_DAYS))
                db.execute(delete(MarketAd).where(MarketAd.region == region, MarketAd.posted < old))
            if done or error:
                st.finished_at = utcnow()
            st.count = db.scalar(
                select(func.count()).select_from(MarketAd).where(MarketAd.region == region)
            )
            db.commit()
        _bump(region)
    return stats


def request(region: str, *, full: bool = False) -> bool:
    """Кнопка «обновить объявления»: регион вне очереди. `full` — загрузить заново за
    `settings.adsapi_days` дней. Снимает остановку после отказа по ключу. False — выключено."""
    global _fatal
    if not enabled():
        return False
    if full:
        with session() as db:
            st = db.get(AdsSync, region)
            if st is not None:
                st.newest, st.pending = None, None
                db.commit()
    with _lock:
        _wanted[region] = None
        _fatal = None
    return True


def _next_region(now: datetime | None = None) -> str | None:
    """Чья очередь: регион по кнопке; потом ни разу не собранные и с незаконченным окном
    (кто дольше ждёт — первым); потом те, кого пора догрузить."""
    now = now or utcnow()
    with _lock:
        if _wanted:
            return next(iter(_wanted))
    with session() as db:
        regions = sorted(db.scalars(select(TorgiSync.region)).all())
        states = {s.region: s for s in db.scalars(select(AdsSync)).all()}
    first: list[tuple[datetime, str]] = []
    later: list[tuple[datetime, str]] = []
    epoch = datetime.min.replace(tzinfo=UTC)
    for region in regions:
        if ads_api.region_name(region) is None:
            continue
        st = states.get(region)
        if st is None:
            first.append((epoch, region))
            continue
        finished = _aware(st.finished_at)
        if st.state == "error":
            if finished is None or now - finished >= timedelta(minutes=ERROR_RETRY_MIN):
                later.append((finished or epoch, region))
        elif st.pending or finished is None:
            first.append((_aware(st.started_at) or epoch, region))
        elif now - finished >= timedelta(hours=settings.adsapi_hours):
            later.append((finished, region))
    for queue in (first, later):
        if queue:
            return min(queue)[1]
    return None


def run_once(client: ads_api.AdsApiClient, now: datetime | None = None) -> str | None:
    """Один шаг цикла: собрать регион, чья очередь. Возвращает его код или None."""
    global _fatal
    if _fatal:
        return None
    region = _next_region(now)
    if region is None:
        return None
    with _lock:
        _wanted.pop(region, None)
    try:
        sync_region(region, client)
    except ads_api.AdsApiError as exc:
        log.warning("ads-api %s: %s", region, exc)
        if exc.code in ads_api.FATAL_CODES:
            with _lock:
                _fatal = str(exc)
    return region


def start_loop(*, first_delay_s: float = 180.0) -> threading.Thread | None:
    """Фоновый сбор объявлений. Без логина и ключа и в демо-режиме не запускается."""
    global _thread
    if not enabled() or (_thread is not None and _thread.is_alive()):
        return None
    with session() as db:  # «идёт» после рестарта — это прерванный проход: продолжим окно
        for st in db.scalars(select(AdsSync).where(AdsSync.state == "running")):
            st.state = "idle"
        db.commit()

    def loop() -> None:
        time.sleep(first_delay_s)
        client = ads_api.AdsApiClient(
            settings.adsapi_user or "", settings.adsapi_token or "", limit=settings.adsapi_limit
        )
        while True:
            try:
                if run_once(client) is None:
                    time.sleep(LOOP_PAUSE_S)
            except Exception as exc:  # noqa: BLE001 — цикл не должен умереть от одной ошибки
                log.warning("ads-api loop: %s", type(exc).__name__)
                time.sleep(LOOP_PAUSE_S)

    _thread = threading.Thread(target=loop, name="ads-api", daemon=True)
    _thread.start()
    return _thread


def status(region: str) -> dict[str, Any]:
    """Строка состояния для страницы: сколько объявлений, до какого времени, что не так."""
    with session() as db:
        st = db.get(AdsSync, region)
        count = db.scalar(
            select(func.count()).select_from(MarketAd).where(MarketAd.region == region)
        )
    with _lock:
        queued = region in _wanted
    return {
        "enabled": enabled(),
        "known": ads_api.region_name(region) is not None,
        "count": count or 0,
        "state": st.state if st else None,
        "newest": st.newest if st else None,
        "pending": bool(st and st.pending),
        "cursor": (st.pending or {}).get("cursor") if st and st.pending else None,
        "error": st.error if st else None,
        "fatal": _fatal,
        "queued": queued,
        "stats": (st.stats or {}) if st else {},
    }
