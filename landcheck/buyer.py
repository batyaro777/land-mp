"""Кабинет покупателя (ТЗ v2, К1 и М4): уточняющие вопросы по шкале осознанности и подбор
из базы верифицированных объявлений под цель покупателя.

Вопросы — детерминированный банк по цели (то, что спросил бы эксперт), без LLM: честно, быстро,
проверяемо. Подбор: фильтры запроса (бюджет, площадь, регион) плюс быстрый вердикт
«цель × категория × ВРИ» по фактам ЕГРН объявления; полный трёхуровневый отчёт — по ссылке.
"""

from __future__ import annotations

import re
from typing import Any

from landcheck.models import SOTKA_M2, Level, Purpose
from landcheck.rules.purpose import CATEGORY_MATRIX, category_class, vri_match, vri_verdict

# key, вопрос, зачем спрашиваем, варианты (значение, подпись)
Question = dict[str, Any]

_Q_LIVING: Question = {
    "key": "living",
    "text": "Как будете использовать дом?",
    "why": "Постоянное жильё — дорога зимой, школа и газ важнее; дача — важнее тишина и вода",
    "options": [
        ("permanent", "Постоянное проживание"),
        ("dacha", "Дача, сезонно"),
        ("invest", "Инвестиция, перепродажа"),
    ],
}
_Q_KIDS: Question = {
    "key": "kids",
    "text": "Дети школьного или детсадовского возраста?",
    "why": "Школа и детсад в радиусе 3 км — в отчёте по участку есть расстояния",
    "options": [("yes", "Да"), ("no", "Нет")],
}
_Q_GAS: Question = {
    "key": "gas",
    "text": "Газ обязателен или хватит электричества?",
    "why": "Газ у границы — разница в стоимости отопления и в цене участка",
    "options": [("required", "Газ обязателен"), ("electric", "Хватит электричества")],
}
_Q_BUDGET: Question = {
    "key": "budget_scope",
    "text": "Бюджет — только на участок или на участок с домом?",
    "why": "Если на всё, на участок обычно уходит не больше трети бюджета",
    "options": [("land", "Только участок"), ("land_house", "Участок и дом")],
}
_Q_HORIZON: Question = {
    "key": "horizon",
    "text": "На какой срок покупаете?",
    "why": "Для перепродажи важны ликвидность и планы района, для себя — комфорт сейчас",
    "options": [("own", "Для себя, надолго"), ("flip", "Перепродать через 1–3 года")],
}
_Q_SEASON: Question = {
    "key": "season",
    "text": "Пользоваться летом или круглый год?",
    "why": "Круглый год — подъезд и электричество зимой, иначе участок полгода недоступен",
    "options": [("summer", "Летом"), ("year", "Круглый год")],
}
_Q_WATER: Question = {
    "key": "water",
    "text": "Нужна вода для полива или скважина?",
    "why": "Вода на участке — главный вопрос для огорода и хозяйства",
    "options": [("yes", "Да, нужна"), ("no", "Не критично")],
}
_Q_ACCESS: Question = {
    "key": "access",
    "text": "Нужен подъезд для техники и грузовиков?",
    "why": "Узкая дорога или сервитут через соседей закроют стройку и хозяйство",
    "options": [("yes", "Да"), ("no", "Нет")],
}
_Q_POWER: Question = {
    "key": "power",
    "text": "Нужна большая мощность электричества (от 30 кВт)?",
    "why": "Для производства и сервиса техусловия на мощность — отдельная история",
    "options": [("yes", "Да"), ("no", "Нет")],
}
_Q_TRAFFIC: Question = {
    "key": "traffic",
    "text": "Нужен трафик с трассы или важна тишина?",
    "why": "Коммерции нужна трасса, отдыху — тишина; расстояние до трассы есть в отчёте",
    "options": [("traffic", "Трафик с трассы"), ("quiet", "Тишина")],
}

QUESTIONS: dict[Purpose, list[Question]] = {
    Purpose.IZHS: [_Q_LIVING, _Q_KIDS, _Q_GAS, _Q_BUDGET, _Q_HORIZON],
    Purpose.LPH: [_Q_LIVING, _Q_WATER, _Q_GAS, _Q_ACCESS, _Q_HORIZON],
    Purpose.SNT: [_Q_SEASON, _Q_WATER, _Q_GAS, _Q_HORIZON],
    Purpose.FARMING: [_Q_WATER, _Q_ACCESS, _Q_POWER, _Q_HORIZON],
    Purpose.RECREATION: [_Q_TRAFFIC, _Q_WATER, _Q_POWER, _Q_HORIZON],
    Purpose.COMMERCE: [_Q_TRAFFIC, _Q_POWER, _Q_ACCESS, _Q_HORIZON],
    Purpose.INDUSTRY: [_Q_POWER, _Q_ACCESS, _Q_HORIZON],
    Purpose.MULTIFAMILY: [_Q_POWER, _Q_ACCESS, _Q_HORIZON],
    Purpose.ANY: [_Q_LIVING, _Q_BUDGET, _Q_HORIZON],
}


def questions_for(purpose: Purpose) -> list[Question]:
    return QUESTIONS.get(purpose, QUESTIONS[Purpose.ANY])


# Что покупателю важно → как это ищем в тексте продавца (только «по словам продавца»).
_CLAIM_PATTERNS: dict[str, tuple[str, re.Pattern[str]]] = {
    "gas": ("газ", re.compile(r"\bгаз", re.I)),
    "water": ("вода / скважина", re.compile(r"скважин|водопровод|колодец|вода\b|водоснабж", re.I)),
    "access": ("подъезд", re.compile(r"подъезд|дорог|асфальт|круглый год|круглогодичн", re.I)),
    "power": ("электричество", re.compile(r"электр|квт|мощност", re.I)),
    "kids": ("школа / детсад", re.compile(r"школ|детсад|детский сад", re.I)),
}


def seller_claims(text: str, answers: dict[str, str]) -> list[dict[str, Any]]:
    """Что покупателю важно по ответам и упоминает ли это продавец. Не проверка, а подсказка."""
    out = []
    wanted = {
        "gas": answers.get("gas") == "required",
        "water": answers.get("water") == "yes",
        "access": answers.get("access") == "yes" or answers.get("season") == "year",
        "power": answers.get("power") == "yes",
        "kids": answers.get("kids") == "yes",
    }
    for key, (label, rx) in _CLAIM_PATTERNS.items():
        if not wanted.get(key):
            continue
        out.append({"key": key, "label": label, "mentioned": bool(rx.search(text or ""))})
    return out


def quick_verdict(purpose: Purpose, category: str | None, vri: str | None) -> dict[str, Any]:
    """Быстрый вердикт «цель × категория × ВРИ» по фактам ЕГРН объявления (без терзоны)."""
    if purpose is Purpose.ANY:
        return {"level": Level.OK.value, "text": "цель не задана: подходит любое назначение"}
    cls = category_class(category)
    level, text = Level.OK, "категория и ВРИ допускают цель"
    cat_rule = CATEGORY_MATRIX.get(purpose, {}).get(cls or "")
    if cat_rule is not None:
        level, text = cat_rule
    special = vri_verdict(purpose, vri or "", cls)
    if special is not None:
        s_level, s_text, _ = special
        if s_level.rank >= level.rank:
            level, text = s_level, s_text
    else:
        m = vri_match(purpose, vri or "")
        if m == "conflict" and Level.STOP.rank > level.rank:
            level, text = Level.STOP, f"ВРИ «{vri}» несовместим с целью"
        elif m == "unknown" and level is Level.OK and vri:
            level, text = Level.CHECK, f"ВРИ «{vri}»: совместимость с целью уточнить"
    return {"level": level.value, "text": text}


def match_listing(
    card: dict[str, Any],
    *,
    purpose: Purpose,
    budget_rub: float | None,
    min_area_m2: float | None,
    max_area_m2: float | None,
    region: str | None,
    answers: dict[str, str],
    text: str = "",
) -> dict[str, Any] | None:
    """Проходит ли объявление фильтры запроса; если да — вердикт, причины и балл для сортировки."""
    hay = f"{card.get('address') or ''} {card.get('title') or ''}".lower()
    if region and region.lower() not in hay:
        return None
    price = card.get("price_rub")
    if budget_rub and price and price > budget_rub:
        return None
    area_m2 = (card.get("area_sotka") or 0) * SOTKA_M2
    if min_area_m2 and area_m2 and area_m2 < min_area_m2 * 0.9:
        return None
    if max_area_m2 and area_m2 and area_m2 > max_area_m2 * 1.1:
        return None
    verdict = quick_verdict(purpose, card.get("category"), card.get("permitted_use"))
    claims = seller_claims(text, answers)
    score = {"ок": 100.0, "проверить": 60.0, "стоп": 0.0}[verdict["level"]]
    score += 5 * sum(1 for c in claims if c["mentioned"]) - 5 * sum(
        1 for c in claims if not c["mentioned"]
    )
    if budget_rub and price:
        score += 10 * (1 - price / budget_rub)  # дешевле бюджета — небольшой плюс
    return {**card, "verdict": verdict, "claims": claims, "score": round(score, 1)}


def rank(cards: list[dict[str, Any]], texts: dict[int, str], **kw) -> list[dict[str, Any]]:
    out = []
    for c in cards:
        m = match_listing(c, text=texts.get(c["id"], ""), **kw)
        if m is not None:
            out.append(m)
    out.sort(key=lambda m: (-m["score"], m.get("price_rub") or 0))
    return out


def region_from_text(text: str) -> str | None:
    """«… в Истре», «Истра», «Алтай», «Московская область» — последнее слово с заглавной
    после предлога или в конце фразы; без геокодера это подсказка, не правило."""
    m = re.search(r"\b(?:в|во|под|около|рядом с|район)\s+([А-ЯЁ][а-яё\-]+(?:\s+[а-яё\-]+)?)", text)
    if m:
        return m.group(1).strip()
    words = [w for w in re.findall(r"[А-ЯЁ][а-яё\-]{2,}", text)]
    return words[-1] if words else None
