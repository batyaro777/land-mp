"""Свободная публичная земля на территории: кадастровый квартал или окрестность участка.

ТЗ v3, п. 8 (этапы 3–4). Запрос экспертов на зуме 28.10: «сервис, который находит участки,
никем не занятые, которые можно оформить на себя» — аренда или собственность у муниципалитета
без торгов (101-ФЗ для сельхозземли, ст. 39.18 ЗК), через предварительное согласование или
через торги. Раньше блок «Свободная земля рядом» показывал только соседей проверяемого участка;
здесь — все участки территории.

Что делаем с участками из НСПД (слой «Земельные участки из ЕГРН»): оставляем публичные
(муниципальные, государственные) и участки без зарегистрированного права, убираем архивные,
земли общего пользования и то, что гражданам и бизнесу не передаётся или занято сетями,
дорогами, учреждениями (те же правила, что в блоке «Свободная земля рядом»). По оставшимся
отмечаем: выставлен на аукцион (слой НСПД), попадает в проект межевания (может быть
распределён), был на торгах (база лотов).

Участок без зарегистрированного права — пустая форма собственности в НСПД. Так выглядит
неразграниченная государственная земля, то есть главное, что здесь ищут: в квартале
10:20:0060108 (прод, 10.11.2026) из 77 участков 64 частных и 13 без формы, и среди них участок,
который администрация выставляла на аукцион. Но так же выглядит ранее учтённый надел, чьё право
возникло до 1998 года и в реестр не внесено, — поэтому такие строки помечены отдельно.

Поиск по кварталу НСПД ведёт по контуру и возвращает и соседей, касающихся границы: оставляем
только участки с номером этого квартала.

Чего открытые данные не дают: действующую аренду. Поэтому «свободен» здесь значит «по открытым
данным препятствий нет» — первым шагом в карточке «как оформить» всегда стоит выписка ЕГРН.
"""

from __future__ import annotations

from typing import Any

from landcheck import torgi_scan
from landcheck.location import (
    HOWTO_AGRI,
    HOWTO_OTHER,
    HOWTO_SETTLEMENT,
    NO_RIGHT,
    NOTE_NO_RIGHT,
    NOTE_NO_RIGHT_PRIOR,
    _get,
    _unavailable,
    auction_cads,
    is_prior,
    ownership_class,
)

MAX_ITEMS = 500
SOURCE = (
    "НСПД: земельные участки из ЕГРН, участки на аукционе, участки по проектам межевания. "
    "Аренду и права, возникшие до 1998 года, открытые данные не показывают — их видно только "
    "в выписке ЕГРН и в администрации"
)
RADII_M = (500, 1000, 2000)


def _opts(feature: dict[str, Any]) -> dict[str, Any]:
    return (feature.get("properties") or {}).get("options") or {}


def _shape(feature: dict[str, Any]):
    geom = feature.get("geometry")
    if not geom:
        return None
    try:
        from shapely.geometry import shape

        g = shape(geom)
        return g if not g.is_empty else None
    except Exception:  # noqa: BLE001 — кривая геометрия: считаем, что контура нет
        return None


def howto_key(category: str | None) -> str:
    cat = (category or "").lower().replace("ё", "е")
    if "сельскохоз" in cat:
        return "agri"
    if "населенных пунктов" in cat:
        return "settlement"
    return "other"


HOWTO = {"agri": HOWTO_AGRI, "settlement": HOWTO_SETTLEMENT, "other": HOWTO_OTHER}
HOWTO_LABEL = {
    "agri": "сельхозземля",
    "settlement": "земли населённых пунктов",
    "other": "иная категория",
}


def analyze(
    landplots: list[dict[str, Any]],
    auction: list[dict[str, Any]] | None = None,
    pmt: list[dict[str, Any]] | None = None,
    *,
    quarter: str | None = None,
) -> dict[str, Any]:
    """Участки территории → кандидаты без видимых препятствий, со счётчиками по отсеву.
    Вход — признаки слоёв НСПД как их отдаёт провайдер (`properties.options`, `geometry`).
    `quarter` — оставить только участки этого квартала (поиск по контуру отдаёт и соседей)."""
    on_auction = auction_cads(auction)
    pmt_shapes = [g for f in pmt or [] if (g := _shape(f)) is not None]
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    counts = {
        "total": 0,
        "outside": 0,
        "private": 0,
        "no_right": 0,
        "archived": 0,
        "common": 0,
        "unavailable": 0,
    }
    reasons: dict[str, int] = {}
    for f in landplots:
        o = _opts(f)
        cad = o.get("cad_num")
        if not cad or cad in seen:
            continue
        seen.add(cad)
        if quarter and not str(cad).startswith(quarter + ":"):
            counts["outside"] += 1
            continue
        counts["total"] += 1
        own = str(_get(o, "ownership_type", default="") or "")
        own_class = ownership_class(own)
        if own_class == "private":
            counts["private"] += 1
            continue
        if own_class == "unregistered":
            counts["no_right"] += 1
        vri = str(_get(o, "permitted_use_established_by_document", default="") or "")
        status = str(_get(o, "status", default="") or "")
        if "архив" in status.lower():
            counts["archived"] += 1
            continue
        if "общего пользования" in vri.lower():
            counts["common"] += 1
            continue
        category = str(_get(o, "land_record_category_type", default="") or "")
        why = _unavailable(category, vri)
        if why:
            counts["unavailable"] += 1
            reasons[why.split(" (")[0]] = reasons.get(why.split(" (")[0], 0) + 1
            continue
        area = _get(o, "specified_area", "declared_area", "area", "land_record_area")
        cost = _get(o, "cost_value")
        g = _shape(f)
        point = None
        in_pmt = False
        if g is not None:
            c = g.centroid
            point = [round(c.y, 6), round(c.x, 6)]
            in_pmt = any(g.intersects(p) for p in pmt_shapes)
        items.append(
            {
                "cad_num": cad,
                "category": category or None,
                "vri": vri or None,
                "group": torgi_scan.vri_group(vri, None, category),
                "how": howto_key(category),
                "area_m2": float(area) if isinstance(area, int | float) else None,
                "ownership": own or NO_RIGHT,
                "own_class": own_class,
                "prior": is_prior(status),
                "own_note": None
                if own_class == "public"
                else (NOTE_NO_RIGHT_PRIOR if is_prior(status) else NOTE_NO_RIGHT),
                "status": status or None,
                "address": _get(o, "readable_address"),
                "cad_cost": float(cost) if isinstance(cost, int | float) else None,
                "on_auction": cad in on_auction,
                "in_pmt": in_pmt,
                "point": point,
            }
        )
    # Сначала то, что надёжнее: публичная собственность, затем учтённые без права, затем ранее
    # учтённые без права (у них может найтись хозяин).
    items.sort(
        key=lambda i: (i["how"], i["own_class"] != "public", i["prior"], -(i["area_m2"] or 0))
    )
    return {
        "counts": {
            **counts,
            "public_free": len(items),
            "free_public": sum(1 for i in items if i["own_class"] == "public"),
            "free_no_right": sum(
                1 for i in items if i["own_class"] == "unregistered" and not i["prior"]
            ),
            "free_prior": sum(1 for i in items if i["own_class"] == "unregistered" and i["prior"]),
        },
        "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
        "items": items[:MAX_ITEMS],
        "truncated": len(items) > MAX_ITEMS,
        "source": SOURCE,
    }


def filter_items(
    items: list[dict[str, Any]],
    *,
    how: str | None = None,
    group: str | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    hide_busy: bool = False,
    own: str | None = None,
) -> list[dict[str, Any]]:
    """Фильтр списка на странице: категория, назначение, площадь; `hide_busy` убирает то, что
    уже на аукционе, в проекте межевания или сдано с торгов; `own` — `public` (собственность
    названа публичной), `no_right` (учтённые без зарегистрированного права), `prior` (ранее
    учтённые без права)."""
    out = []
    for i in items:
        if how and i.get("how") != how:
            continue
        if own and own_kind(i) != own:
            continue
        if group and i.get("group") != group:
            continue
        area = i.get("area_m2")
        if area_min_m2 and (not area or area < area_min_m2):
            continue
        if area_max_m2 and (not area or area > area_max_m2):
            continue
        if hide_busy and (
            i.get("on_auction") or i.get("in_pmt") or (i.get("torgi") or {}).get("busy")
        ):
            continue
        out.append(i)
    return out


def own_kind(item: dict[str, Any]) -> str:
    """Группа строки для фильтра и подписи: `public`, `no_right`, `prior`. У поисков, сохранённых
    до 10.11.2026, класса нет — там были только публичные."""
    if (item.get("own_class") or "public") == "public":
        return "public"
    return "prior" if item.get("prior") else "no_right"


OWN_KIND_LABEL = {
    "public": "публичная собственность",
    "no_right": "право не зарегистрировано",
    "prior": "ранее учтённые без права",
}


def torgi_note(lot: dict[str, Any]) -> dict[str, Any]:
    """Что значит для «свободности» последний лот участка в базе торгов."""
    status, kind = lot.get("status"), lot.get("kind")
    date = lot.get("auction_date") or lot.get("bidd_end") or lot.get("published")
    if torgi_scan.is_notice(lot):
        # Извещение о предоставлении (ст. 39.18 ЗК): участок уже просит заявитель.
        if status in torgi_scan.ACTIVE:
            text = (
                "на участок подано заявление, идёт извещение о предоставлении (ст. 39.18): "
                "до конца срока можно подать своё — тогда будет аукцион"
            )
        elif status == "CANCELED":
            text = "извещение о предоставлении отозвано"
        else:
            text = "по участку было извещение о предоставлении (ст. 39.18) — скорее всего, занят"
        return {
            "text": text,
            "busy": status != "CANCELED",
            "date": date,
            "lot_id": lot.get("id"),
            "kind": kind,
            "final": status not in torgi_scan.ACTIVE,
        }
    outcome = torgi_scan._outcome(lot)
    if status in torgi_scan.ACTIVE:
        text, busy = "идут торги", True
    elif status == "SUCCEED":
        text = "сдан в аренду с торгов" if kind == "rent" else "продан с торгов"
        busy = True
    elif outcome == "single":
        text = "торги с одной заявкой: договор с единственным участником — скорее всего, занят"
        busy = True
    elif outcome == "none":
        text, busy = "выставлялся на торги и не ушёл", False
    elif status == "FAILED":
        text, busy = "торги не состоялись, итог не прочитан", False
    else:
        text, busy = "торги отменены", False
    return {
        "text": text,
        "busy": busy,
        "date": date,
        "lot_id": lot.get("id"),
        "kind": kind,
        # торги уже закончились: отметка «на аукционе» из слоя НСПД устарела
        "final": status not in torgi_scan.ACTIVE,
    }


XLSX_COLUMNS = (
    "Кадастровый номер",
    "Категория",
    "Вид разрешённого использования",
    "Площадь, сот.",
    "Форма собственности",
    "Что это значит",
    "Кадастровая стоимость, ₽",
    "Адрес",
    "На аукционе (НСПД)",
    "В проекте межевания",
    "Торги по участку",
    "Как оформить",
)


def xlsx_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            i["cad_num"],
            i.get("category"),
            i.get("vri"),
            round(i["area_m2"] / 100, 2) if i.get("area_m2") else None,
            i.get("ownership"),
            i.get("own_note") or "",
            i.get("cad_cost"),
            i.get("address"),
            "да" if i.get("on_auction") else "",
            "да" if i.get("in_pmt") else "",
            (i.get("torgi") or {}).get("text") or "",
            HOWTO[i["how"]]["source"],
        ]
        for i in items
    ]


def to_xlsx(items: list[dict[str, Any]], path: Any, *, title: str = "Свободная земля") -> Any:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append(list(XLSX_COLUMNS))
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in xlsx_rows(items):
        ws.append(row)
    for col, width in {1: 22, 2: 30, 3: 40, 5: 28, 6: 50, 8: 50, 11: 34, 12: 34}.items():
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width
    ws.freeze_panes = "B2"
    howto = wb.create_sheet("Как оформить")
    for key in ("agri", "settlement", "other"):
        card = HOWTO[key]
        howto.append([card["title"]])
        howto.cell(row=howto.max_row, column=1).font = Font(bold=True)
        for n, step in enumerate(card["steps"], 1):
            howto.append([f"{n}. {step}"])
        howto.append([f"Основание: {card['source']}"])
        howto.append([])
    howto.column_dimensions["A"].width = 120
    wb.save(path)
    return path
