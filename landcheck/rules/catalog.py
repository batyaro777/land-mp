"""Каталог риск-правил М1. Каждое — одно предложение понятным языком и источник."""

from __future__ import annotations

from datetime import UTC, datetime

from landcheck.models import SOTKA_M2, Flag, Level, Purpose
from landcheck.rules import classifier
from landcheck.rules.engine import RuleContext, rule
from landcheck.rules.purpose import (
    CATEGORY_CLASS_LABEL,
    CATEGORY_MATRIX,
    category_class,
    house_possible_on_agri,
    vri_match,
    vri_verdict,
)
from landcheck.rules.zones import SRC_GRK, SRC_GRK_36, zone_class, zone_verdict

SRC_NSPD = "НСПД, карточка участка (слой «Земельные участки из ЕГРН»)"
SRC_NSPD_SEARCH = "НСПД, поиск по кадастровому номеру"
SRC_NSPD_ZOUIT = "НСПД, слои ЗОУИТ, пересечение с контуром участка"
SRC_NSPD_TZ = "НСПД, слой «Территориальные зоны», пересечение с контуром участка"
SRC_487 = "487-ФЗ от 26.12.2024: с 01.03.2025 сделки с участком без границ в ЕГРН не регистрируются"
SRC_ZK7 = "ст. 7 ЗК РФ: целевое назначение по категории; ВРИ — классификатор П/0412"
SRC_LISTING = "объявление брокера против карточки НСПД"
SRC_BATCH = "расчёт по подборке: медиана цены за сотку"
SRC_LAW_ZOUIT = "ст. 104–106 ЗК РФ: в ЗОУИТ ограничения на строительство и использование"
SRC_PZZ = "ПЗЗ муниципалитета; в НСПД опубликованы не для всех территорий"
SRC_KURORT = (
    "26-ФЗ о природных лечебных ресурсах, ст. 16: режим округа санитарной охраны курорта "
    "действует на всю территорию курорта; ограничения для участка — по зонам 1–3 в положении об округе"
)


def _fmt_money(v: float) -> str:
    return f"{v:,.0f} ₽".replace(",", " ")


def _fmt_sotka(m2: float) -> str:
    return f"{m2 / SOTKA_M2:.1f} сот."


# --- данные ----------------------------------------------------------------------------
@rule("R00", "Данные не получены", "НСПД не ответила и кэша нет — проверка невозможна")
def r00_fetch_failed(ctx: RuleContext):
    if ctx.parcel.found or not ctx.fetch_errors:
        return None
    return Flag(
        "R00",
        Level.CHECK,
        "данные НСПД не получены, проверить вручную: " + "; ".join(ctx.fetch_errors[:2]),
        SRC_NSPD_SEARCH,
        {"errors": ctx.fetch_errors},
    )


@rule("R01", "Участок не найден", "Кадастровый номер отсутствует в НСПД")
def r01_not_found(ctx: RuleContext):
    if ctx.parcel.found or ctx.fetch_errors:
        return None
    return Flag(
        "R01",
        Level.STOP,
        "участок с таким кадастровым номером в НСПД не найден: ошибка в номере или объект снят с учёта",
        SRC_NSPD_SEARCH,
    )


@rule("R02", "Статус участка", "Архивный, аннулированный, временный статус")
def r02_status(ctx: RuleContext):
    p = ctx.parcel
    if not p.found:
        return None
    s = (p.status or "").lower()
    if not s:
        return Flag(
            "R02", Level.CHECK, "статус участка в НСПД не указан, запросить выписку ЕГРН", SRC_NSPD
        )
    if any(k in s for k in ("архив", "аннулир", "снят", "погаш")):
        return Flag(
            "R02",
            Level.STOP,
            f"статус «{p.status}»: участок снят с учёта, сделка невозможна",
            SRC_NSPD,
        )
    if "времен" in s:
        return Flag(
            "R02",
            Level.CHECK,
            f"статус «{p.status}»: участок образован, но права не зарегистрированы — до регистрации сделки нет",
            SRC_NSPD,
        )
    return None


@rule("R03", "Границы не установлены", "Нет координат границ в ЕГРН")
def r03_borders(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or p.has_borders:
        return None
    return Flag(
        "R03",
        Level.STOP,
        "границы участка в ЕГРН не установлены: без межевания сделку не зарегистрируют",
        SRC_487,
    )


@rule("R04", "Категория земель против цели", "Матрица цель × категория")
def r04_category(ctx: RuleContext):
    p = ctx.parcel
    if not p.found:
        return None
    cls = category_class(p.category)
    land = ctx.request.land_purpose
    matrix = CATEGORY_MATRIX[land]
    if cls is None:
        if p.category:
            return Flag(
                "R04",
                Level.CHECK,
                f"категория «{p.category}» не распознана, проверить вручную",
                SRC_ZK7,
            )
        return Flag("R04", Level.CHECK, "категория земель в НСПД не указана", SRC_NSPD)
    hit = matrix.get(cls)
    if hit is None:
        return None
    level, text = hit
    source = SRC_ZK7
    if cls == "agri" and land is Purpose.IZHS:
        exception = house_possible_on_agri(p.permitted_use_all)
        if exception:
            level, text, source = exception
    return Flag(
        "R04",
        level,
        f"цель «{land.label}»: " + text.format(cat=CATEGORY_CLASS_LABEL[cls]),
        source,
        {"category": p.category, "category_class": cls},
    )


@rule("R05", "ВРИ против цели", "Вид разрешённого использования не соответствует цели")
def r05_vri(ctx: RuleContext):
    p = ctx.parcel
    purpose = ctx.request.land_purpose
    if not p.found or purpose is Purpose.ANY or purpose.professional:
        return None
    label = purpose.label
    vri = p.permitted_use or (p.permitted_use_tab[0] if p.permitted_use_tab else "—")
    special = vri_verdict(purpose, p.permitted_use_all, category_class(p.category))
    if special is not None:
        level, text, source = special
        return Flag("R05", level, text, source, {"vri": p.permitted_use_all, "nuance": True})
    res = vri_match(purpose, p.permitted_use_all)
    if res == "match":
        return None
    if res == "empty":
        return Flag("R05", Level.CHECK, "ВРИ в НСПД не указан, уточнить по выписке и ПЗЗ", SRC_NSPD)
    if res == "conflict":
        return Flag(
            "R05",
            Level.STOP,
            f"ВРИ «{vri}» несовместим с целью «{label}»: без смены ВРИ использовать нельзя",
            SRC_ZK7,
            {"vri": p.permitted_use_all},
        )
    codes = classifier.codes_for(p.permitted_use_all)
    if codes and vri_match(purpose, " ".join(f"код {c}" for c in codes)) == "match":
        # Старая формулировка без кода («под жилую застройку индивидуальную»): по смыслу
        # это код классификатора, который цель допускает. Запись действительна (171-ФЗ).
        m = classifier.match(vri)
        code = m.code if m is not None else codes[0]
        name = m.name if m is not None else classifier.by_code()[code].name
        return Flag(
            "R05",
            Level.OK,
            f"ВРИ «{vri}» записан старой формулировкой; по смыслу это код {code} «{name}» — "
            f"цель «{label}» допустима, в ГПЗУ администрация проставит код",
            classifier.SRC_171,
            {"vri": p.permitted_use_all, "code": code, "legacy": True},
        )
    return Flag(
        "R05",
        Level.CHECK,
        f"ВРИ «{vri}» не подтверждает цель «{label}»: проверить допустимые ВРИ в терзоне по ПЗЗ",
        SRC_ZK7,
        {"vri": p.permitted_use_all},
    )


@rule("R06", "Площадь объявления против ЕГРН", "Расхождение заявленной и кадастровой площади")
def r06_area(ctx: RuleContext):
    p, lst, cfg = ctx.parcel, ctx.listing, ctx.config
    if not p.found or lst.declared_area_m2 is None or not p.best_area_m2:
        return None
    dev = abs(lst.declared_area_m2 - p.best_area_m2) / p.best_area_m2 * 100
    if dev < cfg.area_check_pct:
        return None
    level = Level.STOP if dev >= cfg.area_stop_pct else Level.CHECK
    return Flag(
        "R06",
        level,
        f"в объявлении {_fmt_sotka(lst.declared_area_m2)}, в ЕГРН {_fmt_sotka(p.best_area_m2)} "
        f"(расхождение {dev:.0f}%)",
        SRC_LISTING,
        {"deviation_pct": round(dev, 1)},
    )


@rule("R07", "Площадь декларированная", "Площадь не уточнена межеванием")
def r07_declared_only(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or not p.has_borders:
        return None
    if p.specified_area_m2 is None and p.declared_area_m2 is not None:
        return Flag(
            "R07",
            Level.CHECK,
            "площадь в ЕГРН декларированная, не уточнена межеванием: фактические границы могут отличаться",
            SRC_NSPD,
        )
    return None


@rule("R08", "Цена против кадастровой стоимости", "Аномально дёшево или дорого")
def r08_price_vs_cadastral(ctx: RuleContext):
    p, lst, cfg = ctx.parcel, ctx.listing, ctx.config
    if not p.found or lst.price_rub is None or not p.cadastral_cost_rub:
        return None
    ratio = lst.price_rub / p.cadastral_cost_rub
    if ratio < cfg.price_vs_cadastral_low:
        return Flag(
            "R08",
            Level.CHECK,
            f"цена {_fmt_money(lst.price_rub)} — это {ratio:.0%} кадастровой стоимости "
            f"{_fmt_money(p.cadastral_cost_rub)}: подозрительно дёшево, возможен фейк или скрытое обременение",
            SRC_LISTING,
            {"ratio": round(ratio, 2)},
        )
    if ratio > cfg.price_vs_cadastral_high:
        return Flag(
            "R08",
            Level.OK,
            f"цена в {ratio:.1f} раза выше кадастровой стоимости {_fmt_money(p.cadastral_cost_rub)}: "
            "справочно, торг уместен",
            SRC_LISTING,
            {"ratio": round(ratio, 2)},
        )
    return None


@rule("R09", "Цена против медианы подборки", "Отклонение цены за сотку от медианы")
def r09_price_vs_median(ctx: RuleContext):
    lst, cfg, st = ctx.listing, ctx.config, ctx.stats
    if not ctx.parcel.found:
        return None
    if st.median_price_per_sotka is None or st.priced_count < cfg.median_min_samples:
        return None
    area = ctx.parcel.best_area_m2 or lst.declared_area_m2
    if lst.price_rub is None or not area:
        return None
    pps = lst.price_rub / (area / SOTKA_M2)
    ratio = pps / st.median_price_per_sotka
    if ratio < cfg.price_vs_median_low:
        return Flag(
            "R09",
            Level.CHECK,
            f"{_fmt_money(pps)} за сотку — {ratio:.0%} медианы подборки "
            f"({_fmt_money(st.median_price_per_sotka)}): проверить причину дешевизны",
            SRC_BATCH,
            {"ratio": round(ratio, 2)},
        )
    if ratio > cfg.price_vs_median_high:
        return Flag(
            "R09",
            Level.OK,
            f"{_fmt_money(pps)} за сотку — в {ratio:.1f} раза выше медианы подборки "
            f"({_fmt_money(st.median_price_per_sotka)})",
            SRC_BATCH,
            {"ratio": round(ratio, 2)},
        )
    return None


ZOUIT_SEVERITY: tuple[tuple[str, Level, str], ...] = (
    # (подстрока в type_zone/name, уровень для жилых целей, пояснение)
    ("санитарно-защитн", Level.STOP, "жилая застройка в санитарно-защитной зоне запрещена"),
    ("затоплен", Level.STOP, "зона затопления: строительство без защитных сооружений запрещено"),
    ("подтоплен", Level.CHECK, "зона подтопления: строительство с инженерной защитой"),
    ("приаэродром", Level.CHECK, "приаэродромная территория: согласование строительства"),
    ("водоохран", Level.CHECK, "водоохранная зона: ограничения на строительство и стоки"),
    ("прибрежн", Level.CHECK, "прибрежная защитная полоса: распашка и застройка ограничены"),
    (
        "культурного наследия",
        Level.CHECK,
        "зона охраны ОКН: высотность и облик согласуются с органом охраны",
    ),
    (
        "инженерных коммуникаций",
        Level.CHECK,
        "охранная зона сетей: строительство только по согласованию с сетевой организацией",
    ),
    ("электросет", Level.CHECK, "охранная зона электросетей: строительство только по согласованию"),
    ("газопровод", Level.CHECK, "зона газопровода: минимальные расстояния до построек"),
    ("кладбищ", Level.STOP, "санитарная зона кладбища: жильё запрещено"),
)
RESIDENTIAL = {"izhs", "lph", "snt"}
SRC_SANPIN = "СанПиН 2.2.1/2.1.1.1200-03, п. 5.1: жилая застройка в СЗЗ не допускается"
# ЗОУИТ «на весь муниципалитет»: округ санитарной охраны курорта накрывает весь Сочи или всю
# Белокуриху и на выделение, покупку и стройку конкретного участка не влияет (Влад, зум 28.10).
# Это факт для карточки, а не риск.
WIDE_ZONE_KEYS = (
    "лечебно-оздоровительн",
    "горно-санитарн",
    "санитарной охраны курорт",
    "округ санитарной",
)


def is_wide_zone(kind: str | None, name: str | None) -> bool:
    _t = f"{kind or ''} {name or ''}".lower()
    if "водоохран" in _t or "прибрежн" in _t or "затоплен" in _t or "подтоплен" in _t:
        return False  # прод 29.10: «город-курорт Сочи» в имени водоохранной зоны делал её «широкой»
    text = f"{kind or ''} {name or ''}".lower()
    return any(k in text for k in WIDE_ZONE_KEYS)


def _zouit_level(z, residential: bool) -> tuple[Level, str | None]:
    text = f"{z.kind or ''} {z.name or ''}".lower()
    for needle, level, note in ZOUIT_SEVERITY:
        if needle in text:
            if level is Level.STOP and not residential:
                return Level.CHECK, note
            return level, note
    return Level.CHECK, None


@rule("R10", "ЗОУИТ", "Пересечение с зонами с особыми условиями использования")
def r10_zouit(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or not p.zouit_checked or not p.zouit:
        return None
    residential = ctx.request.land_purpose.value in RESIDENTIAL
    wide = [z for z in p.zouit if is_wide_zone(z.kind, z.name)]
    zones = [z for z in p.zouit if not is_wide_zone(z.kind, z.name)]
    details = {
        "zouit": [
            {
                "kind": z.kind,
                "name": z.name,
                "reg_number": z.reg_number,
                "restrictions": z.restrictions,
            }
            for z in zones
        ],
        "wide": [z.describe(max_len=90) for z in wide],
    }
    if not zones:
        return Flag(
            "R10",
            Level.OK,
            f"участок в округе санитарной охраны курорта ({wide[0].describe(max_len=90)}): "
            "зона действует на весь курорт и на покупку, выделение и стройку участка не влияет; "
            "режим конкретной зоны округа (1–3) — по положению об округе",
            SRC_KURORT,
            details,
        )
    worst = Level.CHECK
    notes: list[str] = []
    for z in zones:
        level, note = _zouit_level(z, residential)
        if level.rank > worst.rank:
            worst = level
        if note and note not in notes:
            notes.append(note)
    names = "; ".join(z.describe(max_len=90) for z in zones[:4])
    more = f" и ещё {len(zones) - 4}" if len(zones) > 4 else ""
    tail = ("; ".join(notes)) if notes else "ограничения на строительство"
    source = SRC_NSPD_ZOUIT + "; " + SRC_LAW_ZOUIT
    if worst is Level.STOP:
        source += "; " + SRC_SANPIN
    return Flag(
        "R10",
        worst,
        f"участок пересекает ЗОУИТ ({len(zones)}): {names}{more} — {tail}, текст на листе «ЗОУИТ»",
        source,
        details,
    )


SRC_OOPT = (
    "НСПД, слой «Особо охраняемые природные территории» (реестр границ), пересечение с контуром; "
    "ст. 27 п. 4 и ст. 95 ЗК РФ, 33-ФЗ «Об ООПТ»: земли заповедников и нацпарков — федеральная "
    "собственность, в частную не передаются; режим региональных ООПТ — по положению о территории"
)
SRC_FOREST_PARK = (
    "НСПД, слой «Граница лесопарка»; ст. 62.1–62.5 7-ФЗ «Об охране окружающей среды»: в лесопарковом "
    "зелёном поясе запрещено строительство вне перечня, перевод и изъятие земель ограничены"
)
OOPT_MIN_SHARE = 0.05  # меньше — считаем касанием границы, не вхождением


def _overlap_share(parcel_geojson, features: list) -> float | None:
    """Доля площади участка внутри объединения контуров (0–1); None — геометрии нет."""
    try:
        from shapely.geometry import shape
        from shapely.ops import unary_union
    except Exception:  # noqa: BLE001 — shapely опционален
        return None
    if not parcel_geojson:
        return None
    try:
        parcel = shape(parcel_geojson)
        geoms = [shape(f["geometry"]) for f in features if f.get("geometry")]
        if not geoms or parcel.is_empty or parcel.area == 0:
            return None
        inter = parcel.intersection(unary_union(geoms))
        return round(min(max(inter.area / parcel.area, 0.0), 1.0), 3)
    except Exception:  # noqa: BLE001
        return None


@rule("R22", "Особо охраняемая природная территория", "Участок в границах ООПТ по реестру границ")
def r22_oopt(ctx: RuleContext):
    """Скрытое обременение (зум 28.10, Влад): документы чистые, а участок вырезан из нацпарка —
    сделку разворачивают. Категория в ЕГРН может быть любой, границу ООПТ видно только по слою."""
    p = ctx.parcel
    if not p.found or not p.oopt_checked or not p.oopt:
        return None
    share = _overlap_share(p.geometry, p.raw.get("oopt") or [])
    names = "; ".join(z.describe(max_len=80) for z in p.oopt[:3])
    details = {
        "oopt": [{"reg_number": z.reg_number, "name": z.name, "kind": z.kind} for z in p.oopt],
        "share": share,
    }
    protected_cat = category_class(p.category) == "protected"
    if share is not None and share < OOPT_MIN_SHARE:
        return Flag(
            "R22",
            Level.CHECK,
            f"граница ООПТ ({names}) задевает участок на {share:.0%} площади: проверить, не "
            "наложение ли это границ — запрос в дирекцию ООПТ или Минприроды региона",
            SRC_OOPT,
            details,
        )
    pct = f" ({share:.0%} площади)" if share is not None else ""
    if protected_cat:
        return Flag(
            "R22",
            Level.CHECK,
            f"участок в границах ООПТ ({names}){pct}, категория — особо охраняемые территории: "
            "оборот и использование ограничены режимом (ст. 27, 95 ЗК; положение об ООПТ)",
            SRC_OOPT,
            details,
        )
    return Flag(
        "R22",
        Level.STOP,
        f"участок в границах ООПТ ({names}){pct}, при этом категория в ЕГРН — "
        f"«{p.category or 'не указана'}»: земли заповедников и нацпарков — федеральная "
        "собственность и в частную не передаются, образование и продажа такого участка "
        "оспариваются (ст. 27 п. 4, ст. 95 ЗК; 33-ФЗ); до ответа дирекции ООПТ и выписки о "
        "переходе прав сделку не вести",
        SRC_OOPT,
        details,
    )


@rule("R23", "Лесопарковый зелёный пояс", "Участок в границах лесопарка")
def r23_forest_park(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or not p.forest_park_checked or not p.forest_park:
        return None
    share = _overlap_share(p.geometry, p.raw.get("forest_park") or [])
    if share is not None and share < OOPT_MIN_SHARE:
        return None  # касание границы
    pct = f" ({share:.0%} площади)" if share is not None else ""
    names = "; ".join(z.describe(max_len=80) for z in p.forest_park[:2])
    return Flag(
        "R23",
        Level.CHECK,
        f"участок в границах лесопаркового зелёного пояса{pct}{(' — ' + names) if names else ''}: "
        "строительство только из разрешённого перечня, сверить режим с лесничеством и ПЗЗ",
        SRC_FOREST_PARK,
        {"share": share, "zones": [z.describe(max_len=80) for z in p.forest_park]},
    )


@rule("R11", "Территориальная зона против цели", "Зона ПЗЗ: что здесь можно строить")
def r11_territorial_zone(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or not p.territorial_zone_checked:
        return None
    if not p.territorial_zones:
        return Flag(
            "R11",
            Level.CHECK,
            "территориальная зона к участку в НСПД не привязана (ПЗЗ муниципалитета в НСПД "
            "не загружены): проверить зону по карте ПЗЗ на сайте администрации или во ФГИС ТП",
            SRC_PZZ,
        )
    purpose = ctx.request.land_purpose
    zones = [(z, zone_class(z)) for z in p.territorial_zones]
    names = "; ".join(z.describe() for z, _ in zones[:3])
    details = {
        "zones": [z.describe() for z, _ in zones],
        "classes": [cls for _, cls in zones],
    }
    if purpose is Purpose.ANY or purpose.professional:
        return Flag("R11", Level.OK, f"территориальная зона по НСПД: {names}", SRC_NSPD_TZ, details)
    # Участок может лежать в нескольких зонах: берём худшую для цели, пояснения — по каждой.
    worst = Level.OK
    notes: list[str] = []
    for z, cls in zones:
        verdict = zone_verdict(purpose, cls)
        if verdict is None:
            continue
        level, text = verdict
        if level.rank > worst.rank:
            worst = level
        prefix = z.describe(60) if cls is not None else (z.reg_number or z.layer_short)
        notes.append(f"{prefix} — {text}")
    message = "; ".join(notes) if notes else f"территориальная зона по НСПД: {names}"
    source = SRC_GRK_36 if any(c == "sx_ugodia" for _, c in zones) else SRC_GRK
    if all(c is None for _, c in zones):
        source = SRC_NSPD_TZ
    return Flag("R11", worst, message, source, details)


@rule("R12", "Форма собственности", "Публичная или неразграниченная собственность")
def r12_ownership(ctx: RuleContext):
    p = ctx.parcel
    if not p.found:
        return None
    o = (p.ownership_type or "").lower()
    if not o:
        return Flag(
            "R12",
            Level.OK,
            "форма собственности в карточке НСПД не указана (обычно для НСПД): собственника смотреть по выписке ЕГРН",
            SRC_NSPD,
        )
    if "частн" in o:
        return None
    if any(k in o for k in ("публичн", "государств", "муниципальн", "не разграничен", "федеральн")):
        return Flag(
            "R12",
            Level.CHECK,
            f"собственность «{p.ownership_type}»: участок либо свободен (оформление через торги "
            "или предварительное согласование), либо уже в аренде у третьего лица — "
            "НСПД этого не показывает, нужна выписка ЕГРН, раздел «Права и ограничения»",
            SRC_NSPD,
        )
    return Flag(
        "R12",
        Level.CHECK,
        f"форма собственности «{p.ownership_type}»: проверить по выписке",
        SRC_NSPD,
    )


SRC_ZK_PROVISION = (
    "гл. V.1 ЗК РФ: предоставление публичных участков — торги (ст. 39.11–39.12), без торгов "
    "(ст. 39.3, 39.6), предварительное согласование (ст. 39.15), извещение для ИЖС/ЛПХ/КФХ (ст. 39.18)"
)
SRC_ZK_LEASE = (
    "ст. 22 ЗК РФ: арендатор публичного участка передаёт права по договору третьему лицу "
    "(договор свыше 5 лет — с уведомлением, до 5 лет — с согласия арендодателя)"
)
SRC_NK_217 = (
    "ст. 217.1 НК РФ: минимальный срок владения 5 лет (3 года — отдельные случаи), "
    "иначе НДФЛ с дохода"
)
_PUBLIC_OWN = ("публичн", "государств", "муниципальн", "не разграничен", "федеральн")


@rule("R21", "Путь получения участка", "Как цель профессионала соотносится с формой собственности")
def r21_acquisition(ctx: RuleContext):
    """Цели «оформить у муниципалитета», «торги / переуступка», «инвестиция» (ТЗ v3, п. 1.12):
    форма собственности в НСПД определяет путь. НСПД не показывает действующую аренду —
    поэтому для публичного участка всегда «проверить» по выписке."""
    p = ctx.parcel
    purpose = ctx.request.purpose
    if not p.found or not purpose.professional:
        return None
    o = (p.ownership_type or "").lower()
    public = any(k in o for k in _PUBLIC_OWN)
    private = "частн" in o
    details = {"purpose": purpose.value, "ownership": p.ownership_type}
    if purpose is Purpose.MUNICIPAL:
        if private:
            return Flag(
                "R21",
                Level.STOP,
                "участок в частной собственности: у муниципалитета его не получить — только "
                "сделка с собственником; для оформления «с нуля» искать неразграниченные земли рядом",
                SRC_NSPD,
                details,
            )
        if public:
            return Flag(
                "R21",
                Level.CHECK,
                f"собственность «{p.ownership_type}»: путь — заявление о предварительном "
                "согласовании (ст. 39.15 ЗК) или без торгов по ст. 39.18 ЗК для ИЖС/ЛПХ/КФХ "
                "(извещение 30 дней; есть другие заявки — аукцион); сельхозземля в аренду КФХ без "
                "торгов — ст. 10 п. 8 101-ФЗ. Сначала выписка: участок может быть уже в аренде",
                SRC_ZK_PROVISION,
                details,
            )
        return Flag(
            "R21",
            Level.CHECK,
            "форма собственности в НСПД не указана: по выписке ЕГРН; если правообладатель — "
            "публичное образование или права не зарегистрированы, путь — ст. 39.15 / 39.18 ЗК",
            SRC_NSPD,
            details,
        )
    if purpose is Purpose.AUCTION:
        if private:
            return Flag(
                "R21",
                Level.CHECK,
                "участок в частной собственности: на торги попадает только при банкротстве или "
                "исполнительном производстве (127-ФЗ, 229-ФЗ) — искать на ЕФРСБ и torgi.gov.ru "
                "в разделе реализации имущества; иначе — прямая сделка с собственником",
                SRC_NSPD,
                details,
            )
        if public:
            return Flag(
                "R21",
                Level.CHECK,
                f"собственность «{p.ownership_type}»: продажа или аренда только через аукцион "
                "(ст. 39.11–39.12 ЗК), извещение на torgi.gov.ru; право аренды с торгов можно "
                "переуступить: договор свыше 5 лет — уведомив арендодателя, до 5 лет — с его "
                "согласия (ст. 22 ЗК). По выписке проверить, не сдан ли участок уже",
                SRC_ZK_LEASE,
                details,
            )
        return Flag(
            "R21",
            Level.CHECK,
            "форма собственности в НСПД не указана: искать извещение по кадастровому номеру на "
            "torgi.gov.ru и смотреть правообладателя по выписке ЕГРН",
            SRC_NSPD,
            details,
        )
    # INVEST
    agri_note = (
        "; сельхозземля: при продаже у региона преимущественное право покупки, извещение и до "
        "30 дней ожидания (ст. 8 101-ФЗ)"
        if ctx.request.use is not Purpose.ANY and category_class(p.category) == "agri"
        else ""
    )
    if public:
        return Flag(
            "R21",
            Level.CHECK,
            f"собственность «{p.ownership_type}»: сначала оформить право (торги ст. 39.11 или "
            "ст. 39.18 ЗК), перепродажа — только после регистрации; право аренды переуступается "
            "(ст. 22 ЗК) — это и есть сделка «вход на торгах → выход переуступкой»" + agri_note,
            SRC_ZK_PROVISION,
            details,
        )
    if private:
        return Flag(
            "R21",
            Level.CHECK if agri_note else Level.OK,
            "частная собственность: обычная купля-продажа; при перепродаже раньше 5 лет владения — "
            "НДФЛ 13 % с разницы (ст. 217.1 НК), заложить в расчёт доходности" + agri_note,
            SRC_NK_217,
            details,
        )
    return Flag(
        "R21",
        Level.CHECK,
        "форма собственности в НСПД не указана: собственника и обременения смотреть по выписке "
        "ЕГРН до расчёта сделки",
        SRC_NSPD,
        details,
    )


@rule("R13", "Дубли в подборке", "Один участок в нескольких объявлениях")
def r13_duplicates(ctx: RuleContext):
    if not ctx.duplicates:
        return None
    prices = [x.price_rub for x in [ctx.listing, *ctx.duplicates] if x.price_rub is not None]
    spread = ""
    if len(prices) >= 2 and min(prices) > 0:
        spread = f", цены от {_fmt_money(min(prices))} до {_fmt_money(max(prices))}"
    return Flag(
        "R13",
        Level.OK,
        f"участок встречается в {len(ctx.duplicates) + 1} объявлениях{spread}",
        "подборка брокера",
        {"count": len(ctx.duplicates) + 1},
    )


@rule("R14", "Бюджет", "Цена выше бюджета запроса")
def r14_budget(ctx: RuleContext):
    b = ctx.request.budget_rub
    price = ctx.listing.price_rub
    if b is None or price is None or price <= b:
        return None
    return Flag(
        "R14",
        Level.OK,
        f"цена {_fmt_money(price)} выше бюджета {_fmt_money(b)} на {(price / b - 1):.0%}",
        "запрос брокера",
    )


@rule("R15", "Площадь против запроса", "Площадь вне заданного диапазона")
def r15_area_range(ctx: RuleContext):
    rq = ctx.request
    area = ctx.parcel.best_area_m2 or ctx.listing.declared_area_m2
    if area is None or (rq.min_area_m2 is None and rq.max_area_m2 is None):
        return None
    if rq.min_area_m2 is not None and area < rq.min_area_m2:
        return Flag(
            "R15",
            Level.OK,
            f"площадь {_fmt_sotka(area)} меньше запрошенной {_fmt_sotka(rq.min_area_m2)}",
            "запрос брокера",
        )
    if rq.max_area_m2 is not None and area > rq.max_area_m2:
        return Flag(
            "R15",
            Level.OK,
            f"площадь {_fmt_sotka(area)} больше запрошенной {_fmt_sotka(rq.max_area_m2)}",
            "запрос брокера",
        )
    return None


@rule("R16", "Устаревшие данные", "Ответ НСПД старше срока кэша")
def r16_stale(ctx: RuleContext):
    p = ctx.parcel
    if not p.found:
        return None
    age = datetime.now(UTC) - p.fetched_at
    if age.days > ctx.config.stale_days:
        return Flag(
            "R16",
            Level.OK,
            f"данные НСПД от {p.fetched_at:%d.%m.%Y}, обновить перед сделкой",
            SRC_NSPD,
        )
    return None


@rule("R17", "Кадастровая стоимость", "Кадастровая стоимость не указана")
def r17_cost_missing(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or p.cadastral_cost_rub:
        return None
    return Flag("R17", Level.OK, "кадастровая стоимость в НСПД не указана", SRC_NSPD)


@rule(
    "R19",
    "Код классификатора ВРИ",
    "Формулировка ВРИ сопоставлена с кодом П/0412; старая или местная запись — справочно",
)
def r19_vri_code(ctx: RuleContext):
    p = ctx.parcel
    if not p.found:
        return None
    info = classifier.describe(p.permitted_use, p.permitted_use_tab)
    if info is None:
        return None
    main = info["main"]
    if main is None:
        first = info["unmatched"][0]
        return Flag(
            "R19",
            Level.OK,
            f"ВРИ «{first}» не сопоставлен с классификатором П/0412: формулировка местная или "
            "устаревшая — что именно разрешено, скажет ГПЗУ или выписка из ПЗЗ",
            classifier.SRC_P0412,
            {"codes": [], "unmatched": info["unmatched"]},
        )
    if not main["legacy"]:
        return None  # наименование или код из классификатора: достаточно строки в карточке
    text = (
        f"ВРИ «{main['wording']}» записан без кода классификатора; по смыслу это код "
        f"{main['code']} «{main['name']}». Запись действительна, соответствие коду "
        "администрация устанавливает по заявлению правообладателя бесплатно; в ГПЗУ код проставят"
    )
    if main.get("note"):
        text += f"; {main['note']}"
    return Flag(
        "R19",
        Level.OK,
        text,
        classifier.SRC_171,
        {"codes": info["codes"], "legacy": True},
    )
