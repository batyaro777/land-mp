"""«Итог простыми словами» в конце отчёта — для клиента-непрофессионала.

Эксперт в чате «МП Земля» (06.11, сообщение 25): отчёт стал информативнее, но нужен общий вывод
по итогу «не для профи». Блок собирается без модели, только из уже посчитанного: вердикт, цель,
флаги, рыночная оценка и наличие ГПЗУ. Каждый риск — одной бытовой фразой, без статей закона;
подробности и источники остаются в разделах выше.
"""

from __future__ import annotations

from typing import Any

# Риск одной фразой без терминов: (стоп, проверить). None — взять текст флага.
PLAIN: dict[str, tuple[str | None, str | None]] = {
    "R00": ("данные из госреестра сейчас не получены — проверку нужно повторить", None),
    "R01": ("участка с таким номером нет в госреестре", None),
    "R02": (
        "у участка в реестре нерабочий статус — сделку по нему могут не зарегистрировать",
        "статус участка в реестре нужно подтвердить свежей выпиской",
    ),
    "R03": (
        "границы участка не установлены — без межевания сделку не зарегистрируют",
        "границы участка не установлены — нужно межевание до сделки",
    ),
    "R04": (
        "категория земли не подходит под вашу цель — сменить её долго и без гарантии",
        "категорию земли нужно уточнить по выписке",
    ),
    "R05": (
        "по документам на участке нельзя то, что вы планируете (вид разрешённого использования)",
        "разрешённое использование участка нужно уточнить",
    ),
    "R06": (None, "площадь в объявлении не совпадает с площадью по документам"),
    "R07": (None, "площадь не уточнена межеванием — фактическая может отличаться"),
    "R08": (None, "цена сильно отличается от государственной оценки — выясните причину"),
    "R09": (None, "цена заметно отличается от других участков в подборке — выясните причину"),
    "R10": (
        "через участок проходят охранные зоны — часть земли застраивать нельзя",
        "участок задевают охранные зоны (сети, дороги, вода) — часть земли может быть "
        "под ограничениями",
    ),
    "R11": (
        "по правилам застройки муниципалитета здесь нельзя то, что вы планируете",
        "что разрешено строить по правилам застройки, нужно подтвердить документом (ГПЗУ)",
    ),
    "R12": (
        "участок государственный или муниципальный — купить у частного лица нельзя, "
        "только через администрацию",
        "участок государственный или муниципальный — оформлять через администрацию",
    ),
    "R13": (None, "участок продаётся в нескольких объявлениях — проверьте, кто реальный продавец"),
    "R14": (None, "цена выше вашего бюджета"),
    "R15": (None, "площадь не попадает в нужный вам диапазон"),
    "R16": (None, "данные реестра устарели — нужна свежая выписка"),
    "R17": (None, "нет кадастровой стоимости — сравнить цену с госоценкой нельзя"),
    "R18": (
        "на участке зарегистрированы обременения или арест — сделка под угрозой",
        "обременения по участку нужно проверить по выписке ЕГРН",
    ),
    "R20": (
        "документ администрации (ГПЗУ) противоречит данным реестра",
        "данные ГПЗУ и реестра расходятся — уточнить в администрации",
    ),
    "R21": (
        "получить участок выбранным путём (торги, аренда, выкуп) не выйдет",
        "путь получения участка (торги, аренда, выкуп) нужно уточнить",
    ),
    "R22": (
        "участок в границах особо охраняемой природной территории — строить нельзя "
        "или почти нельзя",
        "участок рядом с природоохранной территорией — проверить ограничения",
    ),
    "R23": (
        "участок в лесопарковом поясе — строительство почти запрещено",
        "участок в лесопарковом поясе — проверить, что разрешено",
    ),
    "R24": (None, "у участка сложная история образования — юристу проверить цепочку"),
    "R25": (None, "участок или его квартал упоминается в документах администрации — прочитать"),
}

MAX_REASONS = 3
NEAR = 0.1  # до 10 % за границей диапазона — «у границы рынка», не повод для выводов


def _questions(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "вопрос"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "вопроса"
    return "вопросов"


def _flag_phrase(f: dict[str, Any]) -> str:
    stop, check = PLAIN.get(f.get("rule_id") or "", (None, None))
    text = stop if f.get("level") == "стоп" else check
    if text:
        return text
    msg = (f.get("message") or "").strip()
    # «ВРИ «…» несовместим с целью: без смены ВРИ…» — первая смысловая часть
    for sep in (": ", " — "):
        if sep in msg[12:]:
            msg = msg[: 12 + msg[12:].index(sep)]
            break
    return msg[:160]


def _reasons(flags: list[dict[str, Any]], level: str) -> list[str]:
    out: list[str] = []
    for f in flags:
        if f.get("level") != level:
            continue
        phrase = _flag_phrase(f)
        if phrase and phrase not in out:
            out.append(phrase)
    return out


def _price_line(valuation: dict[str, Any] | None, data: dict[str, Any]) -> str | None:
    pps = data.get("price_per_sotka")
    if not valuation:
        return None
    if valuation.get("has_market") and valuation.get("range") and pps:
        low, high = valuation["range"]
        where = (valuation.get("range_source") or "").replace("объявления ", "")
        if low <= pps <= high:
            return f"Цена в рынке: в середине предложений ({where})."
        if pps > high:
            over = pps / high - 1
            if over < NEAR:
                return f"Цена у верхней границы рынка (выше на {over:.0%}, {where})."
            return f"Цена выше рынка на {over:.0%} ({where}) — есть повод торговаться."
        under = 1 - pps / low
        if under < NEAR:
            return f"Цена у нижней границы рынка (ниже на {under:.0%}, {where})."
        return (
            f"Цена ниже рынка на {under:.0%} ({where}) — дёшево неспроста: "
            "выясните причину до задатка."
        )
    if valuation.get("has_market") and not pps:
        return "Цена в подборке не указана — рыночный диапазон смотрите в блоке «Рынок»."
    if pps:
        return (
            "Рыночную цену по открытым объявлениям оценить не удалось — сравните с похожими "
            "участками рядом на досках объявлений сами."
        )
    return None


def _next_steps(
    data: dict[str, Any], verdict: str, goal: dict[str, Any] | None, gpzu: dict | None
) -> list[str]:
    flags = data.get("flags") or []
    ids = {f.get("rule_id") for f in flags if f.get("level") in ("стоп", "проверить")}
    stop_goal = bool(goal) and goal.get("level") == "стоп"
    steps: list[str] = []
    if stop_goal and ids & {"R04", "R05", "R11", "R22", "R23"}:
        steps.append(
            "Под вашу цель лучше искать другой участок; смена категории или вида использования "
            "— месяцы и без гарантии"
        )
    steps.append("Попросить у продавца свежую выписку ЕГРН (не старше 30 дней) и сверить с отчётом")
    building = bool(goal) and not goal.get("professional")
    if building and not gpzu and not stop_goal:
        steps.append(
            "Попросить у продавца ГПЗУ — там прямо написано, что и какой высоты можно строить"
        )
    if "R10" in ids:
        steps.append("Посмотреть на карте, какую часть участка занимают охранные зоны")
    if "R03" in ids:
        steps.append("Договориться, что межевание продавец делает до сделки")
    if verdict != "стоп" or not stop_goal:
        steps.append("Перед задатком — проверка юристом или нотариусом")
    return steps[:4]


def plain_summary(
    data: dict[str, Any],
    *,
    goal: dict[str, Any] | None,
    valuation: dict[str, Any] | None,
    gpzu: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """{level, headline, reasons, price, steps} для блока в конце отчёта; None без участка."""
    p = data.get("parcel") or {}
    if not p.get("found"):
        return None
    verdict = data.get("verdict") or "проверить"
    flags = data.get("flags") or []
    stops, checks = _reasons(flags, "стоп"), _reasons(flags, "проверить")
    activity = (goal or {}).get("activity")
    if verdict == "стоп":
        goal_text = activity[:1].lower() + activity[1:] if activity else ""
        headline = (
            f"Для цели «{goal_text}» участок не подходит без серьёзных шагов."
            if activity and (goal or {}).get("level") == "стоп"
            else "Есть серьёзное препятствие: до его снятия сделку проводить нельзя."
        )
        reasons = (stops + checks)[:MAX_REASONS]
    elif verdict == "проверить":
        n = len(checks)
        headline = (
            f"Участок можно рассматривать, но до задатка нужно закрыть {n} {_questions(n)}."
            if n
            else "Участок можно рассматривать, но часть сведений нужно подтвердить документами."
        )
        reasons = checks[:MAX_REASONS]
    else:
        headline = "По открытым данным серьёзных препятствий не видно."
        reasons = []
    more = max(0, len(stops + checks) - len(reasons)) if verdict != "ок" else 0
    return {
        "level": verdict,
        "headline": headline,
        "reasons": reasons,
        "more": more,
        "price": _price_line(valuation, data),
        "steps": _next_steps(data, verdict, goal, gpzu),
    }


def merge_documents(
    blocks: list[dict[str, Any]], documents: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Документы из плана по рискам — в блок «Документы» чек-листа, без повторов
    (раньше выписка ЕГРН стояла и в «Документах к запросу», и в чек-листе)."""
    if not documents:
        return blocks
    out = [dict(b, items=list(b["items"])) for b in blocks]
    target = next((b for b in out if b["title"].lower().startswith("документ")), None)
    if target is None:
        target = {"title": "Документы, которые запросить", "items": []}
        out.insert(0, target)
    have = " ".join(i.lower() for i in target["items"])
    for d in documents:
        key = d["name"].split(" — ")[0].lower()[:24]
        if key not in have:
            target["items"].append(f"{d['name']} — {d['where']}")
    return out
