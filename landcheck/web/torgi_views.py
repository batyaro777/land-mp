"""Страницы анализатора торгов (решение зума 09.11; ТЗ v3, п. 9).

Внутренний режим: видят администратор и аккаунты, которым он включил доступ («Торги» на
странице пользователей). Страницы:
  /tools/torgi-scan       — идущие лоты региона с вердиктом и доходностью;
  /tools/torgi-lot/{id}   — карточка лота: причины, сценарии, похожие завершённые торги;
  /tools/torgi-liquidity  — ликвидность по районам и кварталам + слой рынка;
  /tools/torgi-deals      — журнал своих сделок (факт: доходность, срок экспозиции).
"""

from __future__ import annotations

import logging
import re
import statistics
import tempfile
import threading
import time
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from landcheck import ads_api, rr_deals, ruproxy, torgi, torgi_calc, torgi_hot, torgi_scan
from landcheck.models import Purpose
from landcheck.models import Request as CheckRequest
from landcheck.regions import CADASTRAL_REGIONS
from landcheck.web import (
    ads_sync,
    jobs,
    rr_sync,
    torgi_hotjobs,
    torgi_places,
    torgi_sync,
    torgi_zones,
)
from landcheck.web import db as dbmod
from landcheck.web.app import render, require_user
from landcheck.web.config import settings
from landcheck.web.db import (
    Check,
    TorgiDeal,
    TorgiLot,
    TorgiMark,
    TorgiMarket,
    TorgiPick,
    TorgiSync,
    User,
    session,
)
from landcheck.web.serialize import request_to_dict

log = logging.getLogger(__name__)
router = APIRouter()
PAGE_ROWS = 150
MAX_CHECK_LOTS = 30
MAX_POOL_ROWS = 60  # торгов в таблице «из чего взята цена»
MAX_AD_ROWS = 15
# Объявлений нашлось меньше, чем нужно для оценки: цены в слое нет, но найденное показываем —
# иначе кнопка выглядит сломанной (прод, 12.11.2026: по лоту нашлось одно объявление из трёх
# нужных, страница сказала «не нашлось»).
ADS_FEW = "ads_few"
# Назначение лота → «что делать на участке» в проверке (цель — купить на торгах)
GROUP_USE = {
    "housing": Purpose.IZHS,
    "garden": Purpose.SNT,
    "agri": Purpose.FARMING,
    "commerce": Purpose.COMMERCE,
    "industry": Purpose.INDUSTRY,
    "recreation": Purpose.RECREATION,
}
PHASE_RU = {
    "queued": "в очереди",
    "active": "идущие лоты",
    "active_cards": "карточки идущих лотов: шаг, задаток, документы",
    "finished": "итоги завершившихся",
    "history": "завершённые за год",
    "history:SUCCEED": "завершённые за год: состоявшиеся",
    "history:FAILED": "завершённые за год: не состоявшиеся",
    "cards": "карточки завершённых: цены и заявки",
    "nspd": "кадастровая стоимость и точки на карте",
    "done": "готово",
}


def require_torgi(request: Request) -> User:
    user = require_user(request)
    if not user.can_torgi:
        raise HTTPException(404)
    return user


TorgiDep = Annotated[User, Depends(require_torgi)]


# --- разбор параметров ---------------------------------------------------------------------
def _num(value: str | None) -> float | None:
    v = torgi_calc._num(value, -1.0)
    return v if v >= 0 else None


def _region_code(value: str | None) -> str | None:
    value = (value or "").strip()
    # из поля с подсказками приходит «24 — Красноярский край»: берём код
    if (m := re.match(r"(\d{1,2})\b", value)) is not None:
        value = m.group(1)
    if value.isdigit() and value.zfill(2) in CADASTRAL_REGIONS:
        return value.zfill(2)
    for code, name in CADASTRAL_REGIONS.items():
        if name == value:
            return code
    # начало названия: «краснояр» → Красноярский край, если такой регион один
    low = value.lower().split(" · ")[0].strip()
    hits = [c for c, n in CADASTRAL_REGIONS.items() if low and n.lower().startswith(low)]
    return hits[0] if len(hits) == 1 else None


ALL_REGIONS = "all"  # «все собранные регионы» в поле региона списка лотов
ALL_REGIONS_LABEL = "все собранные регионы"


def _is_all(value: str | None) -> bool:
    """«all», «все», «все — все собранные регионы» из подсказки поля региона."""
    v = (value or "").strip().lower()
    return v == ALL_REGIONS or v.startswith("все")


def _synced_regions() -> list[str]:
    """Регионы, которые собирали (или собирают сейчас): вход общего списка лотов."""
    with session() as db:
        return sorted(db.scalars(select(TorgiSync.region)).all())


def _default_region(user: User) -> str | None:
    code = _region_code(user.region)
    with session() as db:
        synced = db.execute(
            select(TorgiSync.region, TorgiSync.finished_at).order_by(TorgiSync.finished_at.desc())
        ).all()
    known = [r for r, _ in synced]
    if code and code in known:
        return code
    return known[0] if known else code


def _rub(value: float | None) -> float | None:
    """Сумма из формы: «1,5» — миллионы, «500000» — рубли."""
    if not value:
        return None
    return value * 1_000_000 if value < 1000 else value


def _scan_params(q) -> torgi_scan.ScanParams:
    p = torgi_scan.ScanParams()
    if q.get("roi"):
        p.target_roi = max(torgi_calc._pct(q.get("roi"), p.target_roi), 0.0)
    if q.get("tax") not in (None, ""):
        p.tax = min(max(torgi_calc._pct(q.get("tax"), p.tax), 0.0), 0.99)
    p.extra = _num(q.get("extra")) or 0.0
    budget = _num(q.get("budget"))
    if budget:
        p.budget = _rub(budget)
    p.price_min = _rub(_num(q.get("pmin")))
    p.price_max = _rub(_num(q.get("pmax")))
    amin, amax = _num(q.get("amin")), _num(q.get("amax"))
    p.area_min_m2 = amin * torgi_scan.SOTKA if amin else None
    p.area_max_m2 = amax * torgi_scan.SOTKA if amax else None
    hold = _num(q.get("hold"))
    if hold:
        p.hold_months = int(min(max(hold, 1), 120))
    markup = _num(q.get("markup"))
    if markup:
        p.market_markup = min(max(markup, 0.1), 10.0)
    if q.get("kind") in torgi_scan.KIND_RU:
        p.kinds = (q.get("kind"),)
    groups = [g for g in q.getlist("group") if g in torgi_scan.GROUPS]
    p.groups = tuple(groups)
    if q.get("form") in ("notice", "all"):
        p.forms = q.get("form")
    p.forest = q.get("forest") == "1"
    p.place, _ = torgi_scan.place_of(q.get("d"))
    if q.get("assign") not in (None, ""):
        ratio = torgi_calc._pct(q.get("assign"), 0.0)
        p.assign_ratio = min(ratio, 2.0) if ratio > 0 else None
    return p


def _layer(region: str, *, few: bool = False) -> list[dict[str, Any]]:
    """Слой рынка региона: ручные записи и объявления из базы + сводка своих закрытых сделок.
    `few` — вместе с записями «объявлений нашлось мало» (цены в них нет, только найденное)."""
    prefix = f"{region}:"
    with session() as db:
        rows = [
            m.to_dict()
            for m in db.scalars(select(TorgiMarket).where(TorgiMarket.scope.like(prefix + "%")))
            if few or m.source != ADS_FEW
        ]
        deals = list(
            db.scalars(
                select(TorgiDeal).where(
                    TorgiDeal.district.like(prefix + "%"), TorgiDeal.sell_price.is_not(None)
                )
            )
        )
    by: dict[tuple[str, str, str], list[TorgiDeal]] = {}
    for d in deals:
        if d.district and d.area_m2 and d.sell_price:
            by.setdefault((d.district, d.kind, d.vri_group or "any"), []).append(d)
    for (district, kind, group), items in by.items():
        per_sotka = [d.sell_price / d.area_m2 * torgi_scan.SOTKA for d in items]
        days = [n for d in items if (n := _deal_days(d)) is not None]
        rows.append(
            {
                "scope": district,
                "kind": kind,
                "vri_group": group,
                "price_min": min(per_sotka),
                "price_mid": statistics.median(per_sotka),
                "price_max": max(per_sotka),
                "exposure_days": round(statistics.median(days)) if days else None,
                "liquid": None,
                "note": f"закрытых сделок: {len(items)}",
                "source": "deals",
            }
        )
    return rows


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _deal_days(d: TorgiDeal) -> int | None:
    start, end = _day(d.listed_at) or _day(d.bought_at), _day(d.sold_at)
    return (end - start).days if start and end and end >= start else None


# «Респ Карелия Питкярантский район», «обл. Калужская Барятинский район», «Калужская обл
# Жуковский р-н» → только район: регион в подписи района лишний
_REGION_WORD = r"(?:респ(?:ублика)?|обл(?:асть)?|край)\.?"
_REGION_PREFIX = re.compile(
    rf"^(?:{_REGION_WORD}\s+\S+|\S+\s+{_REGION_WORD})\s+(?=\S)", re.IGNORECASE
)


def district_names(lots: list[dict[str, Any]]) -> dict[str, str]:
    """Кадастровый район → как его называют в адресах лотов (самое частое название района)."""
    from landcheck.region import district_of, locality_from_address

    votes: dict[str, Counter[str]] = {}
    for lot in lots:
        d = lot.get("district")
        loc = locality_from_address(lot.get("address")) if d else None
        name = district_of(loc) if loc else None
        if d and name:
            votes.setdefault(d, Counter())[name] += 1
    return {d: _REGION_PREFIX.sub("", c.most_common(1)[0][0]) for d, c in votes.items()}


# --- кэш расчёта списка лотов по региону ---------------------------------------------------
# Общий список всех регионов на каждый запрос пересчитывал бы каждый регион: на проде 17.11 —
# 1,17 с на 4 регионах, с ростом числа регионов растёт линейно. Расчёт региона хранится, пока
# в регионе не записали лот, запись рынка или сделку (версия ниже), и не дольше 10 минут
# (срок «осталось дней» и прочее, что зависит от часов). Сырые лоты не храним — только итог
# расчёта и подписи районов: память не растёт с размером базы.
SCAN_CACHE_TTL_S = 600
SCAN_CACHE_MAX = 256
_scan_cache: dict[tuple[str, str], tuple[float, Any, dict[str, Any], dict[str, str]]] = {}
_data_version: dict[str, int] = {}
_cache_lock = threading.Lock()  # расчёты идут в потоках запросов, записи — и в потоке сбора


@event.listens_for(Session, "after_flush")
def _bump_versions(db: Session, _ctx: Any) -> None:
    """Записали лот, запись слоя рынка или сделку — расчёт по этому региону устарел."""
    for obj in (*db.new, *db.dirty, *db.deleted):
        if isinstance(obj, TorgiLot):
            region = obj.region or ""
        elif isinstance(obj, TorgiMarket):
            region = (obj.scope or "")[:2]
        elif isinstance(obj, TorgiDeal):
            region = (obj.district or obj.cad_num or "")[:2]
        else:
            continue
        with _cache_lock:
            _data_version[region] = _data_version.get(region, 0) + 1


def _local_fn(region: str, lots: list[dict[str, Any]]):
    """Оценка по месту самого лота: сделки Росреестра в его квартале или районе и объявления
    вокруг (что из этого загружено). None — нет ни того, ни другого."""
    ads_fn = ads_sync.near_fn(region, lots)
    deals_fn = rr_sync.market_fn()
    if ads_fn is None and deals_fn is None:
        return None

    def fn(lot: dict[str, Any]) -> dict[str, Any] | None:
        return torgi_scan.pick_local(
            deals_fn(lot) if deals_fn else None, ads_fn(lot) if ads_fn else None
        )

    return fn


def _region_scan(
    region: str, params: torgi_scan.ScanParams
) -> tuple[dict[str, Any], dict[str, str]]:
    """Расчёт списка лотов региона и подписи его районов (из кэша, если данные не менялись).
    Отдаём копию: страница сортирует и фильтрует строки на месте."""
    key = (f"{dbmod.generation}:{region}", repr(params))
    # до расчёта: запись во время расчёта его устарит; объявления ленты — своя версия
    version = (_data_version.get(region, 0), ads_sync.version(region), rr_sync.version())
    now = time.monotonic()
    hit = _scan_cache.get(key)
    if hit and hit[1] == version and now - hit[0] < SCAN_CACHE_TTL_S:
        result, names = hit[2], hit[3]
    else:
        lots = torgi_sync.lots_of(region, months=settings.torgi_scan_months)
        result = torgi_scan.scan(
            lots, params, layer=_layer(region), near_fn=_local_fn(region, lots)
        )
        names = district_names(lots)
        with _cache_lock:
            _scan_cache.pop(key, None)
            while len(_scan_cache) >= SCAN_CACHE_MAX:
                _scan_cache.pop(next(iter(_scan_cache)))  # самый давний расчёт
            _scan_cache[key] = (now, version, result, names)
    return {**result, "rows": list(result["rows"])}, names


def _scan_all(params: torgi_scan.ScanParams) -> tuple[dict[str, Any], dict[str, str]]:
    """Все собранные регионы одним списком (запрос Тони 17.11: видеть все ликвидные
    предложения сразу). Ликвидность и цены перепродажи считаются внутри своего региона,
    общий у списка только порядок: лучшие лоты всех регионов первыми."""
    merged: dict[str, Any] = {
        "rows": [],
        "counts": {},
        "total": 0,
        "stats": {},
        "forms": {},
        "hidden": {},
        "market_based": 0,
        "priced": 0,
    }
    names_all: dict[str, str] = {}
    regions = _synced_regions()
    for code in regions:
        result, names = _region_scan(code, params)
        merged["rows"] += result["rows"]
        for key in ("total", "market_based", "priced"):
            merged[key] += result[key]
        for group in ("counts", "forms", "hidden"):
            for k, v in result[group].items():
                merged[group][k] = merged[group].get(k, 0) + v
        names_all.update(names)
    for k in torgi_scan.VERDICTS:
        merged["counts"].setdefault(k, 0)
    for k in ("auction", "notice"):
        merged["forms"].setdefault(k, 0)
    merged["regions"] = regions
    merged["rows"].sort(key=lambda r: (r["s"]["rank"], -r["s"]["score"]))
    return merged, names_all


def _scan(region: str, q) -> tuple[dict[str, Any], torgi_scan.ScanParams, dict[str, str]]:
    params = _scan_params(q)
    if region == ALL_REGIONS:
        result, names = _scan_all(params)
    else:
        result, names = _region_scan(region, params)
    rows = result["rows"]
    _, parcel = torgi_scan.place_of(q.get("d"))
    verdict = q.get("verdict") or ""
    if verdict == "go":
        rows = [r for r in rows if r["s"]["verdict"] == "go"]
    elif verdict == "edge":
        rows = [r for r in rows if r["s"]["verdict"] in ("go", "edge")]
    sort = q.get("sort") or ""
    if sort == "deadline":
        rows.sort(key=lambda r: r.get("bidd_end") or "9999")
    elif sort == "price":
        rows.sort(key=lambda r: r.get("price_start") or float("inf"))
    elif sort == "price_desc":
        rows.sort(key=lambda r: -(r.get("price_start") or 0))
    elif sort == "auction":
        rows.sort(key=lambda r: r.get("auction_date") or "9999")
    elif sort == "roi":
        rows.sort(key=lambda r: -(r["s"].get("roi_start") or -9))
    if parcel:  # ввели номер участка — он первым в списке и подсвечен
        rows.sort(key=lambda r: r.get("cad_num") != parcel)
    result["rows"] = rows
    result["shown"] = len(rows)
    result["place"] = params.place
    result["parcel"] = parcel
    return result, params, names


def _query(q, **override: Any) -> str:
    """Текущие фильтры строкой запроса (для ссылок на выгрузки и сортировки)."""
    pairs: list[tuple[str, str]] = []
    for key in q.keys():
        if key in override or key == "page":
            continue
        for value in q.getlist(key):
            if value != "":
                pairs.append((key, value))
    pairs += [(k, str(v)) for k, v in override.items() if v not in (None, "")]
    seen: set[tuple[str, str]] = set()
    uniq = [p for p in pairs if not (p in seen or seen.add(p))]
    return urlencode(uniq)


def _regions_ctx() -> list[dict[str, Any]]:
    with session() as db:
        synced = {
            r.region: r
            for r in db.scalars(select(TorgiSync).order_by(TorgiSync.finished_at.desc()))
        }
    out = [
        {"code": code, "name": CADASTRAL_REGIONS.get(code, code), "synced": True} for code in synced
    ]
    out += [
        {"code": code, "name": name, "synced": False}
        for code, name in sorted(CADASTRAL_REGIONS.items(), key=lambda kv: kv[1])
        if code not in synced
    ]
    return out


# --- сканер ---------------------------------------------------------------------------------
@router.get("/tools/torgi-scan", response_class=HTMLResponse)
def torgi_scan_page(request: Request, user: TorgiDep, page: int = 1):
    q = request.query_params
    all_mode = _is_all(q.get("region"))
    region = ALL_REGIONS if all_mode else _region_code(q.get("region")) or _default_region(user)
    ctx: dict[str, Any] = {
        "regions": _regions_ctx(),
        "region": region,
        "region_name": ALL_REGIONS_LABEL if all_mode else CADASTRAL_REGIONS.get(region or "", ""),
        "all_mode": all_mode,
        "region_names": CADASTRAL_REGIONS,
        "q": q,
        "groups": torgi_scan.GROUPS,
        "sel_groups": q.getlist("group"),
        "kinds": torgi_scan.KIND_RU,
        "user": user,
        "phase_ru": PHASE_RU,
        "img_base": torgi.FILE_URL.format(file_id=""),
    }
    if not region:
        return render(request, "torgi_scan.html", sync=None, result=None, params=None, **ctx)
    result, params, names = _scan(region, q)
    place_label = None
    if params.place:
        level = "квартале" if params.place.count(":") >= 2 else "районе"
        dname = names.get(params.place[:5]) if level == "районе" else None
        place_label = f"{level} {params.place}" + (f" ({dname})" if dname else "")
    pages = max(1, (len(result["rows"]) + PAGE_ROWS - 1) // PAGE_ROWS)
    page = min(max(page, 1), pages)
    rows = result["rows"][(page - 1) * PAGE_ROWS : page * PAGE_ROWS]
    # обременения у «интересен» и «на грани» на этой странице (обещано в чате 17.11 к 19.11)
    torgi_zones.start_fill([r["id"] for r in rows if r["s"]["verdict"] in ("go", "edge")])
    return render(
        request,
        "torgi_scan.html",
        zone_label=torgi_zones.label,
        sync=None if all_mode else torgi_sync.state_of(region),
        result=result,
        rows=rows,
        params=params,
        names=names,
        place_label=place_label,
        points=[
            {
                "id": r["id"],
                "lat": r["lat"],
                "lon": r["lon"],
                "v": r["s"]["verdict"],
                "label": r["s"]["verdict_label"],
                "cad": r.get("cad_num"),
            }
            for r in rows
            if r.get("lat") is not None and r.get("lon") is not None
        ],
        page=page,
        pages=pages,
        qs=_query(q, region=region),
        qs_nosort=_query(q, region=region, sort=None),
        qs_form=_query(q, region=region, form=None),
        qs_noforest=_query(q, region=region, forest=None),
        bidd_short=torgi_scan.BIDD_TYPE_SHORT,
        today=datetime.now(UTC).date().isoformat(),
        **ctx,
    )


# --- горячие предложения (ТЗ v5, зум 18.11.2026) -------------------------------------------
HOT_KEYS = (
    "annual",
    "cap",
    "days",
    "kind",
    "region",
    "top",
    "profit",
    "extra",
    "assign",
    # утренняя сводка: включена, час отправки, пояс пользователя в часах от UTC
    "dg",
    "dh",
    "dz",
)
HOT_CAPS = (1_000_000, 2_000_000, 3_000_000, 5_000_000)
HOT_CHECK_ROWS = 50  # столько лотов показываем в списке «Проверить рынок руками»
RETURNING_ROWS = 30  # столько строк в списке «Вернутся на торги»


def _hot_criteria(q, user: User) -> tuple[dict[str, str], bool]:
    """Критерии «горячих» и признак, что их только что задали. Из адреса страницы, а если
    там их нет — те, с которыми пользователь смотрел список в прошлый раз."""
    if any(k in q for k in HOT_KEYS):
        return {k: (q.get(k) or "").strip() for k in HOT_KEYS if (q.get(k) or "").strip()}, True
    return {k: str(v) for k, v in (user.hot_criteria or {}).items() if k in HOT_KEYS}, False


def _hot_params(c: dict[str, str]) -> tuple[torgi_hot.HotParams, torgi_scan.ScanParams]:
    hp, sp = torgi_hot.HotParams(), torgi_scan.ScanParams()
    annual = _num(c.get("annual"))
    if annual is not None:
        hp.min_annual = min(annual, 10_000.0) / 100
    cap = _num(c.get("cap"))
    if cap is not None:
        hp.price_cap = _rub(cap)  # 0 — без лимита
    days = _num(c.get("days"))
    if days is not None:
        hp.min_days = int(min(days, 90))
    if c.get("kind") in torgi_scan.KIND_RU:
        hp.kinds = (c["kind"],)
    top = _num(c.get("top"))
    if top is not None and int(top) in torgi_hot.TOPS:
        hp.top = int(top)
    hp.min_profit = _rub(_num(c.get("profit")))
    sp.extra = _num(c.get("extra")) or 0.0
    if c.get("assign"):
        ratio = torgi_calc._pct(c.get("assign"), 0.0)
        sp.assign_ratio = min(ratio, 2.0) if ratio > 0 else None
    return hp, sp


def _skip_reasons() -> dict[str, Any]:
    """Почему лоты убирали из «Горячих» — по всем пользователям (видит администратор):
    по этим причинам правятся критерии отбора."""
    with session() as db:
        rows = db.execute(
            select(TorgiMark.reason, TorgiMark.created_at, User.name, TorgiLot.cad_num, TorgiLot.id)
            .join(User, User.id == TorgiMark.user_id)
            .join(TorgiLot, TorgiLot.id == TorgiMark.lot_id)
            .where(TorgiMark.mark == "skip")
            .order_by(TorgiMark.created_at.desc())
        ).all()
    counts = Counter(r[0] or "без причины" for r in rows)
    return {
        "total": len(rows),
        "counts": counts.most_common(),
        "recent": [
            {"reason": r[0] or "без причины", "at": r[1], "user": r[2], "cad": r[3], "lot": r[4]}
            for r in rows[:15]
        ],
    }


@router.get("/tools/torgi-hot", response_class=HTMLResponse)
def torgi_hot_page(request: Request, user: TorgiDep):
    """«Горячие предложения»: идущие лоты, которые проходят критерии «заходим», лучшие
    первыми, и отдельно — лоты, у которых цена рынка не прошла сверку."""
    q = request.query_params
    crit, fresh = _hot_criteria(q, user)
    if fresh and crit != (user.hot_criteria or {}):
        with session() as db:
            u = db.get(User, user.id)
            if u is not None:
                u.hot_criteria = crit
                db.commit()
    hp, sp = _hot_params(crit)
    region = _region_code(crit.get("region")) if not _is_all(crit.get("region")) else None
    result, names = _region_scan(region, sp) if region else _scan_all(sp)
    res = torgi_hot.select(result["rows"], sp, hp)
    # отметки пользователя: «не подходит» убирает лот из списков, «в работе» — отдельный блок
    marks = torgi_hotjobs.marks_of(user.id)
    show_skipped = q.get("skipped") == "1"

    def kept(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # лот «в работе» стоит в своём блоке, второй раз в предложениях его не показываем
        hide = ("work",) if show_skipped else ("work", "skip")
        return [i for i in items if marks.get(i["row"]["id"], ("", None))[0] not in hide]

    hot, weak, check_all = kept(res["hot"]), kept(res["weak"]), kept(res["check"])
    skipped = sum(
        1
        for k in ("hot", "weak", "check")
        for i in res[k]
        if marks.get(i["row"]["id"], ("", None))[0] == "skip"
    )
    shown = hot[: hp.top]
    check = check_all[:HOT_CHECK_ROWS]
    by_id = {r["id"]: r for r in result["rows"]}
    work_ids = [lot_id for lot_id, (mark, _) in marks.items() if mark == "work"]
    work = [torgi_hot.item_of(by_id[i], sp, hp) for i in work_ids if i in by_id]
    work.sort(key=lambda i: i["row"]["s"].get("days_left") or 0)
    # обременения и карточки — у всех, кто прошёл отбор: без них лот не поднимется в класс A,
    # а в сводке не будет задатка и шага
    candidates = hot + weak + check + work
    queued = torgi_zones.start_fill(
        [i["row"]["id"] for i in candidates if i["zones"][0] == "unknown"]
    )
    torgi_hotjobs.start_cards([i["row"]["id"] for i in candidates if not i["row"].get("card")])
    lot_qs = urlencode(
        {k: v for k, v in (("extra", crit.get("extra")), ("assign", crit.get("assign"))) if v}
    )
    digest_on, digest_hour, digest_tz = torgi_hotjobs.digest_settings(crit)
    # участки без заявок, которые прошли бы отбор и сейчас не на торгах: ждём повторных торгов
    scope = [region] if region else _synced_regions()
    backtest_start(scope)
    returning = torgi_hot.merge_backtests(list(backtest_ready(scope).values())).get("returning", [])
    return render(
        request,
        "torgi_hot.html",
        user=user,
        res=res,
        hot_n=len(hot),
        weak=weak,
        shown=shown,
        check=check,
        check_n=len(check_all),
        work=work,
        work_elsewhere=len(work_ids) - len(work),
        marks=marks,
        skipped=skipped,
        show_skipped=show_skipped,
        skip_reasons=torgi_hotjobs.SKIP_REASONS,
        skip_stats=_skip_reasons() if user.is_admin else None,
        hp=hp,
        sp=sp,
        crit=crit,
        caps=HOT_CAPS,
        tops=torgi_hot.TOPS,
        region=region or ALL_REGIONS,
        region_label=CADASTRAL_REGIONS.get(region or "", ALL_REGIONS_LABEL),
        synced=_synced_regions(),
        region_names=CADASTRAL_REGIONS,
        names=names,
        kinds=torgi_scan.KIND_RU,
        groups=torgi_scan.GROUPS,
        zones_queued=queued or torgi_zones.queued(),
        cards_queued=torgi_hotjobs.cards_queued(),
        lot_qs=lot_qs,
        img_base=torgi.FILE_URL.format(file_id=""),
        check_ratio=torgi_hot.START_RATIO,
        # подсказка «при пороге ниже пройдёт N» — только там, где лотов станет больше
        more_at=[
            (lvl, n) for lvl, n in res["relaxed"].items() if n > len(res["hot"]) + len(res["weak"])
        ],
        digest_on=digest_on,
        digest_hour=digest_hour,
        digest_tz=digest_tz,
        returning=returning[:RETURNING_ROWS],
        returning_n=len(returning),
        backtest_pending=backtest_pending(),
        can_telegram=bool(settings.telegram_bot_token and user.telegram_chat_id),
        can_email=bool(settings.smtp_url and user.email and user.notify_email),
    )


@router.post("/tools/torgi-mark/{lot_id}/{mark}")
async def torgi_mark(request: Request, user: TorgiDep, lot_id: str, mark: str):
    """Отметка на лоте: `work` — взял в работу, `skip` — не подходит (с причиной), `clear` —
    снять отметку. Возврат — на страницу, с которой нажали."""
    if mark not in ("work", "skip", "clear"):
        raise HTTPException(404)
    form = await request.form()
    reason = str(form.get(f"reason-{lot_id}") or "").strip()[:200] or None
    with session() as db:
        if db.get(TorgiLot, lot_id) is None:
            raise HTTPException(404)
        row = db.scalar(
            select(TorgiMark).where(TorgiMark.user_id == user.id, TorgiMark.lot_id == lot_id)
        )
        if mark == "clear":
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(
                TorgiMark(
                    user_id=user.id,
                    lot_id=lot_id,
                    mark=mark,
                    reason=reason if mark == "skip" else None,
                )
            )
        else:
            row.mark, row.reason = mark, reason if mark == "skip" else None
            row.created_at = datetime.now(UTC)
        db.commit()
    ref = urlparse(request.headers.get("referer") or "")
    back = ref.path + (f"?{ref.query}" if ref.query else "")
    return RedirectResponse(
        back if ref.path.startswith("/tools/torgi") else "/tools/torgi-hot", status_code=303
    )


# --- проверка модели на завершённых торгах (ТЗ v5, п. 8) -----------------------------------
BACKTEST_TTL_S = 6 * 3600  # история меняется медленно: пересчёт региона не чаще раза в 6 часов
# (база, регион) → (когда посчитан, итоги); со сменой базы прежние итоги не годятся
_backtest: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_backtest_lock = threading.Lock()
_backtest_running: list[str] = []  # регионы в очереди на расчёт, первый — считается сейчас


def _backtest_region(region: str) -> dict[str, Any]:
    """Сырые итоги проверки по региону при критериях по умолчанию (100 % годовых, 2 млн)."""
    lots = torgi_sync.lots_of(region, months=settings.torgi_scan_months)
    return torgi_hot.backtest(lots, layer=_layer(region), near_fn=_local_fn(region, lots))


def backtest_start(regions: list[str]) -> int:
    """Поставить в расчёт регионы, по которым проверки нет или она устарела. На боевом
    сервере считает фоновый поток (регион — до десятков секунд), в демо и тестах — сразу.
    Возвращает, сколько регионов поставлено."""
    now = time.monotonic()
    with _backtest_lock:
        stale = [
            r
            for r in regions
            if r not in _backtest_running
            and (
                (dbmod.generation, r) not in _backtest
                or now - _backtest[(dbmod.generation, r)][0] >= BACKTEST_TTL_S
            )
        ]
        if not stale:
            return 0
        idle = not _backtest_running
        _backtest_running.extend(stale)

    def job() -> None:
        while True:
            with _backtest_lock:
                if not _backtest_running:
                    return
                region = _backtest_running[0]
            try:
                raw = _backtest_region(region)
                with _backtest_lock:
                    _backtest[(dbmod.generation, region)] = (time.monotonic(), raw)
            except Exception as exc:  # noqa: BLE001 — расчёт региона не должен ронять сервер
                log.warning("torgi backtest %s: %s", region, exc)
            finally:
                with _backtest_lock:
                    if _backtest_running and _backtest_running[0] == region:
                        _backtest_running.pop(0)

    if settings.fixtures is not None or not ruproxy.enabled():
        job()
    elif idle:
        threading.Thread(target=job, name="torgi-backtest", daemon=True).start()
    return len(stale)


def backtest_ready(regions: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Готовые итоги проверки по регионам (все посчитанные либо только названные)."""
    with _backtest_lock:
        return {
            r: raw
            for (gen, r), (_, raw) in _backtest.items()
            if gen == dbmod.generation and (regions is None or r in regions)
        }


def backtest_pending() -> int:
    with _backtest_lock:
        return len(_backtest_running)


@router.get("/tools/torgi-backtest", response_class=HTMLResponse)
def torgi_backtest_page(request: Request, user: TorgiDep):
    """«Насколько точна модель»: завершённые аукционы продажи оценены так, как если бы они
    шли сейчас, и сравнены с тем, чем торги кончились."""
    regions = _synced_regions()
    backtest_start(regions)
    ready = backtest_ready(regions)
    rows = [
        {"code": r, "name": CADASTRAL_REGIONS.get(r, r), **torgi_hot.backtest_summary(raw)}
        for r, raw in sorted(ready.items())
    ]
    rows.sort(key=lambda x: -x["hot"])
    total = torgi_hot.backtest_summary(torgi_hot.merge_backtests(list(ready.values())))
    return render(
        request,
        "torgi_backtest.html",
        user=user,
        total=total,
        rows=rows,
        regions_n=len(regions),
        pending=backtest_pending(),
        band=int(torgi_hot.ACCURACY_BAND * 100),
        region="all",
    )


# --- сводка для инвестора и подборка (ТЗ v5, п. 4) -----------------------------------------
PICK_MAX = 12  # столько лотов помещается в сравнительную таблицу


def _lot_row(lot_id: str, sp: torgi_scan.ScanParams) -> tuple[dict[str, Any], dict[str, str]]:
    """Оценённый лот по номеру (как строка списка лотов) и подписи районов его региона.
    Идущий аукцион берётся из готового расчёта региона; завершённый, лесной или извещение —
    считается отдельно на той же статистике."""
    with session() as db:
        lot = db.get(TorgiLot, lot_id)
        region = lot.region if lot is not None else None
    if not region:
        raise HTTPException(404)
    result, names = _region_scan(region, sp)
    for row in result["rows"]:
        if row["id"] == lot_id:
            return row, names
    lots = torgi_sync.lots_of(region, months=settings.torgi_scan_months)
    found = next((x for x in lots if x["id"] == lot_id), None)
    if found is None:
        raise HTTPException(404)
    near_fn = _local_fn(region, lots)
    s = torgi_scan.score_lot(
        found,
        result["stats"],
        sp,
        layer=_layer(region),
        repeats=torgi_scan.repeats_of(lots),
        near=near_fn(found) if near_fn else None,
    )
    return {**found, "s": s}, names


def _invest_item(lot_id: str, crit: dict[str, str]) -> dict[str, Any]:
    """Всё для сводки по одному лоту при данных критериях расчёта."""
    hp, sp = _hot_params(crit)
    row, names = _lot_row(lot_id, sp)
    item = torgi_hot.item_of(row, sp, hp)
    item["district_name"] = names.get(row.get("district") or "", "")
    item["region_name"] = CADASTRAL_REGIONS.get(row.get("region") or "", "")
    item["active"] = row.get("status") in torgi_scan.ACTIVE
    item["lot_url"] = f"https://torgi.gov.ru/new/public/lots/lot/{row['id']}"
    item["stop_text"] = torgi_hot.STEP_RU.get(item["stop"] or "")
    return item


def _invest_ctx(crit: dict[str, str]) -> dict[str, Any]:
    hp, sp = _hot_params(crit)
    return {
        "hp": hp,
        "sp": sp,
        "kinds": torgi_scan.KIND_RU,
        "groups": torgi_scan.GROUPS,
        "img_base": torgi.FILE_URL.format(file_id=""),
        "made": datetime.now(UTC).date(),
        "zone_unknown": torgi_hot.ZONES_UNKNOWN,
    }


def _crit_of(user: User) -> dict[str, str]:
    return {k: str(v) for k, v in (user.hot_criteria or {}).items() if k in HOT_KEYS}


@router.get("/tools/torgi-lot/{lot_id}/invest", response_class=HTMLResponse)
def torgi_invest_page(request: Request, user: TorgiDep, lot_id: str):
    """Сводка по лоту для инвестора — так, как её увидит человек по ссылке без входа."""
    crit = _crit_of(user)
    item = _invest_item(lot_id, crit)
    if item["zones"][0] == "unknown":
        torgi_zones.start_fill([lot_id])
    return render(
        request,
        "torgi_invest.html",
        user=user,
        i=item,
        public=False,
        pick=None,
        **_invest_ctx(crit),
    )


@router.post("/tools/torgi-picks")
def torgi_pick_create(
    user: TorgiDep,
    lot: Annotated[list[str] | None, Form()] = None,
    title: Annotated[str, Form()] = "",
):
    """Подборка из отмеченных лотов: критерии расчёта берём те, с которыми автор смотрит
    «Горячие», и запоминаем вместе с подборкой."""
    ids = list(dict.fromkeys(i for i in (lot or []) if i))[:PICK_MAX]
    with session() as db:
        ids = [i for i in ids if db.get(TorgiLot, i) is not None]
        if not ids:
            return RedirectResponse("/tools/torgi-hot?nopick=1", status_code=303)
        pick = TorgiPick(
            user_id=user.id, title=title.strip()[:160] or None, lot_ids=ids, criteria=_crit_of(user)
        )
        db.add(pick)
        db.commit()
        pick_id = pick.id
    return RedirectResponse(f"/tools/torgi-picks/{pick_id}", status_code=303)


def _own_pick(db: Session, user: User, pick_id: int) -> TorgiPick:
    pick = db.get(TorgiPick, pick_id)
    if pick is None or (pick.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    return pick


def _pick_view(pick: TorgiPick) -> dict[str, Any]:
    crit = {k: str(v) for k, v in (pick.criteria or {}).items() if k in HOT_KEYS}
    items = []
    for lot_id in pick.lot_ids or []:
        try:
            items.append(_invest_item(lot_id, crit))
        except HTTPException:  # лот исчез из базы (старше года) — подборка остаётся
            continue
    return {
        "pick": {
            "id": pick.id,
            "token": pick.token,
            "title": pick.title,
            "created": pick.created_at,
            "url": f"{settings.base_url}/t/{pick.token}",
            "n": len(pick.lot_ids or []),
        },
        "items": items,
        **_invest_ctx(crit),
    }


@router.get("/tools/torgi-picks", response_class=HTMLResponse)
def torgi_picks_page(request: Request, user: TorgiDep):
    with session() as db:
        q = select(TorgiPick).where(TorgiPick.user_id == user.id).order_by(TorgiPick.id.desc())
        picks = [
            {
                "id": p.id,
                "title": p.title,
                "n": len(p.lot_ids or []),
                "created": p.created_at,
                "url": f"{settings.base_url}/t/{p.token}",
            }
            for p in db.scalars(q)
        ]
    return render(request, "torgi_picks.html", user=user, picks=picks)


@router.get("/tools/torgi-picks/{pick_id}", response_class=HTMLResponse)
def torgi_pick_page(request: Request, user: TorgiDep, pick_id: int):
    with session() as db:
        view = _pick_view(_own_pick(db, user, pick_id))
    torgi_zones.start_fill([i["row"]["id"] for i in view["items"] if i["zones"][0] == "unknown"])
    return render(request, "torgi_pick.html", user=user, public=False, **view)


@router.post("/tools/torgi-picks/{pick_id}/delete")
def torgi_pick_delete(user: TorgiDep, pick_id: int):
    with session() as db:
        db.delete(_own_pick(db, user, pick_id))
        db.commit()
    return RedirectResponse("/tools/torgi-picks", status_code=303)


def _public_pick(db: Session, token: str) -> TorgiPick:
    pick = db.scalar(select(TorgiPick).where(TorgiPick.token == token))
    if pick is None:
        raise HTTPException(404)
    return pick


@router.get("/t/{token}", response_class=HTMLResponse)
def torgi_pick_public(request: Request, token: str):
    """Подборка по ссылке без входа: один лот — сразу сводка, несколько — сравнение."""
    with session() as db:
        view = _pick_view(_public_pick(db, token))
    if len(view["items"]) == 1:
        return render(
            request, "torgi_invest.html", user=None, public=True, i=view["items"][0], **view
        )
    return render(request, "torgi_pick.html", user=None, public=True, **view)


@router.get("/t/{token}/{lot_id}", response_class=HTMLResponse)
def torgi_invest_public(request: Request, token: str, lot_id: str):
    with session() as db:
        pick = _public_pick(db, token)
        if lot_id not in (pick.lot_ids or []):
            raise HTTPException(404)
        view = _pick_view(pick)
    item = next((i for i in view["items"] if i["row"]["id"] == lot_id), None)
    if item is None:
        raise HTTPException(404)
    return render(request, "torgi_invest.html", user=None, public=True, i=item, **view)


@router.get("/tools/torgi-scan/status", response_class=HTMLResponse)
def torgi_scan_status(request: Request, user: TorgiDep, region: str):
    code = _region_code(region)
    if not code:
        raise HTTPException(404)
    return render(
        request,
        "_torgi_sync.html",
        sync=torgi_sync.state_of(code),
        region=code,
        phase_ru=PHASE_RU,
        user=user,
    )


@router.post("/tools/torgi-scan/sync")
def torgi_scan_sync(
    user: TorgiDep, region: Annotated[str, Form()], back: Annotated[str, Form()] = ""
):
    code = _region_code(region)
    if not code:
        raise HTTPException(404)
    torgi_sync.start_sync(code)
    return RedirectResponse(_back(back, f"/tools/torgi-scan?region={code}"), status_code=303)


@router.post("/tools/torgi-scan/auto")
def torgi_scan_auto(
    user: TorgiDep,
    region: Annotated[str, Form()],
    action: Annotated[str, Form()] = "on",
    back: Annotated[str, Form()] = "",
):
    code = _region_code(region)
    if not code:
        raise HTTPException(404)
    with session() as db:
        st = db.get(TorgiSync, code)
        if st is not None:
            st.auto = action != "off"
            db.commit()
    return RedirectResponse(_back(back, f"/tools/torgi-scan?region={code}"), status_code=303)


@router.post("/tools/torgi-ads/sync")
def torgi_ads_sync(
    user: TorgiDep,
    region: Annotated[str, Form()],
    full: Annotated[str, Form()] = "",
    back: Annotated[str, Form()] = "",
):
    """Загрузить объявления региона из ленты вне очереди; `full` — заново за весь срок.
    Только администратору: кнопка тратит запросы платной ленты."""
    code = _region_code(region)
    if not code or not user.is_admin:
        raise HTTPException(404)
    ads_sync.request(code, full=bool(full))
    return RedirectResponse(_back(back, f"/tools/torgi-liquidity?region={code}"), status_code=303)


def _back(back: str, default: str) -> str:
    """Возврат только на свои страницы инструментов (никаких внешних адресов)."""
    return back if back.startswith("/tools/torgi-") else default


@router.get("/tools/torgi-scan/export.{fmt}")
def torgi_scan_export(request: Request, user: TorgiDep, fmt: str):
    q = request.query_params
    region = ALL_REGIONS if _is_all(q.get("region")) else _region_code(q.get("region"))
    if not region or fmt not in ("xlsx", "kml"):
        raise HTTPException(404)
    result, _, _ = _scan(region, q)
    stamp = f"{datetime.now(UTC):%Y%m%d}"
    if fmt == "kml":
        title = (
            ALL_REGIONS_LABEL if region == ALL_REGIONS else CADASTRAL_REGIONS.get(region, region)
        )
        name = f"Торги {title} · {stamp}"
        return Response(
            torgi_scan.to_kml(result["rows"], name=name),
            media_type="application/vnd.google-earth.kml+xml",
            headers={"Content-Disposition": f'attachment; filename="torgi-{region}-{stamp}.kml"'},
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "torgi.xlsx"
        title = "Торги, все регионы" if region == ALL_REGIONS else f"Торги {region}"
        torgi_scan.to_xlsx(result["rows"], path, title=title)
        body = path.read_bytes()
    return Response(
        body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="torgi-{region}-{stamp}.xlsx"'},
    )


@router.post("/tools/torgi-scan/check")
async def torgi_scan_check(request: Request, user: TorgiDep):
    """Полная проверка отобранных лотов: обычная подборка с целью «на торгах» (права, зоны,
    ГПЗУ из лота, документы администрации) — всё, что уже умеет проверка участка."""
    form = await request.form()
    ids = [str(v) for v in form.getlist("lot")][:MAX_CHECK_LOTS]
    if not ids:
        raise HTTPException(400, "не выбрано ни одного лота")
    with session() as db:
        lots = [lot for lot in (db.get(TorgiLot, i) for i in ids) if lot and lot.cad_num]
        if not lots:
            raise HTTPException(400, "у выбранных лотов нет кадастровых номеров")
        groups = Counter(lot.vri_group for lot in lots)
        req = CheckRequest(purpose=Purpose.AUCTION)
        use = GROUP_USE.get(groups.most_common(1)[0][0] or "")
        if use and len(groups) == 1:
            req.use = use
        lines = []
        for lot in lots:
            parts = [lot.cad_num]
            if lot.kind == "sale" and lot.price_start:
                parts.append(f"{lot.price_start:.0f} руб")
            parts.append(torgi.LOT_URL.format(lot_id=lot.id))
            lines.append("; ".join(parts))
        region = lots[0].region or ""
        title = (
            f"Торги {CADASTRAL_REGIONS.get(region, region)}: {len(lots)} лот. · "
            f"{datetime.now(UTC):%d.%m.%Y}"
        )
        check = Check(
            user_id=user.id,
            title=title[:200],
            request=request_to_dict(req),
            input_text="\n".join(lines),
            total=len({lot.cad_num for lot in lots}),
        )
        db.add(check)
        db.commit()
        check_id = check.id
    jobs.enqueue(check_id)
    return RedirectResponse(f"/checks/{check_id}", status_code=303)


# --- карточка лота -------------------------------------------------------------------------
_IMG_ID_RE = re.compile(r"[0-9A-Za-z-]{8,64}")
_IMG_TYPES = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png"), (b"RIFF", "image/webp"))


def _img_type(data: bytes) -> str | None:
    return next((t for magic, t in _IMG_TYPES if data.startswith(magic)), None)


@router.get("/tools/torgi-img/{file_id}")
def torgi_img(user: TorgiDep, file_id: str):
    """Фото лота через сервер — запасной путь, когда браузер не достаёт file-store площадки
    (torgi.gov.ru из-за рубежа закрыт). Скачанное лежит на диске рядом с базой и больше не
    запрашивается; сайт пропускает запрос раз в 6 секунд, поэтому первый показ небыстрый."""
    if not _IMG_ID_RE.fullmatch(file_id):
        raise HTTPException(404)
    folder = settings.db_path.parent / "torgi-img"
    path = folder / file_id
    if not path.exists():
        if not ruproxy.enabled() or settings.fixtures is not None:
            raise HTTPException(404)
        api = torgi.TorgiClient(retry_delays=(), transport_retry=())
        try:
            data = api.file(file_id, max_mb=5)
        except Exception:  # noqa: BLE001 — нет фото, сайт не ответил: просто без картинки
            raise HTTPException(404) from None
        finally:
            api.close()
        if _img_type(data) is None:
            raise HTTPException(404)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    media = _img_type(path.read_bytes()[:8]) or "application/octet-stream"
    return FileResponse(
        path, media_type=media, headers={"Cache-Control": "private, max-age=604800"}
    )


@router.get("/tools/torgi-lot/{lot_id}", response_class=HTMLResponse)
def torgi_lot_page(request: Request, user: TorgiDep, lot_id: str, saved: str = ""):
    with session() as db:
        row = db.get(TorgiLot, lot_id)
        if row is None:
            raise HTTPException(404)
        lot = row.to_dict()
    region = lot.get("region") or ""
    q = request.query_params
    params = _scan_params(q)
    lots = torgi_sync.lots_of(region, months=settings.torgi_scan_months)
    stats = torgi_scan.index_by(lots)
    layer = _layer(region, few=True)
    near_fn = _local_fn(region, lots)
    s = torgi_scan.score_lot(
        lot,
        stats,
        params,
        layer=layer,
        repeats=torgi_scan.repeats_of(lots),
        near=near_fn(lot) if near_fn else None,
    )
    # объявления, из которых сложилась цена рядом с лотом, — показать, чтобы проверить глазами
    mk = s.get("market") or {}
    near_ads = ads_sync.rows_for(mk.get("ad_dist")) if mk.get("near") else []
    near_cheap = ads_sync.rows_for(mk.get("cheap_dist")) if mk.get("near") else []
    same_parcel = [
        x
        for x in lots
        if x.get("cad_num") and x["cad_num"] == lot.get("cad_num") and x["id"] != lot_id
    ]
    # Сначала — ровно те торги, из которых взяты цена перепродажи (продажа) или типичная
    # годовая плата (аренда): эксперт должен видеть, на чём стоит оценка. Если выборки нет —
    # просто похожие завершённые торги района.
    pool_ids = set(torgi_scan.auction_pool_ids(lot, stats))
    pool_total = len(pool_ids)
    if pool_ids:
        comparables = sorted(
            (x for x in lots if x["id"] in pool_ids and x.get("price_fin") and x.get("area_m2")),
            key=lambda x: x["price_fin"] / x["area_m2"],
        )[:MAX_POOL_ROWS]
    else:
        comparables = [
            x
            for x in lots
            if x["id"] != lot_id
            and x.get("district") == lot.get("district")
            and x.get("kind") == lot.get("kind")
            and x.get("status") == "SUCCEED"
            and x.get("price_fin")
        ]
        same_group = [x for x in comparables if x.get("vri_group") == lot.get("vri_group")]
        if len(same_group) >= torgi_scan.MIN_SAMPLE:
            comparables = same_group
        near = [x for x in comparables if x.get("quarter") == lot.get("quarter")]
        comparables = (near + [x for x in comparables if x not in near])[:12]
    calc = {
        "start": lot.get("price_start"),
        "step": lot.get("price_step"),
        "area": lot.get("area_m2"),
        "kind": torgi_scan.KIND_RU.get(lot.get("kind") or "", "продажа"),
        "lot_url": torgi.LOT_URL.format(lot_id=lot_id),
        "cad": lot.get("cad_num"),
        "roi": round(params.target_roi * 100),
        "tax": round(params.tax * 100, 1),
        "extra": params.extra or None,
        "smin": s["sale"]["min"],
        "smid": s["sale"]["mid"],
        "smax": s["sale"]["max"],
    }
    calc_qs = urlencode({k: (int(v) if isinstance(v, float) else v) for k, v in calc.items() if v})
    names = district_names(lots)
    return render(
        request,
        "torgi_lot.html",
        lot=lot,
        s=s,
        near_ads=near_ads,
        near_cheap=near_cheap,
        rr_source=rr_deals.SOURCE_URL,
        params=params,
        lot_url=torgi.LOT_URL.format(lot_id=lot_id),
        same_parcel=sorted(same_parcel, key=lambda x: x.get("published") or "", reverse=True),
        comparables=comparables,
        pool_total=pool_total,
        district_stats=stats.get(lot.get("district") or ""),
        quarter_stats=stats.get(lot.get("quarter") or ""),
        district_name=names.get(lot.get("district") or ""),
        layer=[m for m in layer if m["scope"] in (lot.get("district"), lot.get("quarter"))],
        calc_url=f"/tools/torgi-calc?{calc_qs}",
        groups=torgi_scan.GROUPS,
        kinds=torgi_scan.KIND_RU,
        status_ru=torgi.STATUS_RU,
        qs=_query(q, saved=None, region=ALL_REGIONS if _is_all(q.get("region")) else region),
        q=q,
        img_base=torgi.FILE_URL.format(file_id=""),
        saved=saved,
        user=user,
    )


def _ad_rows(ads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Объявления для показа в карточке: только то, что нужно глазам, и только веб-ссылки."""
    out = []
    for a in ads[:MAX_AD_ROWS]:
        url = str(a.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        out.append(
            {
                "title": str(a.get("title") or "")[:120],
                "url": url[:500],
                "host": str(a.get("host") or "")[:60],
                "price_rub": a.get("price_rub"),
                "area_m2": a.get("area_m2"),
                "pps": a.get("pps"),
                "scope": a.get("scope"),
            }
        )
    return out


@router.post("/tools/torgi-lot/{lot_id}/ads")
def torgi_lot_ads(request: Request, user: TorgiDep, lot_id: str):
    """Цены предложений рядом — через поиск объявлений (как блок «Оценка стоимости» в отчёте):
    медиана и 25–75 % за сотку записываются в слой рынка района с источником «объявления»,
    сами объявления со ссылками — рядом, чтобы их можно было проверить глазами."""
    from landcheck.market_ads import MIN_N, collect_ads, district_fallback
    from landcheck.region import locality_from_address, short_place

    back_qs = _query(request.query_params, saved=None)

    def back(code: str) -> RedirectResponse:
        tail = f"&{back_qs}" if back_qs else ""
        return RedirectResponse(f"/tools/torgi-lot/{lot_id}?saved={code}{tail}", status_code=303)

    with session() as db:
        lot = db.get(TorgiLot, lot_id)
        if lot is None:
            raise HTTPException(404)
        address, area, group, district = lot.address, lot.area_m2, lot.vri_group, lot.district
        category, vri = lot.category, lot.vri
    locality = locality_from_address(address)
    if not locality or not district:
        return back("noloc")
    from landcheck.web.content import land_kind

    ads = collect_ads(
        short_place(locality),
        land_kind(category, vri),
        area_m2=area,
        fallback=district_fallback(locality),
    )
    where = ads.get("scope_label") or short_place(locality)
    found = _ad_rows(ads.get("ads") or [])
    details = {
        "ads": found,
        "where": where,
        "lot_id": lot_id,
        "fetched_at": ads.get("fetched_at"),
        # отсеянные: аренда под видом продажи и подозрительно дешёвые — показать, не считать
        "leasehold": _ad_rows(ads.get("leasehold") or []),
        "cheap": _ad_rows(ads.get("cheap") or []),
    }
    key = {"scope": district, "kind": "sale", "vri_group": group or "any"}
    if ads.get("n", 0) < MIN_N or not ads.get("median"):
        if not found:
            return back("noads")
        _save_market(
            **key,
            price_min=None,
            price_mid=None,
            price_max=None,
            exposure_days=None,
            liquid=None,
            note=f"найдено {len(found)} объявл., {where} — для оценки мало, нужно от {MIN_N}",
            source=ADS_FEW,
            user_id=user.id,
            details=details,
        )
        return back("fewads")
    note = f"{ads['n']} объявл. с ценой и площадью, {where}; цены предложений, не сделок"
    _save_market(
        **key,
        price_min=ads.get("p25"),
        price_mid=ads["median"],
        price_max=ads.get("p75"),
        exposure_days=None,
        liquid=None,
        note=note,
        source="ads",
        user_id=user.id,
        details=details,
    )
    with session() as db:  # прежняя запись «нашлось мало» по этому месту больше не нужна
        stale = db.scalar(
            select(TorgiMarket).where(
                TorgiMarket.scope == key["scope"],
                TorgiMarket.kind == key["kind"],
                TorgiMarket.vri_group == key["vri_group"],
                TorgiMarket.source == ADS_FEW,
            )
        )
        if stale is not None:
            db.delete(stale)
            db.commit()
    return back("ads")


# --- ликвидность и слой рынка ----------------------------------------------------------------
@router.get("/tools/torgi-liquidity", response_class=HTMLResponse)
def torgi_liquidity_page(request: Request, user: TorgiDep):
    q = request.query_params
    region = _region_code(q.get("region")) or _default_region(user)
    level = "quarter" if q.get("level") == "quarter" else "district"
    group = q.get("group") if q.get("group") in torgi_scan.GROUPS else None
    place = (q.get("d") or "").strip()
    ctx: dict[str, Any] = {
        "regions": _regions_ctx(),
        "region": region,
        "region_name": CADASTRAL_REGIONS.get(region or "", ""),
        "level": level,
        "group": group,
        "place": place,
        "groups": torgi_scan.GROUPS,
        "kinds": torgi_scan.KIND_RU,
        "fail_ru": torgi.FAIL_KIND_RU,
        "user": user,
        "phase_ru": PHASE_RU,
        "q": q,
    }
    bids = int(min(max(_num(q.get("bids")) or 0, 0), MAX_BIDS))
    ctx["bids"] = bids
    if not region:
        return render(request, "torgi_liquidity.html", rows=[], sync=None, layer=[], **ctx)
    lots = torgi_sync.lots_of(region, months=settings.torgi_scan_months)
    if place:
        lots = [x for x in lots if (x.get("cad_num") or "").startswith(place)]
    rows = torgi_scan.liquidity(lots, level=level, group=group)
    hidden_bids = 0
    if bids:  # чат 17.11 №72: «ползунок — выбрать минимальное количество заявок»
        kept = [r for r in rows if (r.get("applied_median") or 0) >= bids]
        hidden_bids = len(rows) - len(kept)
        rows = kept
    rows = rows[:400]
    layer = _layer(region)
    by_scope: dict[str, list[dict[str, Any]]] = {}
    for m in layer:
        by_scope.setdefault(m["scope"], []).append(m)
    places, points, pending = _map_data(rows, lots, level=level, group=group)
    return render(
        request,
        "torgi_liquidity.html",
        rows=rows,
        hidden_bids=hidden_bids,
        places=places,
        points=points,
        places_pending=pending,
        total=torgi_scan.stats_of(lots),
        names=district_names(lots),
        sync=torgi_sync.state_of(region),
        ads=ads_sync.status(region),
        ads_class=ads_api.LAND_CLASSES,
        rr=rr_sync.status(region),
        rr_source=rr_deals.SOURCE_URL,
        layer=sorted(layer, key=lambda m: (m["scope"], m["kind"], m.get("vri_group") or "")),
        by_scope=by_scope,
        sotka=torgi_scan.SOTKA,
        **ctx,
    )


MAX_BIDS = 10
MAX_MAP_POINTS = 1500


def _map_data(
    rows: list[dict[str, Any]],
    lots: list[dict[str, Any]],
    *,
    level: str,
    group: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Карта ликвидности (зум 13.11, 36:24; чат 17.11 №75): районы или кварталы цветом по
    индексу, точки идущих лотов. Контуры — из НСПД через базу; пока контура нет, место
    рисуется кружком в середине его идущих лотов. Возвращает места, точки и сколько контуров
    ещё догружается."""
    keys = [r["key"] for r in rows]
    have = torgi_places.shapes(keys)
    torgi_places.start_fill(keys)
    # ещё не загружены (в очереди или грузятся сейчас); без НСПД в демо-режиме — 0
    pending = len(torgi_places.missing(keys)) if settings.fixtures is None else 0
    spots: dict[str, list[tuple[float, float]]] = {}
    points = []
    for lot in lots:
        if lot.get("lat") is None or lot.get("lon") is None:
            continue
        if group and lot.get("vri_group") != group:
            continue
        if lot.get("status") not in torgi.ACTIVE or torgi_scan.is_notice(lot):
            continue
        key = lot.get(level)
        if key:
            spots.setdefault(key, []).append((lot["lat"], lot["lon"]))
        if len(points) < MAX_MAP_POINTS:
            points.append(
                {
                    "id": lot["id"],
                    "lat": lot["lat"],
                    "lon": lot["lon"],
                    "cad": lot.get("cad_num"),
                    "kind": torgi_scan.KIND_RU.get(lot.get("kind") or "", ""),
                    "price": lot.get("price_start"),
                }
            )
    places = []
    for r in rows:
        shape = have.get(r["key"]) or {}
        lat, lon = shape.get("lat"), shape.get("lon")
        if lat is None and spots.get(r["key"]):
            pts = spots[r["key"]]
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
        if lat is None and not shape.get("geometry"):
            continue
        places.append(
            {
                "key": r["key"],
                "index": r.get("index"),
                "label": r.get("label"),
                "closed": r.get("closed"),
                "sold": r.get("sold"),
                "single": r.get("single"),
                "none": r.get("none"),
                "active": r.get("active"),
                "applied": r.get("applied_median"),
                "sell": r.get("sell_through"),
                "geometry": shape.get("geometry"),
                "lat": lat,
                "lon": lon,
            }
        )
    return places, points, pending


# --- завершённые торги (чат 17.11 №73–77: «открыть, которые не ушли», «что продано, кому») ---
HISTORY_OUTCOMES = {
    "closed": ("sold", "single", "none", "failed"),
    "taken": ("sold", "single"),
    "sold": ("sold",),
    "single": ("single",),
    "none": ("none",),
    "failed": ("failed",),
    "canceled": ("canceled",),  # отменены организатором (чат 17.11 №76) — не входят в «все»
}
HISTORY_LABELS = {
    "closed": "все завершённые",
    "taken": "нашли хозяина",
    "sold": "продано с борьбой",
    "single": "единственному участнику",
    "none": "не ушли",
    "failed": "итог ещё не прочитан",
    "canceled": "отменены организатором",
}
OUTCOME_RU = {
    "sold": "продан с борьбой",
    "single": "единственному участнику",
    "none": "не ушёл",
    "failed": "не состоялись, итог не прочитан",
    "canceled": "отменён организатором",
}
PRICEY_RATIO = 1.3  # старт за сотку выше медианы проданных в месте — «цена, похоже, завышена»
WEAK_SELL = 0.4  # в районе находят хозяина меньше 40 % участков — «спрос слабый»
HISTORY_ROWS = 200


def history_row(lot: dict[str, Any], stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Строка списка завершённых торгов: исход, рост, заявки, кто взял и почему, вероятно,
    не ушёл (чат 17.11 №76: цена выше рынка, нет спроса, ФАС, отмена, обременения)."""
    outcome = torgi_scan._outcome(lot)
    kind = lot.get("kind") or "sale"
    area = lot.get("area_m2")
    start, fin = lot.get("price_start"), lot.get("price_fin")
    start_m2 = start / area if start and area else None
    growth = fin / start if fin and start and outcome == "sold" else None
    signals: list[str] = []
    median, where = None, None
    for label, key in (("квартале", lot.get("quarter")), ("районе", lot.get("district"))):
        ks = ((stats.get(key or "") or {}).get(kind)) or {}
        if (ks.get("n") or 0) >= torgi_scan.MIN_SAMPLE and ks.get("median_m2"):
            median, where = ks["median_m2"], label
            break
    ratio = start_m2 / median if start_m2 and median else None
    if ratio and ratio >= PRICEY_RATIO and outcome in ("none", "single", "failed"):
        signals.append(f"старт ×{ratio:.1f} к медиане проданных в {where} — цена, похоже, завышена")
    district = stats.get(lot.get("district") or "") or {}
    sell = district.get("sell_through")
    if outcome in ("none", "failed") and sell is not None and sell < WEAK_SELL:
        signals.append(f"в районе находят хозяина {sell:.0%} участков — спрос слабый")
    if lot.get("appeals"):
        signals.append("по торгам были жалобы (ФАС)")
    if lot.get("annulled"):
        signals.append("торги аннулированы")
    if lot.get("stopped"):
        signals.append("торги приостанавливали")
    if lot.get("zones_state") == "ok" and lot.get("zones"):
        zones = lot["zones"]
        more = f" и ещё {len(zones) - 3}" if len(zones) > 3 else ""
        signals.append("обременения: " + "; ".join(zones[:3]) + more)
    reason = torgi.FAIL_KIND_RU.get(torgi_scan._fail_kind(lot) or "")
    return {
        **lot,
        "outcome": outcome,
        "outcome_ru": OUTCOME_RU.get(outcome, outcome),
        "reason": reason,
        "start_sotka": start_m2 * torgi_scan.SOTKA if start_m2 else None,
        "fin_sotka": fin / area * torgi_scan.SOTKA if fin and area else None,
        "median_sotka": median * torgi_scan.SOTKA if median else None,
        "ratio": ratio,
        "growth": growth,
        "signals": signals,
    }


@router.get("/tools/torgi-history", response_class=HTMLResponse)
def torgi_history_page(request: Request, user: TorgiDep, page: int = 1):
    q = request.query_params
    region = _region_code(q.get("region")) or _default_region(user)
    place = (q.get("d") or "").strip()
    outcome = q.get("outcome") if q.get("outcome") in HISTORY_OUTCOMES else "closed"
    kind = q.get("kind") if q.get("kind") in torgi_scan.KIND_RU else None
    group = q.get("group") if q.get("group") in torgi_scan.GROUPS else None
    # вид торгов: по умолчанию земельные по ЗК — как в ликвидности; остальные отдельно
    bt = q.get("bt") if q.get("bt") == "other" else "zk"
    ctx: dict[str, Any] = {
        "bt": bt,
        "bidd": torgi_scan.bidd_label,
        "regions": _regions_ctx(),
        "region": region,
        "region_name": CADASTRAL_REGIONS.get(region or "", ""),
        "place": place,
        "outcome": outcome,
        "outcomes": HISTORY_LABELS,
        "kind": kind,
        "group": group,
        "groups": torgi_scan.GROUPS,
        "kinds": torgi_scan.KIND_RU,
        "user": user,
        "q": q,
    }
    if not region:
        return render(request, "torgi_history.html", rows=[], counts={}, **ctx)
    lots = torgi_sync.lots_of(region, months=settings.torgi_scan_months)
    stats = torgi_scan.index_by(lots)
    wanted = HISTORY_OUTCOMES[outcome]
    rows, counts = [], {key: 0 for key in HISTORY_LABELS}
    other = 0  # завершённые торги другого вида (должники, лесные, изъятые) в этом месте
    for lot in lots:
        if place and not torgi_scan.in_place(lot, place):
            continue
        if (kind and lot.get("kind") != kind) or (group and lot.get("vri_group") != group):
            continue
        o = torgi_scan._outcome(lot)
        if o not in HISTORY_OUTCOMES["closed"] and o != "canceled":
            continue
        if torgi_scan.in_market(lot) != (bt == "zk"):
            other += o in HISTORY_OUTCOMES["closed"]
            continue
        for key, members in HISTORY_OUTCOMES.items():
            counts[key] += o in members
        if o in wanted:
            rows.append(history_row(lot, stats))
    rows.sort(key=lambda r: r.get("auction_date") or r.get("published") or "", reverse=True)
    pages = max(1, (len(rows) + HISTORY_ROWS - 1) // HISTORY_ROWS)
    page = min(max(page, 1), pages)
    shown = rows[(page - 1) * HISTORY_ROWS : page * HISTORY_ROWS]
    # обременения — у тех, кто не нашёл хозяина (чат 17.11 №76): фоном, страница не ждёт НСПД
    unsold = [r["id"] for r in shown if r["outcome"] in ("none", "failed")]
    torgi_zones.start_fill(unsold)
    return render(
        request,
        "torgi_history.html",
        rows=shown,
        zones_pending=len(torgi_zones.missing(unsold)),
        total=len(rows),
        counts=counts,
        names=district_names(lots),
        page=page,
        pages=pages,
        qs=_query(q, region=region),
        qs_outcome=_query(q, region=region, outcome=None),
        qs_bt=_query(q, region=region, bt=None, page=None),
        other=other,
        **ctx,
    )


def _save_market(
    *,
    scope: str,
    kind: str,
    vri_group: str,
    price_min: float | None,
    price_mid: float | None,
    price_max: float | None,
    exposure_days: int | None,
    liquid: str | None,
    note: str | None,
    source: str,
    user_id: int | None,
    details: dict[str, Any] | None = None,
) -> None:
    with session() as db:
        row = db.scalar(
            select(TorgiMarket).where(
                TorgiMarket.scope == scope,
                TorgiMarket.kind == kind,
                TorgiMarket.vri_group == vri_group,
                TorgiMarket.source == source,
            )
        )
        if row is None:
            row = TorgiMarket(scope=scope, kind=kind, vri_group=vri_group, source=source)
            db.add(row)
        row.price_min, row.price_mid, row.price_max = price_min, price_mid, price_max
        row.exposure_days = exposure_days
        row.liquid = liquid
        row.note = (note or "").strip()[:500] or None
        row.details = details
        row.user_id = user_id
        row.updated_at = datetime.now(UTC)
        db.commit()


@router.post("/tools/torgi-market")
def torgi_market_save(
    user: TorgiDep,
    scope: Annotated[str, Form()],
    kind: Annotated[str, Form()] = "sale",
    vri_group: Annotated[str, Form()] = "any",
    price_min: Annotated[str, Form()] = "",
    price_mid: Annotated[str, Form()] = "",
    price_max: Annotated[str, Form()] = "",
    exposure_days: Annotated[str, Form()] = "",
    liquid: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    back: Annotated[str, Form()] = "",
):
    """Ручная запись слоя рынка: цена перепродажи за сотку по району или кварталу."""
    scope = scope.strip()
    parts = scope.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise HTTPException(400, "район — как 40:13, квартал — как 40:13:120101")
    scope = ":".join([parts[0].zfill(2), parts[1].zfill(2), *parts[2:]])
    mid = _num(price_mid)
    lo, hi = _num(price_min), _num(price_max)
    if not mid and lo and hi:
        mid = (lo + hi) / 2
    if not mid and liquid not in ("liquid", "illiquid"):
        raise HTTPException(400, "нужна цена за сотку или отметка ликвид / неликвид")
    days = _num(exposure_days)
    _save_market(
        scope=scope,
        kind=kind if kind in torgi_scan.KIND_RU else "sale",
        vri_group=vri_group if vri_group in torgi_scan.GROUPS else "any",
        price_min=lo,
        price_mid=mid,
        price_max=hi,
        exposure_days=int(days) if days else None,
        liquid=liquid if liquid in ("liquid", "illiquid") else None,
        note=note,
        source="manual",
        user_id=user.id,
    )
    default = f"/tools/torgi-liquidity?region={scope[:2]}"
    return RedirectResponse(_back(back, default), status_code=303)


@router.post("/tools/torgi-market/{market_id}/delete")
def torgi_market_delete(user: TorgiDep, market_id: int, back: Annotated[str, Form()] = ""):
    region = ""
    with session() as db:
        row = db.get(TorgiMarket, market_id)
        if row is not None:
            region = row.scope[:2]
            db.delete(row)
            db.commit()
    return RedirectResponse(_back(back, f"/tools/torgi-liquidity?region={region}"), status_code=303)


# --- журнал своих сделок ----------------------------------------------------------------------
def deal_view(d: TorgiDeal, today: date | None = None) -> dict[str, Any]:
    """Сделка с расчётом: вложено, прибыль и доходность по факту (или по цене выставления),
    сколько дней в продаже."""
    today = today or datetime.now(UTC).date()
    cost = (d.buy_price or 0) + (d.extra_costs or 0)
    out_price = d.sell_price or d.list_price
    profit = out_price - cost if out_price and cost else None
    listed = _day(d.listed_at) or _day(d.bought_at)
    sold = _day(d.sold_at)
    days = (sold - listed).days if listed and sold else ((today - listed).days if listed else None)
    held_from = _day(d.bought_at)
    held = ((sold or today) - held_from).days if held_from else None
    roi = round(profit / cost, 3) if profit is not None and cost else None
    annual = None
    if roi is not None and held and held > 0 and roi > -1 and d.sell_price:
        annual = round((1 + roi) ** (365 / held) - 1, 3)
    return {
        "id": d.id,
        "cad_num": d.cad_num,
        "district": d.district,
        "kind": d.kind,
        "vri_group": d.vri_group,
        "area_m2": d.area_m2,
        "lot_url": d.lot_url,
        "bought_at": d.bought_at,
        "buy_price": d.buy_price,
        "extra_costs": d.extra_costs,
        "listed_at": d.listed_at,
        "list_price": d.list_price,
        "sold_at": d.sold_at,
        "sell_price": d.sell_price,
        "note": d.note,
        "cost": cost or None,
        "profit": profit,
        "roi": roi,
        "annual": annual,
        "days": days,
        "closed": bool(d.sell_price),
    }


@router.get("/tools/torgi-deals", response_class=HTMLResponse)
def torgi_deals_page(request: Request, user: TorgiDep, lot: str = "", edit: int = 0):
    with session() as db:
        q = select(TorgiDeal).order_by(TorgiDeal.created_at.desc())
        if not user.is_admin:
            q = q.where(TorgiDeal.user_id == user.id)
        deals = [deal_view(d) for d in db.scalars(q)]
        prefill = None
        if edit:
            row = next((d for d in deals if d["id"] == edit), None)
            if row is not None:
                prefill = {
                    **row,
                    "deal_id": row["id"],
                    "area": round(row["area_m2"] / torgi_scan.SOTKA, 2) if row["area_m2"] else None,
                }
        if lot:
            src = db.get(TorgiLot, lot)
            if src is not None:
                prefill = {
                    "cad_num": src.cad_num,
                    "kind": src.kind,
                    "vri_group": src.vri_group,
                    "area": round(src.area_m2 / torgi_scan.SOTKA, 2) if src.area_m2 else None,
                    "lot_url": torgi.LOT_URL.format(lot_id=src.id),
                    "buy_price": src.price_fin or src.price_start,
                    "bought_at": src.auction_date,
                }
    closed = [d for d in deals if d["closed"] and d["roi"] is not None]
    summary = {
        "n": len(deals),
        "closed": len(closed),
        "roi_median": round(statistics.median([d["roi"] for d in closed]), 3) if closed else None,
        "days_median": round(
            statistics.median([d["days"] for d in closed if d["days"] is not None])
        )
        if any(d["days"] is not None for d in closed)
        else None,
        "profit": sum(d["profit"] for d in closed if d["profit"] is not None) if closed else None,
        "invested_open": sum(d["cost"] or 0 for d in deals if not d["closed"]),
    }
    return render(
        request,
        "torgi_deals.html",
        deals=deals,
        summary=summary,
        prefill=prefill or {},
        groups=torgi_scan.GROUPS,
        kinds=torgi_scan.KIND_RU,
        user=user,
    )


def _iso_day(value: str) -> str | None:
    """Дата из формы: «2026-11-10» или «10.11.2026» → ISO; мусор — None."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@router.post("/tools/torgi-deals")
def torgi_deal_save(
    user: TorgiDep,
    cad_num: Annotated[str, Form()],
    deal_id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "sale",
    vri_group: Annotated[str, Form()] = "any",
    area: Annotated[str, Form()] = "",
    lot_url: Annotated[str, Form()] = "",
    bought_at: Annotated[str, Form()] = "",
    buy_price: Annotated[str, Form()] = "",
    extra_costs: Annotated[str, Form()] = "",
    listed_at: Annotated[str, Form()] = "",
    list_price: Annotated[str, Form()] = "",
    sold_at: Annotated[str, Form()] = "",
    sell_price: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    from landcheck.inputs import normalize_cad

    cad = normalize_cad(cad_num)
    if not cad:
        raise HTTPException(400, "кадастровый номер — как 40:13:120101:22")
    area_sotka = _num(area)
    with session() as db:
        d = db.get(TorgiDeal, int(deal_id)) if deal_id.isdigit() else None
        if d is not None and d.user_id != user.id and not user.is_admin:
            raise HTTPException(404)
        if d is None:
            d = TorgiDeal(user_id=user.id, cad_num=cad)
            db.add(d)
        d.cad_num = cad
        d.district = torgi.district_of(cad)
        d.kind = kind if kind in torgi_scan.KIND_RU else "sale"
        d.vri_group = vri_group if vri_group in torgi_scan.GROUPS else "any"
        d.area_m2 = area_sotka * torgi_scan.SOTKA if area_sotka else None
        url = lot_url.strip()
        d.lot_url = url[:300] if url.startswith("https://") else None
        d.bought_at, d.listed_at, d.sold_at = (
            _iso_day(bought_at),
            _iso_day(listed_at),
            _iso_day(sold_at),
        )
        d.buy_price, d.extra_costs = _num(buy_price), _num(extra_costs)
        d.list_price, d.sell_price = _num(list_price), _num(sell_price)
        d.note = note.strip()[:1000] or None
        db.commit()
    return RedirectResponse("/tools/torgi-deals", status_code=303)


@router.post("/tools/torgi-deals/{deal_id}/delete")
def torgi_deal_delete(user: TorgiDep, deal_id: int):
    with session() as db:
        d = db.get(TorgiDeal, deal_id)
        if d is not None and (d.user_id == user.id or user.is_admin):
            db.delete(d)
            db.commit()
    return RedirectResponse("/tools/torgi-deals", status_code=303)
