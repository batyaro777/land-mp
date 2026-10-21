"""Статический контент отчёта: чек-лист покупателя и ценовой ориентир.

Чек-лист — стартовая версия по итогам встречи 09.10; правки Анастасии вносятся сюда.
"""

from __future__ import annotations

from typing import Any

from landcheck.models import SOTKA_M2, Level, Purpose

CHECKLIST: list[dict[str, Any]] = [
    {
        "title": "Документы, которые запросить у продавца",
        "items": [
            "Выписка ЕГРН об объекте не старше 30 дней: собственник, обременения, ограничения, границы",
            "Выписка о переходе прав: сколько раз и когда менялся собственник за 5 лет",
            "Правоустанавливающий документ: договор, свидетельство о наследстве, акт органа власти",
            "Межевой план и акт согласования границ с соседями",
            "Нотариальное согласие супруга, если участок куплен в браке",
            "Справка об отсутствии долгов по земельному налогу и взносам СНТ",
            "Паспорт продавца; при представителе — нотариальная доверенность с правом продажи",
        ],
    },
    {
        "title": "Наследство",
        "items": [
            "Как участок попал к продавцу: покупка, дарение, наследство, приватизация",
            "Если наследство — дата смерти наследодателя и дата свидетельства; прошло ли 3 года",
            "Были ли другие наследники первой очереди и отказы от наследства",
            "Есть ли судебные споры по участку: проверить по ФИО продавца на сайте суда",
        ],
    },
    {
        "title": "Границы и межевание",
        "items": [
            "Границы установлены в ЕГРН: без этого с 01.03.2025 сделку не зарегистрируют",
            "Забор совпадает с границами по выписке; нет захвата соседней земли или дороги",
            "Площадь по документам совпадает с площадью в объявлении",
            "Нет споров с соседями по границе",
        ],
    },
    {
        "title": "Что проверить на месте",
        "items": [
            "Подъезд: чья дорога, есть ли зимнее содержание, не проходит ли через чужой участок",
            "Электричество: технические условия, выделенная мощность, расстояние до опоры",
            "Газ, вода, канализация: что подведено фактически, а не «по плану»",
            "Рельеф и вода: следы подтопления весной, высокий уровень грунтовых вод, низина",
            "Соседство: свалка, кладбище, промзона, ферма, трасса, ЛЭП в радиусе 1–3 км",
            "Строения без регистрации на участке и у соседей по границе",
            "Ограничения из ЗОУИТ в отчёте: охранные зоны сетей, санитарные зоны",
        ],
    },
]

PRICE_DISCLAIMER = "Справочно. Не является отчётом об оценке."


def price_orientation(item: dict[str, Any], median_pps: float | None) -> dict[str, Any] | None:
    """Диапазон за сотку: кадастровая стоимость и медиана подборки."""
    p = item["parcel"]
    area = p.get("area_m2")
    points: list[tuple[str, float]] = []
    if p.get("cadastral_cost_rub") and area:
        points.append(("кадастровая стоимость", p["cadastral_cost_rub"] / (area / SOTKA_M2)))
    if median_pps:
        points.append(("медиана подборки", median_pps))
    if item.get("price_per_sotka"):
        points.append(("цена в объявлении", item["price_per_sotka"]))
    if not points:
        return None
    values = [v for _, v in points]
    return {
        "points": points,
        "low": min(values),
        "high": max(values),
        "disclaimer": PRICE_DISCLAIMER,
    }


MEDIAN_BAND = 0.15  # ±15 % к медиане подборки считаем «в медиане»
MEDIAN_MIN_PRICED = 3  # медиану показываем, когда цен в подборке хотя бы три
MIN_AREA_M2 = 50  # площадь ЕГРН 1 м² (бывает в данных) делает любую цену за сотку абсурдной


def price_position(item: dict[str, Any], median_pps: float | None) -> dict[str, Any] | None:
    """Цена за сотку к ориентирам одной строкой (ТЗ v2, М1.4): кадастровая стоимость участка,
    медиана подборки и, если блок «Локация» уже собран, кадастровая ставка соседей.

    Рынка здесь нет: это честные ориентиры по открытым данным, не оценка.
    """
    pps = item.get("price_per_sotka")
    if not pps:
        return None
    p = item.get("parcel") or {}
    area = p.get("area_m2")
    if area is not None and area < MIN_AREA_M2:
        return None
    parts: list[str] = []
    out: dict[str, Any] = {
        "pps": pps,
        "vs_cadastral": None,
        "vs_median": None,
        "vs_neighbors": None,
        "label": None,
        "parts": parts,
    }
    if p.get("cadastral_cost_rub") and area:
        ratio = pps / (p["cadastral_cost_rub"] / (area / SOTKA_M2))
        out["vs_cadastral"] = round(ratio, 2)
        parts.append(f"×{ratio:.1f} к кадастровой")
    if median_pps:
        diff = pps / median_pps - 1
        out["vs_median"] = round(diff, 3)
        if abs(diff) <= MEDIAN_BAND:
            out["label"] = "в медиане подборки"
        elif diff > 0:
            out["label"] = f"дороже подборки на {diff:.0%}"
        else:
            out["label"] = f"дешевле подборки на {-diff:.0%}"
    loc_price = (item.get("location") or {}).get("price") or {}
    if loc_price.get("listing_vs_gko"):
        out["vs_neighbors"] = loc_price["listing_vs_gko"]
        parts.append(f"×{loc_price['listing_vs_gko']:.1f} к ставке соседей")
    if not parts and out["label"] is None:
        return None
    out["text"] = " · ".join(([out["label"]] if out["label"] else []) + parts)
    return out


def batch_median(check_stats: dict[str, Any] | None, items: list[dict[str, Any]]) -> float | None:
    """Медиана подборки для сравнения: только если цен в подборке не меньше трёх."""
    priced = sum(1 for d in items if d.get("price_per_sotka"))
    if priced < MEDIAN_MIN_PRICED:
        return None
    return (check_stats or {}).get("median_price_per_sotka")


# --- «Можно ли здесь то, что хочет покупатель»: категория × ВРИ × терзона ----------------
_GOAL_ROWS: tuple[tuple[str, str], ...] = (
    ("R04", "Категория земель"),
    ("R05", "Вид разрешённого использования"),
    ("R11", "Территориальная зона по ПЗЗ"),
)
_GOAL_OK_TEXT = {
    "R04": "категория допускает цель",
    "R05": "ВРИ прямо предусматривает цель",
    "R11": "зона допускает цель",
}
_OVERALL_TEXT = {
    Level.OK: "Можно: категория, ВРИ и территориальная зона допускают цель",
    Level.CHECK: "Возможно с оговорками: есть что уточнить до сделки",
    Level.STOP: "Нельзя без смены назначения: один из уровней запрещает цель",
}


def goal_summary(item: dict[str, Any], purpose: Purpose) -> dict[str, Any] | None:
    """Три уровня проверки цели одной таблицей. None — цель не задана или участка нет."""
    parcel = item.get("parcel") or {}
    if purpose is Purpose.ANY or not parcel.get("found"):
        return None
    flags = {f["rule_id"]: f for f in item.get("flags") or []}
    rows: list[dict[str, Any]] = []
    worst = Level.OK
    for rule_id, title in _GOAL_ROWS:
        if rule_id == "R04":
            value = parcel.get("category")
        elif rule_id == "R05":
            value = parcel.get("permitted_use") or " | ".join(parcel.get("permitted_use_tab") or [])
        else:
            value = "; ".join(
                z.get("describe") or "" for z in parcel.get("territorial_zones") or []
            )
        f = flags.get(rule_id)
        if f is not None:
            level, text, source = Level(f["level"]), f["message"], f.get("source")
        elif rule_id == "R11" and not parcel.get("territorial_zone_checked"):
            level, text, source = Level.CHECK, "зона не проверялась: у участка нет контура", None
        else:
            level, text, source = Level.OK, _GOAL_OK_TEXT[rule_id], None
        if level.rank > worst.rank:
            worst = level
        rows.append(
            {
                "rule_id": rule_id,
                "title": title,
                "value": value or "—",
                "level": level.value,
                "text": text,
                "source": source,
            }
        )
    return {
        "activity": purpose.activity,
        "level": worst.value,
        "text": _OVERALL_TEXT[worst],
        "rows": rows,
        "short": " · ".join(f"{r['title'].split()[0].lower()}: {r['level']}" for r in rows),
    }


# --- коридор цены за сотку по базе Land-MP (план 20.10, п.4) -----------------------------
CORRIDOR_DAYS = 180
CORRIDOR_MIN_N = 3
CORRIDOR_MAX_POINTS = 3000
KIND_LABELS = {
    "izhs": "ИЖС и жильё в населённом пункте",
    "snt": "садоводство и дачи",
    "agri": "сельхозземля",
    "industry": "промышленность и коммерция",
    "other": "прочее назначение",
}


def land_kind(category: str | None, vri: str | None) -> str:
    """Тип участка для сравнения цен: ИЖС/жильё, садоводство, сельхоз, промка, прочее."""
    from landcheck.rules.purpose import category_class, vri_match

    cls = category_class(category)
    vri = vri or ""
    if cls == "industry":
        return "industry"
    if vri_match(Purpose.SNT, vri) == "match":
        return "snt"
    if cls == "agri":
        return "agri"
    if cls == "settlement" or vri_match(Purpose.IZHS, vri) == "match":
        return "izhs"
    return "other"


def locality_keys(address: str | None) -> list[tuple[str, str]]:
    """Уровни сравнения от узкого к широкому: населённый пункт → район → регион.
    «Республика Алтай, Чемальский район, с. Элекмонар» → три ключа."""
    from landcheck.region import locality_from_address

    loc = locality_from_address(address)
    if not loc:
        return []
    parts = [x.strip() for x in loc.split(",") if x.strip()]
    out: list[tuple[str, str]] = []
    for i in range(len(parts), 0, -1):
        key = ", ".join(parts[:i]).lower()
        level = "settlement" if i >= 3 else ("district" if i == 2 else "region")
        if i == len(parts) and i < 3:
            level = "district" if i == 2 else "region"
        out.append((level, key))
    return out


def price_point(cad_num: str, *, price_rub, area_m2, category, vri, address, when) -> dict | None:
    if not price_rub or not area_m2 or area_m2 < MIN_AREA_M2:
        return None
    return {
        "cad_num": cad_num,
        "pps": price_rub / (area_m2 / SOTKA_M2),
        "kind": land_kind(category, vri),
        "keys": {k for _, k in locality_keys(address)},
        "when": when,
    }


_POINTS_CACHE: dict[str, Any] = {"at": 0.0, "points": []}
POINTS_CACHE_SECONDS = 60.0


def collect_price_points(db, *, days: int = CORRIDOR_DAYS) -> list[dict[str, Any]]:
    """Цены из проверок брокеров и объявлений за `days` дней; один участок — одна (свежая) цена.
    Результат кэшируется на минуту: отчёт открывают чаще, чем появляются новые цены."""
    import time
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from landcheck.web.db import Check, CheckItem
    from landcheck.web.db import Listing as ListingRow

    now = time.monotonic()
    if now - _POINTS_CACHE["at"] < POINTS_CACHE_SECONDS and _POINTS_CACHE["points"]:
        return _POINTS_CACHE["points"]
    cutoff = datetime.now(UTC) - timedelta(days=days)
    points: dict[str, dict[str, Any]] = {}
    rows = db.execute(
        select(CheckItem.cad_num, CheckItem.data, Check.created_at)
        .join(Check, Check.id == CheckItem.check_id)
        .order_by(Check.created_at.desc())
        .limit(CORRIDOR_MAX_POINTS)
    ).all()
    for cad_num, data, created in rows:
        if created and created.replace(tzinfo=created.tzinfo or UTC) < cutoff:
            continue
        p = (data or {}).get("parcel") or {}
        lst = (data or {}).get("listing") or {}
        pt = price_point(
            cad_num,
            price_rub=lst.get("price_rub"),
            area_m2=p.get("area_m2"),
            category=p.get("category"),
            vri=p.get("permitted_use"),
            address=p.get("address"),
            when=created,
        )
        if pt and cad_num not in points:
            points[cad_num] = pt
    for row in db.execute(select(ListingRow).order_by(ListingRow.id.desc()).limit(1000)).scalars():
        d = row.data or {}
        pf = d.get("parcel_facts") or {}
        pt = price_point(
            row.cad_num,
            price_rub=d.get("price_rub"),
            area_m2=pf.get("best_area_m2"),
            category=pf.get("category"),
            vri=pf.get("permitted_use"),
            address=d.get("address"),
            when=row.created_at,
        )
        if pt:
            points[row.cad_num] = pt  # цена продавца свежее цены из чужой подборки
    out = list(points.values())
    _POINTS_CACHE.update(at=now, points=out)
    return out


def _quantile(values: list[float], q: float) -> float:
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


LEVEL_LABELS = {
    "settlement": "в этом населённом пункте",
    "district": "в этом районе",
    "region": "в этом регионе",
}


def price_corridor(
    points: list[dict[str, Any]],
    *,
    address: str | None,
    category: str | None,
    vri: str | None,
    pps: float | None = None,
    exclude_cad: str | None = None,
    min_n: int = CORRIDOR_MIN_N,
) -> dict[str, Any] | None:
    """Медиана и квартили ₽/сот по участкам того же типа на самом узком уровне, где есть
    хотя бы `min_n` цен; позиция цены участка относительно коридора."""
    kind = land_kind(category, vri)
    for level, key in locality_keys(address):
        vals = [
            p["pps"]
            for p in points
            if p["kind"] == kind and key in p["keys"] and p["cad_num"] != exclude_cad
        ]
        if len(vals) < min_n:
            continue
        med = _quantile(vals, 0.5)
        out: dict[str, Any] = {
            "n": len(vals),
            "median": med,
            "p25": _quantile(vals, 0.25),
            "p75": _quantile(vals, 0.75),
            "level": level,
            "where": LEVEL_LABELS[level],
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
            "days": CORRIDOR_DAYS,
            "position": None,
            "position_text": None,
        }
        if pps:
            diff = pps / med - 1
            out["position"] = round(diff, 3)
            if out["p25"] <= pps <= out["p75"]:
                out["position_text"] = "в коридоре базы"
            elif diff > 0:
                out["position_text"] = f"дороже медианы базы на {diff:.0%}"
            else:
                out["position_text"] = f"дешевле медианы базы на {-diff:.0%}"
        return out
    return None
