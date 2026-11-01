"""Калькулятор входа в торги с порогом доходности (ТЗ v3, п. 9 этап 4 и п. 13.7).

Спецификация — таблица Андрея «Калькулятор эффективности объектов»: ввод (площадь, начальная
цена, шаг аукциона, доп. расходы, налог с прибыли, аренда в месяц), три сценария продажи
MIN / MEDIUM / MAX и цель ROI (40 %). Предельная цена входа по сценарию:

    P = ((S − E) × (1 − t) − r × E) / (r + 1 − t)

где S — цена продажи, E — доп. расходы, t — налог с прибыли (доля), r — целевой ROI (доля).
Вывод: при покупке за P и расходах E прибыль после налога (S − P − E)(1 − t) равна r × (P + E).
«Безопасный ориентир» — минимум из предельных цен (обычно по сценарию MIN). Таблица по шагам
аукциона показывает, на каком шаге сделка выходит за порог: «считать до аукциона, чтобы зайти
без азарта» (Андрей, зум 28.10). Для лота в аренду «цена» — годовая арендная плата, а продажа —
переуступка права аренды.
"""

from __future__ import annotations

from typing import Any

DEFAULT_ROI = 0.40  # «royal 40 %» Андрея
DEFAULT_TAX = 0.13  # НДФЛ с прибыли физлица; для ИП/самозанятого подставить своё
MAX_STEPS = 40
SCENARIOS = (("min", "MIN"), ("mid", "MEDIUM"), ("max", "MAX"))


def limit_price(sale: float, extra: float, tax: float, roi: float) -> float | None:
    """Предельная цена покупки, при которой ROI после налога равен целевому."""
    denom = roi + (1 - tax)
    if denom <= 0 or sale <= 0:
        return None
    return ((sale - extra) * (1 - tax) - roi * extra) / denom


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def _pct(value: Any, default: float) -> float:
    """Процент из формы («40», «40 %», «0.4») → доля."""
    v = _num(value, default * 100)
    return v / 100 if v > 1 else v


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "area_m2": max(_num(raw.get("area")), 0.0),
        "price_start": max(_num(raw.get("start")), 0.0),
        "step": max(_num(raw.get("step")), 0.0),
        "extra": max(_num(raw.get("extra")), 0.0),
        "tax": min(max(_pct(raw.get("tax"), DEFAULT_TAX), 0.0), 0.99),
        "roi": max(_pct(raw.get("roi"), DEFAULT_ROI), 0.0),
        "rent_month": max(_num(raw.get("rent")), 0.0),
        "sale": {
            "min": max(_num(raw.get("smin")), 0.0),
            "mid": max(_num(raw.get("smid")), 0.0),
            "max": max(_num(raw.get("smax")), 0.0),
        },
        "steps": int(min(max(_num(raw.get("steps"), 15), 1), MAX_STEPS)),
        "kind": "аренда" if raw.get("kind") == "аренда" else "продажа",
        "lot_url": raw.get("lot_url") or None,
        "cad_num": raw.get("cad") or None,
    }


def _row(k: int, inp: dict[str, Any], limits: dict[str, float | None]) -> dict[str, Any]:
    price = inp["price_start"] + k * inp["step"]
    cost = price + inp["extra"]
    tax, roi = inp["tax"], inp["roi"]
    scen: dict[str, dict[str, Any]] = {}
    for key, _ in SCENARIOS:
        sale = inp["sale"][key]
        if sale <= 0:
            scen[key] = {"profit": None, "roi": None, "ok": None}
            continue
        profit = (sale - cost) * (1 - tax)
        scen[key] = {
            "profit": round(profit),
            "roi": round(profit / cost, 3) if cost > 0 else None,
            "ok": cost > 0 and profit / cost >= roi,
        }
    oks = [key for key, _ in SCENARIOS if scen[key]["ok"]]
    if not any(s["ok"] is not None for s in scen.values()):
        status = "нет сценариев"
    elif len(oks) == sum(1 for s in scen.values() if s["ok"] is not None):
        status = "в пределах по всем"
    elif oks:
        status = "только " + ", ".join(dict(SCENARIOS)[k_] for k_ in oks)
    else:
        status = "вне порога"
    rent_year = inp["rent_month"] * 12
    return {
        "step": k,
        "price": round(price),
        "cost": round(cost),
        "per_m2": round(price / inp["area_m2"], 1) if inp["area_m2"] else None,
        "payback_years": round(cost / rent_year, 1) if rent_year > 0 and cost > 0 else None,
        "scenarios": scen,
        "within_safe": limits.get("safe") is not None and price <= limits["safe"],
        "status": status,
    }


def compute(raw: dict[str, Any]) -> dict[str, Any]:
    """Полный расчёт для формы: предельные цены, безопасный ориентир, таблица по шагам."""
    inp = normalize(raw)
    limits: dict[str, float | None] = {}
    for key, _ in SCENARIOS:
        sale = inp["sale"][key]
        lp = limit_price(sale, inp["extra"], inp["tax"], inp["roi"]) if sale > 0 else None
        limits[key] = round(lp) if lp is not None else None
    known = [v for v in limits.values() if v is not None]
    limits["safe"] = min(known) if known else None
    rows = [_row(k, inp, limits) for k in range(inp["steps"] + 1)]
    # последний «безопасный» шаг — из предельной цены, а не из длины таблицы
    safe = limits["safe"]
    last_ok: int | None = None
    if safe is not None and inp["price_start"] > 0 and safe >= inp["price_start"]:
        last_ok = int((safe - inp["price_start"]) // inp["step"]) if inp["step"] > 0 else 0
    rent_year = inp["rent_month"] * 12
    cost0 = inp["price_start"] + inp["extra"]
    return {
        "inputs": inp,
        "limits": limits,
        "entry": {
            "price": round(inp["price_start"]),
            "cost": round(cost0),
            "per_m2": round(inp["price_start"] / inp["area_m2"], 1) if inp["area_m2"] else None,
            "rent_year": round(rent_year) if rent_year else None,
            "payback_years": round(cost0 / rent_year, 1) if rent_year > 0 and cost0 > 0 else None,
        },
        "rows": rows,
        "last_safe_step": last_ok,
        "verdict": _verdict(inp, limits, last_ok),
    }


def _verdict(inp: dict[str, Any], limits: dict[str, float | None], last_ok: int | None) -> str:
    safe = limits.get("safe")
    if safe is None:
        return "задайте хотя бы один сценарий цены продажи"
    if inp["price_start"] <= 0:
        return "задайте начальную цену лота"
    if safe < inp["price_start"]:
        return (
            f"уже начальная цена выше безопасного ориентира {round(safe):,} ₽ — "
            "при целевом ROI входить не стоит".replace(",", " ")
        )
    if last_ok is None:
        return "ни один шаг не укладывается в ориентир"
    return (
        f"безопасный ориентир {round(safe):,} ₽ — выходить из торгов после шага {last_ok} "
        f"(цена {round(inp['price_start'] + last_ok * inp['step']):,} ₽)"
    ).replace(",", " ")


def defaults_from_valuation(valuation: dict[str, Any] | None) -> dict[str, float]:
    """Сценарии MIN/MEDIUM/MAX из нашего ориентира стоимости (диапазон за весь участок)."""
    if not valuation:
        return {}
    rng = valuation.get("total_range")
    if not rng or rng[0] is None or rng[1] is None:
        return {}
    lo, hi = float(rng[0]), float(rng[1])
    return {"smin": round(lo), "smid": round((lo + hi) / 2), "smax": round(hi)}
