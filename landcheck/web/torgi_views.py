"""Страницы анализатора торгов (решение зума 09.11; ТЗ v3, п. 9).

Внутренний режим: видят администратор и аккаунты, которым он включил доступ («Торги» на
странице пользователей). Страницы:
  /tools/torgi-scan       — идущие лоты региона с вердиктом и доходностью;
  /tools/torgi-lot/{id}   — карточка лота: причины, сценарии, похожие завершённые торги;
  /tools/torgi-liquidity  — ликвидность по районам и кварталам + слой рынка;
  /tools/torgi-deals      — журнал своих сделок (факт: доходность, срок экспозиции).
"""

from __future__ import annotations

import re
import statistics
import tempfile
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from landcheck import torgi, torgi_calc, torgi_scan
from landcheck.models import Purpose
from landcheck.models import Request as CheckRequest
from landcheck.regions import CADASTRAL_REGIONS
from landcheck.web import jobs, torgi_sync
from landcheck.web.app import render, require_user
from landcheck.web.config import settings
from landcheck.web.db import (
    Check,
    TorgiDeal,
    TorgiLot,
    TorgiMarket,
    TorgiSync,
    User,
    session,
)
from landcheck.web.serialize import request_to_dict

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
    if value.isdigit() and value.zfill(2) in CADASTRAL_REGIONS:
        return value.zfill(2)
    for code, name in CADASTRAL_REGIONS.items():
        if name == value:
            return code
    return None


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


def _scan_params(q) -> torgi_scan.ScanParams:
    p = torgi_scan.ScanParams()
    if q.get("roi"):
        p.target_roi = max(torgi_calc._pct(q.get("roi"), p.target_roi), 0.0)
    if q.get("tax") not in (None, ""):
        p.tax = min(max(torgi_calc._pct(q.get("tax"), p.tax), 0.0), 0.99)
    p.extra = _num(q.get("extra")) or 0.0
    budget = _num(q.get("budget"))
    if budget:
        p.budget = budget * 1_000_000 if budget < 1000 else budget  # «1,5» — миллионы
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


def _scan(region: str, q) -> tuple[dict[str, Any], torgi_scan.ScanParams, list[dict[str, Any]]]:
    params = _scan_params(q)
    lots = torgi_sync.lots_of(region, months=settings.torgi_scan_months)
    result = torgi_scan.scan(lots, params, layer=_layer(region))
    rows = result["rows"]
    place = (q.get("d") or "").strip()
    if place:
        rows = [r for r in rows if (r.get("cad_num") or "").startswith(place)]
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
    elif sort == "roi":
        rows.sort(key=lambda r: -(r["s"].get("roi_start") or -9))
    result["rows"] = rows
    result["shown"] = len(rows)
    return result, params, lots


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
    region = _region_code(q.get("region")) or _default_region(user)
    ctx: dict[str, Any] = {
        "regions": _regions_ctx(),
        "region": region,
        "region_name": CADASTRAL_REGIONS.get(region or "", ""),
        "q": q,
        "groups": torgi_scan.GROUPS,
        "sel_groups": q.getlist("group"),
        "kinds": torgi_scan.KIND_RU,
        "user": user,
        "phase_ru": PHASE_RU,
    }
    if not region:
        return render(request, "torgi_scan.html", sync=None, result=None, params=None, **ctx)
    result, params, lots = _scan(region, q)
    pages = max(1, (len(result["rows"]) + PAGE_ROWS - 1) // PAGE_ROWS)
    page = min(max(page, 1), pages)
    rows = result["rows"][(page - 1) * PAGE_ROWS : page * PAGE_ROWS]
    return render(
        request,
        "torgi_scan.html",
        sync=torgi_sync.state_of(region),
        result=result,
        rows=rows,
        params=params,
        names=district_names(lots),
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
        bidd_short=torgi_scan.BIDD_TYPE_SHORT,
        today=datetime.now(UTC).date().isoformat(),
        **ctx,
    )


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


def _back(back: str, default: str) -> str:
    """Возврат только на свои страницы инструментов (никаких внешних адресов)."""
    return back if back.startswith("/tools/torgi-") else default


@router.get("/tools/torgi-scan/export.{fmt}")
def torgi_scan_export(request: Request, user: TorgiDep, fmt: str):
    q = request.query_params
    region = _region_code(q.get("region"))
    if not region or fmt not in ("xlsx", "kml"):
        raise HTTPException(404)
    result, _, _ = _scan(region, q)
    stamp = f"{datetime.now(UTC):%Y%m%d}"
    if fmt == "kml":
        name = f"Торги {CADASTRAL_REGIONS.get(region, region)} · {stamp}"
        return Response(
            torgi_scan.to_kml(result["rows"], name=name),
            media_type="application/vnd.google-earth.kml+xml",
            headers={"Content-Disposition": f'attachment; filename="torgi-{region}-{stamp}.kml"'},
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "torgi.xlsx"
        torgi_scan.to_xlsx(result["rows"], path, title=f"Торги {region}")
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
    s = torgi_scan.score_lot(lot, stats, params, layer=layer, repeats=torgi_scan.repeats_of(lots))
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
        qs=_query(q, saved=None),
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
    details = {"ads": found, "where": where, "lot_id": lot_id, "fetched_at": ads.get("fetched_at")}
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
        "user": user,
        "phase_ru": PHASE_RU,
        "q": q,
    }
    if not region:
        return render(request, "torgi_liquidity.html", rows=[], sync=None, layer=[], **ctx)
    lots = torgi_sync.lots_of(region, months=settings.torgi_scan_months)
    if place:
        lots = [x for x in lots if (x.get("cad_num") or "").startswith(place)]
    rows = torgi_scan.liquidity(lots, level=level, group=group)
    layer = _layer(region)
    by_scope: dict[str, list[dict[str, Any]]] = {}
    for m in layer:
        by_scope.setdefault(m["scope"], []).append(m)
    return render(
        request,
        "torgi_liquidity.html",
        rows=rows[:400],
        total=torgi_scan.stats_of(lots),
        names=district_names(lots),
        sync=torgi_sync.state_of(region),
        layer=sorted(layer, key=lambda m: (m["scope"], m["kind"], m.get("vri_group") or "")),
        by_scope=by_scope,
        sotka=torgi_scan.SOTKA,
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
