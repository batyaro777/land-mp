"""Фоновая работа «Горячих предложений» (ТЗ v5, этап 3; зум 18.11.2026).

Три вещи, которые нельзя делать в запросе страницы:

1. Карточки кандидатов вне общей очереди. Задаток, шаг аукциона, градплан и жалобы есть
   только в карточке лота, а общий сбор читает по 200 карточек идущих лотов за проход
   региона (прод, 19.11.2026: в Московской области не прочитано 994 карточки из 1519 идущих
   продаж, у половины лотов топа не было задатка). Лотов-кандидатов десятки — им карточку
   читаем сразу, тем же клиентом и с той же паузой между запросами.
2. Прогрев расчёта. Первое открытие «Горячих» после рестарта считало все регионы 12 секунд.
3. Утренняя сводка (Антон, 01:11: «утром проснулись — система оповестила: нашла объявления
   под эти критерии»): раз в сутки в выбранный час — новые лоты по сохранённым критериям и
   сроки по лотам, взятым в работу. Нет нового — сообщение не шлём.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from landcheck import ruproxy, torgi, torgi_calc, torgi_hot, torgi_scan
from landcheck.web import notify, torgi_sync
from landcheck.web.config import settings
from landcheck.web.db import TorgiLot, TorgiMark, User, session

log = logging.getLogger(__name__)

CARDS_PER_RUN = 40  # карточек за один заход: при паузе 6 с это четыре минуты
CARD_FAILURES = 3  # столько неудач подряд — площадка не отвечает, заход прекращаем
DIGEST_MAX = 10  # лотов в одном сообщении: длиннее в Telegram не читают
DUE_DAYS = 3  # за сколько дней до конца приёма заявок напоминать о лоте «в работе»
SKIP_REASONS = (
    "цена рынка завышена",
    "место неликвидное",
    "обременения или зоны",
    "нет подъезда или далеко",
    "не моя ниша",
    "другое",
)

_cards_lock = threading.Lock()
_cards_running = False
_cards_queue: dict[str, None] = {}


def _live() -> bool:
    """Фоновые потоки — только на боевом сервере: не в демо на фикстурах и не без шлюза."""
    return settings.fixtures is None and ruproxy.enabled()


# --- карточки кандидатов -----------------------------------------------------------------------
NEED_CARD = (*torgi.ACTIVE, "FAILED")  # идущие лоты и не состоявшиеся торги без итога
HISTORY_CARDS = 80  # столько непрочитанных итогов в кварталах кандидатов берём за раз


def unread_outcomes(quarters: Iterable[str | None], limit: int = HISTORY_CARDS) -> list[str]:
    """Не состоявшиеся торги в кварталах кандидатов, у которых карточка не прочитана. Без неё
    не видно, был на торгах единственный участник или заявок не подали вовсе, — а от этого
    зависит, есть ли в квартале спрос (прод, 20.11.2026: у лота класса A итог был известен
    по одним торгам квартала из восьми). Свежие — первыми."""
    qs = list(dict.fromkeys(q for q in quarters if q))[:300]
    if not qs:
        return []
    with session() as db:
        q = (
            select(TorgiLot.id)
            .where(
                TorgiLot.quarter.in_(qs),
                TorgiLot.status == "FAILED",
                TorgiLot.card_at.is_(None),
            )
            .order_by(TorgiLot.published.desc(), TorgiLot.id)
            .limit(limit)
        )
        return list(db.scalars(q))


def missing_cards(lot_ids: Iterable[str | None]) -> list[str]:
    """Лоты из списка, у которых карточка ещё не прочитана: идущие и не состоявшиеся
    (порядок сохраняется). Состоявшимся торгам карточка для итога не нужна."""
    todo = list(dict.fromkeys(i for i in lot_ids if i))
    out: list[str] = []
    with session() as db:
        for i in range(0, len(todo), 500):
            part = todo[i : i + 500]
            q = select(TorgiLot.id).where(
                TorgiLot.id.in_(part), TorgiLot.card_at.is_(None), TorgiLot.status.in_(NEED_CARD)
            )
            unread = set(db.scalars(q))
            out += [x for x in part if x in unread]
    return out


def fill_cards(
    lot_ids: Iterable[str | None], *, api: Any = None, now: datetime | None = None
) -> int:
    """Прочитать карточки лотов без карточки (не больше `CARDS_PER_RUN`). Возвращает, сколько
    прочитано."""
    todo = missing_cards(lot_ids)[:CARDS_PER_RUN]
    if not todo:
        return 0
    own = api is None
    if own:
        api = torgi.TorgiClient()
    stats = {"cards": 0}
    failures = 0
    try:
        for lot_id in todo:
            with session() as db:
                lot = db.get(TorgiLot, lot_id)
                region = lot.region if lot is not None else None
            if not region:
                continue
            if torgi_sync._read_card(api, region, lot_id, now or datetime.now(UTC), stats):
                failures = 0
            else:
                failures += 1
                if failures >= CARD_FAILURES:
                    break
    finally:
        if own:
            api.close()
    return stats["cards"]


def start_cards(lot_ids: Iterable[str | None]) -> int:
    """Фоном, один поток на сервер, общая очередь (как у проверки обременений). Возвращает,
    сколько лотов добавлено в очередь."""
    global _cards_running
    if not _live():
        return 0
    todo = missing_cards(lot_ids)
    with _cards_lock:
        fresh = [i for i in todo if i not in _cards_queue]
        for i in fresh:
            _cards_queue[i] = None
        if not _cards_queue or _cards_running:
            return len(fresh)
        _cards_running = True

    def job() -> None:
        global _cards_running
        while True:
            with _cards_lock:
                batch = list(_cards_queue)[:CARDS_PER_RUN]
                if not batch:
                    _cards_running = False
                    return
            try:
                fill_cards(batch)
            except Exception as exc:  # noqa: BLE001 — фоновое чтение не должно ронять сервер
                log.warning("torgi hot cards: %s", exc)
            finally:
                with _cards_lock:
                    for i in batch:
                        _cards_queue.pop(i, None)

    threading.Thread(target=job, name="torgi-hot-cards", daemon=True).start()
    return len(fresh)


def cards_queued() -> int:
    with _cards_lock:
        return len(_cards_queue)


# --- прогрев расчёта ---------------------------------------------------------------------------
WARM_CANDIDATES = 40  # по стольким лучшим лотам готовим карточку, обременения и карту заранее


def warm_candidates() -> int:
    """Лучшим лотам при критериях по умолчанию заранее читаем карточку, обременения и карту:
    утренняя сводка уходит до того, как кто-то откроет страницу, а без этих данных класс
    лота и строка сводки неполны. Возвращает, сколько лотов поставлено в очередь карты."""
    from landcheck.web import torgi_nearby, torgi_views, torgi_zones

    result, _names = torgi_views._scan_all(torgi_scan.ScanParams())
    res = torgi_hot.select(result["rows"])
    top = (res["hot"] + res["weak"])[:WARM_CANDIDATES]
    torgi_zones.start_fill([i["row"]["id"] for i in top if i["zones"][0] == "unknown"])
    start_cards(
        [
            *(i["row"]["id"] for i in top if not i["row"].get("card")),
            *unread_outcomes(i["row"].get("quarter") for i in top),
        ]
    )
    return torgi_nearby.start_fill([i["row"]["id"] for i in top if i["place"] is None])


def start_warm_loop(*, first_delay_s: float = 20.0, period_s: float = 180.0):
    """Держим расчёт всех собранных регионов тёплым: страница берёт его из памяти, а
    пересчитывается только регион, в котором с прошлого раза что-то записано."""
    if not _live():
        return None

    def loop() -> None:
        time.sleep(first_delay_s)
        while True:
            try:
                from landcheck.web import torgi_views

                t0 = time.monotonic()
                torgi_views._scan_all(torgi_scan.ScanParams())
                took = time.monotonic() - t0
                # проверка на завершённых торгах: по ней же собирается «вернутся на торги»
                torgi_views.backtest_start(torgi_views._synced_regions())
                warm_candidates()
                if took > 5:
                    log.info("горячие: расчёт всех регионов прогрет за %.1f с", took)
            except Exception as exc:  # noqa: BLE001 — прогрев не должен ронять сервер
                log.warning("torgi hot warm: %s", exc)
            time.sleep(period_s)

    thread = threading.Thread(target=loop, name="torgi-hot-warm", daemon=True)
    thread.start()
    return thread


# --- отметки -----------------------------------------------------------------------------------
def marks_of(user_id: int) -> dict[str, tuple[str, str | None]]:
    """Отметки пользователя: лот → (`work` | `skip`, причина)."""
    with session() as db:
        rows = db.execute(
            select(TorgiMark.lot_id, TorgiMark.mark, TorgiMark.reason).where(
                TorgiMark.user_id == user_id
            )
        ).all()
    return {lot_id: (mark, reason) for lot_id, mark, reason in rows}


# --- утренняя сводка ---------------------------------------------------------------------------
def digest_settings(crit: dict[str, Any]) -> tuple[bool, int, float]:
    """Включена ли сводка, час отправки и пояс пользователя (часов от UTC)."""

    def num(key: str, default: float) -> float:
        try:
            return float(str(crit.get(key) or default).replace(",", "."))
        except ValueError:
            return default

    hour = int(min(max(num("dh", 9), 0), 23))
    tz = min(max(num("dz", 7), -12), 14)
    return crit.get("dg") == "1", hour, tz


def digest_due(crit: dict[str, Any], state: dict[str, Any] | None, now: datetime) -> str | None:
    """День (по часам пользователя), за который пора слать сводку, или None: сводка
    выключена, час ещё не наступил либо за этот день уже отправляли."""
    on, hour, tz = digest_settings(crit)
    if not on:
        return None
    local = now + timedelta(hours=tz)
    if local.hour < hour:
        return None
    day = local.date().isoformat()
    return None if (state or {}).get("day") == day else day


def auction_of(row: dict[str, Any]) -> str:
    """Аукцион лота — номер извещения; нет номера — лот считается отдельным аукционом."""
    return row.get("notice") or row["id"]


def _count(n: int, one: str, few: str, many: str) -> str:
    k = n % 100
    if 11 <= k <= 14:
        return f"{n} {many}"
    return f"{n} {one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many}"


def _place_fallback(row: dict[str, Any]) -> str:
    """Место лота, когда у кадастрового района нет названия из адресов (прод, 19.11.2026: в
    сводке вместо района стояло «18:26» — это город, слова «район» в его адресах нет): адрес
    до населённого пункта, а без адреса — регион и код района."""
    from landcheck.region import locality_from_address
    from landcheck.regions import CADASTRAL_REGIONS

    loc = locality_from_address(row.get("address"))
    if loc:
        return loc
    region = CADASTRAL_REGIONS.get(row.get("region") or "", "")
    return ", ".join(
        x for x in (region, f"район {row['district']}" if row.get("district") else "") if x
    )


def _lot_line(n: int, item: dict[str, Any], names: dict[str, str], base: str, more: int = 0) -> str:
    r = item["row"]
    e = item["sc"]["expected"]
    place = names.get(r.get("district") or "") or _place_fallback(r)
    area = f"{r['area_m2'] / 100:.1f} сот." if r.get("area_m2") else ""
    till = f"{r['bidd_end'][8:10]}.{r['bidd_end'][5:7]}" if r.get("bidd_end") else "—"
    head = ", ".join(x for x in (place, area, torgi_scan.GROUPS.get(r.get("vri_group") or "")) if x)
    cut = f", не выше {torgi_calc.rub(item['cutoff'])}" if item["cutoff"] else ""
    also = (
        f"\nв этом аукционе ещё {_count(more, 'лот', 'лота', 'лотов')} под критерии" if more else ""
    )
    return (
        f"{n}. {head} — вход {torgi_calc.rub(e['entry'])}{cut}, "
        f"{round((e['annual'] or 0) * 100)} % годовых, класс {item['cls']}, "
        f"заявки до {till}{also}\n"
        f"{base}/tools/torgi-lot/{r['id']}/invest"
    )


def build_digest(
    user_id: int, crit: dict[str, Any], state: dict[str, Any] | None
) -> tuple[str | None, dict[str, Any]]:
    """Текст сводки и новое состояние: какие лоты теперь считаются показанными (`seen`) и за
    какими участками без заявок следим (`returning`). Текста нет (None), когда нет ни новых
    лотов, ни лотов «в работе» с близким сроком, ни участков, вернувшихся на торги."""
    from landcheck.web import torgi_views as tv

    hp, sp = tv._hot_params(crit)
    region = None if tv._is_all(crit.get("region")) else tv._region_code(crit.get("region"))
    result, names = tv._region_scan(region, sp) if region else tv._scan_all(sp)
    res = torgi_hot.select(result["rows"], sp, hp)
    marks = marks_of(user_id)
    seen = set((state or {}).get("seen") or [])
    hot_ids = [i["row"]["id"] for i in res["hot"]]
    # Прод, 19.11.2026: все пять строк пробной сводки оказались лотами одного аукциона.
    # Строка сводки — аукцион: лучший лот и число остальных, прошедших отбор.
    groups: dict[str, list[dict[str, Any]]] = {}
    for i in res["hot"]:
        lot_id = i["row"]["id"]
        if lot_id not in seen and lot_id not in marks:  # убранные и «в работе» не новые
            groups.setdefault(auction_of(i["row"]), []).append(i)
    picked = list(groups.values())[: min(hp.top, DIGEST_MAX)]
    fresh = [g[0] for g in picked]
    fresh_ids = [i["row"]["id"] for g in picked for i in g]
    by_id = {r["id"]: r for r in result["rows"]}
    due = []
    for lot_id, (mark, _reason) in marks.items():
        row = by_id.get(lot_id)
        days = row["s"].get("days_left") if row else None
        if mark == "work" and days is not None and 0 <= days <= DUE_DAYS:
            due.append((days, row))
    due.sort(key=lambda x: x[0])
    # показанными считаем то, что уже показывали и что ещё идёт, плюс сегодняшние
    kept = [i for i in hot_ids if i in seen] + fresh_ids
    # участки, за которыми следили после торгов без заявок, снова выставлены
    watched = set((state or {}).get("returning") or [])
    back = [r for r in result["rows"] if r.get("cad_num") in watched]
    ready = tv.backtest_ready([region] if region else None)
    now_watch = sorted(
        {x["cad_num"] for raw in ready.values() for x in raw.get("returning", [])}
        | (watched - {r["cad_num"] for r in back} if not ready else set())
    )
    new_state = {"seen": kept[-500:], "returning": now_watch[:1000]}
    if not fresh and not due and not back:
        return None, new_state
    base = settings.base_url or ""
    lines: list[str] = []
    if fresh:
        where = (
            f" в {_count(len(picked), 'аукционе', 'аукционах', 'аукционах')}"
            if len(fresh_ids) > len(picked)
            else ""
        )
        lines.append(
            f"Новых лотов под ваши критерии: {len(fresh_ids)}{where} "
            f"(всего проходит {len(hot_ids)})"
        )
        lines += [_lot_line(n, g[0], names, base, len(g) - 1) for n, g in enumerate(picked, 1)]
    if due:
        if lines:
            lines.append("")
        lines.append("В работе, приём заявок заканчивается:")
        for days, row in due:
            left = "сегодня" if days == 0 else f"осталось {days} дн."
            lines.append(
                f"• {row.get('cad_num') or row['id']} — до "
                f"{row['bidd_end'][8:10]}.{row['bidd_end'][5:7]}, {left}"
            )
    if back:
        if lines:
            lines.append("")
        lines.append("Снова на торгах — участки, на которые раньше не было заявок:")
        for row in back[:DIGEST_MAX]:
            till = f"{row['bidd_end'][8:10]}.{row['bidd_end'][5:7]}" if row.get("bidd_end") else "—"
            lines.append(
                f"• {row['cad_num']} — старт {torgi_calc.rub(row.get('price_start') or 0)}, "
                f"заявки до {till}\n{base}/tools/torgi-lot/{row['id']}/invest"
            )
    return "\n".join(lines), new_state


def run_digests(now: datetime | None = None) -> int:
    """Отправить сводки всем, кому пора. Возвращает, сколько сообщений ушло."""
    now = now or datetime.now(UTC)
    with session() as db:
        users = [
            (u.id, dict(u.hot_criteria or {}), dict(u.hot_digest or {}))
            for u in db.scalars(select(User).where(User.is_admin | User.pro_tools))
        ]
    sent = 0
    for user_id, crit, state in users:
        day = digest_due(crit, state, now)
        if day is None:
            continue
        try:
            text, new_state = build_digest(user_id, crit, state)
        except Exception as exc:  # noqa: BLE001 — сбой одного пользователя не мешает остальным
            log.warning("torgi hot digest for user %s: %s", user_id, exc)
            continue
        with session() as db:
            user = db.get(User, user_id)
            if user is None:
                continue
            user.hot_digest = {"day": day, **new_state}
            if text:
                notify.notify(db, user_id, "hot", text, "/tools/torgi-hot")
                sent += 1
            db.commit()
    return sent


def start_digest_loop(*, first_delay_s: float = 90.0, period_s: float = 600.0):
    """Раз в десять минут смотрим, кому пора отправить утреннюю сводку."""
    if not _live():
        return None

    def loop() -> None:
        time.sleep(first_delay_s)
        while True:
            try:
                n = run_digests()
                if n:
                    log.info("горячие: утренних сводок отправлено %s", n)
            except Exception as exc:  # noqa: BLE001 — сводка не должна ронять сервер
                log.warning("torgi hot digest: %s", exc)
            time.sleep(period_s)

    thread = threading.Thread(target=loop, name="torgi-hot-digest", daemon=True)
    thread.start()
    return thread
