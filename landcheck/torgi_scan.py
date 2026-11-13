"""Анализатор торгов: искать на torgi.gov.ru ликвидные участки с запасом к цене перепродажи.

Зачем (зум 09.11.2026, решение трёх сооснователей): маркетплейс на паузе, в приоритете вторая
модель — находить на государственных торгах участки, которые можно взять дёшево и перепродать
(цель фонда — 100 % за ~24 месяца; порог эксперта по торгам — 40 % на сделку). Вручную эксперт
ведёт слои в Google My Maps («ликвид / неликвид», цена торгов, цена рынка, срок экспозиции) и
таблицу доходности (ТЗ v3, п. 9 и 13.7). Модуль делает то же по всему региону сразу.

Здесь только расчёты, без сети и базы:
  * `lot_row` — строка лота из ответа API torgi.gov.ru (поиск или карточка);
  * `liquidity` — «карта ликвидности»: по кадастровому району и кварталу — сколько торгов
    состоялось, сколько ушло единственному участнику, сколько осталось без заявок, во сколько
    раз цена выросла к старту, сколько заявок на лот, медианы ₽/м² (ТЗ п. 9, этапы 1–3);
  * `score_lot` — оценка идущего лота: цена входа, три сценария перепродажи, предельная цена
    входа при целевой доходности (формула эксперта из `torgi_calc`), вердикт «интересен / на
    грани / нет» и причины по пунктам.

Цена перепродажи берётся из самого надёжного доступного источника, и источник всегда назван:
ручной слой рынка по кварталу или району → свои сделки → объявления → итоги торгов района
(консервативно: рынок обычно выше цены торгов) → кадастровая стоимость (нижний ориентир).
Без данных вердикта нет: «достоверность выше полноты» (ТЗ п. 10). «Интересен» ставится только
по цене рынка (первые три источника): итоги торгов говорят, почём землю покупают у государства,
а не почём её перепродают; по кадастровой стоимости не ставится и «нет».
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from xml.sax.saxutils import escape

from landcheck import torgi, torgi_calc

SOTKA = 100.0
ACTIVE = torgi.ACTIVE
FINAL = {"SUCCEED", "FAILED", "CANCELED"}

KIND_RU = {"sale": "продажа", "rent": "аренда"}
GROUPS: dict[str, str] = {
    "housing": "жильё: ИЖС, ЛПХ",
    "garden": "сад, огород, дача",
    "agri": "сельхозземля",
    "commerce": "коммерция и сервис",
    "industry": "производство и склады",
    "recreation": "отдых и туризм",
    "garage": "гаражи и хранение",
    "other": "прочее",
}
# Порядок важен: гараж раньше жилья («хранение автотранспорта» в жилой зоне), сад раньше
# сельхоза («ведение садоводства» на сельхозземле — это дача, а не поле).
_GROUP_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("garage", ("гараж", "хранение автотранспорта", "стоянк", "машино-мест")),
    ("garden", ("садовод", "огородничес", "дачн", "для ведения сада")),
    (
        "housing",
        (
            "индивидуального жилищного",
            "индивидуальное жилищное",
            "ижс",
            "личного подсобного",
            "личное подсобное",
            "приусадебн",
            "жилой застройк",
            "жилая застройка",
            "малоэтажн",
            "блокированн",
            "жилищного строительства",
        ),
    ),
    (
        "agri",
        (
            "сельскохозяйствен",
            "сенокошен",
            "выпас",
            "растениевод",
            "животновод",
            "скотовод",
            "крестьянск",
            "фермерск",
            "пчеловод",
            "рыбовод",
            "питомник",
            "овощевод",
            "зерновых",
        ),
    ),
    (
        "recreation",
        ("отдых", "рекреац", "туристическ", "санаторн", "курортн", "спорт", "охота и рыбалка"),
    ),
    (
        "commerce",
        (
            "магазин",
            "торговл",
            "общественное питание",
            "гостинич",
            "делов",
            "предпринимательств",
            "придорожного сервиса",
            "бытовое обслуживание",
            "автомобильные мойки",
            "ремонт автомобилей",
            "заправка",
            "рынки",
            "развлечен",
            "офис",
        ),
    ),
    (
        "industry",
        (
            "производствен",
            "промышлен",
            "склад",
            "транспорт",
            "недропользован",
            "коммунальное обслуживание",
            "энергетик",
            "связь",
            "строительная промышленность",
        ),
    ),
)
_CODE_GROUP: tuple[tuple[str, str], ...] = (
    ("2.7.1", "garage"),
    ("2.7.2", "garage"),
    ("4.9", "garage"),
    ("13.", "garden"),
    ("2.", "housing"),
    ("1.", "agri"),
    ("5.", "recreation"),
    ("4.", "commerce"),
    ("6.", "industry"),
    ("7.", "industry"),
    ("3.1", "industry"),
)
_VRI_CODE_RE = re.compile(r"(?<![\d.])(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)(?![\d.])")


def vri_group(vri: str | None, code: str | None = None, category: str | None = None) -> str:
    """Группа назначения для сравнения цен и фильтра: по коду классификатора, затем по словам."""
    text = (vri or "").lower().replace("ё", "е")
    code = code or (m.group(1) if (m := _VRI_CODE_RE.search(text)) else None)
    if code:
        for prefix, group in _CODE_GROUP:
            if code == prefix.rstrip(".") or code.startswith(prefix):
                return group
    for group, keys in _GROUP_KEYS:
        if any(k in text for k in keys):
            return group
    cat = (category or "").lower()
    if "сельскохозяйствен" in cat:
        return "agri"
    if "промышлен" in cat:
        return "industry"
    return "other"


def quarter_of(cad_num: str | None) -> str | None:
    """`40:13:120101:22` → `40:13:120101` (кадастровый квартал)."""
    if not cad_num:
        return None
    parts = cad_num.strip().split(":")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return None
    return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2]}"


def place_of(text: str | None) -> tuple[str | None, str | None]:
    """Что ввели в «Район или квартал»: (место для отбора, номер участка для подсветки).
    `10:03:0010901:1354` → квартал `10:03:0010901` и сам участок; `40:13` → район; пробелы и
    точки вместо двоеточий прощаем (так номер копируют из выписок)."""
    raw = re.sub(r"[\s.]+", ":", (text or "").strip()).strip(":")
    parts = [p for p in raw.split(":") if p]
    if len(parts) < 2 or not all(p.isdigit() for p in parts):
        return None, None
    parts[0], parts[1] = parts[0].zfill(2), parts[1].zfill(2)
    if len(parts) >= 4:
        return ":".join(parts[:3]), ":".join(parts[:4])
    return ":".join(parts), None


def in_place(lot: dict[str, Any], place: str) -> bool:
    """Лот лежит в районе или квартале: по номеру участка, а у не образованных — по кварталу
    из описания. Сравнение по целым частям номера (`10:03:001090` не ловит `10:03:0010901`)."""
    for value in (lot.get("cad_num"), lot.get("quarter")):
        if value and (value == place or value.startswith(place + ":")):
            return True
    return False


# Извещения о предоставлении (ст. 39.18 ЗК) лежат в том же поиске, что и аукционы, но это не
# торги: цены нет, участок часто ещё не образован. В Карелии их 319 из 392 идущих лотов (прод,
# 10.11.2026) — в ликвидность и вердикты они не идут, показываются отдельно.
NOTICE_FORMS = frozenset({"IPS"})
_QUARTER_RE = re.compile(r"(?<![\d:])(\d{2}):(\d{2}):(\d{6,7})(?![\d:])")


def is_notice(lot: dict[str, Any]) -> bool:
    return lot.get("form") in NOTICE_FORMS


# Вид торгов, когда это не обычный земельный аукцион (ZK): у них свои правила и сроки.
BIDD_TYPE_SHORT = {
    "229FZ": "имущество должников",
    "178FZ": "приватизация",
    "200FZ": "лесной участок",
    "127FZ": "банкротство",
}


def bidd_label(lot: dict[str, Any]) -> str:
    """Короткая подпись вида торгов для таблицы: пусто у обычного земельного аукциона."""
    if is_notice(lot):
        return "извещение о предоставлении (ст. 39.18 ЗК)"
    code = lot.get("bidd_type")
    if not code or code == "ZK":
        return ""
    return BIDD_TYPE_SHORT.get(code) or lot.get("bidd_type_name") or code


def quarter_from_text(card: dict[str, Any]) -> str | None:
    """Кадастровый квартал из названия и описания лота — когда участок не образован и номера
    нет («…в кадастровом квартале 10:20:0060108»)."""
    text = f"{card.get('lotName') or ''} {card.get('lotDescription') or ''}"
    m = _QUARTER_RE.search(text)
    return f"{m.group(1)}:{m.group(2)}:{m.group(3)}" if m else None


def _vri_code(card: dict[str, Any]) -> str | None:
    val = torgi._chars(card).get("PermittedUse")
    first = val[0] if isinstance(val, list) and val else val
    code = first.get("code") if isinstance(first, dict) else None
    return code if isinstance(code, str) and _VRI_CODE_RE.fullmatch(code) else None


def _bidd_type(card: dict[str, Any]) -> tuple[str | None, str | None]:
    bt = card.get("biddType")
    if isinstance(bt, dict):
        return bt.get("code"), bt.get("name")
    return (bt, None) if isinstance(bt, str) else (None, None)


def _raw_time(v: Any) -> str | None:
    return v.strip()[:32] if isinstance(v, str) and len(v.strip()) >= 10 else None


def local_dates(row: dict[str, Any]) -> dict[str, Any]:
    """Дни окончания заявок и торгов заново из сырого времени и пояса лота — после слияния
    ответа поиска с карточкой: пояс бывает только в одном из них."""
    tz = row.get("tz_offset")
    out = dict(row)
    if row.get("bidd_end_at"):
        out["bidd_end"] = torgi.local_day(row["bidd_end_at"], tz)
    if row.get("auction_at"):
        out["auction_date"] = torgi.local_day(row["auction_at"], tz)
    return out


def lot_row(card: dict[str, Any]) -> dict[str, Any] | None:
    """Плоская строка лота для хранилища. Годится и ответ поиска, и карточка: чего в ответе
    нет (итоговая цена, протоколы, шаг — они только в карточке), то остаётся None и не
    затирает уже сохранённое (см. `merge_row`). Имена участников и победителя не берём."""
    lot_id = card.get("id")
    if not lot_id:
        return None
    cad = torgi.cad_num_of(card)
    quarter = quarter_of(cad) or quarter_from_text(card)
    region = card.get("subjectRFCode") or torgi.region_of(cad or quarter)
    vri = torgi.vri_of(card)
    category = (card.get("category") or {}).get("name")
    type_code, type_name = _bidd_type(card)
    res = torgi._result_of(card) if card.get("protocols") else {}
    tz = torgi.tz_offset_of(card)
    applied, applied_min = res.get("applied"), res.get("applied_min")
    start, fin = torgi._num(card.get("priceMin")), torgi._num(card.get("priceFin"))
    if (
        card.get("protocols")
        and card.get("lotStatus") == "SUCCEED"
        and start
        and fin
        and fin > start * 1.001
        and (applied is None or applied < 2)
    ):
        # цена на аукционе растёт только при двух участниках и больше (зум 13.11, 50:12)
        applied, applied_min = 2, True
    row: dict[str, Any] = {
        "id": str(lot_id),
        "region": str(region).zfill(2) if region else None,
        "district": torgi.district_of(cad or quarter),
        "quarter": quarter,
        "cad_num": cad,
        # None — признаков сделки нет (часть ответов поиска): сохранённое не затираем
        "kind": torgi.kind_of(card),
        "form": torgi._code(card.get("biddForm")),
        "bidd_type": type_code,
        "bidd_type_name": type_name,
        "status": card.get("lotStatus"),
        "area_m2": torgi.area_of(card),
        "price_start": start,
        "price_step": torgi._num(card.get("priceStep")),
        "deposit": torgi._num(card.get("deposit")),
        "price_fin": fin,
        # даты — в поясе лота, как на площадке; сырое время UTC и пояс хранятся отдельно
        "published": torgi.local_day(card.get("noticeFirstVersionPublicationDate"), tz),
        "bidd_end": torgi.local_day(card.get("biddEndTime"), tz),
        "auction_date": torgi.local_day(card.get("auctionStartDate"), tz),
        "bidd_end_at": _raw_time(card.get("biddEndTime")),
        "auction_at": _raw_time(card.get("auctionStartDate")),
        "tz_offset": tz,
        "vri": vri,
        "vri_group": vri_group(vri, _vri_code(card), category),
        "category": category,
        "address": card.get("estateAddress"),
        "name": (card.get("lotName") or "")[:300] or None,
        "notice": card.get("noticeNumber"),
        "applied": applied,
        "applied_min": applied_min,
        "admitted": res.get("admitted"),
        "second_price": res.get("second_price"),
        "winner_type": res.get("winner_type"),
        "fail_code": res.get("fail_code"),
        "fail_reason": res.get("fail_reason"),
        "has_gpzu": None,
        "annulled": bool(card.get("isAnnulled")),
        "stopped": bool(card.get("isStopped")),
        "appeals": bool(card.get("hasAppeals")),
    }
    if "lotAttachments" in card or "noticeAttachments" in card:
        row["has_gpzu"] = any(d.get("gpzu") for d in torgi._docs_of(card))
    return row


def merge_row(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Свежие данные поверх сохранённых; пустое (None) из поиска не затирает то, что уже
    взято из карточки. Флаги отмены обновляются всегда."""
    out = dict(old)
    for k, v in new.items():
        if v is not None or k not in out:
            out[k] = v
    return out


# --- ликвидность ---------------------------------------------------------------------------
MIN_SAMPLE = 3  # меньше завершённых торгов — «мало данных», индекс не считаем
AREA_BAND = 5.0  # сопоставимая площадь: от 1/5 до 5 площадей лота (как в объявлениях)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _rub_m2(lot: dict[str, Any], price_key: str = "price_fin") -> float | None:
    price, area = lot.get(price_key), lot.get("area_m2")
    return price / area if price and area else None


def _fail_kind(lot: dict[str, Any]) -> str | None:
    return torgi.fail_kind(lot.get("fail_code"), lot.get("fail_reason"))


def _outcome(lot: dict[str, Any]) -> str:
    """Чем кончились торги для ликвидности: `sold` — была борьба; `single` — договор с
    единственным участником (ст. 39.12 ЗК): участок нашёл хозяина; `none` — участок
    не ушёл; `failed` — не состоялись, итог неизвестен (карточка не прочитана); `active`,
    `canceled`; `notice` — извещение о предоставлении, не торги.

    Итог не состоявшихся берём из протокола (эксперт по торгам, 13.11.2026: «протокол
    публикуется, и в нём данные»): причина из справочника площадки называет исход прямо —
    подана одна заявка, допущен один заявитель, на торги вышел один участник (ушёл) либо
    заявок не подано, всем отказано, никто не предложил цену (не ушёл). Пока причина не
    прочитана — прежний признак (прод, Карелия, 10.11.2026): есть цена договора или одна
    допущенная заявка — ушёл; карточка прочитана, а в ней ни того ни другого — не ушёл."""
    if is_notice(lot):
        return "notice"
    status = lot.get("status")
    if status == "SUCCEED":
        return "sold"
    if status == "FAILED":
        kind = _fail_kind(lot)
        if kind in torgi.FAIL_TAKEN:
            return "single"
        if kind in torgi.FAIL_LEFT:
            return "none"
        applied = _admitted(lot)
        if applied == 1 or lot.get("price_fin"):
            return "single"
        if applied == 0 or lot.get("card"):
            return "none"
        return "failed"
    if status in ACTIVE:
        return "active"
    return "canceled"


def _admitted(lot: dict[str, Any]) -> int | None:
    """Сколько заявок допущено по протоколу в карточке лота (нет протокола — None)."""
    return lot.get("admitted") if lot.get("admitted") is not None else lot.get("applied")


def stats_of(lots: list[dict[str, Any]]) -> dict[str, Any]:
    """Сводка одной группы лотов (район, квартал или весь регион)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for lot in lots:
        by.setdefault(_outcome(lot), []).append(lot)
    sold, single = by.get("sold", []), by.get("single", [])
    none, failed = by.get("none", []), by.get("failed", [])
    closed = len(sold) + len(single) + len(none) + len(failed)
    taken = len(sold) + len(single)
    # Лоты «не состоялись» без прочитанной карточки делим в той же пропорции, что известные:
    # иначе район с непрочитанными карточками выглядел бы мёртвым.
    known_failed = len(single) + len(none)
    if failed and known_failed:
        taken_est = taken + len(failed) * len(single) / known_failed
    else:
        taken_est = float(taken)
    sell_through = round(taken_est / closed, 2) if closed else None
    growth = [
        lot["price_fin"] / lot["price_start"]
        for lot in sold
        if lot.get("price_fin") and lot.get("price_start")
    ]
    applied = [lot["applied"] for lot in sold + single + none if lot.get("applied") is not None]
    out: dict[str, Any] = {
        "closed": closed,
        "sold": len(sold),
        "single": len(single),
        "none": len(none),
        "failed_unknown": len(failed),
        # На чём стоит итог не состоявшихся: причина из протокола (по видам) либо, пока она
        # не прочитана, цена договора и число заявок. Страница показывает и то и другое.
        "reasons": {
            kind: n
            for kind in torgi.FAIL_KIND_RU
            if (n := sum(1 for lot in single + none if _fail_kind(lot) == kind))
        },
        "single_no_reason": sum(1 for lot in single if _fail_kind(lot) is None),
        "none_no_reason": sum(1 for lot in none if _fail_kind(lot) is None),
        "active": len(by.get("active", [])),
        # извещения о предоставлении за период: спрос на землю без торгов (ст. 39.18 ЗК)
        "notices": len(by.get("notice", [])),
        "sell_through": sell_through,
        "growth_median": _median(growth),
        "applied_median": _median([float(a) for a in applied]),
        "with_price": len(growth),
    }
    for kind in ("sale", "rent"):
        samples = [
            (v, lot["area_m2"], lot.get("quarter"), lot.get("id"))
            for lot in sold
            if lot.get("kind") == kind and (v := _rub_m2(lot))
        ]
        per_m2 = [x[0] for x in samples]
        # Рост цены и доля торгов с борьбой — отдельно по сделке: годовую плату на аукционах
        # аренды разгоняют в разы (Карелия, 10.11.2026: медиана ×6), на продаже — нет.
        k_sold = [lot for lot in sold if lot.get("kind") == kind]
        k_growth = [
            lot["price_fin"] / lot["price_start"]
            for lot in k_sold
            if lot.get("price_fin") and lot.get("price_start")
        ]
        out[kind] = {
            "closed": sum(1 for lot in sold + single + none + failed if lot.get("kind") == kind),
            "sold": len(k_sold),
            "growth_n": len(k_growth),
            "growth_median": _median(k_growth),
            "n": len(per_m2),
            "median_m2": _median(per_m2),
            "p25_m2": torgi._quantile(per_m2, 0.25),
            "p75_m2": torgi._quantile(per_m2, 0.75),
            # (₽/м², площадь, квартал, лот) — чтобы для лота отобрать сопоставимые по площади
            "samples": samples,
        }
    out["index"], out["label"] = liquidity_index(out)
    return out


def liquidity_index(s: dict[str, Any]) -> tuple[int | None, str]:
    """Индекс ликвидности 0–100 и ярлык. Основа — доля участков, которые нашли хозяина (60),
    плюс конкуренция: заявок на лот (до 20) и рост цены к старту (до 20)."""
    if not s.get("closed") or s["closed"] < MIN_SAMPLE or s.get("sell_through") is None:
        return None, "мало данных"
    idx = 60 * s["sell_through"]
    applied = s.get("applied_median")
    if applied is not None:
        idx += 20 * min(max(applied - 1, 0) / 3, 1.0)  # 4 заявки и больше — максимум
    growth = s.get("growth_median")
    if growth is not None:
        idx += 20 * min(max(growth - 1, 0) / 1.0, 1.0)  # цена удвоилась — максимум
    idx = int(round(idx))
    if idx >= 55:
        return idx, "ликвид"
    if idx >= 30:
        return idx, "средне"
    return idx, "неликвид"


def liquidity(
    lots: list[dict[str, Any]], *, level: str = "district", group: str | None = None
) -> list[dict[str, Any]]:
    """«Карта ликвидности» таблицей: строка на кадастровый район (`level="district"`) или
    квартал (`"quarter"`). `group` — только лоты одной группы назначения."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for lot in lots:
        key = lot.get(level)
        if not key or (group and lot.get("vri_group") != group):
            continue
        buckets.setdefault(key, []).append(lot)
    rows = [{"key": key, **stats_of(items)} for key, items in buckets.items()]
    rows.sort(key=lambda r: (-(r["index"] if r["index"] is not None else -1), -r["closed"]))
    return rows


def index_by(lots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Сводки по району и кварталу, а внутри района — по группе назначения, плюс весь
    регион. Ключи: `40:13`, `40:13:120101`, `40:13|housing`, `*`. Для оценки лота берём самую
    узкую выборку, в которой хватает данных."""
    buckets: dict[str, list[dict[str, Any]]] = {REGION_KEY: list(lots)}
    for lot in lots:
        d, q, g = lot.get("district"), lot.get("quarter"), lot.get("vri_group")
        if d:
            buckets.setdefault(d, []).append(lot)
            if g:
                buckets.setdefault(f"{d}|{g}", []).append(lot)
        if q:
            buckets.setdefault(q, []).append(lot)
    return {key: stats_of(items) for key, items in buckets.items()}


REGION_KEY = "*"


def growth_for(
    lot: dict[str, Any], stats: dict[str, dict[str, Any]]
) -> tuple[float | None, float | None, str | None]:
    """Типичный рост цены к старту и доля торгов с борьбой для лота: (рост, доля, где взяли).
    Только торги той же сделки (продажа или аренда); квартал → район → регион, не меньше
    `MIN_SAMPLE` торгов с ценой. Меньше — роста не знаем: один лот с ростом в 25 раз не
    повод ждать такого же."""
    kind = lot.get("kind") or "sale"
    for where, key in (
        ("квартал", lot.get("quarter")),
        ("район", lot.get("district")),
        ("регион", REGION_KEY),
    ):
        ks = ((stats.get(key or "") or {}).get(kind)) or {}
        if (ks.get("growth_n") or 0) >= MIN_SAMPLE and ks.get("closed"):
            return ks["growth_median"], ks["sold"] / ks["closed"], where
    return None, None, None


def times_ru(n: int) -> str:
    """«1 раз», «2 раза», «5 раз»."""
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} раза"
    return f"{n} раз"


def repeats_of(lots: list[dict[str, Any]]) -> dict[str, int]:
    """Сколько раз участок уже выставляли без результата (нет заявок или отмена): повторный
    лот — признак того, что участок не берут."""
    out: dict[str, int] = {}
    for lot in lots:
        cad = lot.get("cad_num")
        if cad and _outcome(lot) in ("none", "failed", "canceled"):
            out[cad] = out.get(cad, 0) + 1
    return out


# --- оценка идущего лота -------------------------------------------------------------------
@dataclass
class ScanParams:
    """Критерии отбора. Значения по умолчанию — цель фонда с зума 09.11 (100 % за 24 месяца);
    порог эксперта по торгам — 40 % на сделку — задаётся в форме."""

    target_roi: float = 1.0
    horizon_months: int = 24
    tax: float = torgi_calc.DEFAULT_TAX
    extra: float = 0.0  # расходы на сделку сверх цены: регистрация, межевание, реклама
    budget: float | None = None
    min_days_left: int = 2  # меньше — заявку и задаток уже не успеть
    min_liquidity: int = 30  # индекс района ниже — «неликвид», перепродажа затянется
    hold_months: int = 12  # срок до продажи по умолчанию, если в слое рынка срока нет
    market_markup: float = 1.0  # рынок к итогам торгов, когда рыночной цены нет (1 — не знаем)
    area_min_m2: float | None = None
    area_max_m2: float | None = None
    kinds: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    forms: str = "auction"  # что показывать: auction — торги, notice — извещения 39.18, all
    place: str | None = None  # район `40:13` или квартал `40:13:120101` (см. `place_of`)
    # Аренда под переуступку: цена переуступки как доля цены такого же участка в собственности.
    # None — не оцениваем: цену переуступки задают в слое рынка района.
    assign_ratio: float | None = None
    liquid_area: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "housing": (400.0, 2500.0),
            "garden": (400.0, 1500.0),
        }
    )


SOURCE_RU = {
    "manual": "слой рынка",
    "deals": "свои сделки",
    "ads": "объявления",
    "auctions": "итоги торгов района",
    "cadastre": "кадастровая стоимость",
}
CONFIDENCE_RU = {3: "высокая", 2: "средняя", 1: "низкая"}
# Источники с ценой рынка. Итоги торгов и кадастровая стоимость — не рынок: по ним «интересен»
# не ставим. Прод, 12.11.2026, Калужская область и Карелия, проверка на завершённых торгах:
# старт ниже медианы итогов торгов района у 96 % лотов (медиана — в 2,3 раза), то есть «старт
# ниже итогов» лоты не различает; ожидаемая цена победителя попадает в ±30 % от итога в 4
# случаях из 10 на продаже и в 3 из 10 на аренде (там, где была борьба).
MARKET_SOURCES = frozenset({"manual", "deals", "ads"})
WIDE_SPREAD = 2.0  # верх сценариев вдвое выше низа — середине верить нельзя


def _pool(lot: dict[str, Any], stats: dict[str, dict[str, Any]]) -> tuple[str | None, list[tuple]]:
    key = f"{lot.get('district')}|{lot.get('vri_group')}"
    samples = ((stats.get(key) or {}).get(lot.get("kind") or "sale") or {}).get("samples") or []
    samples = [x for x in samples if len(x) < 4 or x[3] != lot.get("id")]  # сам лот — не образец
    area = lot.get("area_m2")
    if area:
        samples = [x for x in samples if area / AREA_BAND <= x[1] <= area * AREA_BAND]
    near = [x for x in samples if x[2] and x[2] == lot.get("quarter")]
    for where, pool in (("квартал", near), ("район", samples)):
        if len(pool) >= MIN_SAMPLE:
            return where, pool
    return None, []


def auction_pool(
    lot: dict[str, Any], stats: dict[str, dict[str, Any]]
) -> tuple[str | None, list[float]]:
    """Итоговые цены за м² состоявшихся торгов, сопоставимых с лотом: та же сделка, то же
    назначение, площадь от 1/5 до 5 площадей лота. Цена за метр у поля и у участка под дом в
    одном квартале различается в десятки раз, поэтому чужое назначение не берём. Сначала свой
    квартал, потом район; меньше `MIN_SAMPLE` — пусто."""
    where, pool = _pool(lot, stats)
    return where, [x[0] for x in pool]


def auction_pool_ids(lot: dict[str, Any], stats: dict[str, dict[str, Any]]) -> list[str]:
    """Лоты той же выборки — чтобы карточка показывала ровно те торги, из которых взята цена."""
    return [x[3] for x in _pool(lot, stats)[1] if len(x) > 3 and x[3]]


def market_for(
    lot: dict[str, Any],
    stats: dict[str, dict[str, Any]],
    layer: list[dict[str, Any]] | None = None,
    markup: float = 1.0,
    assign_ratio: float | None = None,
) -> dict[str, Any] | None:
    """Три сценария цены перепродажи за м² и источник. `layer` — строки слоя рынка
    (`scope`, `vri_group`, `kind`, `price_min/mid/max` за сотку, `exposure_days`, `source`),
    от ручного ввода, своих сделок или объявлений; сначала квартал, потом район.

    Аренда: если цены переуступки в слое нет, а задана `assign_ratio`, берём цену такого же
    участка в собственности (тем же порядком источников) и умножаем на эту долю."""
    kind, group = lot.get("kind"), lot.get("vri_group")
    scopes = [s for s in (lot.get("quarter"), lot.get("district")) if s]
    for scope in scopes:
        best = None
        for m in layer or []:
            if m.get("scope") != scope or (m.get("kind") or "sale") != kind:
                continue
            if m.get("vri_group") not in (None, "", "any", group):
                continue
            if not m.get("price_mid"):
                continue
            rank = (
                {"manual": 3, "deals": 2, "ads": 1}.get(m.get("source") or "manual", 0),
                1 if m.get("vri_group") == group else 0,
            )
            if best is None or rank > best[0]:
                best = (rank, m)
        if best:
            m = best[1]
            mid = m["price_mid"] / SOTKA
            src = m.get("source") or "manual"
            return {
                "min_m2": (m.get("price_min") or m["price_mid"] * 0.85) / SOTKA,
                "mid_m2": mid,
                "max_m2": (m.get("price_max") or m["price_mid"] * 1.15) / SOTKA,
                "source": src,
                "source_label": f"{SOURCE_RU.get(src, src)}, {scope}",
                "confidence": 3 if src in ("manual", "deals") else 2,
                "exposure_days": m.get("exposure_days"),
                "liquid": m.get("liquid"),
                "note": m.get("note"),
            }
    # Итоги торгов годятся как цена перепродажи только для продажи: у аренды итог — это
    # годовая плата, а не цена переуступки права.
    where, vals = auction_pool(lot, stats)
    if kind == "sale" and vals:
        med = statistics.median(vals)
        return {
            "min_m2": (torgi._quantile(vals, 0.25) or med) * markup,
            "mid_m2": med * markup,
            "max_m2": (torgi._quantile(vals, 0.75) or med) * markup,
            "source": "auctions",
            "source_label": f"итоги торгов: {where}, {len(vals)} лот.",
            "confidence": 2 if len(vals) >= 5 else 1,
            "exposure_days": None,
            "liquid": None,
            "note": "то же назначение, площадь от 1/5 до 5 площадей лота"
            + (f"; рынок принят в {markup:g} раза выше торгов" if markup != 1 else ""),
            # вся выборка, а не только 25–75 %: по краям видно, насколько район неоднороден
            "n": len(vals),
            "lo_m2": min(vals),
            "hi_m2": max(vals),
        }
    cad_cost, area = lot.get("cad_cost"), lot.get("area_m2")
    if kind == "sale" and cad_cost and area:
        m2 = cad_cost / area
        return {
            "min_m2": m2,
            "mid_m2": None,
            "max_m2": None,
            "source": "cadastre",
            "source_label": "кадастровая стоимость (нижний ориентир)",
            "confidence": 1,
            "exposure_days": None,
            "liquid": None,
            "note": None,
        }
    if kind == "rent" and assign_ratio:
        own = market_for({**lot, "kind": "sale"}, stats, layer, markup)
        if own:
            share = f"{assign_ratio:.0%}"
            return {
                **own,
                **{
                    key: own[key] * assign_ratio if own.get(key) else None
                    for key in ("min_m2", "mid_m2", "max_m2")
                },
                "source_label": f"переуступка = {share} цены в собственности; "
                + own["source_label"],
                # доля задана критерием, а не сделками: выше «средней» достоверности не ставим
                "confidence": min(own["confidence"], 2),
                "note": "; ".join(
                    t
                    for t in (
                        f"цена переуступки принята как {share} цены такого же участка в "
                        "собственности — доля из критериев отбора",
                        own.get("note"),
                    )
                    if t
                ),
                "assign_ratio": assign_ratio,
            }
    return None


def _days_left(lot: dict[str, Any], today: date) -> int | None:
    raw = lot.get("bidd_end")
    try:
        return (date.fromisoformat(raw) - today).days if raw else None
    except ValueError:
        return None


def _annual(roi: float | None, months: float) -> float | None:
    if roi is None or months <= 0 or roi <= -1:
        return None
    return round((1 + roi) ** (12 / months) - 1, 3)


def _roi(sale: float | None, entry: float, extra: float, tax: float) -> float | None:
    cost = entry + extra
    if not sale or cost <= 0:
        return None
    return round((sale - cost) * (1 - tax) / cost, 3)


VERDICTS = {
    "go": "интересен",
    "edge": "на грани",
    "no": "не подходит",  # зум 13.11, 22:15–22:40: «нет» без пояснения непонятно (Тони, Антон)
    "unknown": "нет цены перепродажи",
    "notice": "извещение 39.18",
}
_RANK = {"go": 0, "edge": 1, "unknown": 2, "notice": 3, "no": 4}
NOTICE_TEXT = (
    "это извещение о предоставлении (ст. 39.18 ЗК), не торги: участок просит конкретный "
    "заявитель, цены нет. До конца срока можно подать своё заявление — тогда участок выставят "
    "на аукцион"
)


def score_lot(
    lot: dict[str, Any],
    stats: dict[str, dict[str, Any]],
    params: ScanParams | None = None,
    *,
    layer: list[dict[str, Any]] | None = None,
    repeats: dict[str, int] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Оценка идущего лота. Вердикт строится на предельной цене входа:
    «интересен» — цена перепродажи взята с рынка (слой рынка, свои сделки, объявления), предел
    при целевой доходности выше ожидаемой цены победителя (старт × типичный рост цены в районе),
    сделка проходит на старте и по нижней цене, нет стоп-причин; «на грани» — предел выше
    старта, но типичная конкуренция его съедает, либо цена перепродажи известна только по
    итогам торгов, ориентир ненадёжен или район неликвиден; «нет» — уже старт выше предела или
    есть стоп-причина; без цены перепродажи вердикта нет, и по одной кадастровой стоимости
    (нижний ориентир) «нет» не ставится."""
    p = params or ScanParams()
    today = today or datetime.now(UTC).date()
    area, start = lot.get("area_m2"), lot.get("price_start")
    district = lot.get("district") or ""
    d_stats = stats.get(f"{district}|{lot.get('vri_group')}") or {}
    if (d_stats.get("closed") or 0) < MIN_SAMPLE:
        d_stats = stats.get(district) or {}
    q_stats = stats.get(lot.get("quarter") or "") or {}
    liq = q_stats if (q_stats.get("closed") or 0) >= MIN_SAMPLE else d_stats
    plus: list[str] = []
    minus: list[str] = []
    stop: list[str] = []
    ask: list[str] = []

    sell_through = liq.get("sell_through")
    # Ожидаемая цена победителя: старт × рост, взвешенный долей торгов с борьбой. Где лоты
    # чаще уходят единственному участнику по старту, ждать кратного роста не из чего.
    growth, fought, growth_where = growth_for(lot, stats)
    expected = start
    if start and growth and growth > 1 and fought:
        expected = round(start * (1 + (growth - 1) * min(fought * 1.5, 1.0)))

    mk = market_for(lot, stats, layer, p.market_markup, p.assign_ratio)
    sale = {"min": None, "mid": None, "max": None}
    if mk and area:
        for key in ("min", "mid", "max"):
            v = mk.get(f"{key}_m2")
            sale[key] = round(v * area) if v else None
    limits: dict[str, float | None] = {}
    for key in ("min", "mid", "max"):
        lp = torgi_calc.limit_price(sale[key], p.extra, p.tax, p.target_roi) if sale[key] else None
        limits[key] = round(lp) if lp is not None else None
    # Опорный сценарий — MEDIUM; когда его нет (только кадастр) — MIN.
    base_key = "mid" if sale["mid"] else "min"
    base_sale, limit = sale[base_key], limits[base_key]
    known = [v for v in limits.values() if v is not None]
    safe = min(known) if known else None

    hold = p.hold_months
    if mk and mk.get("exposure_days"):
        hold = max(1, round(mk["exposure_days"] / 30) + 2)  # +2 мес.: договор и регистрация
    roi_start = _roi(base_sale, start, p.extra, p.tax) if start else None
    roi_expected = _roi(base_sale, expected, p.extra, p.tax) if expected else None
    src = (mk or {}).get("source")
    sale_sotka = {
        key: round(mk[f"{key}_m2"] * SOTKA) if mk and mk.get(f"{key}_m2") else None
        for key in ("min", "mid", "max")
    }
    spread = None
    if sale_sotka["min"] and sale_sotka["max"]:
        spread = round(sale_sotka["max"] / sale_sotka["min"], 1)

    # --- причины по пунктам ---------------------------------------------------------------
    days = _days_left(lot, today)
    if lot.get("annulled") or lot.get("stopped"):
        stop.append("торги приостановлены или извещение аннулировано")
    if days is not None and days < 0:
        stop.append("приём заявок закончен")
    elif days is not None and days < p.min_days_left:
        stop.append(f"до конца приёма заявок {days} дн. — заявку и задаток не успеть")
    elif days is not None:
        plus.append(f"до конца приёма заявок {days} дн.")
    if not area:
        ask.append("площадь в лоте не указана")
    notice = is_notice(lot)
    if notice:
        ask.append(NOTICE_TEXT)
    elif not start:
        ask.append("начальная цена не указана")
    if p.budget and start and start + p.extra > p.budget:
        stop.append("начальная цена с расходами выше бюджета")
    elif p.budget and lot.get("deposit") and lot["deposit"] > p.budget:
        stop.append("задаток выше бюджета")
    group = lot.get("vri_group")
    band = p.liquid_area.get(group or "")
    if band and area:
        if band[0] <= area <= band[1]:
            plus.append("площадь в ходовом диапазоне для такого назначения")
        else:
            minus.append(
                f"площадь {area / SOTKA:.0f} сот. вне ходового диапазона "
                f"{band[0] / SOTKA:.0f}–{band[1] / SOTKA:.0f} сот."
            )
    idx = liq.get("index")
    if idx is None:
        ask.append("по району мало завершённых торгов — ликвидность не посчитать")
    elif idx >= 55:
        plus.append(f"район ликвидный: индекс {idx}, участки находят хозяина")
    elif idx >= p.min_liquidity:
        minus.append(f"ликвидность района средняя: индекс {idx}")
    else:
        minus.append(f"район неликвидный: индекс {idx}, большинство участков с торгов не уходит")
    if mk and mk.get("liquid") == "illiquid":
        minus.append("в слое рынка локация помечена как неликвид")
    elif mk and mk.get("liquid") == "liquid":
        plus.append("в слое рынка локация помечена как ликвид")
    n_rep = (repeats or {}).get(lot.get("cad_num") or "", 0)
    if n_rep:
        minus.append(f"участок уже выставляли без результата: {times_ru(n_rep)}")
    if lot.get("has_gpzu"):
        plus.append("к лоту приложен ГПЗУ — параметры застройки известны")
    if lot.get("appeals"):
        minus.append("по торгам есть жалобы")
    cad_cost = lot.get("cad_cost")
    if cad_cost and start and lot.get("kind") == "sale":
        ratio = start / cad_cost
        if ratio <= 0.8:
            plus.append(f"старт ниже кадастровой стоимости: {ratio:.0%}")
        elif ratio >= 1.5:
            minus.append(f"старт в {ratio:.1f} раза выше кадастровой стоимости")
    if cad_cost and start and lot.get("kind") == "rent":
        plus.append(f"годовая плата на старте — {start / cad_cost:.1%} кадастровой стоимости")
    if lot.get("kind") == "rent" and start and area:
        where, vals = auction_pool(lot, stats)
        if vals:
            typical = statistics.median(vals) * SOTKA
            mine = start / area * SOTKA
            text = (
                f"годовая плата на старте {torgi_calc.rub(mine)} за сотку, на торгах в этом "
                f"месте ({where}, {len(vals)} лот.) аренда уходит по {torgi_calc.rub(typical)}"
            )
            (plus if mine <= typical else minus).append(text)

    if spread and spread >= WIDE_SPREAD:
        minus.append(
            f"разброс цен перепродажи большой: от {torgi_calc.rub(sale_sotka['min'])} до "
            f"{torgi_calc.rub(sale_sotka['max'])} за сотку — середине верить нельзя"
        )

    # --- вердикт --------------------------------------------------------------------------
    why: list[str] = []  # причины самого вердикта — первыми в «против»
    # Всё, чем объясняется вердикт, отдельно от прочих фактов о лоте (зум 13.11, 32:49: в
    # «Почему нет цены перепродажи» стояли «до конца приёма заявок 10 дн.» и «район ликвидный»)
    reasons: list[str] = []
    if stop:
        verdict = "no"
        reasons += stop
    elif notice:
        verdict = "notice"
        reasons.append(NOTICE_TEXT)
    elif limit is None or not start:
        verdict = "unknown"
        if not start:
            text = "начальная цена не указана"
        elif lot.get("kind") == "rent":
            text = (
                "для аренды нужна цена переуступки — задайте её в слое рынка района или долю "
                "от цены участка в собственности в критериях"
            )
        else:
            text = "нет цены перепродажи — задайте рынок района или дождитесь сбора торгов"
        if text not in ask:
            ask.append(text)
        reasons.append(text)
    elif limit < start and src == "cadastre":
        # Кадастровая стоимость — нижний ориентир, а старт обычно ей и равен: «нет» по ней
        # получали 53 из 147 идущих лотов Калужской области (прод, 12.11.2026).
        verdict = "unknown"
        text = (
            "рыночной цены нет: известна только кадастровая стоимость, это нижний ориентир — "
            "«не подходит» по ней не ставим. Найдите объявления рядом или задайте рынок района"
        )
        ask.append(text)
        reasons.append(text)
    elif limit < start:
        verdict = "no"
        why.append(
            f"уже старт выше предельной цены входа {torgi_calc.rub(limit)} при цели "
            f"{p.target_roi:.0%}"
            + (" (цена перепродажи — по итогам торгов района)" if src == "auctions" else "")
        )
    else:
        low_conf = (mk or {}).get("confidence", 0) <= 1 or base_key == "min"
        weak = (
            low_conf
            or (idx is not None and idx < p.min_liquidity)
            or (mk or {}).get("liquid") == "illiquid"
        )
        market_based = src in MARKET_SOURCES
        low_fail = base_key != "min" and limits["min"] is not None and limits["min"] < start
        if limit >= (expected or start) and not weak and market_based and not low_fail:
            verdict = "go"
            good = [
                f"предел входа {torgi_calc.rub(limit)} выше ожидаемой цены победителя "
                f"{torgi_calc.rub(expected or start)}"
            ]
            if limits["min"] is not None:
                good.append(
                    "сделка проходит на старте и по нижней цене перепродажи: безопасный "
                    f"ориентир {torgi_calc.rub(limits['min'])}"
                )
            plus += good
            reasons += good
        else:
            verdict = "edge"
            if limit < (expected or start):
                why.append(
                    f"предел входа {torgi_calc.rub(limit)} ниже ожидаемой цены победителя "
                    f"{torgi_calc.rub(expected or start)}: пройдёт только при слабой конкуренции"
                )
            if src == "auctions":
                why.append(
                    "цена перепродажи взята из итогов торгов района, а не с рынка — по одним "
                    "торгам «интересен» не ставим: найдите объявления рядом или задайте рынок"
                )
            elif src == "cadastre":
                plus.append("сделка проходит на старте даже по кадастровой стоимости")
                why.append(
                    "рыночной цены нет, расчёт по кадастровой стоимости (нижний ориентир) — "
                    "найдите объявления рядом или задайте рынок района"
                )
            elif low_fail:
                why.append(
                    "по нижней цене перепродажи сделка не проходит уже на старте: безопасный "
                    f"ориентир {torgi_calc.rub(limits['min'])} ниже старта"
                )
            elif low_conf:
                why.append("ориентир цены перепродажи ненадёжный — уточните рынок")
    reasons += why
    minus = why + minus
    headroom = round(limit / start, 2) if limit and start else None
    steps_left = None
    if limit and start and lot.get("price_step") and limit >= start:
        steps_left = int((limit - start) // lot["price_step"])
    score = 0.0
    if headroom:
        score += 50 * min(headroom, 3.0) / 3.0
    score += 0.3 * (idx or 0)
    score += {3: 20, 2: 12, 1: 4}.get((mk or {}).get("confidence", 0), 0)
    return {
        "verdict": verdict,
        "verdict_label": VERDICTS[verdict],
        "rank": _RANK[verdict],
        "score": round(score, 1),
        "plus": plus,
        "minus": minus,
        "stop": stop,
        "ask": ask,
        "why": reasons,
        "days_left": days,
        "expected_price": expected,
        "sale": sale,
        "limits": limits,
        "limit": limit,
        "safe": safe,
        "base": base_key,
        "headroom": headroom,
        "steps_left": steps_left,
        "roi_start": roi_start,
        "roi_expected": roi_expected,
        "annual_start": _annual(roi_start, hold),
        "hold_months": hold,
        "market": mk,
        "market_based": src in MARKET_SOURCES,
        "sale_sotka": sale_sotka,
        "spread": spread,
        "confidence": CONFIDENCE_RU.get((mk or {}).get("confidence", 0)),
        "liquidity": {
            "index": idx,
            "label": liq.get("label"),
            "sell_through": sell_through,
            "growth": growth,
            "growth_where": growth_where,
            "applied": liq.get("applied_median"),
            "closed": liq.get("closed"),
        },
        "start_m2": round(start / area, 1) if start and area else None,
        "start_sotka": round(start / area * SOTKA) if start and area else None,
        "repeats": n_rep,
    }


def scan(
    lots: list[dict[str, Any]],
    params: ScanParams | None = None,
    *,
    layer: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Весь регион разом: идущие лоты с оценкой (лучшие первыми) и счётчики по вердиктам."""
    p = params or ScanParams()
    stats = index_by(lots)
    repeats = repeats_of(lots)
    rows = []
    forms = {"auction": 0, "notice": 0}
    # что в выбранном месте отсеяли остальные фильтры — чтобы пустой список объяснял себя
    # (зум 13.11, 07:06–09:50: в квартале 11–13 лотов, а у нас «лотов нет»)
    hidden = {"form": 0, "kind": 0, "group": 0, "area": 0}
    for lot in lots:
        if lot.get("status") not in ACTIVE:
            continue
        if p.place and not in_place(lot, p.place):
            continue
        form = "notice" if is_notice(lot) else "auction"
        forms[form] += 1
        if p.forms in forms and form != p.forms:
            hidden["form"] += 1
            continue
        if p.kinds and lot.get("kind") not in p.kinds:
            hidden["kind"] += 1
            continue
        if p.groups and lot.get("vri_group") not in p.groups:
            hidden["group"] += 1
            continue
        area = lot.get("area_m2")
        if (p.area_min_m2 and (not area or area < p.area_min_m2)) or (
            p.area_max_m2 and (not area or area > p.area_max_m2)
        ):
            hidden["area"] += 1
            continue
        rows.append(
            {**lot, "s": score_lot(lot, stats, p, layer=layer, repeats=repeats, today=today)}
        )
    rows.sort(key=lambda r: (r["s"]["rank"], -r["s"]["score"]))
    counts = {key: 0 for key in VERDICTS}
    for r in rows:
        counts[r["s"]["verdict"]] += 1
    return {
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        "stats": stats,
        "forms": forms,
        "hidden": hidden,
        # у скольких лотов цена перепродажи взята с рынка: только они могут быть «интересен»
        "market_based": sum(1 for r in rows if r["s"]["market_based"]),
        "priced": sum(1 for r in rows if r["s"]["market"]),
    }


# --- выгрузки ------------------------------------------------------------------------------
XLSX_COLUMNS = (
    "Вердикт",
    "Кадастровый номер",
    "Сделка",
    "Назначение",
    "Адрес",
    "Площадь, сот.",
    "Начальная цена, ₽",
    "Шаг, ₽",
    "Задаток, ₽",
    "Старт, ₽/сот.",
    "Заявки до",
    "Торги",
    "Ожидаемая цена победителя, ₽",
    "Продажа MIN, ₽",
    "Продажа MEDIUM, ₽",
    "Продажа MAX, ₽",
    "Источник цены продажи",
    "Предельная цена входа, ₽",
    "Безопасный ориентир, ₽",
    "Шагов до предела",
    "ROI при входе по старту",
    "Годовых при входе по старту",
    "Индекс ликвидности района",
    "Доля проданных в районе",
    "Рост цены к старту в районе",
    "Что за",
    "Что против",
    "Что уточнить",
    "Вид торгов",
    "Лот",
)


def xlsx_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    out = []
    for r in rows:
        s = r["s"]
        liq = s["liquidity"]
        out.append(
            [
                s["verdict_label"],
                r.get("cad_num"),
                KIND_RU.get(r.get("kind") or "", r.get("kind")),
                r.get("vri"),
                r.get("address"),
                round(r["area_m2"] / SOTKA, 2) if r.get("area_m2") else None,
                r.get("price_start"),
                r.get("price_step"),
                r.get("deposit"),
                s.get("start_sotka"),
                r.get("bidd_end"),
                r.get("auction_date"),
                s.get("expected_price"),
                s["sale"]["min"],
                s["sale"]["mid"],
                s["sale"]["max"],
                (s.get("market") or {}).get("source_label"),
                s.get("limit"),
                s.get("safe"),
                s.get("steps_left"),
                s.get("roi_start"),
                s.get("annual_start"),
                liq.get("index"),
                liq.get("sell_through"),
                liq.get("growth"),
                "; ".join(s["plus"]),
                "; ".join(s["minus"] + s["stop"]),
                "; ".join(s["ask"]),
                bidd_label(r),
                torgi.LOT_URL.format(lot_id=r["id"]),
            ]
        )
    return out


def to_xlsx(rows: list[dict[str, Any]], path: Any, *, title: str = "Торги") -> Any:
    """Таблица в формате, привычном эксперту: строки — лоты, колонки — как в его
    «Калькуляторе эффективности объектов» плюс ликвидность и причины."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append(list(XLSX_COLUMNS))
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    fills = {
        VERDICTS["go"]: PatternFill("solid", fgColor="D9EAD3"),
        VERDICTS["edge"]: PatternFill("solid", fgColor="FCE8B2"),
        VERDICTS["no"]: PatternFill("solid", fgColor="F4C7C3"),
    }
    for row in xlsx_rows(rows):
        ws.append(row)
        fill = fills.get(row[0])
        if fill:
            ws.cell(row=ws.max_row, column=1).fill = fill
        for col in (21, 22, 24):
            ws.cell(row=ws.max_row, column=col).number_format = "0%"
    widths = {1: 14, 2: 20, 4: 30, 5: 40, 17: 30, 26: 50, 27: 50, 28: 40, 29: 50}
    for col, width in widths.items():
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width
    ws.freeze_panes = "C2"
    wb.save(path)
    return path


_KML_STYLE = {
    "go": "ff3c8f3c",
    "edge": "ff1fa5e6",
    "no": "ff4040d0",
    "unknown": "ff999999",
    "notice": "ffb07a3a",
}


def to_kml(rows: list[dict[str, Any]], *, name: str = "Торги: идущие лоты") -> str:
    """Слои для Google My Maps (эксперт ведёт там «ликвид / неликвид»): папка на вердикт,
    метка на лот с координатами. Лоты без координат в файл не попадают."""
    folders: dict[str, list[str]] = {key: [] for key in VERDICTS}
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        s = r["s"]
        title = f"{r.get('cad_num') or r['id']} · {KIND_RU.get(r.get('kind') or '', '')}"
        if r.get("price_start"):
            title += f" · {torgi_calc.rub(r['price_start'])}"
        lines = [
            f"Вердикт: {s['verdict_label']}",
            r.get("vri") or "",
            r.get("address") or "",
            f"Площадь: {r['area_m2'] / SOTKA:.1f} сот." if r.get("area_m2") else "",
            f"Заявки до {r['bidd_end']}" if r.get("bidd_end") else "",
            f"Предельная цена входа: {torgi_calc.rub(s['limit'])}" if s.get("limit") else "",
            f"Индекс ликвидности района: {s['liquidity']['index']}"
            if s["liquidity"].get("index") is not None
            else "",
            "За: " + "; ".join(s["plus"]) if s["plus"] else "",
            "Против: " + "; ".join(s["minus"] + s["stop"]) if s["minus"] or s["stop"] else "",
            torgi.LOT_URL.format(lot_id=r["id"]),
        ]
        desc = "\n".join(line for line in lines if line)
        folders[s["verdict"]].append(
            f"<Placemark><name>{escape(title)}</name>"
            f"<description>{escape(desc)}</description>"
            f"<styleUrl>#v-{s['verdict']}</styleUrl>"
            f"<Point><coordinates>{r['lon']:.6f},{r['lat']:.6f},0</coordinates></Point>"
            "</Placemark>"
        )
    styles = "".join(
        f'<Style id="v-{key}"><IconStyle><color>{color}</color></IconStyle></Style>'
        for key, color in _KML_STYLE.items()
    )
    body = "".join(
        f"<Folder><name>{escape(VERDICTS[key])}</name>{''.join(marks)}</Folder>"
        for key, marks in folders.items()
        if marks
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{escape(name)}</name>{styles}{body}</Document></kml>"
    )
