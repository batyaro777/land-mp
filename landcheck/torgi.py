"""Торги земельными участками рядом — ГИС «Торги» torgi.gov.ru (ТЗ v3, п. 9).

Что считаем: за последние 12 месяцев в кадастровом районе участка (первые две группы
номера, например `40:13`) — сколько электронных аукционов по земле состоялось, сколько
не состоялось, сколько идёт сейчас; по состоявшимся — медиана итоговой цены за м²
(продажа и аренда отдельно) и насколько итог выше старта. Это ликвидность локации
и ориентир «почём реально уходит земля у государства», а не рыночная оценка.

Источник — публичный API torgi.gov.ru (без авторизации). Из-за гео-блока запросы идут
через RU-шлюз (`landcheck.ruproxy`). Ограничения API, снятые 01.11.2026: страница не
больше 10 лотов, `text=` ищет по названию и описанию (префикс кадастрового номера
работает), итоговая цена `priceFin` есть только в карточке лота, у «сообщений о
предоставлении» (biddForm=IPS) цены нет — поэтому считаем только электронные аукционы (EA).
"""

from __future__ import annotations

import logging
import re
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from landcheck import ruproxy

log = logging.getLogger(__name__)

API = "https://torgi.gov.ru/new/api/public"
LOT_URL = "https://torgi.gov.ru/new/public/lots/lot/{lot_id}"
SEARCH_URL = "https://torgi.gov.ru/new/public/lots/reg"
SOURCE = (
    "ГИС «Торги» torgi.gov.ru: земельные участки (catCode=2), электронные аукционы (EA), "
    "публикация за последние 12 месяцев, поиск по кадастровому району"
)
SCHEMA_VERSION = 1
MONTHS = 12
PAGE_SIZE = 10  # сервер молча режет size до 10
MAX_SUCCEED_PAGES = 3  # до 30 состоявшихся лотов → карточки с итоговой ценой
MAX_CARDS = 30
STATUS_ACTIVE = "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER"
STATUS_SUCCEED = "SUCCEED"
STATUS_FAILED = "FAILED"
MIN_AREA_M2 = 50.0  # меньше — не участок, а опечатка в карточке

_CAD_RE = re.compile(r"\b(\d{2}):(\d{2}):\d{6,7}:\d+\b")


def district_of(cad_num: str | None) -> str | None:
    """`40:13:120101:22` → `40:13` (кадастровый район)."""
    if not cad_num:
        return None
    parts = cad_num.strip().split(":")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"


def region_of(cad_num: str | None) -> str | None:
    d = district_of(cad_num)
    return d.split(":")[0] if d else None


# --- клиент ------------------------------------------------------------------------------
class TorgiClient:
    def __init__(self, client: httpx.Client | None = None):
        self._c = client or ruproxy.client()
        self.requests = 0

    def close(self) -> None:
        self._c.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests += 1
        r = self._c.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def search(
        self,
        *,
        region: str,
        text: str,
        statuses: str,
        pub_from: str,
        page: int = 0,
        form: str = "EA",
    ) -> dict[str, Any]:
        params = {
            "catCode": "2",
            "subjRF": region,
            "text": text,
            "lotStatus": statuses,
            "biddForm": form,
            "pubFrom": pub_from,
            "byFirstVersion": "true",
            "size": str(PAGE_SIZE),
            "page": str(page),
            "sort": "firstVersionPublicationDate,desc",
        }
        return self._get(f"{API}/lotcards/search", params)

    def lot(self, lot_id: str) -> dict[str, Any]:
        return self._get(f"{API}/lotcards/{lot_id}")


# --- разбор карточки --------------------------------------------------------------------
def _chars(card: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ch in card.get("characteristics") or []:
        code = ch.get("code")
        if code:
            out[code] = ch.get("characteristicValue")
    return out


def cad_num_of(card: dict[str, Any]) -> str | None:
    val = _chars(card).get("CadastralNumber")
    if isinstance(val, str) and _CAD_RE.search(val):
        return _CAD_RE.search(val).group(0)
    text = f"{card.get('lotName') or ''} {card.get('lotDescription') or ''}"
    m = _CAD_RE.search(text)
    return m.group(0) if m else None


def area_of(card: dict[str, Any]) -> float | None:
    val = _chars(card).get("SquareZU")
    try:
        area = float(val) if val is not None else None
    except (TypeError, ValueError):
        return None
    return area if area and area >= MIN_AREA_M2 else None


def vri_of(card: dict[str, Any]) -> str | None:
    val = _chars(card).get("PermittedUse")
    if isinstance(val, list) and val:
        return (val[0] or {}).get("name")
    if isinstance(val, dict):
        return val.get("name")
    return None


def _date(card: dict[str, Any]) -> str | None:
    raw = (
        card.get("auctionStartDate")
        or card.get("biddEndTime")
        or card.get("noticeFirstVersionPublicationDate")
    )
    return raw[:10] if isinstance(raw, str) else None


def _code(obj: Any) -> str | None:
    return obj.get("code") if isinstance(obj, dict) else (obj if isinstance(obj, str) else None)


def item_of(card: dict[str, Any], district: str) -> dict[str, Any] | None:
    """Строка таблицы «Торги рядом»; None — лот не из этого кадастрового района."""
    cad = cad_num_of(card)
    if cad is None or district_of(cad) != district:
        return None
    area = area_of(card)
    start = card.get("priceMin")
    fin = card.get("priceFin")
    kind = "аренда" if card.get("typeTransaction") == "rent" else "продажа"
    price = fin if isinstance(fin, int | float) else None
    rub_m2 = round(price / area, 1) if price and area else None
    uplift = (
        round(fin / start - 1, 3)
        if isinstance(fin, int | float) and isinstance(start, int | float) and start > 0
        else None
    )
    return {
        "id": card.get("id"),
        "url": LOT_URL.format(lot_id=card.get("id")),
        "cad_num": cad,
        "kind": kind,
        "status": card.get("lotStatus"),
        "area_m2": area,
        "price_start": start if isinstance(start, int | float) else None,
        "price_fin": price,
        "rub_m2": rub_m2,
        "uplift": uplift,
        "date": _date(card),
        "address": card.get("estateAddress"),
        "vri": vri_of(card),
        "category": (card.get("category") or {}).get("name"),
        "form": _code(card.get("biddForm")),
    }


# --- статистика --------------------------------------------------------------------------
def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 1)


def _stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    per_m2 = [i["rub_m2"] for i in items if i.get("rub_m2")]
    uplifts = [i["uplift"] for i in items if i.get("uplift") is not None]
    return {
        "count": len(items),
        "with_price": len(per_m2),
        "median_rub_m2": round(statistics.median(per_m2), 1) if per_m2 else None,
        "p25_rub_m2": _quantile(per_m2, 0.25),
        "p75_rub_m2": _quantile(per_m2, 0.75),
        "median_uplift": round(statistics.median(uplifts), 3) if uplifts else None,
    }


def empty_result(cad_num: str, error: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "region": region_of(cad_num),
        "district": district_of(cad_num),
        "months": MONTHS,
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "succeed_total": 0,
        "failed_total": 0,
        "active_total": 0,
        "ea_total": 0,
        "liquidity": None,
        "sale": _stats([]),
        "rent": _stats([]),
        "items": [],
        "sample_note": None,
        "search_url": SEARCH_URL,
        "source": SOURCE,
        "error": error,
        "liquidity_text": error or "торги не запрашивались",
    }


def nearby_auctions(
    cad_num: str,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Сводка торгов по кадастровому району участка. Ошибки — внутри (`error`), не наружу."""
    district = district_of(cad_num)
    region = region_of(cad_num)
    if district is None or region is None:
        return empty_result(cad_num, "кадастровый номер без района — торги не ищем")
    if client is None and not ruproxy.enabled():
        return empty_result(
            cad_num, "RU-шлюз не настроен (LANDCHECK_RU_PROXY) — torgi.gov.ru из-за рубежа закрыт"
        )
    now = now or datetime.now(UTC)
    pub_from = (now - timedelta(days=30 * MONTHS)).date().isoformat()
    api = TorgiClient(client)
    result = empty_result(cad_num)
    try:
        # 1. Состоявшиеся аукционы: страницы поиска → карточки (итоговая цена только там).
        succeed_pages: list[dict[str, Any]] = []
        first = api.search(region=region, text=district, statuses=STATUS_SUCCEED, pub_from=pub_from)
        succeed_pages.append(first)
        total_pages = int(first.get("totalPages") or 0)
        for page in range(1, min(total_pages, MAX_SUCCEED_PAGES)):
            succeed_pages.append(
                api.search(
                    region=region,
                    text=district,
                    statuses=STATUS_SUCCEED,
                    pub_from=pub_from,
                    page=page,
                )
            )
        result["succeed_total"] = int(first.get("totalElements") or 0)
        items: list[dict[str, Any]] = []
        cards = 0
        for page_json in succeed_pages:
            for hit in page_json.get("content") or []:
                if cards >= MAX_CARDS:
                    break
                if item_of(hit, district) is None:  # чужой район по тексту — карточку не тянем
                    continue
                cards += 1
                try:
                    card = api.lot(hit["id"])
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("torgi: карточка %s: %s", hit.get("id"), exc)
                    card = hit
                row = item_of(card, district)
                if row:
                    items.append(row)
        # 2. Не состоявшиеся и идущие сейчас — только счётчики (цены старта в поиске есть,
        #    но ориентир строим по итоговым).
        failed = api.search(region=region, text=district, statuses=STATUS_FAILED, pub_from=pub_from)
        active = api.search(region=region, text=district, statuses=STATUS_ACTIVE, pub_from=pub_from)
        result["failed_total"] = int(failed.get("totalElements") or 0)
        result["active_total"] = int(active.get("totalElements") or 0)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("torgi: %s: %s", district, exc)
        result["error"] = f"torgi.gov.ru не ответил: {type(exc).__name__}"
        result["liquidity_text"] = result["error"]
        return result
    finally:
        if client is None:
            api.close()
    result["ea_total"] = result["succeed_total"] + result["failed_total"] + result["active_total"]
    closed = result["succeed_total"] + result["failed_total"]
    result["liquidity"] = round(result["succeed_total"] / closed, 2) if closed else None
    items.sort(key=lambda i: i.get("date") or "", reverse=True)
    result["items"] = items[:15]
    result["sale"] = _stats([i for i in items if i["kind"] == "продажа"])
    result["rent"] = _stats([i for i in items if i["kind"] == "аренда"])
    if result["succeed_total"] > len(items):
        result["sample_note"] = (
            f"цены посчитаны по {len(items)} последним состоявшимся аукционам из "
            f"{result['succeed_total']}"
        )
    result["requests"] = api.requests
    result["liquidity_text"] = liquidity_text(result)
    return result


def liquidity_text(a: dict[str, Any]) -> str:
    """Одна фраза для отчёта: насколько охотно здесь покупают землю на торгах."""
    if a.get("error"):
        return a["error"]
    if not a.get("ea_total"):
        return "за год электронных аукционов по земле в этом кадастровом районе не было"
    liq = a.get("liquidity")
    if liq is None:
        return f"идут {a['active_total']} аукционов, завершённых за год ещё нет"
    if liq >= 0.6:
        verdict = "спрос есть: большинство аукционов состоялись"
    elif liq >= 0.3:
        verdict = "спрос неровный: состоялась примерно треть–половина аукционов"
    else:
        verdict = "спрос слабый: большинство аукционов не состоялись (нет заявок)"
    return (
        f"состоялись {a['succeed_total']} из {a['succeed_total'] + a['failed_total']} "
        f"завершённых ({int(liq * 100)} %), идут сейчас {a['active_total']} — {verdict}"
    )
