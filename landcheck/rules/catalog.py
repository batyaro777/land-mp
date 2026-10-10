"""Каталог риск-правил М1. Каждое — одно предложение понятным языком и источник."""

from __future__ import annotations

from datetime import UTC, datetime

from landcheck.models import SOTKA_M2, Flag, Level
from landcheck.rules.engine import RuleContext, rule
from landcheck.rules.purpose import (
    CATEGORY_CLASS_LABEL,
    CATEGORY_MATRIX,
    category_class,
    vri_match,
)

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
    matrix = CATEGORY_MATRIX[ctx.request.purpose]
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
    return Flag(
        "R04",
        level,
        f"цель «{ctx.request.purpose.label}»: " + text.format(cat=CATEGORY_CLASS_LABEL[cls]),
        SRC_ZK7,
        {"category": p.category, "category_class": cls},
    )


@rule("R05", "ВРИ против цели", "Вид разрешённого использования не соответствует цели")
def r05_vri(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or ctx.request.purpose.value == "any":
        return None
    res = vri_match(ctx.request.purpose, p.permitted_use_all)
    label = ctx.request.purpose.label
    vri = p.permitted_use or (p.permitted_use_tab[0] if p.permitted_use_tab else "—")
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


@rule("R10", "ЗОУИТ", "Пересечение с зонами с особыми условиями использования")
def r10_zouit(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or not p.zouit_checked:
        return None
    if not p.zouit:
        return None
    names = "; ".join(z.describe() for z in p.zouit[:4])
    more = f" и ещё {len(p.zouit) - 4}" if len(p.zouit) > 4 else ""
    return Flag(
        "R10",
        Level.CHECK,
        f"участок пересекает ЗОУИТ ({len(p.zouit)}): {names}{more} — уточнить ограничения на строительство",
        SRC_NSPD_ZOUIT + "; " + SRC_LAW_ZOUIT,
        {"zouit": [z.describe() for z in p.zouit]},
    )


@rule("R11", "Территориальная зона", "Зона ПЗЗ не опубликована в НСПД")
def r11_territorial_zone(ctx: RuleContext):
    p = ctx.parcel
    if not p.found or not p.territorial_zone_checked:
        return None
    if not p.territorial_zones:
        return Flag(
            "R11",
            Level.CHECK,
            "территориальная зона в НСПД не найдена: ПЗЗ не опубликованы, уточнять в администрации",
            SRC_PZZ,
        )
    names = "; ".join(z.describe() for z in p.territorial_zones[:3])
    return Flag(
        "R11",
        Level.OK,
        f"территориальная зона по НСПД: {names}",
        SRC_NSPD_TZ,
        {"zones": [z.describe() for z in p.territorial_zones]},
    )


@rule("R12", "Форма собственности", "Публичная или неразграниченная собственность")
def r12_ownership(ctx: RuleContext):
    p = ctx.parcel
    if not p.found:
        return None
    o = (p.ownership_type or "").lower()
    if not o:
        return Flag(
            "R12",
            Level.CHECK,
            "форма собственности в НСПД не указана: права могут быть не зарегистрированы, запросить выписку ЕГРН",
            SRC_NSPD,
        )
    if "частн" in o:
        return None
    if any(k in o for k in ("публичн", "государств", "муниципальн", "не разграничен", "федеральн")):
        return Flag(
            "R12",
            Level.CHECK,
            f"собственность «{p.ownership_type}»: продавец может быть арендатором, покупка только через торги",
            SRC_NSPD,
        )
    return Flag(
        "R12",
        Level.CHECK,
        f"форма собственности «{p.ownership_type}»: проверить по выписке",
        SRC_NSPD,
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
