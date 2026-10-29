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

CHECKLIST_PUBLIC: list[dict[str, Any]] = [
    {
        "title": "Муниципальный или государственный участок: как оформить",
        "items": [
            "Выписка ЕГРН об объекте: есть ли уже аренда, сервитут, арест — если участок в аренде у третьего лица, с администрацией работать нельзя, ищите арендатора",
            "Свободный участок: аукцион (ст. 39.11–39.12 ЗК РФ) или без торгов — предварительное согласование предоставления (ст. 39.15), для КФХ и ЛПХ — ст. 39.18",
            "Сельхозземля: преимущественное право субъекта РФ при продаже (101-ФЗ, ст. 8); аренда без торгов для КФХ — ст. 10 101-ФЗ",
            "Договор аренды: срок, арендная плата, условия выкупа (ст. 39.20 ЗК РФ — при наличии здания), запрет переуступки",
            "Переуступка аренды: согласие арендодателя, если договор его требует; уведомление администрации",
        ],
    },
    {
        "title": "Документы от продавца или администрации",
        "items": [
            "Правоустанавливающий документ продавца: договор аренды с регистрацией в ЕГРН или решение о предоставлении",
            "Выписка о переходе прав: не было ли переуступок и расторжений",
            "Межевой план и акт согласования границ; границы в ЕГРН установлены",
            "Задолженность по арендной плате — справка от арендодателя",
        ],
    },
]

CHECKLIST_SITE_FARMING: dict[str, Any] = {
    # Поле под КФХ/ЛПХ: вопросы про дом, газ и «зимнее содержание дороги» тут не к месту
    # (Андрей, чат «МП Земля» 29.10).
    "title": "Что проверить на месте для сельхозучастка",
    "items": [
        "Подъезд техники: полевая дорога или дорога общего пользования, проезд в распутицу, "
        "не через чужой участок",
        "Фактическое использование: кто сейчас косит, пасёт или пашет; следы заброшенности — "
        "залежь, кустарник, лес на поле (риск изъятия за неиспользование, 101-ФЗ ст. 6)",
        "Почва и мелиорация: дренаж и каналы (мелиорированные земли — особый режим), "
        "заболоченность, камни, уклон",
        "Вода: источник для полива или водопоя — скважина, пруд, ручей; водоохранная зона "
        "на участке",
        "Электричество: ближайшая опора и мощность под ферму или насос, плата за присоединение "
        "по региону (см. блок про сети)",
        "Границы: межевые знаки на месте, нет ли вклинивания, пересечения с лесфондом или дорогой",
        "Соседи: действующие фермы и животноводческие комплексы (санзоны), склады агрохимии, "
        "свалки",
        "Ограничения из ЗОУИТ в отчёте: ЛЭП и газопроводы пашне не мешают, но запрещают "
        "постройки в охранной зоне",
    ],
}


CHECKLIST_SITE_PRODUCTION: dict[str, Any] = {
    "title": "Что проверить на месте и в документах для производства или торговли",
    "items": [
        "Подъезд для грузового транспорта: ширина, покрытие, ограничения по тоннажу, зимнее содержание",
        "Электричество: свободная мощность на подстанции, категория надёжности, технические условия и срок",
        "Газ, вода, канализация, связь: точки подключения и стоимость по техусловиям",
        "СЗЗ самого объекта: не накроет ли жильё соседей (СанПиН 2.2.1/2.1.1.1200-03), иначе проект не согласуют",
        "Ограничения из ЗОУИТ в отчёте: охранные зоны сетей, приаэродромная территория, водоохранная зона",
    ],
}

PRICE_DISCLAIMER = "Справочно. Не является отчётом об оценке."


def checklist_for(item: dict[str, Any], purpose: Purpose) -> dict[str, Any]:
    """Чек-лист под форму собственности и цель: для муниципального участка под КФХ «нотариальное
    согласие супруга» и «взносы СНТ» не нужны (зум 28.10, п. 1.12)."""
    parcel = item.get("parcel") or {}
    own = (parcel.get("ownership_type") or "").lower()
    public = any(
        k in own for k in ("публичн", "государств", "муниципальн", "не разграничен", "федеральн")
    )
    site = site_block(parcel, purpose)
    if public:
        blocks = list(CHECKLIST_PUBLIC)
        blocks.append(
            {
                "title": "Границы и межевание",
                "items": [i for i in CHECKLIST[2]["items"] if "объявлении" not in i],
            }
        )
        blocks.append(site)
        return {"title": "Чек-лист перед оформлением участка", "blocks": blocks}
    blocks = [dict(b) for b in CHECKLIST]
    if purpose in (Purpose.FARMING, Purpose.INDUSTRY, Purpose.COMMERCE):
        blocks[0] = {
            "title": blocks[0]["title"],
            "items": [i for i in blocks[0]["items"] if "СНТ" not in i]
            + ["Справка об отсутствии долгов по земельному налогу"],
        }
    blocks[3] = site
    return {"title": "Чек-лист перед сделкой", "blocks": blocks}


def site_block(parcel: dict[str, Any], purpose: Purpose) -> dict[str, Any]:
    """Блок «что проверить на месте» по цели и категории: производство/торговля — свой,
    поле (цель КФХ, или сельхозземля без цели/под ЛПХ) — сельхозный, иначе — под дом."""
    from landcheck.rules.purpose import category_class

    if purpose in (Purpose.INDUSTRY, Purpose.COMMERCE):
        return CHECKLIST_SITE_PRODUCTION
    agri = category_class(parcel.get("category")) == "agri"
    if purpose is Purpose.FARMING or (agri and purpose in (Purpose.ANY, Purpose.LPH)):
        return CHECKLIST_SITE_FARMING
    return CHECKLIST[3]


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


# --- оценка стоимости (R6): якоря → диапазон → «в рынке ли» --------------------------------
MIN_MARKET_N = 3


def valuation(
    item: dict[str, Any],
    *,
    corridor: dict[str, Any] | None,
    ads: dict[str, Any] | None,
    median_batch: float | None,
) -> dict[str, Any] | None:
    """Оценка ориентира за сотку по открытым данным: база Land-MP, объявления в локации,
    кадастр участка и соседей, медиана подборки. Диапазон — середина рынка (25–75 %) по самому
    надёжному источнику; вердикт — положение цены объявления. Не отчёт об оценке."""
    p = item.get("parcel") or {}
    area = p.get("area_m2")
    if not area or area < MIN_AREA_M2:
        return None
    area_sotka = area / SOTKA_M2
    pps = item.get("price_per_sotka")
    anchors: list[dict[str, Any]] = []
    if p.get("cadastral_cost_rub"):
        anchors.append(
            {
                "label": "кадастровая стоимость участка",
                "pps": p["cadastral_cost_rub"] / area_sotka,
                "note": "государственная оценка, обычно ниже рынка",
            }
        )
    loc_price = (item.get("location") or {}).get("price") or {}
    if loc_price.get("per_sotka"):
        anchors.append(
            {
                "label": "кадастровая ставка соседей",
                "pps": loc_price["per_sotka"],
                "note": loc_price.get("basis"),
            }
        )
    if median_batch:
        anchors.append(
            {
                "label": "медиана вашей подборки",
                "pps": median_batch,
                "note": "цены объявлений в подборке",
            }
        )
    source = None
    rng = None
    if corridor and corridor.get("n", 0) >= MIN_MARKET_N:
        anchors.append(
            {
                "label": f"база Land-MP, {corridor['where']}",
                "pps": corridor["median"],
                "note": f"{corridor['n']} уч. того же типа за {corridor['days']} дней",
            }
        )
        source, rng = "база Land-MP", (corridor["p25"], corridor["p75"])
    if ads and ads.get("n", 0) >= MIN_MARKET_N:
        anchors.append(
            {
                "label": "объявления в локации",
                "pps": ads["median"],
                "note": f"{ads['n']} объявлений с ценой и площадью, цены предложений",
            }
        )
        if rng is None:
            source, rng = "объявления в локации", (ads["p25"], ads["p75"])
    if not anchors:
        return None
    verdict = None
    position = None
    if pps and rng:
        low, high = rng
        mid = (low + high) / 2
        position = round(pps / mid - 1, 3)
        if low <= pps <= high:
            verdict = "в рынке: цена в середине диапазона предложений"
        elif pps > high:
            verdict = f"выше рынка: дороже верхней границы на {pps / high - 1:.0%}"
        else:
            verdict = (
                f"ниже рынка: дешевле нижней границы на {1 - pps / low:.0%} — проверить, почему"
            )
    elif pps and anchors:
        ref = anchors[-1]["pps"]
        position = round(pps / ref - 1, 3)
        verdict = (
            "рынок по данным не определён: сравните с ориентирами ниже "
            f"(к «{anchors[-1]['label']}» ×{pps / ref:.1f})"
        )
    confidence = "низкая"
    n_sources = sum(1 for x in (corridor, ads) if x and x.get("n", 0) >= MIN_MARKET_N)
    if n_sources == 2 or (corridor and corridor.get("n", 0) >= 8) or (ads and ads.get("n", 0) >= 8):
        confidence = "средняя"
    if n_sources == 2 and corridor.get("n", 0) >= 8 and ads.get("n", 0) >= 8:
        confidence = "высокая"
    return {
        "area_sotka": round(area_sotka, 2),
        "anchors": anchors,
        "range": rng,
        "range_source": source,
        "total_range": (rng[0] * area_sotka, rng[1] * area_sotka) if rng else None,
        "verdict": verdict,
        "position": position,
        "confidence": confidence,
        "ads": ads,
        "disclaimer": "Ориентир по открытым данным (кадастр, база Land-MP, объявления). "
        "Не отчёт об оценке по 135-ФЗ: для сделки, залога или суда нужен оценщик.",
    }
