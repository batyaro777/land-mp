"""Мониторинг участка (R9): снимок ключевых полей ЕГРН/ЗОУИТ по сохранённому номеру и
уведомление при изменениях (статус, границы, категория/ВРИ, площадь, стоимость, зоны).
Перепроверка — раз в `settings.watch_hours` через живой НСПД, один запрос на номер.

Этап 3 п. 2.5 ТЗ v3: раз в `settings.watch_docs_hours` перечитываются документы администрации
по участку («первоисточники»); новое упоминание участка или квартала в проекте межевания,
постановлении, извещении — уведомление со ссылкой на страницу документа."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from landcheck import evidence
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
    """Короткая подпись зоны без обрезки реестрового номера: «вид (номер)»; у терзон без ПЗЗ
    в НСПД есть только номер — «терзона (04:05-7.124)». describe() из отчёта режет длинные
    названия многоточием и теряет номер (прод 21.10), поэтому собираем сами."""
    out: set[str] = set()
    for z in zones or []:
        label = (z.get("kind") or z.get("name") or "").strip()
        if not label:
            d = str(z.get("describe") or "")
            label = d.split(" (")[0].strip() or "зона"
        label = label[:80].rstrip()
        reg = (z.get("reg_number") or "").strip()
        out.add(f"{label} ({reg})" if reg else label)
    return sorted(out)


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


# --- документы администрации по участку (ТЗ v3, п. 2.5, этап 3) ---------------------------------
def docs_snapshot(ev: dict[str, Any] | None) -> dict[str, Any]:
    """Снимок «первоисточников»: упоминания участка/квартала в документах администрации
    по ссылке на страницу — сравнивать между перечитками."""
    ev = ev or {}
    strong: dict[str, dict[str, Any]] = {}
    for d in ev.get("docs") or []:
        if d.get("kind") not in evidence.STRONG_KINDS:
            continue
        for m in d.get("mentions") or []:
            if m.get("term_kind") not in ("cad", "quarter"):
                continue
            key = m.get("url") or d.get("final_url") or d.get("url") or ""
            if not key or key in strong:
                continue
            where = m.get("label") or (f"стр. {m['page']}" if m.get("page") else None)
            strong[key] = {
                "title": (d.get("title") or key)[:120],
                "kind": d.get("kind"),
                "where": where,
                "term": m.get("term"),
                "quote": (m.get("quote") or "")[:300],
            }
    return {
        "strong": strong,
        "hits": ev.get("hits", 0),
        "checked": ev.get("checked", 0),
        "fetched_ok": ev.get("fetched_ok", 0),
        "checked_at": ev.get("checked_at"),
    }


def docs_diff(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    """Новые упоминания: ссылки на страницы, которых не было в прошлом снимке."""
    seen = set(((old or {}).get("strong") or {}).keys())
    return [{"url": k, **v} for k, v in (new.get("strong") or {}).items() if k not in seen]


def docs_text(cad_num: str, added: list[dict[str, Any]]) -> str:
    parts = []
    for a in added[:3]:
        where = f" ({a['where']})" if a.get("where") else ""
        quote = f": «{a['quote'][:160]}»" if a.get("quote") else ""
        parts.append(f"«{a['title']}»{where}{quote} — {a['url']}")
    more = f" и ещё {len(added) - 3}" if len(added) > 3 else ""
    return (
        f"Участок {cad_num} упомянут в новом документе администрации{more}: "
        + "; ".join(parts)
        + ". Откройте первоисточник и прочитайте пункт: проект межевания, извещение или "
        "постановление может означать распределение, резервирование или изъятие."
    )


def latest_item_any(db, cad_num: str) -> CheckItem | None:
    return db.scalar(
        select(CheckItem).where(CheckItem.cad_num == cad_num).order_by(CheckItem.id.desc()).limit(1)
    )


def run_doc_watches(
    *,
    max_age_h: float = 168,
    now: datetime | None = None,
    fetch_admin_docs=None,
    fetch_evidence=None,
) -> dict[str, Any]:
    """Перечитать документы администрации по участкам под слежением, которые не читались дольше
    max_age_h; первая перечитка — базовый снимок без уведомлений, дальше — уведомление о каждом
    новом упоминании участка или квартала."""
    from landcheck.region import resolve_locality
    from landcheck.web import jobs

    fetch_admin_docs = fetch_admin_docs or jobs.fetch_admin_docs
    fetch_evidence = fetch_evidence or jobs.fetch_evidence
    now = now or datetime.now(UTC)
    with session() as db:
        groups: dict[str, list[int]] = {}
        for w in db.scalars(select(Watch).order_by(Watch.id)):
            checked = _aware(w.docs_checked_at)
            if checked is None or now - checked >= timedelta(hours=max_age_h):
                groups.setdefault(w.cad_num, []).append(w.id)
    result: dict[str, Any] = {"checked": 0, "new": 0, "errors": []}
    for cad_num, ids in groups.items():
        with session() as db:
            item = latest_item_any(db, cad_num)
            parcel = dict((item.data or {}).get("parcel") or {}) if item else {}
        if not parcel:
            result["errors"].append(f"{cad_num}: нет данных участка")
            continue
        try:
            locality = resolve_locality(parcel.get("address"), parcel.get("geometry"))
            admin_docs = fetch_admin_docs(parcel, locality)
            ev = fetch_evidence(parcel, admin_docs, locality)
        except Exception as exc:  # noqa: BLE001 — один участок не должен ронять обход
            log.warning("doc watch %s failed: %s", cad_num, exc)
            result["errors"].append(f"{cad_num}: {type(exc).__name__}")
            continue
        new = docs_snapshot(ev)
        with session() as db:
            for wid in ids:
                w = db.get(Watch, wid)
                if w is None:
                    continue
                old = w.docs or {}
                added = docs_diff(old, new) if old.get("checked_at") else []
                w.docs = {
                    **new,
                    "new": added,
                    "new_at": now.isoformat() if added else old.get("new_at"),
                }
                w.docs_checked_at = now
                result["checked"] += 1
                if added:
                    result["new"] += len(added)
                    mine = latest_item(db, w.user_id, cad_num)
                    url = (
                        f"/checks/{mine.check_id}/items/{cad_num}" if mine else f"/watch#{cad_num}"
                    )
                    notify(db, w.user_id, "watch-docs", docs_text(cad_num, added), url=url)
            db.commit()
    log.info("doc watch run: %s", result)
    return result
