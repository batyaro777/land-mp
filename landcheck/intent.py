"""Разбор запроса брокера текстом: цель, бюджет, площадь. Без LLM — ключевые слова и числа.

Голосовой ввод и уточняющие вопросы (М1.2) — следующий шаг поверх этой функции.
"""

from __future__ import annotations

import re

from landcheck.inputs import parse_number
from landcheck.models import SOTKA_M2, Purpose, Request

# Порядок важен: сначала более специфичные цели, чтобы «многоэтажный дом» не стал ИЖС,
# а «фермерское хозяйство» — ЛПХ.
PURPOSE_KEYWORDS: dict[Purpose, tuple[str, ...]] = {
    Purpose.MULTIFAMILY: (
        "многоквартир",
        "многоэтаж",
        "среднеэтаж",
        "таунхаус",
        "застройщик",
        "жилой комплекс",
        "пятиэтаж",
        "девелоп",
    ),
    Purpose.INDUSTRY: ("пром", "производств", "склад", "логист", "цех", "завод", "ангар"),
    Purpose.COMMERCE: (
        "коммерц",
        "магазин",
        "торгов",
        "офис",
        "сервис",
        "автомойк",
        "гостиниц",
        "азс",
        "кафе",
        "ресторан",
        "шиномонтаж",
    ),
    Purpose.RECREATION: (
        "рекреац",
        "отдых",
        "туризм",
        "глэмпинг",
        "кемпинг",
        "база отдыха",
        "гостевые дома",
        "курорт",
    ),
    Purpose.FARMING: (
        "ферм",
        "кфх",
        "выращив",
        "сельхозпроизв",
        "сельхоз",
        "посев",
        "пашн",
        "теплиц",
        "сад яблон",
        "скотовод",
        "животновод",
        "пасек",
        "разводить",
    ),
    Purpose.LPH: (
        "лпх",
        "подсобн",
        "хозяйств",
        "коз",
        "овец",
        "куры",
        "кур ",
        "кур,",
        "курей",
        "куриц",
        "курят",
        "кроли",
        "скот",
        "пасти",
        "животн",
        "птиц",
        "огород и",
    ),
    Purpose.SNT: ("снт", "дач", "садов", "огород", "сезонн"),
    Purpose.IZHS: (
        "ижс",
        "под дом",
        "жилой дом",
        "построить дом",
        "построиться",
        "жилищн",
        "коттедж",
        "пмж",
        "для жизни",
        "жить",
        "дом для семьи",
        "дом",
    ),
}


_BUDGET_RE = re.compile(
    r"(?:до|бюджет|не дороже|максимум|в пределах|за)\s*([\d\s .,]+\s*(?:млн|тыс|руб|₽|р\.|k)?)",
    re.IGNORECASE,
)
_AREA_RANGE_RE = re.compile(
    r"(?:от\s*)?([\d.,]+)\s*(?:до|[-–—])\s*([\d.,]+)\s*(сот|га|м2|м²|кв)", re.IGNORECASE
)
_AREA_RE = re.compile(r"([\d.,]+)\s*(сот|га|м2|м²|кв\.?\s*м)", re.IGNORECASE)


def _has_keyword(text: str, key: str) -> bool:
    """Ключ должен начинаться на границе слова: «кур» в «Белокуриха» — не куры (прод, 19.10)."""
    return re.search(r"(?<![а-яёa-z])" + re.escape(key), text) is not None


def detect_purpose(text: str) -> Purpose:
    t = text.lower()
    for purpose, keys in PURPOSE_KEYWORDS.items():
        if any(_has_keyword(t, k) for k in keys):
            return purpose
    return Purpose.ANY


def _area_to_m2(value: float, unit: str) -> float:
    u = unit.lower()
    if u.startswith("га"):
        return value * 10_000
    if u.startswith("сот"):
        return value * SOTKA_M2
    return value


def parse_request(text: str) -> Request:
    t = text.strip()
    purpose = detect_purpose(t)
    budget: float | None = None
    m = _BUDGET_RE.search(t)
    if m:
        v = parse_number(m.group(1))
        # «до 3» без единиц читаем как млн, если число маленькое.
        if v is not None and v < 1000 and not re.search(r"млн|тыс|руб|₽", m.group(1), re.I):
            v *= 1_000_000
        if v is not None and v >= 10_000:
            budget = v
    min_area = max_area = None
    rm = _AREA_RANGE_RE.search(t)
    if rm:
        unit = rm.group(3)
        min_area = _area_to_m2(float(rm.group(1).replace(",", ".")), unit)
        max_area = _area_to_m2(float(rm.group(2).replace(",", ".")), unit)
        if min_area > max_area:
            min_area, max_area = max_area, min_area
    else:
        am = _AREA_RE.search(t)
        if am:
            v = _area_to_m2(float(am.group(1).replace(",", ".")), am.group(2))
            # Одно число — трактуем как минимум с допуском +50%.
            min_area, max_area = v * 0.9, v * 1.5
    return Request(purpose=purpose, budget_rub=budget, min_area_m2=min_area, max_area_m2=max_area)
