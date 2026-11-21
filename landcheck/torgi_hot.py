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

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from landcheck import nearby, torgi_calc, torgi_scan

SOTKA = torgi_scan.SOTKA

START_RATIO = 5.0  # рынок за сотку выше старта во столько раз — рынку не верим
CAD_RATIO = 5.0  # то же против кадастровой стоимости участка
ALT_RATIO = 2.0  # два источника цены расходятся во столько раз — класс не выше B
MIN_DEALS_A = 5  # меньше сделок в выборке — класс не выше B
MIN_HOLD = 6  # короче срок до продажи в расчёт не берём, даже если так записано в слое рынка
CAUTIOUS_HOLD = 1.5  # осторожный сценарий: продаём в полтора раза дольше
LIQUID_INDEX = 55  # индекс ликвидности района, с которого район считаем ликвидным
ASK_PRICE = (
    "цена взята по объявлениям с поправкой на торг: это гипотеза, пока её не подтвердят сделки"
)
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


# Спрос на торгах в месте лота. Замер на проде 19.11.2026: 269 лотов из 5 980 завершённых
# аукционов продажи прошли бы в «горячие»; там, где без заявок прошла половина торгов места
# и больше, без покупателя остались 18 из 21 (86 %), где 20–49 % — 33 %, где меньше — 14 %.
NO_DEMAND_SHARE = 0.5
THIN_DEMAND_SHARE = 0.2


# Прод, 21.11.2026: «Выбор системы» поставил первым лот в Новгородской области, а проверка на
# завершённых торгах там же показывает, что больше половины «горячих» остались без заявок
# (Новгородская 56 %, Пермский край 55 %, Краснодарский 50 % при 22 % в среднем и 2 % в
# Московской области). Отбор прошёл все проверки места и всё равно ошибается в этих регионах
# через раз — значит, лот оттуда не доказан, пока его не посмотрел человек.
TRUST_MIN = 10  # «горячих» на истории региона, с которых о точности отбора в нём можно судить
TRUST_NONE = 0.4  # доля оставшихся без заявок, с которой отбору в регионе не верим
TRUST_GOOD = 0.15  # доля не выше — отбор в регионе подтверждается, это довод «за»


def trust_of(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Проверка отбора в регионе по его же завершённым торгам (`backtest`): сколько лотов
    прошли бы в «горячие» и сколько из них остались без заявок. None — проверки ещё нет."""
    outcomes = (raw or {}).get("hot_outcomes") or {}
    n = sum(outcomes.values())
    if not n:
        return None
    none = outcomes.get("none", 0)
    return {"n": n, "none": none, "share": round(none / n, 2)}


def shaky(s: dict[str, Any]) -> bool:
    """В регионе лота отбор на истории слишком часто выбирал участки, на которые никто не
    пришёл."""
    trust = s.get("trust")
    return bool(trust and trust["n"] >= TRUST_MIN and trust["share"] >= TRUST_NONE)


# Прод, 21.11.2026, «горячие» на завершённых торгах всех регионов (321 лот): у участков без
# прежних попыток без заявок остались 22 % торгов; у тех, кого дважды и больше выставляли по
# той же цене, — 86 % (14 лотов); у повторных со сниженным стартом — 23 % (13 лотов), и больше
# половины из них ушли единственному участнику. Не берут не «повторные», а «по той же цене».
REPEAT_STOP = 2  # столько раз участок не взяли по той же цене — спроса на него по ней нет
REPEAT_CUT = 0.97  # старт ниже прошлого хотя бы на 3 % — цену снизили


def repeat_cut(s: dict[str, Any]) -> float | None:
    """На сколько снижен старт против прошлой попытки (доля, 0,25 — на четверть); None — не
    снижен или сравнить не с чем."""
    ratio = s.get("repeat_ratio")
    return round(1 - ratio, 3) if ratio is not None and ratio < REPEAT_CUT else None


def outcomes_unread(row: dict[str, Any]) -> bool:
    """Итог не прочитан у половины торгов места и больше: о спросе там судить рано. Прод,
    20.11.2026: три лота вышли в класс A и в утреннюю сводку, хотя у семи из восьми торгов
    квартала итог не был прочитан; после чтения оказалось — семь из восьми без заявок."""
    q = row["s"]["liquidity"]
    closed, unknown = q.get("closed") or 0, q.get("unknown") or 0
    return closed >= CALM_MIN and unknown * 2 >= closed


def demand_check(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Есть ли спрос на такие участки в этом месте: (почему расчёту нельзя верить, что снижает
    уверенность). Судим по двум вещам, которые видны до торгов: чем кончались торги в квартале
    или районе лота и сколько сделок с такими участками зарегистрировал Росреестр."""
    s = row["s"]
    q = s["liquidity"]
    fail: list[str] = []
    warn: list[str] = []
    closed, none = q.get("closed") or 0, q.get("none") or 0
    unknown = q.get("unknown") or 0
    known = (q.get("sold") or 0) + (q.get("single") or 0) + none  # торги с известным итогом
    where = "квартале" if q.get("where") == "квартал" else "районе"
    if known >= CALM_MIN:
        share = none / known
        if share >= NO_DEMAND_SHARE:
            fail.append(
                f"в этом {where} {none} из {known} торгов за год прошли без единой заявки: "
                "по таким ценам участки здесь не берут, перепродажа под вопросом"
            )
        elif share >= THIN_DEMAND_SHARE:
            warn.append(f"в этом {where} без заявок прошли {none} из {known} торгов за год")
    if outcomes_unread(row):
        warn.append(
            f"итоги {unknown} из {closed} торгов в этом {where} ещё не прочитаны: были ли на "
            "них заявки, пока неизвестно"
        )
    same = s.get("repeats_same") or 0
    if same >= REPEAT_STOP and s.get("repeat_ratio") is not None and repeat_cut(s) is None:
        fail.append(
            f"участок уже выставляли {torgi_scan.times_ru(same)} без результата, и цена с тех "
            "пор не снижена: по этой цене его не берут"
        )
    turn = s.get("turnover")
    if turn and turn["quarter_n"] == 0:
        warn.append(
            "в квартале за год ни одной сделки с такими участками "
            f"(в районе — {turn['district_n']}): срок продажи в расчёте увеличен"
        )
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


# --- сводка как защита сделки (зум 20.11.2026) --------------------------------------------------
# Практик на первом просмотре: «не очевидно, по какой цене я его могу продать», «доходность от
# какой цены — от начальной или от цены отсечения?», «ROI при продаже за 2,5 и за 3,5», «по
# три месяца: продали за три — такая доходность, за шесть — такая; больше двенадцати не берём,
# будем сбрасывать цену», «аргументы за, аргументы против и риски».
TERMS = (3, 6, 9, 12)  # за сколько месяцев продали — строки таблицы сроков
RANGE = (("min", "низ рынка"), ("mid", "середина рынка"), ("max", "верх рынка"))
PRO_RATIO = 1.3  # рынок выше цены входа во столько раз — это довод «за»
BIG_DEPOSIT = 0.5  # задаток от начальной цены, с которого о нём предупреждаем
SOON_DAYS = 7  # до конца приёма заявок меньше — предупреждаем


def sale_range(item: dict[str, Any], scan_params: torgi_scan.ScanParams) -> list[dict[str, Any]]:
    """Что даёт сделка при покупке по ожидаемой цене победителя и продаже по низу, середине и
    верху рынка: цена продажи, прибыль после налога, ROI и годовая доходность за срок расчёта."""
    e = item["sc"]["expected"]
    if not e:
        return []
    s = item["row"]["s"]
    out = []
    for key, label in RANGE:
        one = _one(
            e["entry"], s["sale"].get(key), item["sc"]["hold"], scan_params.extra, scan_params.tax
        )
        if one:
            out.append({"key": key, "label": label, "sotka": s["sale_sotka"].get(key), **one})
    return out


def by_term(item: dict[str, Any], scan_params: torgi_scan.ScanParams) -> list[dict[str, Any]]:
    """Доходность, если продать за 3, 6, 9 и 12 месяцев: по середине рынка и со скидкой до его
    низа (продать быстрее можно, уступив в цене). Вход — ожидаемая цена победителя."""
    e = item["sc"]["expected"]
    if not e:
        return []
    sale = item["row"]["s"]["sale"]
    mid, low = sale.get("mid"), sale.get("min")
    rows = []
    for months in TERMS:
        rows.append(
            {
                "months": months,
                "mid": _one(e["entry"], mid, months, scan_params.extra, scan_params.tax),
                "low": (
                    _one(e["entry"], low, months, scan_params.extra, scan_params.tax)
                    if low and low != mid
                    else None
                ),
            }
        )
    return rows


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return "больше 1000 %" if v > 10 else f"{round(v * 100)} %"


def case_of(
    item: dict[str, Any], scan_params: torgi_scan.ScanParams, params: HotParams
) -> dict[str, list[str]]:
    """Доводы по сделке: `pro` — что говорит за неё, `con` — что против и чего не хватает,
    `risk` — что может пойти не так даже при верном расчёте. Только то, что видно из данных
    лота: каждый довод с числом."""
    row, sc = item["row"], item["sc"]
    s = row["s"]
    e = sc["expected"]
    mk = s.get("market") or {}
    pro: list[str] = []
    con: list[str] = [*item["fail"], *item["nodemand"]]
    risk: list[str] = []
    if e:
        mid, low = s["sale"].get("mid"), s["sale"].get("min")
        if mid and mid / e["entry"] >= PRO_RATIO:
            pro.append(
                f"середина рынка выше ожидаемой цены входа в {mid / e['entry']:.1f} раза: "
                f"вход {torgi_calc.rub(e['entry'])}, продажа {torgi_calc.rub(mid)}"
            )
        at_low = _one(e["entry"], low, sc["hold"], scan_params.extra, scan_params.tax)
        if at_low and low != mid:
            if at_low["profit"] > 0:
                pro.append(
                    "даже при продаже по низу рынка сделка в плюсе: "
                    f"{torgi_calc.rub(at_low['profit'])} после налога, ROI {_pct(at_low['roi'])}"
                )
            else:
                risk.append(
                    "при продаже по низу рынка сделка уходит в минус: "
                    f"{torgi_calc.rub(at_low['profit'])}"
                )
        start = row.get("price_start")
        if item["cutoff"] and start and item["cutoff"] > start:
            pro.append(
                f"запас на торгах: от старта {torgi_calc.rub(start)} до цены отсечения "
                f"{torgi_calc.rub(item['cutoff'])} — в {item['cutoff'] / start:.1f} раза"
            )
        cautious = sc["cautious"]
        if cautious and (cautious["annual"] is None or cautious["annual"] < params.min_annual):
            risk.append(
                f"в осторожном сценарии (низ рынка, продажа за {cautious['months']} мес.) "
                f"доходность {_pct(cautious['annual'])} годовых — ниже порога "
                f"{round(params.min_annual * 100)} %"
            )
    comp = item.get("comp")
    if comp and comp["level"] == "calm":
        pro.append(f"{comp['text']}: {comp['detail']}")
    elif comp and comp["level"] == "fight":
        risk.append(f"на торгах {comp['text']}: {comp['detail']}")
    if mk.get("deals_level") == "quarter":
        n = mk.get("deals_n")
        pro.append(
            "цена взята по сделкам Росреестра в самом кадастровом квартале"
            + (f" ({n} сдел.)" if n else "")
            + ", а не по району"
        )
    turn = s.get("turnover")
    if turn and turn["quarter_n"] >= torgi_scan.LIVE_DEALS:
        pro.append(
            f"место продаётся: в квартале за год {turn['quarter_n']} сдел. с такими участками"
        )
    idx = s["liquidity"].get("index")
    if idx is not None and idx >= LIQUID_INDEX:
        pro.append(f"район ликвидный: индекс {idx} из 100")
    if item["zones"][0] == "clean":
        pro.append("обременений и охранных зон на участке нет")
    trust = s.get("trust")
    if trust and trust["n"] >= TRUST_MIN and trust["share"] <= TRUST_GOOD:
        pro.append(
            f"в этом регионе отбор подтверждается: из {trust['n']} таких же лотов прошлых "
            f"торгов покупателя нашли {trust['n'] - trust['none']}"
        )
    place = item.get("place")
    if place and place["level"] == "good":
        pro.append("доступность хорошая: " + ", ".join(place["why"]))
    if row.get("has_gpzu"):
        pro.append("к лоту приложен градостроительный план")
    seen = set(con)
    con += [t for t in item["cls_why"] if t not in seen]
    if sc["hold"] > TERMS[-1]:
        risk.append(
            f"срок продажи в расчёте — {sc['hold']} мес.: сделок с такими участками в квартале "
            "за год не было"
        )
    deposit, start = row.get("deposit"), row.get("price_start")
    if deposit and start and deposit / start >= BIG_DEPOSIT:
        risk.append(
            f"задаток {torgi_calc.rub(deposit)} — {round(deposit / start * 100)} % начальной "
            "цены, вносится до торгов"
        )
    days = s.get("days_left")
    if days is not None and 0 <= days < SOON_DAYS:
        risk.append(f"до конца приёма заявок {days} дн.: на выезд и проверку времени мало")
    return {"pro": pro, "con": con, "risk": risk}


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


def place_of(row: dict[str, Any]) -> dict[str, Any] | None:
    """Доступность места по карте (ТЗ v5, п. 7): уровень, из чего он сложен, что портит место
    и строки для сводки. None — карту по этому лоту ещё не спрашивали или она не ответила."""
    facts = row.get("nearby")
    if row.get("nearby_state") != "ok" or facts is None:
        return None
    return {**nearby.access(facts), "bad": nearby.nuisances(facts), "lines": nearby.lines(facts)}


def klass(
    row: dict[str, Any],
    warn: list[str],
    scan_params: torgi_scan.ScanParams,
    fail: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Класс надёжности и чего не хватило до A. A — всё доказано: цена по кварталу или точнее,
    сделок достаточно, район ликвидный, обременений нет. C — цена только по району и район
    при этом не ликвидный либо цены расходятся вдвое, либо цена взята по объявлениям, либо
    цена рынка не прошла сверку (`fail`): это гипотеза, а не предложение."""
    s = row["s"]
    mk = s.get("market") or {}
    why: list[str] = []
    conf = mk.get("confidence", 0)
    if conf < 2:
        why.append("цена рынка взята по району, а не по кварталу")
    if mk.get("source") == "ads":
        # прод, 20.11.2026: в первый час работы ленты объявлений «горячих» стало 157
        # вместо 15 и десять лотов вышли в класс A. Объявление — цена, которую хотят, а не
        # цена, по которой купили. После поправки на торг проверка на завершённых торгах
        # дала у таких лотов 38 % торгов без заявок (6 из 16) против 17 % у оценённых по
        # сделкам квартала — это уровень оценки по району, то есть гипотеза
        why.append(ASK_PRICE)
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
    place = place_of(row)
    remote = place is not None and place["level"] == "weak"
    if remote:
        why.append("до участка трудно добраться: " + ", ".join(place["why"]))
    if place and place["bad"]:
        why.append("рядом: " + ", ".join(place["bad"]))
    unproven = shaky(s)
    if unproven:
        trust = s["trust"]
        why.append(
            f"в этом регионе отбор часто ошибается: из {trust['n']} таких же лотов прошлых "
            f"торгов {trust['none']} остались без заявок ({round(trust['share'] * 100)} %)"
        )
    if fail:
        # цена рынка не прошла сверку — это гипотеза, какими бы ни были остальные признаки
        return "C", [*fail, *why]
    if not why:
        return "A", why
    weak = conf < 2 and (
        idx is None
        or idx < scan_params.min_liquidity
        or bool(spread and spread >= torgi_scan.WIDE_SPREAD)
    )
    by_ads = mk.get("source") == "ads"
    # участок, до которого по карте не доехать, цена по объявлениям и регион, где отбор на
    # истории ошибается через раз, — гипотезы
    return ("C" if weak or remote or by_ads or unproven else "B"), why


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


def towns_of(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Города рядом с лотом — из справочника, сети не нужно: видны и до ответа карты. Считаем
    только для страницы одного лота (прод, 20.11.2026: поиск по 2 675 городам для каждого из
    восьми тысяч лотов стоил «Горячим» две секунды на каждый запрос)."""
    if row.get("lat") is None or row.get("lon") is None:
        return []
    return nearby.lines(nearby.far_places(row["lat"], row["lon"]))


def item_of(
    row: dict[str, Any], scan_params: torgi_scan.ScanParams, params: HotParams
) -> dict[str, Any]:
    """Всё, что «Горячие» знают об одном оценённом лоте: сценарии, цена отсечения, сверка
    цены рынка, класс, обременения и условие, на котором лот выбыл (`stop`; None — прошёл)."""
    fail, warn = price_check(row)
    nodemand, thin = demand_check(row)
    warn = warn + thin
    sc = scenarios(row, scan_params.extra, scan_params.tax)
    cls, cls_why = klass(row, warn, scan_params, fail + nodemand)
    item = {
        "row": row,
        "sc": sc,
        "extra": scan_params.extra,
        "fail": fail,
        "nodemand": nodemand,
        "warn": warn,
        "cls": cls,
        "cls_why": cls_why,
        "zones": zones_of(row),
        "place": place_of(row),
        "cutoff": cutoff_price(
            row["s"]["sale"].get("mid"),
            sc["hold"],
            params.min_annual,
            scan_params.extra,
            scan_params.tax,
        ),
    }
    stop = _passes(item, params, params.min_annual)
    item["stop"] = stop or ("price" if fail else "demand" if nodemand else None)
    item["comp"] = competition(row)
    return item


CALM_SHARE = 0.6  # доля торгов места без борьбы, с которой ждём покупку по старту
CALM_MIN = 5  # меньше завершённых торгов в месте — о конкуренции не судим


def competition(row: dict[str, Any]) -> dict[str, Any] | None:
    """Чем кончались торги в месте лота (квартал, а если там мало торгов — район) и что из
    этого следует. Числа заявок у идущего лота площадка не публикует (проверено на проде
    19.11.2026), поэтому конкуренцию оцениваем по истории (Антон, зум 18.11, 01:01: «есть
    участки, которые забирают без конкуренции»)."""
    q = row["s"]["liquidity"]
    closed = q.get("closed") or 0
    if closed < CALM_MIN:
        return None
    sold, single, none = q.get("sold") or 0, q.get("single") or 0, q.get("none") or 0
    unknown = q.get("unknown") or 0
    known = sold + single + none
    # без борьбы — и торги одного участника, и торги без заявок, и те, чей итог не прочитан:
    # не состоявшиеся торги борьбой не были в любом случае
    quiet = (closed - sold) / closed
    if known >= CALM_MIN and none / known >= NO_DEMAND_SHARE:
        level, text = "empty", "заявок здесь обычно нет"
    elif quiet >= CALM_SHARE:
        level, text = "calm", "скорее всего без борьбы"
    elif sold / closed >= CALM_SHARE:
        level, text = "fight", "обычно с борьбой"
    else:
        level, text = "mixed", "бывает по-разному"
    where = "квартале" if q.get("where") == "квартал" else "районе"
    return {
        "level": level,
        "text": text,
        "detail": (
            f"в этом {where} {closed} торгов за год: с борьбой {sold}, одному участнику "
            f"{single}, без заявок {none}" + (f", итог не прочитан у {unknown}" if unknown else "")
        ),
    }


STEP_RU = {
    "type": "это не обычный земельный аукцион",
    "time": "торги закончились, остановлены или до конца приёма заявок слишком мало времени",
    "kind": "сделка не та, что выбрана в критериях",
    "market": "цены перепродажи с рынка нет",
    "annual": "доходность при ожидаемой цене победителя ниже порога",
    "cautious": "в осторожном сценарии сделка уходит в минус",
    "cap": "ожидаемая цена входа выше лимита",
    "profit": "прибыль меньше заданной",
    "zones": "участок в ООПТ или лесопарке",
    "price": "цена рынка не прошла сверку",
    "demand": "торги в этом месте проходят без заявок: спроса нет",
}

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
    ("demand", "на торги в этом месте приходят покупатели"),
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
    items = [item_of(row, sp, hp) for row in rows]
    left = {key: 0 for key, _ in STEPS}
    passed: list[dict[str, Any]] = []
    check: list[dict[str, Any]] = []
    order = [key for key, _ in STEPS]
    for item in items:
        stop = item["stop"]
        if stop in ("price", "demand"):
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
                1
                for i in items
                if not i["fail"] and not i["nodemand"] and _passes(i, hp, level) is None
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


# --- «Выбор системы» (ТЗ v5, п. 9) ---------------------------------------------------------------
# Тони, 20.11.2026: нужны «самые ликвидные, самые недооценённые участки, которые мы можем продать
# быстрее всего и дороже всего». Пользователь задаёт только бюджет или ничего. Система ставит
# лоты всех регионов в один ряд по одной мере и по каждому называет стратегию, на которой он
# выбран. Все числа считает код; текст объяснения собран из этих чисел.
STRATEGY_RU = {
    "calm": "ниже рынка, торги без борьбы",
    "fight": "ниже рынка с запасом на борьбу",
    "repeat": "повторные торги",
    "lease": "аренда под переуступку",
}
BEST_TOPS = (5, 10, 20)
PLAN_CELLS = 4000  # на столько шагов делим бюджет при подборе набора лотов
PLAN_MAX_LOTS = 60


def measure(item: dict[str, Any]) -> float:
    """Мера, по которой система сравнивает сделки: годовая доходность в осторожном сценарии —
    покупаем по ожидаемой цене победителя, продаём по низу рынка и в полтора раза дольше. В ней
    сразу и недооценка (низ рынка против цены входа), и скорость (срок продажи по обороту
    сделок места), и расходы с налогом."""
    c = item["sc"]["cautious"]
    return c["annual"] if c and c["annual"] is not None else -1.0


def strategy_of(item: dict[str, Any]) -> tuple[str, str]:
    """Стратегия, на которой лот выбран, и что из неё следует для ставки."""
    row = item["row"]
    e = item["sc"]["expected"]
    if row.get("kind") == "rent":
        return "lease", (
            "на торгах покупается право аренды, продаётся переуступка; её цена взята как доля "
            "цены такого же участка в собственности — долю вы задали сами"
        )
    n = row["s"].get("repeats") or 0
    if n:
        cut = repeat_cut(row["s"])
        lower = f", старт с тех пор снижен на {round(cut * 100)} %" if cut else ""
        return "repeat", (
            f"участок уже выставляли {torgi_scan.times_ru(n)} без результата{lower}: соперников, "
            "скорее всего, не будет, вход рядом со стартом; почему его не взяли раньше — "
            "проверить на месте"
        )
    comp = item.get("comp")
    if comp and comp["level"] == "calm":
        return "calm", f"{comp['detail']} — рассчитываем купить по старту или рядом с ним"
    start = row.get("price_start")
    grow = f" (старт × {e['entry'] / start:.2f})" if e and start and e["entry"] != start else ""
    cutoff = f"; ставим не выше {torgi_calc.rub(item['cutoff'])}" if item.get("cutoff") else ""
    return "fight", (
        f"в расчёт заложена цена победителя {torgi_calc.rub(e['entry'])}{grow}: и при ней "
        f"сделка проходит порог{cutoff}"
    )


def dims_of(item: dict[str, Any]) -> dict[str, Any]:
    """Четыре числа, из которых складывается выбор: во сколько раз рынок выше входа, сколько
    сделок с такими участками в квартале за год, срок продажи в расчёте и чем здесь обычно
    кончаются торги."""
    row, sc = item["row"], item["sc"]
    s = row["s"]
    e = sc["expected"]
    mid, low = s["sale"].get("mid"), s["sale"].get("min")
    turn = s.get("turnover") or {}
    comp = item.get("comp")
    return {
        "under": round(mid / e["entry"], 1) if e and mid else None,
        "under_low": round(low / e["entry"], 1) if e and low else None,
        "deals_quarter": turn.get("quarter_n"),
        "deals_district": turn.get("district_n"),
        "hold": sc["hold"],
        "comp": comp["text"] if comp else None,
        "liquidity": s["liquidity"].get("index"),
        "trust": s.get("trust"),
    }


def plan_of(
    items: list[dict[str, Any]], budget: float | None, extra: float = 0.0
) -> dict[str, Any] | None:
    """Набор лотов под бюджет: какие сделки взять, чтобы прибыль в осторожном сценарии была
    наибольшей, а сумма цен входа с расходами не вышла за бюджет. Точная задача о рюкзаке на
    бюджете, поделённом на `PLAN_CELLS` шагов. None — бюджет не задан или лотов нет."""
    pool = [
        i
        for i in items[:PLAN_MAX_LOTS]
        if i["sc"]["expected"] and i["sc"]["cautious"] and i["sc"]["cautious"]["profit"] > 0
    ]
    if not budget or budget <= 0 or not pool:
        return None
    step = max(1.0, budget / PLAN_CELLS)
    cap = int(budget // step)
    costs = [i["sc"]["expected"]["entry"] + extra for i in pool]
    weights = [max(1, math.ceil(c / step)) for c in costs]
    values = [i["sc"]["cautious"]["profit"] for i in pool]
    best_at = [0.0] * (cap + 1)
    took = [[False] * (cap + 1) for _ in pool]
    for n, (w, v) in enumerate(zip(weights, values, strict=True)):
        for c in range(cap, w - 1, -1):
            if best_at[c - w] + v > best_at[c]:
                best_at[c] = best_at[c - w] + v
                took[n][c] = True
    chosen, c = [], cap
    for n in range(len(pool) - 1, -1, -1):
        if took[n][c]:
            chosen.append(n)
            c -= weights[n]
    chosen.reverse()
    picked = [pool[n] for n in chosen]
    if not picked:
        return None
    cost = sum(costs[n] for n in chosen)
    deposits = [i["row"].get("deposit") for i in picked]
    return {
        "budget": round(budget),
        "lots": picked,
        "cost": round(cost),
        "left": round(budget - cost),
        "profit": sum(i["sc"]["expected"]["profit"] for i in picked),
        "cautious": sum(i["sc"]["cautious"]["profit"] for i in picked),
        # задаток вносится до торгов по каждому лоту; у кого карточка не прочитана — неизвестен
        "deposit": round(sum(d for d in deposits if d)),
        "deposit_unknown": sum(1 for d in deposits if not d),
    }


def best(
    rows: list[dict[str, Any]],
    scan_params: torgi_scan.ScanParams | None = None,
    *,
    budget: float | None = None,
    top: int = 10,
    skip: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """«Выбор системы»: лучшие сделки всех регионов одним списком и набор под бюджет.

    Отбор тот же, что у «Горячих» с критериями по умолчанию (от 100 % годовых при ожидаемой
    цене победителя, осторожный сценарий не в минус, сверка цены и спроса пройдена), лимит цены
    входа — бюджет. Порядок: сначала класс (A — всё доказано), внутри класса — по `measure`.
    `skip` — лоты, которые пользователь убрал сам."""
    sp = scan_params or torgi_scan.ScanParams()
    hp = HotParams(price_cap=budget or None, top=top)
    res = select(rows, sp, hp)

    def keep(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [i for i in items if not (skip and skip(i))]
        return sorted(kept, key=lambda i: ("ABC".index(i["cls"]), -measure(i)))

    hot, weak = keep(res["hot"]), keep(res["weak"])
    for item in (*hot, *weak):
        item["strategy"], item["strategy_why"] = strategy_of(item)
        item["dims"] = dims_of(item)
    lease_waiting = 0
    if sp.assign_ratio is None:
        lease_waiting = sum(
            1
            for r in rows
            if r.get("kind") == "rent"
            and torgi_scan.in_market(r)
            and not torgi_scan.is_notice(r)
            and not r["s"].get("stop")
            and (r["s"].get("days_left") or 0) >= hp.min_days
        )
    by_strategy: dict[str, int] = {}
    for item in hot:
        by_strategy[item["strategy"]] = by_strategy.get(item["strategy"], 0) + 1
    return {
        "total": res["total"],
        "passed": len(hot),
        "picks": hot[:top],
        "plan": plan_of(hot, budget, sp.extra),
        "weak": weak[:top],
        "weak_n": len(weak),
        "check_n": len(res["check"]),
        "funnel": res["funnel"],
        "classes": res["classes"],
        "by_strategy": by_strategy,
        "lease_waiting": lease_waiting,
        "params": hp,
    }


# --- большой участок под раздел: гипотеза (ТЗ v5, п. 9, стратегия 4) ------------------------------
SPLIT_FACTOR = 3.0  # участок больше верхней границы ходовой площади во столько раз — большой
SPLIT_PLOT = 1000.0  # на какие участки делим, м²
SPLIT_USE = 0.8  # доля площади, которая останется участками после проездов — допущение
SPLIT_MIN_RATIO = 3.0  # выручка от участков к цене входа, с которой гипотезу показываем
SPLIT_MAX_AREA = 100_000.0  # крупнее — это проект планировки территории, а не межевание


def split_candidates(
    rows: list[dict[str, Any]],
    market_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
    scan_params: torgi_scan.ScanParams | None = None,
    *,
    min_days: int = 3,
    top: int = 10,
) -> list[dict[str, Any]]:
    """Большие участки на продажу, которые после раздела на участки по десять соток стоили бы
    по сделкам своего квартала в разы дороже цены входа. Это гипотеза, а не расчёт сделки:
    можно ли делить, сколько уйдёт под проезды, сколько стоит и длится межевание — код не
    знает. Цена участка после раздела берётся только по сделкам Росреестра в том же квартале."""
    if market_fn is None:
        return []
    sp = scan_params or torgi_scan.ScanParams()
    out = []
    for row in sorted(rows, key=lambda r: r.get("region") or ""):  # округ за округом
        s = row["s"]
        band = sp.liquid_area.get(row.get("vri_group") or "")
        area, start = row.get("area_m2"), row.get("price_start")
        if not band or not area or not start or row.get("kind") != "sale":
            continue
        if not band[1] * SPLIT_FACTOR <= area <= SPLIT_MAX_AREA:
            continue
        if not torgi_scan.in_market(row) or torgi_scan.is_notice(row):
            continue
        if s.get("stop") or (s.get("days_left") or 0) < min_days:
            continue
        mk = market_fn({**row, "area_m2": SPLIT_PLOT})
        if not mk or mk.get("deals_level") != "quarter":
            continue
        plots = int(area * SPLIT_USE // SPLIT_PLOT)
        entry = s.get("expected_price") or start
        gross = plots * SPLIT_PLOT * mk["mid_m2"]
        if plots < 2 or gross / entry < SPLIT_MIN_RATIO:
            continue
        out.append(
            {
                "row": row,
                "plots": plots,
                "plot_price": round(SPLIT_PLOT * mk["mid_m2"]),
                "gross": round(gross),
                "entry": round(entry),
                "ratio": round(gross / entry, 1),
                "deals_n": mk.get("deals_n"),
                "deals_span": mk.get("deals_span"),
            }
        )
    out.sort(key=lambda x: -x["ratio"])
    return out[:top]


# --- проверка модели на завершённых торгах (ТЗ v5, п. 8) ---------------------------------------
RETURN_DAYS = 183  # «вернутся на торги»: участки без заявок за последние полгода
RETURN_MAX = 60  # столько строк списка храним на регион
ACCURACY_BAND = 0.3  # ожидаемая цена «попала», если отличается от итоговой не больше чем на 30 %


def _lot_day(lot: dict[str, Any]) -> str:
    return lot.get("auction_date") or lot.get("bidd_end") or lot.get("published") or ""


def backtest(
    lots: list[dict[str, Any]],
    scan_params: torgi_scan.ScanParams | None = None,
    params: HotParams | None = None,
    *,
    layer: list[dict[str, Any]] | None = None,
    near_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Проверка модели на завершённых земельных аукционах продажи: каждый лот оцениваем так,
    как если бы он шёл сейчас, и сравниваем с тем, чем торги кончились.

    Что проверяется: прошёл бы лот в «горячие», как ожидаемая цена победителя соотносится с
    итоговой, сколько таких лотов ушло одному участнику и осталось без заявок, какой вышла
    бы доходность при фактической цене входа. Чего проверить нельзя: цену, по которой
    победитель потом продал участок, — сторона продажи остаётся расчётной. Оценка слегка
    оптимистична: итог самого лота входит в статистику роста цены его места.

    Возвращает сырые счётчики и выборки (их складывают по регионам, см. `merge_backtests`):
    `relist` — во сколько раз менялся старт, когда участок выставляли снова после торгов без
    результата; `returning` — участки без заявок за последние полгода, которые сейчас не на
    торгах и прошли бы отбор."""
    sp = scan_params or torgi_scan.ScanParams()
    hp = params or HotParams()
    today = today or datetime.now(UTC).date()
    stats = torgi_scan.index_by(lots)
    virtual_end = (today + timedelta(days=30)).isoformat()
    recent = (today - timedelta(days=RETURN_DAYS)).isoformat()
    active_cads = {lot.get("cad_num") for lot in lots if lot.get("status") in torgi_scan.ACTIVE}
    raw: dict[str, Any] = {
        "lots": 0,
        "outcomes": {"sold": 0, "single": 0, "none": 0},
        "priced": 0,
        "check": 0,
        "nodemand": 0,  # сняты: в месте лота торги проходят без заявок
        "nodemand_outcomes": {"sold": 0, "single": 0, "none": 0},
        "hot": 0,
        "hot_outcomes": {"sold": 0, "single": 0, "none": 0},
        "hot_ratio": [],  # итоговая цена к ожидаемой у «горячих», которые кто-то купил
        "hot_within_cutoff": 0,  # из них: итоговая цена не выше цены отсечения
        "hot_annual": [],  # годовых при фактической цене входа
        "all_ratio": [],  # итоговая цена к ожидаемой у всех торгов с борьбой и ценой рынка
        "relist": [],
        "returning": [],
    }
    # прежние попытки продать участок без результата — на день каждого лота: правило «не берут
    # по той же цене» проверяется на истории так же, как работает на идущих лотах
    attempts: dict[tuple[str, str], list[tuple[str, float | None]]] = {}
    for lot in lots:
        if lot.get("cad_num") and torgi_scan._outcome(lot) in ("none", "failed", "canceled"):
            key = (lot["cad_num"], lot.get("kind") or "sale")
            attempts.setdefault(key, []).append((_lot_day(lot), lot.get("price_start")))
    for found in attempts.values():
        found.sort(key=lambda a: a[0])
    by_cad: dict[str, list[dict[str, Any]]] = {}
    for lot in lots:
        if (
            torgi_scan.is_notice(lot)
            or not torgi_scan.in_market(lot)
            or lot.get("kind") != "sale"
            or not lot.get("price_start")
            or not lot.get("area_m2")
        ):
            continue
        if lot.get("cad_num"):
            by_cad.setdefault(lot["cad_num"], []).append(lot)
        outcome = torgi_scan._outcome(lot)
        if outcome not in raw["outcomes"]:
            continue
        raw["lots"] += 1
        raw["outcomes"][outcome] += 1
        virt = {**lot, "status": "APPLICATIONS_SUBMISSION", "bidd_end": virtual_end}
        cad, day = lot.get("cad_num") or "", _lot_day(lot)
        earlier = [a for a in attempts.get((cad, "sale"), ()) if a[0] < day]
        s = torgi_scan.score_lot(
            virt,
            stats,
            sp,
            layer=layer,
            today=today,
            near=near_fn(virt) if near_fn else None,
            repeats={cad: len(earlier)} if earlier else None,
            prior={(cad, "sale"): (len(earlier), earlier[-1][1])} if earlier else None,
        )
        if not s.get("market_based"):
            continue
        raw["priced"] += 1
        item = item_of({**virt, "s": s}, sp, hp)
        expected = item["sc"]["expected"]
        start, fin = lot["price_start"], lot.get("price_fin")
        # единственный участник по закону покупает по начальной цене
        actual = (fin or None) if outcome == "sold" else start if outcome == "single" else None
        if outcome == "sold" and actual and expected:
            raw["all_ratio"].append(round(actual / expected["entry"], 3))
        if item["stop"] == "price":
            raw["check"] += 1
        elif item["stop"] == "demand":
            raw["nodemand"] += 1
            raw["nodemand_outcomes"][outcome] += 1
        if item["stop"] is not None:
            continue
        raw["hot"] += 1
        raw["hot_outcomes"][outcome] += 1
        if actual and expected:
            raw["hot_ratio"].append(round(actual / expected["entry"], 3))
            if item["cutoff"] and actual <= item["cutoff"]:
                raw["hot_within_cutoff"] += 1
            real = _one(actual, expected["sale"], item["sc"]["hold"], sp.extra, sp.tax)
            if real and real["annual"] is not None:
                raw["hot_annual"].append(real["annual"])
        if (
            outcome == "none"
            and _lot_day(lot) >= recent
            and lot.get("cad_num")
            and lot["cad_num"] not in active_cads
        ):
            raw["returning"].append(
                {
                    "id": lot["id"],
                    "cad_num": lot["cad_num"],
                    "region": lot.get("region"),
                    "district": lot.get("district"),
                    "address": lot.get("address"),
                    "area_m2": lot["area_m2"],
                    "vri_group": lot.get("vri_group"),
                    "day": _lot_day(lot),
                    "price_start": start,
                    "sale_sotka": s["sale_sotka"].get("mid"),
                    "source": (s.get("market") or {}).get("source_label"),
                    "annual": item["sc"]["alone"]["annual"] if item["sc"]["alone"] else None,
                    "cls": item["cls"],
                }
            )
    for attempts in by_cad.values():
        if len(attempts) < 2:
            continue
        attempts.sort(key=_lot_day)
        for prev, nxt in zip(attempts, attempts[1:], strict=False):
            if torgi_scan._outcome(prev) in ("none", "failed", "canceled") and _lot_day(nxt) > (
                _lot_day(prev)
            ):
                raw["relist"].append(round(nxt["price_start"] / prev["price_start"], 3))
    raw["returning"].sort(key=lambda x: -(x["annual"] or 0))
    raw["returning"] = raw["returning"][:RETURN_MAX]
    return raw


def merge_backtests(raws: list[dict[str, Any]]) -> dict[str, Any]:
    """Сложить сырые итоги нескольких регионов в один."""
    out: dict[str, Any] = {}
    for raw in raws:
        for key, value in raw.items():
            if isinstance(value, dict):
                box = out.setdefault(key, {})
                for k, v in value.items():
                    box[k] = box.get(k, 0) + v
            elif isinstance(value, list):
                out.setdefault(key, []).extend(value)
            else:
                out[key] = out.get(key, 0) + value
    if out.get("returning"):
        out["returning"] = sorted(out["returning"], key=lambda x: -(x["annual"] or 0))
    return out


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def backtest_summary(raw: dict[str, Any], min_annual: float = 1.0) -> dict[str, Any]:
    """Цифры для страницы «Насколько точна модель» из сырых итогов `backtest`."""

    def spread(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"n": 0, "median": None, "within": None, "low": None, "high": None}
        ordered = sorted(values)
        return {
            "n": len(ordered),
            "median": round(statistics.median(ordered), 2),
            "low": round(ordered[len(ordered) // 4], 2),
            "high": round(ordered[(len(ordered) * 3) // 4], 2),
            "within": _share(sum(1 for v in ordered if abs(v - 1) <= ACCURACY_BAND), len(ordered)),
        }

    hot, ho = raw.get("hot", 0), raw.get("hot_outcomes", {})
    bought = ho.get("sold", 0) + ho.get("single", 0)
    annual = raw.get("hot_annual", [])
    relist = raw.get("relist", [])
    return {
        "lots": raw.get("lots", 0),
        "outcomes": raw.get("outcomes", {}),
        "priced": raw.get("priced", 0),
        "check": raw.get("check", 0),
        "nodemand": raw.get("nodemand", 0),
        "nodemand_none_share": _share(
            raw.get("nodemand_outcomes", {}).get("none", 0), raw.get("nodemand", 0)
        ),
        "hot": hot,
        "hot_outcomes": ho,
        "hot_single_share": _share(ho.get("single", 0), hot),
        "hot_none_share": _share(ho.get("none", 0), hot),
        "hot_sold_share": _share(ho.get("sold", 0), hot),
        "hot_bought": bought,
        "hot_ratio": spread(raw.get("hot_ratio", [])),
        "hot_within_cutoff": _share(raw.get("hot_within_cutoff", 0), bought),
        "hot_annual_median": round(statistics.median(annual), 2) if annual else None,
        "hot_annual_ok": _share(sum(1 for v in annual if v >= min_annual), len(annual)),
        "all_ratio": spread(raw.get("all_ratio", [])),
        "relist_n": len(relist),
        "relist_median": round(statistics.median(relist), 2) if relist else None,
        "relist_lower": _share(sum(1 for v in relist if v < 0.99), len(relist)),
        "relist_same": _share(sum(1 for v in relist if 0.99 <= v <= 1.01), len(relist)),
        "returning": len(raw.get("returning", [])),
    }
