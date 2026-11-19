"""«Горячие предложения» — лоты, по которым можно решать «заходим» (ТЗ v5, зум 18.11.2026).

Критерии сформулировали Тони и Антон М.: от 100 % годовых, цена покупки до 2 млн ₽, до конца
приёма заявок не один день, обременения не мешают. Список строится поверх обычной оценки лота
(`torgi_scan.score_lot`) и добавляет к ней три вещи.

1. Доходность в трёх сценариях. Обычная оценка считает годовые от цены старта — как будто
   конкуренции нет. Здесь порог применяется к ожидаемой цене победителя, а в осторожном
   сценарии (низ рынка, срок в полтора раза дольше) сделка не должна уходить в минус.
2. Сверка цены рынка. Прод, 19.11.2026: у первых 15 лотов по доходности «рынок» был выше
   старта в 4–40 раз. Сортировка по доходности первой поднимает ошибки цены, поэтому лот с
   неправдоподобным рынком уходит в отдельный список «Проверить рынок руками».
3. Класс надёжности. Наверху — самые доказанные лоты, а не самые большие проценты.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from landcheck import torgi_calc, torgi_scan

SOTKA = torgi_scan.SOTKA

START_RATIO = 5.0  # рынок за сотку выше старта во столько раз — рынку не верим
CAD_RATIO = 5.0  # то же против кадастровой стоимости участка
ALT_RATIO = 2.0  # два источника цены расходятся во столько раз — класс не выше B
MIN_DEALS_A = 5  # меньше сделок в выборке — класс не выше B
MIN_HOLD = 6  # короче срок до продажи в расчёт не берём, даже если так записано в слое рынка
CAUTIOUS_HOLD = 1.5  # осторожный сценарий: продаём в полтора раза дольше
LIQUID_INDEX = 55  # индекс ликвидности района, с которого район считаем ликвидным
RELAXED = (0.7, 0.5, 0.3)  # пороги для подсказки «если понизить порог — будет N лотов»
TOPS = (3, 5, 10, 25, 50)
# обременения, с которыми участок под перепродажу не берём вовсе (остальные зоны — класс B)
BLOCKING_ZONES = ("ООПТ", "лесопарк")
ZONES_UNKNOWN = {
    None: "обременения ещё не проверены",
    "noborders": "границы участка не установлены — обременения не определить",
    "notfound": "участок не найден в НСПД — обременения не проверить",
    "error": "НСПД не ответил — обременения не проверены",
}


@dataclass
class HotParams:
    """Критерии «горячего» — значения по умолчанию названы на зуме 18.11.2026."""

    min_annual: float = 1.0  # от 100 % годовых (Антон, 01:07)
    price_cap: float | None = 2_000_000.0  # итоговая цена покупки (Антон, 01:09)
    min_days: int = 3  # «один день остался — нафиг надо» (Антон, 01:07)
    kinds: tuple[str, ...] = ()
    top: int = 10
    min_profit: float | None = None


def _sotka(v: float | None) -> str:
    return torgi_calc.rub(v) if v else "—"


def price_check(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Сверка цены рынка: (почему цене нельзя верить, что снижает уверенность)."""
    s = row["s"]
    mk = s.get("market") or {}
    mid_m2 = mk.get("mid_m2")
    fail: list[str] = []
    warn: list[str] = []
    if not mid_m2:
        return fail, warn
    # У аренды рынок — цена переуступки, а старт — годовая плата: друг с другом не сверяются.
    # С кадастровой стоимостью сравниваем цену участка в собственности.
    ratio = mk.get("assign_ratio")
    own_m2 = mid_m2 / ratio if ratio else mid_m2
    start, area = row.get("price_start"), row.get("area_m2")
    if row.get("kind") == "sale" and start and area:
        k = mid_m2 / (start / area)
        if k > START_RATIO:
            fail.append(
                f"рынок {_sotka(mid_m2 * SOTKA)} за сотку — в {k:.0f} раз выше старта "
                f"{_sotka(start / area * SOTKA)}: начальную цену ставят по оценке или кадастровой "
                "стоимости, такой разрыв говорит о несопоставимых сделках"
            )
    cad = row.get("cad_cost")
    if cad and area:
        k = own_m2 / (cad / area)
        if k > CAD_RATIO:
            fail.append(
                f"рынок в {k:.0f} раз выше кадастровой стоимости участка "
                f"({_sotka(cad / area * SOTKA)} за сотку)"
            )
    ref = mk.get("quarter_ref")
    if ref:
        warn.append(
            f"в самом квартале {ref['n']} сдел. по {_sotka(ref['mid_m2'] * SOTKA)} за сотку — в "
            f"{ref['ratio']} раза выше района; взят район"
        )
    alt = mk.get("alt")
    if alt and alt.get("mid_m2"):
        k = max(mid_m2, alt["mid_m2"]) / min(mid_m2, alt["mid_m2"])
        if k > ALT_RATIO:
            warn.append(
                f"источники расходятся в {k:.1f} раза: {mk.get('source_label')} — "
                f"{_sotka(mid_m2 * SOTKA)} за сотку, {alt.get('label')} — "
                f"{_sotka(alt['mid_m2'] * SOTKA)}"
            )
    if mk.get("source") == "rr" and (mk.get("deals_n") or 0) < MIN_DEALS_A:
        warn.append(f"в выборке всего {mk.get('deals_n')} сдел. — меньше пяти")
    return fail, warn


def _one(
    entry: float | None, sale: float | None, months: float, extra: float, tax: float
) -> dict[str, Any] | None:
    if not entry or not sale:
        return None
    cost = entry + extra
    profit = (sale - cost) * (1 - tax)
    roi = profit / cost
    return {
        "entry": round(entry),
        "sale": round(sale),
        "months": round(months),
        "profit": round(profit),
        "roi": round(roi, 3),
        "annual": torgi_scan._annual(roi, months),
    }


def scenarios(row: dict[str, Any], extra: float, tax: float) -> dict[str, Any]:
    """Три сценария сделки. `alone` — берём по старту (конкуренции нет), `expected` — по
    ожидаемой цене победителя, `cautious` — по ней же, но продаём по низу рынка и в полтора раза
    дольше. Срок короче `MIN_HOLD` месяцев не берём: без данных о скорости продажи быстрый
    срок даёт сотни процентов годовых на пустом месте."""
    s = row["s"]
    start = row.get("price_start")
    expected = s.get("expected_price") or start
    mid, low = s["sale"].get("mid"), s["sale"].get("min")
    hold = max(MIN_HOLD, s.get("hold_months") or 12)
    return {
        "hold": hold,
        "alone": _one(start, mid, hold, extra, tax),
        "expected": _one(expected, mid, hold, extra, tax),
        "cautious": _one(expected, low or mid, hold * CAUTIOUS_HOLD, extra, tax),
    }


def cutoff_price(
    sale: float | None, hold: float, min_annual: float, extra: float, tax: float
) -> int | None:
    """Цена отсечения: наибольшая ставка, при которой годовая доходность ещё не ниже порога
    (Антон, зум 18.11, 01:01: «заявляться и ставить себе цену отсечения»)."""
    if not sale:
        return None
    roi = (1 + min_annual) ** (hold / 12) - 1
    limit = torgi_calc.limit_price(sale, extra, tax, roi)
    return round(limit) if limit and limit > 0 else None


def zones_of(row: dict[str, Any]) -> tuple[str, str]:
    """Обременения лота для отбора: (состояние, подпись). Состояния: `clean` — проверено,
    пересечений нет; `zones` — зоны есть, смотреть глазами; `block` — ООПТ или лесопарк;
    `unknown` — не проверяли или проверить не вышло."""
    state = row.get("zones_state")
    if state != "ok":
        return "unknown", ZONES_UNKNOWN.get(state, "обременения проверить не удалось")
    zones = row.get("zones") or []
    if not zones:
        return "clean", "обременений нет"
    if any(z.startswith(BLOCKING_ZONES) for z in zones):
        return "block", "; ".join(zones)
    return "zones", "; ".join(zones)


def klass(
    row: dict[str, Any], warn: list[str], scan_params: torgi_scan.ScanParams
) -> tuple[str, list[str]]:
    """Класс надёжности и чего не хватило до A. A — всё доказано: цена по кварталу или точнее,
    сделок достаточно, район ликвидный, обременений нет. C — цена только по району и район
    при этом не ликвидный либо цены расходятся вдвое: это гипотеза, а не предложение."""
    s = row["s"]
    mk = s.get("market") or {}
    why: list[str] = []
    conf = mk.get("confidence", 0)
    if conf < 2:
        why.append("цена рынка взята по району, а не по кварталу")
    why += warn
    spread = s.get("spread")
    if spread and spread >= torgi_scan.WIDE_SPREAD:
        why.append(f"разброс цен перепродажи — в {spread} раза")
    idx = s["liquidity"].get("index")
    if idx is None:
        why.append("ликвидность района не посчитать: мало завершённых торгов")
    elif idx < LIQUID_INDEX:
        why.append(f"ликвидность района не высокая: индекс {idx}")
    z_state, z_text = zones_of(row)
    if z_state == "zones":
        why.append(f"есть зоны, смотреть глазами: {z_text}")
    elif z_state == "unknown":
        why.append(z_text)
    band = scan_params.liquid_area.get(row.get("vri_group") or "")
    area = row.get("area_m2")
    if band and area and not band[0] <= area <= band[1]:
        why.append("площадь вне ходового диапазона")
    if s.get("repeats"):
        why.append("участок уже выставляли без результата")
    if row.get("appeals"):
        why.append("по торгам есть жалобы")
    if not why:
        return "A", why
    weak = conf < 2 and (
        idx is None
        or idx < scan_params.min_liquidity
        or bool(spread and spread >= torgi_scan.WIDE_SPREAD)
    )
    return ("C" if weak else "B"), why


def _passes(item: dict[str, Any], hp: HotParams, min_annual: float) -> str | None:
    """Первое условие, которое лот не прошёл (ключ шага воронки), или None — прошёл все,
    кроме сверки цены рынка: она проверяется последней, чтобы собрать список «проверить»."""
    row, sc = item["row"], item["sc"]
    s = row["s"]
    if not torgi_scan.in_market(row) or torgi_scan.is_notice(row):
        return "type"
    days = s.get("days_left")
    if s.get("stop") or days is None or days < hp.min_days:
        return "time"
    if hp.kinds and row.get("kind") not in hp.kinds:
        return "kind"
    exp = sc["expected"]
    if not s.get("market_based") or exp is None:
        return "market"
    if exp["annual"] is None or exp["annual"] < min_annual:
        return "annual"
    cautious = sc["cautious"]
    if cautious is None or cautious["profit"] < 0:
        return "cautious"
    if hp.price_cap and exp["entry"] + item["extra"] > hp.price_cap:
        return "cap"
    if hp.min_profit and exp["profit"] < hp.min_profit:
        return "profit"
    if item["zones"][0] == "block":
        return "zones"
    return None


STEPS = (
    ("type", "обычный земельный аукцион (не лес, не должники, не изъятые)"),
    ("time", "торги идут, времени на заявку хватает"),
    ("kind", "выбранная сделка"),
    ("market", "цена перепродажи взята с рынка"),
    ("annual", "доходность не ниже порога при ожидаемой цене победителя"),
    ("cautious", "в осторожном сценарии сделка не в минус"),
    ("cap", "цена входа укладывается в лимит"),
    ("profit", "прибыль не меньше заданной"),
    ("zones", "нет ООПТ и лесопарка"),
    ("price", "цена рынка прошла сверку"),
)


def select(
    rows: list[dict[str, Any]],
    scan_params: torgi_scan.ScanParams | None = None,
    params: HotParams | None = None,
) -> dict[str, Any]:
    """Отбор «горячих» из оценённых лотов (`torgi_scan.scan()["rows"]`).

    Возвращает: `hot` — классы A и B, лучшие первыми; `weak` — класс C (гипотезы); `check` —
    лоты, которые прошли бы отбор, но цена рынка не прошла сверку; `funnel` — сколько лотов
    осталось после каждого условия; `relaxed` — сколько лотов было бы при пороге ниже."""
    sp = scan_params or torgi_scan.ScanParams()
    hp = params or HotParams()
    items = []
    for row in rows:
        fail, warn = price_check(row)
        sc = scenarios(row, sp.extra, sp.tax)
        cls, cls_why = klass(row, warn, sp)
        items.append(
            {
                "row": row,
                "sc": sc,
                "extra": sp.extra,
                "fail": fail,
                "warn": warn,
                "cls": cls,
                "cls_why": cls_why,
                "zones": zones_of(row),
                "cutoff": cutoff_price(
                    row["s"]["sale"].get("mid"), sc["hold"], hp.min_annual, sp.extra, sp.tax
                ),
            }
        )
    left = {key: 0 for key, _ in STEPS}
    passed: list[dict[str, Any]] = []
    check: list[dict[str, Any]] = []
    order = [key for key, _ in STEPS]
    for item in items:
        stop = _passes(item, hp, hp.min_annual)
        if stop is None and item["fail"]:
            stop = "price"
            check.append(item)
        upto = order.index(stop) if stop else len(order)
        for key in order[:upto]:
            left[key] += 1
        if stop is None:
            passed.append(item)

    def rank(item: dict[str, Any]) -> tuple:
        return ("ABC".index(item["cls"]), -(item["sc"]["expected"]["annual"] or 0))

    passed.sort(key=rank)
    check.sort(key=lambda i: -(i["sc"]["expected"]["annual"] or 0))
    relaxed = {}
    for level in RELAXED:
        if level < hp.min_annual:
            relaxed[level] = sum(
                1 for i in items if not i["fail"] and _passes(i, hp, level) is None
            )
    return {
        "total": len(items),
        "hot": [i for i in passed if i["cls"] != "C"],
        "weak": [i for i in passed if i["cls"] == "C"],
        "check": check,
        "funnel": [
            {"key": key, "label": label, "left": left[key]}
            for key, label in STEPS
            if not (key == "kind" and not hp.kinds) and not (key == "profit" and not hp.min_profit)
        ],
        "relaxed": relaxed,
        "classes": {c: sum(1 for i in passed if i["cls"] == c) for c in "ABC"},
    }
