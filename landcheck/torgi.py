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
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from landcheck import ruproxy

log = logging.getLogger(__name__)

API = "https://torgi.gov.ru/new/api/public"
LOT_URL = "https://torgi.gov.ru/new/public/lots/lot/{lot_id}"
FILE_URL = "https://torgi.gov.ru/new/file-store/v1/{file_id}"
SEARCH_URL = "https://torgi.gov.ru/new/public/lots/reg"
SOURCE = (
    "ГИС «Торги» torgi.gov.ru: земельные участки (catCode=2), электронные аукционы (EA), "
    "публикация за последние 12 месяцев, поиск по кадастровому району"
)
SCHEMA_VERSION = 1
MONTHS = 12
PAGE_SIZE = 10  # сервер молча режет size до 10
# Страниц поиска на каждый статус. Поиск по тексту отдаёт и соседние районы, свои лоты
# размазаны по страницам: при 8 страницах 40:10 терял единственную продажу, при 15 —
# состоявшиеся (145 по тексту) собраны целиком, а не состоявшиеся (255) обрезаны (Клавдий,
# прод 01.11). 30 страниц = 300 лотов на статус; что обрезано — названо в пометке.
MAX_PAGES = 30
MAX_CARDS = 12  # карточек состоявшихся лотов (итоговая цена только в карточке)
# torgi.gov.ru режет частые запросы: после 3 подряд — 503, с паузой 6 с проходят все
# (Клавдий, прод 01.11). Пауза общая на процесс, повтор на 503 с нарастающей выдержкой.
REQUEST_INTERVAL_S = 6.0
RETRY_DELAYS_S = (10.0, 20.0, 40.0)
STATUS_ACTIVE = "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER"
STATUS_SUCCEED = "SUCCEED"
STATUS_FAILED = "FAILED"
MIN_AREA_M2 = 50.0  # меньше — не участок, а опечатка в карточке
MAX_FILE_MB = 40.0  # ГПЗУ со сканами чертежей бывают по 15 МБ (Карелия, 07.11)

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


def subject_code(region: str | None) -> str:
    """Код субъекта для запроса к torgi.gov.ru — без ведущего нуля. У нас код региона — две
    цифры, как в кадастровом номере («04»), а сайт хранит регионы 01–09 как «1»–«9»: прод,
    12.11.2026, Республика Алтай — поиск с «04» вернул 0 лотов, с «4» — 2096. В ответе код
    приходит так же, без нуля; в базу он пишется двумя цифрами (`torgi_scan.lot_row`)."""
    r = (region or "").strip()
    return str(int(r)) if r.isdigit() else r


# --- клиент ------------------------------------------------------------------------------
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _wait_turn(interval: float, sleep=time.sleep) -> None:
    """Не чаще одного запроса в `interval` секунд на весь процесс (все районы, все потоки)."""
    global _last_request_at
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_request_at + interval - now
        if wait > 0:
            sleep(wait)
            now = time.monotonic()
        _last_request_at = now


class TorgiClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        interval: float = REQUEST_INTERVAL_S,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_S,
        sleep=time.sleep,
    ):
        self._c = client or ruproxy.client()
        self.requests = 0
        self.retries = 0
        self._interval = interval
        self._retry_delays = retry_delays
        self._sleep = sleep

    def close(self) -> None:
        self._c.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(len(self._retry_delays) + 1):
            _wait_turn(self._interval, self._sleep)
            self.requests += 1
            r = self._c.get(url, params=params)
            if r.status_code in (429, 503) and attempt < len(self._retry_delays):
                self.retries += 1
                delay = self._retry_delays[attempt]
                log.info("torgi: %s → %s, пауза %.0f с", url, r.status_code, delay)
                self._sleep(delay)
                continue
            r.raise_for_status()
            return r.json()
        raise httpx.HTTPError("torgi: исчерпаны повторы")  # pragma: no cover

    def search(
        self,
        *,
        region: str,
        statuses: str,
        pub_from: str,
        text: str | None = None,
        page: int = 0,
        form: str | None = "EA",
    ) -> dict[str, Any]:
        """Страница поиска лотов по земле. Без `text` — все лоты региона (анализатор торгов)."""
        params = {
            "catCode": "2",
            "subjRF": subject_code(region),
            "lotStatus": statuses,
            "pubFrom": pub_from,
            "byFirstVersion": "true",
            "size": str(PAGE_SIZE),
            "page": str(page),
            "sort": "firstVersionPublicationDate,desc",
        }
        if text:
            params["text"] = text
        if form:
            params["biddForm"] = form
        return self._get(f"{API}/lotcards/search", params)

    def lot(self, lot_id: str) -> dict[str, Any]:
        return self._get(f"{API}/lotcards/{lot_id}")

    def file(self, file_id: str, *, max_mb: float = MAX_FILE_MB) -> bytes:
        """Вложение лота из file-store (ГПЗУ, проект договора) — тот же троттлинг и повторы."""
        url = FILE_URL.format(file_id=file_id)
        for attempt in range(len(self._retry_delays) + 1):
            _wait_turn(self._interval, self._sleep)
            self.requests += 1
            r = self._c.get(url)
            if r.status_code in (429, 503) and attempt < len(self._retry_delays):
                self.retries += 1
                self._sleep(self._retry_delays[attempt])
                continue
            r.raise_for_status()
            if len(r.content) > max_mb * 1_048_576:
                raise ValueError(f"файл больше {max_mb:.0f} МБ")
            return r.content
        raise httpx.HTTPError("torgi: исчерпаны повторы")  # pragma: no cover


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
    """Площадь участка. У извещений о предоставлении участок часто ещё не образован: площадь
    там проектная (`SquareZU_project`), кадастрового номера нет (прод, Карелия, 10.11)."""
    chars = _chars(card)
    val = chars.get("SquareZU")
    if val is None:
        val = chars.get("SquareZU_project")
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
    ratio = (
        round(fin / start, 2)
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
        "ratio": ratio,
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
    ratios = [i["ratio"] for i in items if i.get("ratio") is not None]
    return {
        "count": len(items),
        "with_price": len(per_m2),
        "median_rub_m2": round(statistics.median(per_m2), 1) if per_m2 else None,
        "p25_rub_m2": _quantile(per_m2, 0.25),
        "p75_rub_m2": _quantile(per_m2, 0.75),
        "median_ratio": round(statistics.median(ratios), 2) if ratios else None,
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
        # Чем кончились не состоявшиеся: ушли единственному участнику / не ушли / карточка не
        # прочитана. Есть только в сводке из собранной базы региона (нужна карточка каждого
        # лота); None — известны одни статусы.
        "single_total": None,
        "none_total": None,
        "unknown_total": None,
        "sale": _stats([]),
        "rent": _stats([]),
        "items": [],
        "complete": True,
        "truncated": [],
        "sample_note": None,
        "search_url": SEARCH_URL,
        "source": SOURCE,
        "error": error,
        "liquidity_text": error or "торги не запрашивались",
    }


def _collect(
    api: TorgiClient, region: str, district: str, statuses: str, pub_from: str
) -> tuple[list[dict[str, Any]], bool]:
    """Страницы поиска → лоты своего кадастрового района. Второе значение — все ли страницы
    просмотрены (иначе счётчик занижен)."""
    hits: list[dict[str, Any]] = []
    page, total_pages = 0, 1
    while page < total_pages and page < MAX_PAGES:
        data = api.search(
            region=region, text=district, statuses=statuses, pub_from=pub_from, page=page
        )
        total_pages = int(data.get("totalPages") or 0)
        hits.extend(h for h in data.get("content") or [] if item_of(h, district) is not None)
        page += 1
    return hits, page >= total_pages


def nearby_auctions(
    cad_num: str,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    interval: float = REQUEST_INTERVAL_S,
    sleep=time.sleep,
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
    api = TorgiClient(client, interval=interval, sleep=sleep)
    result = empty_result(cad_num)
    try:
        # Поиск по тексту «40:13» отдаёт и соседние районы (40:14, 40:17…), поэтому считаем
        # только лоты, у которых кадастровый номер действительно из этого района.
        succeed, done1 = _collect(api, region, district, STATUS_SUCCEED, pub_from)
        failed, done2 = _collect(api, region, district, STATUS_FAILED, pub_from)
        active, done3 = _collect(api, region, district, STATUS_ACTIVE, pub_from)
        result["succeed_total"] = len(succeed)
        result["failed_total"] = len(failed)
        result["active_total"] = len(active)
        result["complete"] = done1 and done2 and done3
        result["truncated"] = [
            name
            for name, done in (
                ("состоявшиеся", done1),
                ("не состоявшиеся", done2),
                ("идущие", done3),
            )
            if not done
        ]
        items: list[dict[str, Any]] = []
        for hit in succeed[:MAX_CARDS]:  # поиск уже отсортирован: новые первыми
            try:
                card = api.lot(hit["id"])
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("torgi: карточка %s: %s", hit.get("id"), exc)
                card = hit
            row = item_of(card, district)
            if row:
                items.append(row)
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
    notes = []
    if result["succeed_total"] > len(items):
        notes.append(
            f"цены посчитаны по {len(items)} последним состоявшимся аукционам из "
            f"{result['succeed_total']}"
        )
    if not result["complete"]:
        notes.append(
            f"{', '.join(result['truncated'])}: просмотрено {MAX_PAGES * PAGE_SIZE} последних "
            "лотов поиска — счётчик по району занижен"
        )
    result["sample_note"] = "; ".join(notes) or None
    result["requests"] = api.requests
    result["retries"] = api.retries
    result["liquidity_text"] = liquidity_text(result)
    return result


# --- лоты самого участка (ТЗ v3, п. 1.12; чат «МП Земля», сообщение 27) ------------------
# Эксперт 07.11: веб-поиск находит участок на агрегаторах торгов, а итог нужен по
# первоисточнику — состоялись или нет, за сколько ушёл, какие документы. Карточка лота
# torgi.gov.ru отдаёт статус, итоговую цену, протоколы (без файлов: тип, номер, дата) и
# вложения лота и извещения (ГПЗУ, проект договора, форма заявки). Победителя и участников
# публичный API не отдаёт — они в протоколе на площадке (`etpUrl`).
ALL_STATUSES = "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER,SUCCEED,FAILED,CANCELED"
STATUS_RU = {
    "PUBLISHED": "опубликован",
    "APPLICATIONS_SUBMISSION": "приём заявок",
    "DETERMINING_WINNER": "определение победителя",
    "SUCCEED": "состоялся",
    "FAILED": "не состоялся",
    "CANCELED": "отменён",
}
ACTIVE = {"PUBLISHED", "APPLICATIONS_SUBMISSION", "DETERMINING_WINNER"}
LOT_MONTHS = 36
MAX_OWN_LOTS = 4  # карточек на участок: каждая — запрос раз в 6 с
OWN_LOT_SCHEMA = 2


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) else None


def _day(v: Any) -> str | None:
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def _docs_of(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Вложения лота и извещения: ГПЗУ, проект договора, форма заявки, извещение. Без
    неактивных и без повторов (одни и те же файлы бывают и у лота, и у извещения)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("lotAttachments", "noticeAttachments"):
        for a in card.get(key) or []:
            fid = a.get("fileId")
            if not fid or fid in seen or a.get("inactive"):
                continue
            seen.add(fid)
            out.append(
                {
                    "name": a.get("attachmentTypeName") or a.get("fileName") or "документ",
                    "file": a.get("fileName"),
                    "file_id": fid,
                    "url": FILE_URL.format(file_id=fid),
                    "date": _day(a.get("uploadDate")),
                    "size_mb": round(a["fileSize"] / 1_048_576, 1)
                    if isinstance(a.get("fileSize"), int | float)
                    else None,
                    "gpzu": a.get("attachmentTypeCode") == "Plan_lands",
                }
            )
    return out


ORG_TYPE_RU = {
    "INDIVIDUAL_PERSON": "физлицо",
    "INDIVIDUAL_ENTREPRENEUR": "ИП",
    "LEGAL_ENTITY": "юрлицо",
    "LEGAL_PERSON": "юрлицо",
}


# --- причина «торги не состоялись» ------------------------------------------------------
# Эксперт по торгам (чат, 13.11.2026): «не состоялись» — разные исходы, а надёжный признак —
# протокол: «публикуется, и в нём данные». В карточке лота у протокола, где решилась судьба
# торгов, есть поле abandonedReason {code, name} — причина из справочника площадки. Коды
# сняты с прода 13.11.2026: проба 18 карточек, затем перечитаны 1045 не состоявшихся лотов
# трёх регионов (26 кодов; ZK — земля, 200FZ — леса, 229FZ — имущество должников, 178FZ —
# приватизация).
CARD_REV = 1  # версия разбора карточки: не состоявшиеся лоты со старой версией перечитываются

FAIL_KIND_BY_CODE = {
    # подана одна заявка
    "ZK_EA2_39_1": "one_bid",  # на день окончания приёма подана единственная заявка
    "ZK_EA_14_39.12_1": "one_bid",  # подана только одна заявка
    "ZK_EA2_39_14_1": "one_bid",
    "200FZ_EA_2": "one_bid",
    # допущен один заявитель
    "ZK_EA_12_39.12_2": "one_admitted",  # допущен и признан участником только один заявитель
    "ZK_EA2_39_12_2": "one_admitted",
    "ZK_EA2_39_2": "one_admitted",
    # допущено несколько, на торги вышел один
    "ZK_EA_19_39.12_1": "one_came",  # в аукционе участвовал только один участник
    "ZK_EA2_39_3": "one_came",  # в торгах участвовал один участник торгов
    # заявок не подано
    "ZK_EA_14_39.12_2": "no_bids",  # не подано ни одной заявки
    "ZK_EA2_39_14_2": "no_bids",
    "200FZ_EA_1": "no_bids",
    # всем заявителям отказано
    "ZK_EA_12_39.12_1": "all_rejected",  # отказано в допуске всем заявителям
    "ZK_EA2_39_12_1": "all_rejected",
    # «не подано ни одной заявки либо отказано всем» — одна запись справочника на оба случая
    "ZK_EA2_39_0": "no_bids_or_rejected",
    "178FZ_EA_3": "no_bids_or_rejected",
    # допущенные участники на торги не пришли
    "ZK_EA_19_39.12_2": "no_show",  # не присутствовал ни один из участников аукциона
    # пришли, но цену никто не предложил
    "ZK_EA_19_39.12_3": "no_offers",  # не поступило ни одного предложения о более высокой цене
    "ZK_EA2_39_4": "no_offers",  # ни один из участников не подал предложение о цене
    "200FZ_EA_4": "no_offers",  # никто не заявил о намерении заключить договор по начальной цене
}
# Записи, которые исход не называют: «менее двух», «не явились либо явился один», «иное».
# По ним остаётся прежний признак — цена договора (прод, 13.11.2026: у 200FZ_EA_3 цена есть
# у всех 35 лотов, у 229FZ_* — у одного из 91).
FAIL_AMBIGUOUS = frozenset(
    {"200FZ_EA_3", "229FZ_EA_102_1", "229FZ_PA_102_1", "229FZ_PA_102_2", "ZKPT_PPZ_0", "ZKPT_PA_3"}
)
# Участок ушёл без борьбы — договор с единственным участником (ст. 39.12 ЗК) — и не ушёл.
FAIL_TAKEN = ("one_bid", "one_admitted", "one_came")
FAIL_LEFT = ("no_bids", "all_rejected", "no_bids_or_rejected", "no_show", "no_offers")
FAIL_KIND_RU = {
    "one_bid": "подана одна заявка",
    "one_admitted": "допущен один заявитель",
    "one_came": "на торги вышел один участник",
    "no_bids": "заявок не подано",
    "all_rejected": "всем заявителям отказано в допуске",
    "no_bids_or_rejected": "заявок не подано либо всем отказано",
    "no_show": "допущенные участники не пришли на торги",
    "no_offers": "никто не предложил цену",
}
# Новые коды справочника разбираем по тексту; порядок важен: «ни одной заявки» раньше «одна
# заявка», «ни один из участников» раньше «один участник». Текст с «либо» и «менее двух»
# называет сразу два исхода — по нему не решаем.
_FAIL_TEXT = (
    ("no_bids", ("ни одной заявки", "не подано заяв", "заявки не поданы", "отсутствие заявок")),
    (
        "all_rejected",
        ("отказано в допуске всем", "отказе в допуске", "ни один заявитель не допущен"),
    ),
    ("no_show", ("не присутствовал ни один", "никто из участников не явился")),
    (
        "no_offers",
        ("ни одного предложения", "не подал предложение о цене", "ни один из участников"),
    ),
    ("one_bid", ("единственная заявка", "только одна заявка", "одна заявка")),
    (
        "one_admitted",
        ("только один заявитель", "только одного заявителя", "единственный заявитель"),
    ),
    ("one_came", ("только один участник", "один участник", "единственный участник")),
)


def fail_kind(code: str | None, name: str | None = None) -> str | None:
    """Чем кончились не состоявшиеся торги по причине из протокола; None — причины нет или
    она исход не называет."""
    if code in FAIL_KIND_BY_CODE:
        return FAIL_KIND_BY_CODE[code]
    if code in FAIL_AMBIGUOUS:
        return None
    text = (name or "").lower().replace("ё", "е")
    if not text or "менее чем два" in text or "менее двух" in text or " либо " in text:
        return None
    for kind, needles in _FAIL_TEXT:
        if any(n in text for n in needles):
            return kind
    return None


def _fail_reason_of(card: dict[str, Any]) -> tuple[str | None, str | None]:
    """(код, текст) причины из протоколов лота. Берём только запись справочника {code, name}:
    свободный текст организатора не храним — в нём бывают имена участников. Если причина
    есть в нескольких протоколах, верим более позднему (итог аукциона после рассмотрения)."""
    best: tuple[str, str | None, str | None] | None = None
    for p in card.get("protocols") or []:
        reason = (p or {}).get("abandonedReason")
        if not isinstance(reason, dict):
            continue
        code, name = reason.get("code"), reason.get("name")
        if not isinstance(code, str) or not code:
            continue
        when = str(p.get("publishDate") or "")
        if best is None or when >= best[0]:
            best = (when, code, name if isinstance(name, str) else None)
    return (best[1], best[2]) if best else (None, None)


def _protocols_of(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Протоколы лота: тип, дата, номер. Имена участников и победителя (у физлиц — ФИО) не
    берём вовсе: только число заявок, допущенных и цены первого и второго места."""
    out = []
    for p in card.get("protocols") or []:
        typ = p.get("type") if isinstance(p.get("type"), dict) else {}
        name = (typ.get("name") or "протокол").replace(" для размещения в ГИС Торги", "")
        out.append(
            {"name": name, "date": _day(p.get("publishDate")), "number": p.get("protocolNumber")}
        )
    out.sort(key=lambda p: p.get("date") or "")
    return out


def _result_of(card: dict[str, Any]) -> dict[str, Any]:
    """Итог из протоколов: заявок, допущено, цена победителя и второго места, кто победил
    (физлицо / ИП / юрлицо). Без имён."""
    applied = admitted = None
    first = second = None
    winner = None
    # Допущенных и отклонённых организаторы публикуют в разных протоколах (прод, 13.11.2026:
    # в протоколе результатов — 11 допущенных, в протоколе рассмотрения — один отклонённый;
    # брали последний список и получали «допущено 0»). Считаем по всем протоколам лота.
    ok_max = rej_max = None
    for p in card.get("protocols") or []:
        adm = p.get("participantAdmissions")
        if isinstance(adm, list) and adm:
            ok = sum(1 for a in adm if (a or {}).get("admissionStatus") == "ADMITTED")
            ok_max = max(ok_max or 0, ok)
            rej_max = max(rej_max or 0, len(adm) - ok)
            applied, admitted = ok_max + rej_max, ok_max
        for r in p.get("results") or []:
            price = _num((r or {}).get("participantPrice"))
            if r.get("isFirst"):
                first = price
                winner = ORG_TYPE_RU.get(r.get("orgType") or "")
            elif r.get("isSecond"):
                second = price
    fail_code, fail_reason = _fail_reason_of(card)
    return {
        "applied": applied,
        "admitted": admitted,
        "winner_price": first,
        "second_price": second,
        "winner_type": winner,
        "fail_code": fail_code,
        "fail_reason": fail_reason,
    }


def outcome_of(lot: dict[str, Any]) -> tuple[str, str]:
    """(итог, что это значит) простыми словами — по статусу лота и ценам."""
    status = lot.get("status")
    rent = lot.get("kind") == "аренда"
    if lot.get("annulled"):
        return "извещение аннулировано", "торги отменены целиком — итога нет"
    if lot.get("notice_3918"):
        if status == "CANCELED":
            return "отменено", "извещение о предоставлении отозвано"
        return (
            "извещение о предоставлении (ст. 39.18 ЗК)",
            "участок предлагали конкретному заявителю; если за 30 дней пришли заявки других "
            "желающих — назначают аукцион",
        )
    if status == "SUCCEED":
        fin, start = lot.get("price_fin"), lot.get("price_start")
        raised = bool(fin and start and fin > start)
        if rent:
            note = "право аренды ушло победителю" if raised else "аренда — по начальной цене"
            note += (
                "; у участка есть арендатор по итогам аукциона: если участок вам предлагают, "
                "продавец должен быть этим арендатором (переуступка права аренды)"
            )
        else:
            note = "участок продан победителю" if raised else "продан по начальной цене"
            note += "; сверьте по выписке ЕГРН, что продавец — победитель торгов или его преемник"
        return "торги состоялись", note
    if status == "FAILED":
        kind = fail_kind(lot.get("fail_code"), lot.get("fail_reason"))
        if kind in FAIL_TAKEN:
            return (
                f"торги не состоялись ({FAIL_KIND_RU[kind]})",
                "по протоколу договор заключают с единственным участником (ст. 39.12 ЗК) — "
                "у участка, скорее всего, уже есть арендатор или покупатель",
            )
        if kind in FAIL_LEFT:
            return (
                f"торги не состоялись ({FAIL_KIND_RU[kind]})",
                "по протоколу участок не ушёл — может выйти на торги снова",
            )
        if lot.get("applied") == 0 or lot.get("admitted") == 0:
            return "торги не состоялись", "допущенных заявок не было — участок может выйти снова"
        if lot.get("applied") == 1 or lot.get("admitted") == 1:
            return (
                "торги не состоялись (одна заявка)",
                "договор заключают с единственным участником по начальной цене (ст. 39.12 ЗК) — "
                "у участка, скорее всего, уже есть арендатор или покупатель",
            )
        return (
            "торги не состоялись",
            "заявок не было или подана одна — тогда договор заключают с единственным участником "
            "по начальной цене (ст. 39.12 ЗК); что именно — в протоколе",
        )
    if status == "CANCELED":
        return "торги отменены", "организатор отменил торги — участок может выйти на торги снова"
    if status in ACTIVE:
        return f"идут: {STATUS_RU.get(status, status)}", "итога ещё нет"
    return STATUS_RU.get(status or "", status or "—"), ""


def own_lot_row(card: dict[str, Any]) -> dict[str, Any]:
    form = _code(card.get("biddForm"))
    status = card.get("lotStatus")
    start, fin = _num(card.get("priceMin")), _num(card.get("priceFin"))
    etp = card.get("etpUrl")
    lot = {
        "id": card.get("id"),
        "url": LOT_URL.format(lot_id=card.get("id")),
        "notice": card.get("noticeNumber"),
        "status": status,
        "status_ru": STATUS_RU.get(status or "", status or "—"),
        "form": form,
        "notice_3918": form == "IPS",
        "kind": "аренда" if card.get("typeTransaction") == "rent" else "продажа",
        "price_start": start,
        "price_step": _num(card.get("priceStep")),
        "deposit": _num(card.get("deposit")),
        "price_fin": fin,
        "ratio": round(fin / start, 1) if fin and start else None,
        "area_m2": area_of(card),
        "bidd_end": _day(card.get("biddEndTime")),
        "auction_start": _day(card.get("auctionStartDate")),
        "published": _day(card.get("noticeFirstVersionPublicationDate")),
        "vri": vri_of(card),
        "address": card.get("estateAddress"),
        "annulled": bool(card.get("isAnnulled")),
        "stopped": bool(card.get("isStopped")),
        "appeals": bool(card.get("hasAppeals")),
        "etp_url": etp if isinstance(etp, str) and etp.startswith("https://") else None,
        "protocols": _protocols_of(card),
        "docs": _docs_of(card),
        **_result_of(card),
    }
    lot["outcome"], lot["meaning"] = outcome_of(lot)
    return lot


def gpzu_doc(own: dict[str, Any] | None) -> dict[str, Any] | None:
    """ГПЗУ среди вложений лотов участка (свежий лот первым): чат «МП Земля», сообщение 30 —
    «к торгам приложен ГПЗУ», а блок ГПЗУ в отчёте пишет «не приложен»."""
    for lot in (own or {}).get("lots") or []:
        for d in lot.get("docs") or []:
            if not d.get("gpzu"):
                continue
            fid = d.get("file_id") or (d.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            if fid:
                return {
                    "file_id": fid,
                    "url": d.get("url") or FILE_URL.format(file_id=fid),
                    "file": d.get("file"),
                    "size_mb": d.get("size_mb"),
                    "lot_id": lot.get("id"),
                    "lot_url": lot.get("url"),
                    "published": lot.get("published"),
                }
    return None


def history_note(lots: list[dict[str, Any]]) -> str | None:
    """Участок выставляли не один раз — на что смотреть."""
    if len(lots) < 2:
        return None
    n = len(lots)
    times = f"{n} раза" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else f"{n} раз"
    done = [lot for lot in lots[1:] if lot.get("status") == "SUCCEED"]
    if done:
        when = done[0].get("auction_start") or done[0].get("published") or "ранее"
        return (
            f"участок выставлялся на торги {times}: торги {when} уже состоялись, но его выставили "
            "снова — выясните по протоколам почему (например, победитель не заключил договор "
            "или договор расторгнут)"
        )
    return f"участок выставлялся на торги {times} — предыдущие торги без итога (см. статусы)"


def parcel_lot(
    cad_num: str,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    interval: float = REQUEST_INTERVAL_S,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Все извещения torgi.gov.ru по самому участку за три года: аукционы (EA) и извещения о
    предоставлении (IPS) — итог, цены, протоколы, документы. Поиск + до `MAX_OWN_LOTS`
    карточек через общий троттлинг; ошибки — в результате, исключений наружу нет.
    `lot` — самый свежий лот (для блока «Торги рядом»), `lots` — вся история."""
    now = now or datetime.now(UTC)
    region = region_of(cad_num)
    out: dict[str, Any] = {
        "schema": OWN_LOT_SCHEMA,
        "cad_num": cad_num,
        "found": False,
        "lot": None,
        "lots": [],
        "history_note": None,
        "months": LOT_MONTHS,
        "as_of": now.isoformat(timespec="seconds"),
        "search_url": SEARCH_URL,
        "error": None,
        "requests": 0,
    }
    if region is None:
        out["error"] = "кадастровый номер без региона"
        return out
    if client is None and not ruproxy.enabled():
        out["error"] = "RU-шлюз не настроен — torgi.gov.ru из-за рубежа закрыт"
        return out
    pub_from = (now - timedelta(days=LOT_MONTHS * 30)).date().isoformat()
    api = TorgiClient(client, interval=interval, sleep=sleep)
    try:
        page = api.search(
            region=region, text=cad_num, statuses=ALL_STATUSES, pub_from=pub_from, form=None
        )
        # поиск по номеру нечёткий: соседние участки того же извещения отбрасываем
        cards = [c for c in page.get("content") or [] if cad_num_of(c) == cad_num]
        lots = []
        for hit in cards[:MAX_OWN_LOTS]:  # сортировка по дате публикации, свежее первым
            card = hit
            try:
                card = {**hit, **api.lot(hit.get("id"))}
            except Exception as exc:  # noqa: BLE001 — карточка не критична: есть данные поиска
                log.info("torgi lot %s: %s", hit.get("id"), exc)
            lots.append(own_lot_row(card))
        if lots:
            out.update(found=True, lot=lots[0], lots=lots, history_note=history_note(lots))
            out["more"] = max(0, len(cards) - len(lots))
    except Exception as exc:  # noqa: BLE001
        log.warning("torgi parcel_lot %s: %s", cad_num, exc)
        out["error"] = f"torgi.gov.ru недоступен: {type(exc).__name__}"
    finally:
        out["requests"] = api.requests
        if client is None:
            api.close()
    return out


def liquidity_text(a: dict[str, Any]) -> str:
    """Одна фраза для отчёта: насколько охотно здесь берут землю на торгах.

    «Не состоялись» — два разных исхода (эксперт по торгам, 13.11.2026): подана одна заявка —
    договор заключают с единственным участником по начальной цене (ст. 39.12 ЗК), участок
    ушёл; заявок нет — участок остался. Отличить их можно только по карточке лота, поэтому
    разбивка есть в сводке из собранной базы региона. По одним статусам слабый спрос не
    объявляем: «не состоялись» не значит «никому не нужен»."""
    if a.get("error"):
        return a["error"]
    if not a.get("ea_total"):
        return "за год электронных аукционов по земле в этом кадастровом районе не было"
    liq = a.get("liquidity")
    if liq is None:
        return f"идут {a['active_total']} аукционов, завершённых за год ещё нет"
    closed = a["succeed_total"] + a["failed_total"]
    if a.get("single_total") is None:
        tail = (
            f"не состоялись {a['failed_total']} — по статусу не видно, ушёл ли участок "
            "единственному участнику по начальной цене (ст. 39.12 ЗК) или остался без заявок"
            if a["failed_total"]
            else "несостоявшихся нет"
        )
        verdict = (
            "спрос есть: большинство аукционов прошли с борьбой"
            if liq >= 0.6
            else "доля торгов с борьбой — нижняя граница спроса"
        )
        return (
            f"состоялись {a['succeed_total']} из {closed} завершённых ({int(liq * 100)} %), "
            f"{tail}; идут сейчас {a['active_total']} — {verdict}"
        )
    single, none, unknown = a["single_total"], a.get("none_total") or 0, a.get("unknown_total") or 0
    if liq >= 0.6:
        verdict = "спрос есть: большинство участков находит хозяина"
    elif liq >= 0.3:
        verdict = "спрос неровный: хозяина находит примерно треть–половина участков"
    else:
        verdict = "спрос слабый: большинство участков остаётся без договора"
    parts = [
        f"с борьбой {a['succeed_total']}",
        f"единственному участнику по начальной цене {single}",
        f"не ушли {none}",
    ]
    if unknown:
        parts.append(f"итог неизвестен {unknown}")
    return (
        f"хозяина нашли {int(round(liq * 100))} % из {closed} завершённых: {', '.join(parts)}; "
        f"идут сейчас {a['active_total']} — {verdict}"
    )
