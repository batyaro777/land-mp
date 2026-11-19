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


def demand_check(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Есть ли спрос на такие участки в этом месте: (почему расчёту нельзя верить, что снижает
    уверенность). Судим по двум вещам, которые видны до торгов: чем кончались торги в квартале
    или районе лота и сколько сделок с такими участками зарегистрировал Росреестр."""
    s = row["s"]
    q = s["liquidity"]
    fail: list[str] = []
    warn: list[str] = []
    closed, none = q.get("closed") or 0, q.get("none") or 0
    where = "квартале" if q.get("where") == "квартал" else "районе"
    if closed >= CALM_MIN:
        share = none / closed
        if share >= NO_DEMAND_SHARE:
            fail.append(
                f"в этом {where} {none} из {closed} торгов за год прошли без единой заявки: "
                "по таким ценам участки здесь не берут, перепродажа под вопросом"
            )
        elif share >= THIN_DEMAND_SHARE:
            warn.append(f"в этом {where} без заявок прошли {none} из {closed} торгов за год")
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
    при этом не ликвидный либо цены расходятся вдвое, либо цена рынка не прошла сверку
    (`fail`): это гипотеза, а не предложение."""
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
    place = place_of(row)
    remote = place is not None and place["level"] == "weak"
    if remote:
        why.append("до участка трудно добраться: " + ", ".join(place["why"]))
    if place and place["bad"]:
        why.append("рядом: " + ", ".join(place["bad"]))
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
    # участок, до которого по карте не доехать, — гипотеза, какой бы ни была цена
    return ("C" if weak or remote else "B"), why


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
    quiet = (single + none) / closed
    if none / closed >= NO_DEMAND_SHARE:
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
            f"{single}, без заявок {none}"
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
        s = torgi_scan.score_lot(
            virt, stats, sp, layer=layer, today=today, near=near_fn(virt) if near_fn else None
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
