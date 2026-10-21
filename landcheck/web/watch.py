"""Мониторинг участка (R9): снимок ключевых полей ЕГРН/ЗОУИТ по сохранённому номеру и
уведомление при изменениях (статус, границы, категория/ВРИ, площадь, стоимость, зоны).
Перепроверка — раз в `settings.watch_hours` через живой НСПД, один запрос на номер."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from landcheck.models import Listing, Request
from landcheck.pipeline import check_batch
from landcheck.web.db import Check, CheckItem, Watch, session
from landcheck.web.notify import notify
from landcheck.web.serialize import result_to_dict

log = logging.getLogger(__name__)

FIELDS: tuple[tuple[str, str], ...] = (
    ("found", "участок в НСПД"),
    ("status", "статус"),
    ("category", "категория"),
    ("permitted_use", "ВРИ"),
    ("area_m2", "площадь, м²"),
    ("has_borders", "границы в ЕГРН"),
    ("cadastral_cost_rub", "кадастровая стоимость, ₽"),
    ("ownership_type", "форма собственности"),
    ("zouit", "ЗОУИТ"),
    ("territorial_zones", "терзона ПЗЗ"),
)


def _zone_names(zones: list[dict[str, Any]] | None) -> list[str]:
    """Как в таблице отчёта: «вид · название (реестровый номер)»; у терзон без ПЗЗ в НСПД
    есть только слой и номер — тогда «Территориальные зоны (04:05-7.124)»."""
    return sorted(
        {
            (z.get("describe") or z.get("kind") or z.get("name") or z.get("reg_number") or "")
            for z in zones or []
        }
    )


def snapshot(parcel: dict[str, Any]) -> dict[str, Any]:
    """Ключевые поля карточки (из сериализованного parcel отчёта или результата проверки)."""
    return {
        "found": bool(parcel.get("found")),
        "status": parcel.get("status"),
        "category": parcel.get("category"),
        "permitted_use": parcel.get("permitted_use"),
        "area_m2": parcel.get("specified_area_m2")
        or parcel.get("area_m2")
        or parcel.get("declared_area_m2"),
        "has_borders": bool(parcel.get("has_borders")),
        "cadastral_cost_rub": parcel.get("cadastral_cost_rub"),
        "ownership_type": parcel.get("ownership_type"),
        "zouit": _zone_names(parcel.get("zouit")),
        "territorial_zones": _zone_names(parcel.get("territorial_zones")),
    }


def _fmt(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, list):
        return ", ".join(v) if v else "нет"
    if isinstance(v, (int, float)):
        return f"{v:,.0f}".replace(",", " ")
    return str(v)


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, str]]:
    """Что изменилось между снимками: только поля, которые были в старом снимке."""
    out: list[dict[str, str]] = []
    for key, label in FIELDS:
        if key not in old:
            continue
        a, b = old.get(key), new.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            equal = abs(float(a) - float(b)) < 0.5
        else:
            equal = a == b
        if not equal:
            out.append({"field": label, "old": _fmt(a), "new": _fmt(b)})
    return out


def diff_text(cad_num: str, changes: list[dict[str, str]]) -> str:
    body = "; ".join(f"{c['field']}: {c['old']} → {c['new']}" for c in changes)
    return f"Изменения по участку {cad_num}: {body}"


def latest_item(db, user_id: int, cad_num: str) -> CheckItem | None:
    return db.scalar(
        select(CheckItem)
        .join(Check, Check.id == CheckItem.check_id)
        .where(Check.user_id == user_id, CheckItem.cad_num == cad_num)
        .order_by(CheckItem.id.desc())
        .limit(1)
    )


def get_watch(db, user_id: int, cad_num: str) -> Watch | None:
    return db.scalar(select(Watch).where(Watch.user_id == user_id, Watch.cad_num == cad_num))


def is_watched(db, user_id: int, cad_num: str) -> bool:
    return get_watch(db, user_id, cad_num) is not None


def add_watch(db, user_id: int, cad_num: str, parcel: dict[str, Any]) -> Watch:
    w = get_watch(db, user_id, cad_num)
    if w is None:
        w = Watch(user_id=user_id, cad_num=cad_num, snapshot=snapshot(parcel))
        db.add(w)
        db.flush()
    return w


def remove_watch(db, user_id: int, cad_num: str) -> bool:
    w = get_watch(db, user_id, cad_num)
    if w is None:
        return False
    db.delete(w)
    db.flush()
    return True


def watches_of(db, user_id: int) -> list[Watch]:
    return list(db.scalars(select(Watch).where(Watch.user_id == user_id).order_by(Watch.id.desc())))


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def run_watches(
    *, max_age_h: float = 24, provider=None, now: datetime | None = None
) -> dict[str, Any]:
    """Перепроверить участки, которые не проверялись дольше max_age_h: один запрос на номер,
    снимок сравнить, при изменениях — уведомление владельцу слежения. НСПД не ответила (R00) —
    снимок не трогаем."""
    from landcheck.web import jobs

    now = now or datetime.now(UTC)
    with session() as db:
        groups: dict[str, list[int]] = {}
        for w in db.scalars(select(Watch).order_by(Watch.id)):
            checked = _aware(w.checked_at)
            if checked is None or now - checked >= timedelta(hours=max_age_h):
                groups.setdefault(w.cad_num, []).append(w.id)
    result: dict[str, Any] = {"checked": 0, "changed": 0, "errors": []}
    if not groups:
        return result
    provider = provider or jobs.build_provider()
    try:
        for cad_num, ids in groups.items():
            try:
                batch = check_batch(
                    [Listing(cad_num=cad_num)], Request(), provider, None, refresh=True, workers=1
                )
                data = result_to_dict(batch.results[0])
            except Exception as exc:  # noqa: BLE001 — один участок не должен ронять обход
                log.warning("watch %s failed: %s", cad_num, exc)
                result["errors"].append(f"{cad_num}: {type(exc).__name__}")
                continue
            if any(f.get("rule_id") == "R00" for f in data.get("flags", [])):
                result["errors"].append(f"{cad_num}: НСПД не ответила")
                continue
            new = snapshot(data.get("parcel") or {})
            with session() as db:
                for wid in ids:
                    w = db.get(Watch, wid)
                    if w is None:
                        continue
                    changes = diff(w.snapshot or {}, new)
                    w.checked_at = now
                    result["checked"] += 1
                    if changes:
                        w.snapshot, w.changed_at, w.last_diff = new, now, changes
                        result["changed"] += 1
                        notify(
                            db,
                            w.user_id,
                            "watch",
                            diff_text(cad_num, changes),
                            url=f"/watch#{cad_num}",
                        )
                db.commit()
    finally:
        close = getattr(provider, "close", None)
        if close:
            close()
    log.info("watch run: %s", result)
    return result
