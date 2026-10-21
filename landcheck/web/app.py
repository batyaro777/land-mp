"""FastAPI-приложение кабинета брокера."""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from landcheck import __version__
from landcheck.export import DISCLAIMER, to_csv, to_markdown, to_xlsx
from landcheck.inputs import CAD_RE, load_listings, parse_csv, parse_xlsx
from landcheck.intent import parse_request
from landcheck.models import SOTKA_M2, BatchResult, Purpose
from landcheck.models import Request as CheckRequest
from landcheck.web import jobs
from landcheck.web.auth import (
    COOKIE,
    MAX_AGE,
    current_user,
    make_token,
    record_failed_attempt,
    too_many_attempts,
)
from landcheck.web.config import settings
from landcheck.web.content import (
    CHECKLIST,
    batch_median,
    goal_summary,
    price_orientation,
    price_position,
)
from landcheck.web.db import (
    LISTING_STATUSES,
    PROOF_KINDS,
    PUBLIC_ROLES,
    ROLES,
    Check,
    CheckItem,
    User,
    authenticate,
    ensure_ref_code,
    init_db,
    normalize_email,
    redeem_invite,
    register_user,
    session,
    user_by_ref,
)
from landcheck.web.serialize import (
    LEVEL_RANK,
    flags_by_level,
    fmt_date,
    fmt_day,
    fmt_money,
    fmt_sotka,
    request_from_dict,
    request_to_dict,
    result_from_dict,
)

log = logging.getLogger("landcheck.web")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["money"] = fmt_money
templates.env.filters["sotka"] = fmt_sotka
templates.env.filters["dt"] = fmt_date
templates.env.filters["day"] = fmt_day
templates.env.filters["ucfirst"] = lambda s: (s[:1].upper() + s[1:]) if s else s
templates.env.globals.update(
    brand=settings.brand, version=__version__, Purpose=Purpose, disclaimer=DISCLAIMER
)

PURPOSES = [(p.value, p.activity) for p in Purpose]
VERDICT_ORDER = ["ок", "проверить", "стоп"]
templates.env.globals.update(
    ROLES=ROLES,
    PUBLIC_ROLES_MAP={k: ROLES[k] for k in PUBLIC_ROLES},
    PROOF_KINDS=PROOF_KINDS,
    LISTING_STATUSES=LISTING_STATUSES,
)


def _configure_logging() -> None:
    """Предупреждения модулей (llm, pipeline, jobs) — в stdout контейнера, с временем и именем:
    без явной настройки Python выводит только голый текст через lastResort (прод 20.10)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app() -> FastAPI:
    _configure_logging()
    init_db(settings.db_path)
    app = FastAPI(title="landcheck", version=__version__, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    uploads = settings.db_path.parent / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads)), name="uploads")
    app.include_router(router)
    resumed = jobs.resume_pending()
    if resumed:
        log.warning("возобновлено незавершённых проверок после рестарта: %s", resumed)
    return app


from fastapi import APIRouter  # noqa: E402

router = APIRouter()


# --- helpers ---------------------------------------------------------------------------
def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user


UserDep = Annotated[User, Depends(require_user)]


def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("user", current_user(request))
    if ctx.get("user") is not None and "unread" not in ctx:
        from landcheck.web.notify import unread_count

        try:
            with session() as db:
                ctx["unread"] = unread_count(db, ctx["user"].id)
        except Exception:  # noqa: BLE001 — счётчик не должен ронять страницу
            ctx["unread"] = 0
    # Кнопка «Голосом» есть всегда: в браузере распознаёт Web Speech API (бесплатно),
    # серверный путь (Groq Whisper / локальный whisper / Gemini) — запасной.
    ctx.setdefault("voice_enabled", True)
    ctx.setdefault("asr_server", settings.asr_server_enabled)
    ctx["request"] = request
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


LOGO_EXTS = ("png", "jpg", "webp")
MAX_LOGO_BYTES = 2 * 1024 * 1024


def _logos_dir() -> Path:
    return settings.db_path.parent / "uploads" / "logos"


def logo_url(user_id: int) -> str | None:
    """Логотип брокера лежит файлом uploads/logos/<id>.<ext>; схема БД не трогается."""
    for ext in LOGO_EXTS:
        if (_logos_dir() / f"{user_id}.{ext}").exists():
            return f"/uploads/logos/{user_id}.{ext}"
    return None


def _remove_logo(user_id: int) -> None:
    for ext in LOGO_EXTS:
        p = _logos_dir() / f"{user_id}.{ext}"
        if p.exists():
            p.unlink()


def _own_check(db, user: User, check_id: int) -> Check:
    check = db.get(Check, check_id)
    if check is None or (check.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    return check


def _batch_from_check(check: Check) -> BatchResult:
    results = [result_from_dict(i.data) for i in check.items]
    return BatchResult(
        request=request_from_dict(check.request),
        results=results,
        started_at=check.created_at,
        finished_at=check.finished_at or check.created_at,
        median_price_per_sotka=(check.stats or {}).get("median_price_per_sotka"),
        errors=list((check.stats or {}).get("errors") or []),
    )


# --- auth -------------------------------------------------------------------------------
REF_COOKIE = "lc_ref"


def _remember_ref(request: Request, resp, ref: str) -> None:
    """Партнёрский код из ссылки живёт в cookie 30 дней: регистрация может быть позже."""
    if ref:
        resp.set_cookie(REF_COOKIE, ref.strip().lower()[:16], max_age=MAX_AGE, samesite="lax")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None, tab: str = "password", ref: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    if ref:
        tab = "register"
    resp = render(request, "login.html", error=error, tab=tab, ref=ref, user=None)
    _remember_ref(request, resp, ref)
    return resp


def _login_response(user: User) -> RedirectResponse:
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, make_token(user.id), max_age=MAX_AGE, httponly=True, samesite="lax")
    return resp


def _throttled(request: Request) -> HTMLResponse | None:
    ip = request.client.host if request.client else "?"
    if too_many_attempts(ip):
        return render(
            request,
            "login.html",
            error="Слишком много попыток. Подождите 10 минут.",
            tab="password",
            user=None,
            status_code=429,
        )
    return None


@router.post("/login")
def login(
    request: Request,
    code: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    contact: Annotated[str, Form()] = "",
    brand_name: Annotated[str, Form()] = "",
):
    """Вход по коду приглашения (пилот): код создаёт пользователя-брокера."""
    if (resp := _throttled(request)) is not None:
        return resp
    with session() as db:
        user = redeem_invite(db, code, name or "Брокер", contact, brand_name)
    if user is None:
        record_failed_attempt(request.client.host if request.client else "?")
        return render(
            request,
            "login.html",
            error="Код не найден. Проверьте написание.",
            tab="code",
            user=None,
        )
    return _login_response(user)


@router.post("/login/password")
def login_password(
    request: Request, email: Annotated[str, Form()], password: Annotated[str, Form()]
):
    if (resp := _throttled(request)) is not None:
        return resp
    with session() as db:
        user = authenticate(db, email, password)
    if user is None:
        record_failed_attempt(request.client.host if request.client else "?")
        return render(
            request,
            "login.html",
            error="Email или пароль не подходят.",
            tab="password",
            user=None,
        )
    return _login_response(user)


@router.post("/register")
def register(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "buyer",
    brand_name: Annotated[str, Form()] = "",
    ref: Annotated[str, Form()] = "",
):
    """Регистрация без кода (ТЗ v2, R3): email, пароль, имя, роль; партнёрский код из формы
    или cookie привязывает пришедшего к инфопартнёру (К4)."""
    if (resp := _throttled(request)) is not None:
        return resp
    code = ref or request.cookies.get(REF_COOKIE, "")
    with session() as db:
        partner = user_by_ref(db, code)
        user, error = register_user(
            db, email, password, name, role, brand_name, referred_by=partner.id if partner else None
        )
    if user is None:
        record_failed_attempt(request.client.host if request.client else "?")
        return render(request, "login.html", error=error, tab="register", ref=code, user=None)
    resp = _login_response(user)
    resp.delete_cookie(REF_COOKIE)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --- dashboard & checks -------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: UserDep):
    if user.is_admin:
        return RedirectResponse("/admin", status_code=303)
    if user.role == "seller":
        return RedirectResponse("/listings", status_code=303)
    if user.role == "buyer":
        return RedirectResponse("/search", status_code=303)
    return _broker_dashboard(request, user)


@router.get("/checks", response_class=HTMLResponse)
def checks_page(request: Request, user: UserDep):
    """Подборки брокера — для администратора и прямых ссылок (дом администратора — /admin)."""
    return _broker_dashboard(request, user)


def _broker_dashboard(request: Request, user: User):
    with session() as db:
        checks = db.scalars(
            select(Check)
            .where(Check.user_id == user.id)
            .order_by(Check.created_at.desc())
            .limit(50)
        ).all()
    return render(request, "dashboard.html", checks=checks, purposes=PURPOSES, user=user)


@router.post("/checks")
async def create_check(
    request: Request,
    user: UserDep,
    title: Annotated[str, Form()] = "",
    numbers: Annotated[str, Form()] = "",
    purpose: Annotated[str, Form()] = "any",
    query: Annotated[str, Form()] = "",
    budget: Annotated[str, Form()] = "",
    min_area: Annotated[str, Form()] = "",
    max_area: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
):
    text = numbers or ""
    input_name = None
    if file is not None and file.filename:
        raw = await file.read()
        input_name = file.filename
        suffix = Path(file.filename).suffix.lower()
        try:
            if suffix in (".xlsx", ".xlsm"):
                listings = parse_xlsx(io.BytesIO(raw))  # type: ignore[arg-type]
            elif suffix == ".csv":
                listings = parse_csv(raw.decode("utf-8-sig", errors="replace"))
            else:
                listings = load_listings(raw.decode("utf-8-sig", errors="replace"))
        except Exception as e:  # noqa: BLE001
            return render(
                request, "error.html", message=f"Не удалось прочитать файл: {e}", user=user
            )
        # Переводим в текст «КН; цена; площадь м2», чтобы хранить единый вход.
        lines = []
        for item in listings:
            parts = [item.cad_num]
            parts.append(f"{item.price_rub:.0f} руб" if item.price_rub is not None else "")
            parts.append(
                f"{item.declared_area_m2:.0f} м2" if item.declared_area_m2 is not None else ""
            )
            if item.url:
                parts.append(item.url)
            lines.append("; ".join(parts))
        text = (text + "\n" + "\n".join(lines)).strip()
    numbers_found = CAD_RE.findall(text)
    if not numbers_found:
        return render(
            request,
            "error.html",
            message="Не нашёл ни одного кадастрового номера во вводе.",
            user=user,
        )
    if len(numbers_found) > settings.max_items:
        return render(
            request,
            "error.html",
            message=f"Слишком много номеров: {len(numbers_found)}. "
            f"Лимит за один запуск — {settings.max_items}.",
            user=user,
        )
    req = parse_request(query) if query.strip() else CheckRequest()
    # Явный выбор в форме главнее запроса словами; «any» по умолчанию ничего не затирает.
    if purpose and purpose != Purpose.ANY.value:
        try:
            req.purpose = Purpose(purpose)
        except ValueError:
            pass
    if budget.strip():
        from landcheck.inputs import parse_number

        v = parse_number(budget)
        if v is not None and v < 1000:
            v *= 1_000_000
        req.budget_rub = v
    if min_area.strip():
        try:
            req.min_area_m2 = float(min_area.replace(",", ".")) * SOTKA_M2
        except ValueError:
            pass
    if max_area.strip():
        try:
            req.max_area_m2 = float(max_area.replace(",", ".")) * SOTKA_M2
        except ValueError:
            pass
    with session() as db:
        check = Check(
            user_id=user.id,
            title=title.strip() or f"Подборка из {len(set(numbers_found))} участков",
            request=request_to_dict(req),
            input_text=text,
            input_name=input_name,
            total=len(set(numbers_found)),
        )
        db.add(check)
        db.commit()
        check_id = check.id
    jobs.enqueue(check_id)
    return RedirectResponse(f"/checks/{check_id}", status_code=303)


def _filtered_items(check: Check, verdict: str | None, sort: str) -> list[CheckItem]:
    items = list(check.items)
    if verdict in VERDICT_ORDER:
        items = [i for i in items if i.verdict == verdict]
    if sort == "price":
        items.sort(
            key=lambda i: (
                i.data["listing"].get("price_rub") is None,
                i.data["listing"].get("price_rub") or 0,
            )
        )
    elif sort == "pps":
        items.sort(
            key=lambda i: (
                i.data.get("price_per_sotka") is None,
                i.data.get("price_per_sotka") or 0,
            )
        )
    elif sort == "area":
        items.sort(
            key=lambda i: (
                i.data["parcel"].get("area_m2") is None,
                -(i.data["parcel"].get("area_m2") or 0),
            )
        )
    else:
        items.sort(key=lambda i: (LEVEL_RANK[i.verdict], -i.score))
    return items


@router.get("/checks/{check_id}", response_class=HTMLResponse)
def check_page(
    request: Request, user: UserDep, check_id: int, verdict: str | None = None, sort: str = "score"
):
    with session() as db:
        check = _own_check(db, user, check_id)
        items = _filtered_items(check, verdict, sort) if check.status == "done" else []
        hist = [i.data.get("history") or {} for i in check.items]
        new_count = sum(1 for h in hist if h.get("is_new"))
        changed_count = sum(1 for h in hist if h.get("changed"))
        purpose = _check_purpose(check)
        goals = {i.id: goal_summary(i.data, purpose) for i in items}
        median = batch_median(check.stats, [i.data for i in check.items])
        prices = {i.id: price_position(i.data, median) for i in items}
        return render(
            request,
            "check.html",
            check=check,
            items=items,
            verdict=verdict,
            sort=sort,
            purpose=purpose,
            goals=goals,
            prices=prices,
            counts=(check.stats or {}).get("counts") or {},
            new_count=new_count,
            changed_count=changed_count,
            user=user,
        )


@router.get("/checks/{check_id}/status", response_class=HTMLResponse)
def check_status(request: Request, user: UserDep, check_id: int):
    with session() as db:
        check = _own_check(db, user, check_id)
        resp = render(request, "_status.html", check=check, user=user)
        if check.status in ("done", "error"):
            resp.headers["HX-Refresh"] = "true"
        return resp


@router.post("/checks/{check_id}/delete")
def check_delete(user: UserDep, check_id: int):
    with session() as db:
        check = _own_check(db, user, check_id)
        db.delete(check)
        db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/checks/{check_id}/rerun")
def check_rerun(user: UserDep, check_id: int):
    with session() as db:
        check = _own_check(db, user, check_id)
        check.status, check.done, check.error = "queued", 0, None
        check.request = {**check.request, "refresh": True}
        db.commit()
    jobs.enqueue(check_id)
    return RedirectResponse(f"/checks/{check_id}", status_code=303)


@router.get("/checks/{check_id}/export.{fmt}")
def check_export(user: UserDep, check_id: int, fmt: str):
    with session() as db:
        check = _own_check(db, user, check_id)
        batch = _batch_from_check(check)
        title = check.title
    safe = (
        "".join(c if c.isalnum() or c in "-_ " else "_" for c in title)[:60].strip() or "podborka"
    )
    if fmt == "xlsx":
        buf = io.BytesIO()
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=True) as tmp:
            to_xlsx(batch, tmp.name)
            buf.write(Path(tmp.name).read_bytes())
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe)}.xlsx"},
        )
    if fmt == "csv":
        return Response(
            to_csv(batch).encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe)}.csv"},
        )
    if fmt == "md":
        return Response(to_markdown(batch), media_type="text/markdown; charset=utf-8")
    raise HTTPException(404)


# --- reports ------------------------------------------------------------------------------
def _check_purpose(check: Check) -> Purpose:
    try:
        return Purpose((check.request or {}).get("purpose") or "any")
    except ValueError:
        return Purpose.ANY


def _loc_extras(loc: dict[str, Any] | None, region: dict[str, Any] | None) -> dict[str, Any]:
    """Разделы «Вода» и «Негативные факторы» поверх собранной «Локации» (ТЗ v2, М2.2–2.3)."""
    from landcheck.location import negative_factors, water_summary

    if not loc:
        return {"water": None, "negatives": []}
    return {"water": water_summary(loc), "negatives": negative_factors(loc, region)}


def _corridor_for(data: dict[str, Any]) -> dict[str, Any] | None:
    """Коридор цены за сотку по базе Land-MP для участка отчёта (план 20.10, п.4)."""
    from landcheck.web.content import collect_price_points, price_corridor

    p = data.get("parcel") or {}
    if not p.get("address"):
        return None
    try:
        with session() as db:
            points = collect_price_points(db)
    except Exception as exc:  # noqa: BLE001 — коридор не должен ронять отчёт
        log.warning("corridor: %s", exc)
        return None
    return price_corridor(
        points,
        address=p.get("address"),
        category=p.get("category"),
        vri=p.get("permitted_use"),
        pps=data.get("price_per_sotka"),
        exclude_cad=p.get("cad_num"),
    )


def _valuation_for(data, corridor, median_batch):
    from landcheck.web.content import valuation

    return valuation(data, corridor=corridor, ads=data.get("market_ads"), median_batch=median_batch)


@router.get("/checks/{check_id}/items/{cad_num}/ads", response_class=HTMLResponse)
def report_ads(request: Request, user: UserDep, check_id: int, cad_num: str, refresh: int = 0):
    """Блок «Оценка стоимости» с объявлениями в локации (HTMX): поиск, кэш в item.data."""
    from landcheck.market_ads import collect_ads
    from landcheck.region import resolve_locality, short_place
    from landcheck.web.content import land_kind

    with session() as db:
        check = _own_check(db, user, check_id)
        item = db.scalar(
            select(CheckItem).where(CheckItem.check_id == check.id, CheckItem.cad_num == cad_num)
        )
        if item is None:
            raise HTTPException(404)
        data = dict(item.data)
        parcel = data.get("parcel") or {}
        if data.get("market_ads") is None or refresh:
            locality = resolve_locality(parcel.get("address"), parcel.get("geometry"))
            if locality:
                ads = collect_ads(
                    short_place(locality),
                    land_kind(parcel.get("category"), parcel.get("permitted_use")),
                )
                ads["locality"] = locality
            else:
                ads = {"ads": [], "n": 0, "error": "локация участка не определена"}
            data = _merge_item_data(db, item, market_ads=ads)
        median = batch_median(check.stats, [i.data for i in check.items])
        corridor = _corridor_for(data)
        return render(
            request,
            "_valuation.html",
            valuation=_valuation_for(data, corridor, median),
            d=data,
            public=False,
            ads_url=f"/checks/{check.id}/items/{cad_num}/ads",
            user=user,
        )


def _merge_item_data(db, item: CheckItem, **keys: Any) -> dict[str, Any]:
    """Записать только свои ключи поверх свежих данных строки: блоки «Локация», «Регион»,
    «Документы», «Оценка» грузятся параллельно, и запись всего словаря затирала чужой результат
    (Клавдий, прод 21.10: /location за 94 с стёр market_ads)."""
    db.refresh(item)
    item.data = {**(item.data or {}), **keys}
    db.commit()
    return item.data


def _report_ctx(check: Check, item: CheckItem, public: bool) -> dict[str, Any]:
    data = item.data
    purpose = _check_purpose(check)
    median = batch_median(check.stats, [i.data for i in check.items])
    loc, region = data.get("location"), data.get("region")
    return {
        "check": check,
        "item": item,
        "d": data,
        "by_level": flags_by_level(data),
        "purpose": purpose,
        "goal": goal_summary(data, purpose),
        "loc": loc,
        "terrain": data.get("terrain"),
        "location_url": f"/checks/{check.id}/items/{item.cad_num}/location",
        "region": region,
        "region_url": f"/checks/{check.id}/items/{item.cad_num}/region",
        "docs": data.get("docs"),
        "docs_url": f"/checks/{check.id}/items/{item.cad_num}/docs",
        "region_enabled": settings.region_enabled,
        "checklist": CHECKLIST,
        "price": price_orientation(data, (check.stats or {}).get("median_price_per_sotka")),
        "price_pos": price_position(data, median),
        "corridor": (corridor := _corridor_for(data)),
        "valuation": _valuation_for(data, corridor, median),
        "ads_url": f"/checks/{check.id}/items/{item.cad_num}/ads",
        "logo_url": logo_url(check.user_id),
        **_loc_extras(loc, region),
        "public": public,
        "owner": check.user,
        "share_url": f"{settings.base_url}/r/{item.token}",
    }


@router.get("/checks/{check_id}/items/{cad_num}", response_class=HTMLResponse)
def report_private(request: Request, user: UserDep, check_id: int, cad_num: str):
    with session() as db:
        check = _own_check(db, user, check_id)
        item = db.scalar(
            select(CheckItem).where(CheckItem.check_id == check.id, CheckItem.cad_num == cad_num)
        )
        if item is None:
            raise HTTPException(404)
        _ = check.user  # подгрузить
        return render(request, "report.html", **_report_ctx(check, item, public=False), user=user)


@router.get("/r/{token}", response_class=HTMLResponse)
def report_public(request: Request, token: str):
    with session() as db:
        item = db.scalar(select(CheckItem).where(CheckItem.token == token))
        if item is None:
            raise HTTPException(404)
        check = item.check
        _ = check.user
        return render(request, "report.html", **_report_ctx(check, item, public=True), user=None)


@router.get("/checks/{check_id}/items/{cad_num}/location", response_class=HTMLResponse)
def report_location(request: Request, user: UserDep, check_id: int, cad_num: str, refresh: int = 0):
    """Блок «Локация» по запросу (HTMX): кэш → НСПД вокруг участка → OSM; результат в item.data."""
    from landcheck.location import summarize_location

    with session() as db:
        check = _own_check(db, user, check_id)
        item = db.scalar(
            select(CheckItem).where(CheckItem.check_id == check.id, CheckItem.cad_num == cad_num)
        )
        if item is None:
            raise HTTPException(404)
        data = dict(item.data)
        parcel = data.get("parcel") or {}
        listing = data.get("listing") or {}
        loc = data.get("location")
        if loc is None or refresh:
            raw = jobs.fetch_location(cad_num, parcel.get("geometry"), refresh=bool(refresh))
            loc = summarize_location(
                raw,
                parcel_geojson=parcel.get("geometry"),
                cad_num=cad_num,
                category=parcel.get("category"),
                cost_index=parcel.get("cost_index_rub_m2"),
                listing_price_rub=listing.get("price_rub"),
                area_m2=parcel.get("area_m2"),
            )
            data = _merge_item_data(
                db, item, location=loc, terrain=_terrain_for(parcel.get("geometry"), loc)
            )
        elif data.get("terrain") is None and parcel.get("geometry"):
            data = _merge_item_data(db, item, terrain=_terrain_for(parcel.get("geometry"), loc))
        return render(
            request,
            "_location.html",
            loc=loc,
            terrain=data.get("terrain"),
            public=False,
            location_url=f"/checks/{check.id}/items/{cad_num}/location",
            region=data.get("region"),
            user=user,
            **_loc_extras(loc, data.get("region")),
        )


def _terrain_for(
    geometry: dict[str, Any] | None, loc: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Рельеф с учётом воды рядом и зоны затопления из «Локации» (ТЗ v2, рельеф и вода)."""
    from landcheck.location import water_summary
    from landcheck.terrain import terrain_summary

    if not geometry:
        return None
    water_m = None
    flood = water_zone = False
    if loc:
        items = water_summary(loc).get("items") or []
        # Расстояние — только до самой воды (OSM); «0 м» до водоохранной зоны — это зона,
        # а не река (Клавдий, прод 20.10).
        dists = [
            i["distance_m"]
            for i in items
            if i.get("severity") == "plus" and i.get("distance_m") is not None
        ]
        water_m = min(dists) if dists else None
        flood = any(i.get("severity") == "risk" and i.get("distance_m") == 0 for i in items)
        water_zone = any(i.get("severity") == "neutral" and i.get("distance_m") == 0 for i in items)
    return terrain_summary(
        geometry, water_distance_m=water_m, flood_zone=flood, water_zone=water_zone
    )


@router.get("/checks/{check_id}/items/{cad_num}/docs", response_class=HTMLResponse)
def report_docs(request: Request, user: UserDep, check_id: int, cad_num: str, refresh: int = 0):
    """Блок «Документы и упоминания» (HTMX): поиск по кадастровому номеру, результат в item.data."""
    from landcheck.docs_search import search_documents

    with session() as db:
        check = _own_check(db, user, check_id)
        item = db.scalar(
            select(CheckItem).where(CheckItem.check_id == check.id, CheckItem.cad_num == cad_num)
        )
        if item is None:
            raise HTTPException(404)
        data = dict(item.data)
        docs = data.get("docs")
        if docs is None or refresh:
            docs = search_documents(cad_num)
            data = _merge_item_data(db, item, docs=docs)
        return render(
            request,
            "_docs.html",
            docs=docs,
            public=False,
            docs_url=f"/checks/{check.id}/items/{cad_num}/docs",
            user=user,
        )


@router.get("/checks/{check_id}/items/{cad_num}/region", response_class=HTMLResponse)
def report_region(request: Request, user: UserDep, check_id: int, cad_num: str, refresh: int = 0):
    """Блок «Регион» по запросу (HTMX): поиск по открытым источникам + модель по локации участка."""
    from landcheck.region import resolve_locality

    with session() as db:
        check = _own_check(db, user, check_id)
        item = db.scalar(
            select(CheckItem).where(CheckItem.check_id == check.id, CheckItem.cad_num == cad_num)
        )
        if item is None:
            raise HTTPException(404)
        data = dict(item.data)
        parcel = data.get("parcel") or {}
        region = data.get("region")
        locality = resolve_locality(parcel.get("address"), parcel.get("geometry"))
        if region is None or refresh or region.get("error"):
            if locality is None:
                region = {
                    "sections": [],
                    "sources": [],
                    "error": "у участка нет адреса в НСПД, локацию не определить",
                    "locality": None,
                }
            else:
                region = jobs.fetch_region(
                    locality, _check_purpose(check), refresh=bool(refresh), cad_num=cad_num
                )
                region["locality"] = locality
            data = _merge_item_data(db, item, region=region)
        return render(
            request,
            "_region.html",
            region=region,
            public=False,
            region_url=f"/checks/{check.id}/items/{cad_num}/region",
            user=user,
        )


# --- профиль брокера: имя, бренд, контакт, логотип для отчётов (ТЗ v2, R2) ---------------
@router.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request, user: UserDep, saved: int = 0, error: str | None = None):
    from landcheck.web.notify import link_url

    return render(
        request,
        "profile.html",
        user=user,
        logo_url=logo_url(user.id),
        saved=saved,
        error=error,
        tg_link=link_url(user.id),
    )


@router.post("/profile")
async def profile_save(
    request: Request,
    user: UserDep,
    name: Annotated[str, Form()] = "",
    brand_name: Annotated[str, Form()] = "",
    contact: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    remove_logo: Annotated[str, Form()] = "",
    telegram_chat_id: Annotated[str, Form()] = "",
    notify_email: Annotated[str, Form()] = "",
    logo: Annotated[UploadFile | None, File()] = None,
):
    from landcheck.web.db import EMAIL_RE, MIN_PASSWORD, hash_password

    error = None
    if remove_logo:
        _remove_logo(user.id)
    if logo is not None and logo.filename:
        raw = await logo.read(MAX_LOGO_BYTES + 1)
        kind = _image_kind(raw)
        if kind is None:
            error = "Логотип не загружен: нужен файл PNG, JPG или WebP."
        elif len(raw) > MAX_LOGO_BYTES:
            error = "Логотип не загружен: файл больше 2 МБ."
        else:
            _remove_logo(user.id)
            _logos_dir().mkdir(parents=True, exist_ok=True)
            (_logos_dir() / f"{user.id}.{kind}").write_bytes(raw)
    with session() as db:
        u = db.get(User, user.id)
        assert u is not None
        u.name = name.strip()[:120] or u.name
        u.brand = brand_name.strip()[:120] or None
        u.contact = contact.strip()[:200] or None
        if role in PUBLIC_ROLES and not u.is_admin:
            u.role = role  # администратор роль в профиле не меняет: только через CLI
        if telegram_chat_id.strip():  # ручной ввод остаётся запасным путём
            u.telegram_chat_id = re.sub(r"[^\d-]", "", telegram_chat_id)[:32] or None
        u.notify_email = bool(notify_email)
        new_email = normalize_email(email)
        if new_email and new_email != (u.email or ""):
            taken = db.scalar(select(User.id).where(User.email == new_email, User.id != u.id))
            if not EMAIL_RE.match(new_email):
                error = error or "Email не сохранён: укажите настоящий адрес."
            elif taken is not None:
                error = error or "Email не сохранён: он уже занят другим пользователем."
            else:
                u.email = new_email
        if password:
            if len(password) < MIN_PASSWORD:
                error = error or f"Пароль не сохранён: не короче {MIN_PASSWORD} знаков."
            else:
                u.password_hash = hash_password(password)
        db.commit()
    if error:
        return RedirectResponse(f"/profile?error={quote(error)}", status_code=303)
    return RedirectResponse("/profile?saved=1", status_code=303)


# --- single check ------------------------------------------------------------------------
@router.get("/single", response_class=HTMLResponse)
def single_form(request: Request, user: UserDep, cad_num: str = "", purpose: str = "any"):
    return render(
        request, "single.html", purposes=PURPOSES, cad_num=cad_num, purpose=purpose, user=user
    )


@router.post("/single")
def single_create(
    user: UserDep,
    cad_num: Annotated[str, Form()],
    purpose: Annotated[str, Form()] = "any",
    price: Annotated[str, Form()] = "",
    area: Annotated[str, Form()] = "",
):
    m = CAD_RE.search(cad_num)
    if not m:
        raise HTTPException(400, "Не похоже на кадастровый номер")
    parts = [m.group(0)]
    if price.strip():
        parts.append(price.strip() + " руб")
    if area.strip():
        parts.append(area.strip() + " сот")
    req = CheckRequest()
    try:
        req.purpose = Purpose(purpose)
    except ValueError:
        pass
    with session() as db:
        check = Check(
            user_id=user.id,
            title=f"Разовая проверка {m.group(0)}",
            request=request_to_dict(req),
            input_text="; ".join(parts),
            total=1,
        )
        db.add(check)
        db.commit()
        check_id = check.id
    jobs.enqueue(check_id)
    return RedirectResponse(f"/checks/{check_id}", status_code=303)


# --- listing composer (М3) ----------------------------------------------------------------
MAX_PHOTOS = 10
MAX_PHOTO_BYTES = 10 * 1024 * 1024


def _image_kind(raw: bytes) -> str | None:
    """Тип по сигнатуре файла, а не по расширению."""
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def _listing_warnings(data: dict[str, Any]) -> list[str]:
    """Противоречия конечного текста с кадастром плюс предупреждения по участку."""
    from landcheck.listing import ParcelFacts, contradictions, static_warnings, strip_facts

    pf_raw = data.get("parcel_facts")
    if not pf_raw:
        return list(data.get("warnings") or [])
    pf = ParcelFacts.from_dict(pf_raw)
    text = f"{data.get('title') or ''}\n{data.get('description') or ''}"
    text = strip_facts(text, data.get("facts"), data.get("zouit_notes"))
    return contradictions(pf, text, data.get("price_rub")) + static_warnings(pf)


def _refresh_listing_data(data: dict[str, Any]) -> dict[str, Any] | None:
    """Объявления, созданные до bf4c70d/65502dd, хранят «Площадь по ЕГРН 0.01 сот.» для участка
    с ошибочной площадью и рубрику «Поселений (ИЖС)» без категории и ВРИ (Клавдий, прод 20.10) —
    при открытии приводим к новым правилам."""
    from landcheck.listing import MIN_AREA_M2, ParcelFacts

    pf_raw = data.get("parcel_facts")
    if not pf_raw:
        return None
    pf = ParcelFacts.from_dict(pf_raw)
    out = dict(data)
    changed = False
    facts = list(data.get("facts") or [])
    if pf.best_area_m2 and pf.best_area_m2 < MIN_AREA_M2:
        stale = [f for f in facts if f.startswith("Площадь по ЕГРН ") and "не определена" not in f]
        if stale or data.get("land_area_sotka") is not None:
            fresh = (
                f"Площадь по ЕГРН не определена: в карточке {pf.best_area_m2:g} м², "
                "похоже на ошибку данных — уточнить по выписке"
            )
            facts = [fresh if f in stale else f for f in facts]
            if fresh not in facts:
                facts.insert(0, fresh)
            out["land_area_sotka"] = None
            changed = True
    if pf.category is None and not pf.permitted_use_all:
        unset = [f for f in facts if f.startswith("Категория:") and "не установлен" in f.lower()]
        if unset or data.get("object_type"):
            facts = [f for f in facts if f not in unset]
            out["object_type"] = ""
            changed = True
    if not changed:
        return None
    out["facts"] = facts
    return out


def _listing_block_kind(data: dict[str, Any]) -> str | None:
    """Почему закрыта выгрузка: «text» — противоречия в тексте (правится текстом),
    «data» — проблема самого участка (межевание, площадь), текстом не снять."""
    from landcheck.listing import ParcelFacts, contradictions, strip_facts

    pf_raw = data.get("parcel_facts")
    if not pf_raw or not data.get("warnings"):
        return None
    pf = ParcelFacts.from_dict(pf_raw)
    text = f"{data.get('title') or ''}\n{data.get('description') or ''}"
    text = strip_facts(text, data.get("facts"), data.get("zouit_notes"))
    return "text" if contradictions(pf, text, data.get("price_rub")) else "data"


def _fetch_parcel_now(cad_num: str, *, refresh: bool = False):
    from landcheck.cache import ParcelCache
    from landcheck.normalize import parcel_from_payload
    from landcheck.pipeline import fetch_payload

    provider = jobs.build_provider()
    cache = ParcelCache(settings.cache_path, ttl_days=settings.cache_ttl_days)
    try:
        payload, errors = fetch_payload(cad_num, provider, cache, refresh=refresh)
    finally:
        cache.close()
        close = getattr(provider, "close", None)
        if close:
            close()
    return parcel_from_payload(payload), errors


@router.get("/listing/new", response_class=HTMLResponse)
def listing_form(request: Request, user: UserDep, cad_num: str = ""):
    from landcheck.listing import AVITO_OBJECT_TYPES

    return render(
        request, "listing_new.html", cad_num=cad_num, object_types=AVITO_OBJECT_TYPES, user=user
    )


@router.post("/listing")
async def listing_create(
    request: Request,
    user: UserDep,
    cad_num: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    price: Annotated[str, Form()] = "",
    object_type: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    photos: Annotated[list[UploadFile] | None, File()] = None,
):
    from landcheck.inputs import parse_number
    from landcheck.listing import ParcelFacts, build_draft
    from landcheck.web.db import Listing as ListingRow

    m = CAD_RE.search(cad_num)
    if not m:
        return render(request, "error.html", message="Не похоже на кадастровый номер.", user=user)
    cn = m.group(0)
    parcel, errors = _fetch_parcel_now(cn)
    if not parcel.found:
        msg = "Участок в НСПД не найден." + (" " + "; ".join(errors) if errors else "")
        return render(request, "error.html", message=msg, user=user)
    price_v = parse_number(price) if price.strip() else None
    if price_v is not None and price_v < 1000:
        price_v *= 1_000_000
    draft = build_draft(parcel, notes=notes, price_rub=price_v, object_type=object_type or None)
    with session() as db:
        row = ListingRow(user_id=user.id, cad_num=cn, data={})
        db.add(row)
        db.flush()
        # фото: сохраняем в data/uploads/<id>/ и отдаём по /uploads/...
        image_paths: list[str] = []
        skipped: list[str] = []
        if photos:
            updir = settings.db_path.parent / "uploads" / str(row.id)
            updir.mkdir(parents=True, exist_ok=True)
            for i, ph in enumerate(photos[:MAX_PHOTOS], start=1):
                if not ph.filename:
                    continue
                raw = await ph.read(MAX_PHOTO_BYTES + 1)
                kind = _image_kind(raw)
                if kind is None or len(raw) > MAX_PHOTO_BYTES:
                    skipped.append(ph.filename)
                    continue
                target = updir / f"{i:02d}.{kind}"
                target.write_bytes(raw)
                image_paths.append(f"/uploads/{row.id}/{target.name}")
        row.data = {
            "title": draft.title,
            "description": draft.description,
            "object_type": draft.object_type,
            "land_area_sotka": draft.land_area_sotka,
            "price_rub": draft.price_rub,
            "address": draft.address,
            "facts": draft.facts,
            "warnings": draft.warnings,
            "zouit_notes": draft.zouit_notes,
            "notes": notes,
            "phone": phone.strip(),
            "images": image_paths,
            "skipped_photos": skipped,
            "fetch_errors": errors,
            "parcel_facts": ParcelFacts.from_parcel(parcel).to_dict(),
        }
        row.data = _compose_with_ai(dict(row.data))
        db.commit()
        listing_id = row.id
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


@router.post("/listing/{listing_id}/compose")
def listing_compose(user: UserDep, listing_id: int, notes: Annotated[str, Form()] = ""):
    """Пересобрать текст ИИ по новым заметкам продавца — с обновлением фактов и ЗОУИТ из кэша
    НСПД: старые объявления хранили обрезанные дубли зон (Клавдий, прод 20.10)."""
    with session() as db:
        row = _own_listing(db, user, listing_id)
        data = dict(row.data)
        data["notes"] = notes.strip() or data.get("notes") or ""
        data = _refresh_from_parcel(row.cad_num, data)
        row.data = _compose_with_ai(data)
        unpublished = bool(row.published and row.data.get("warnings"))
        if unpublished:
            row.published = False
        db.commit()
    suffix = "?err=unpublished" if unpublished else ""
    return RedirectResponse(f"/listing/{listing_id}{suffix}", status_code=303)


@router.get("/listings", response_class=HTMLResponse)
def listings_page(request: Request, user: UserDep):
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        rows = db.scalars(
            select(ListingRow)
            .where(ListingRow.user_id == user.id)
            .order_by(ListingRow.created_at.desc())
            .limit(100)
        ).all()
    return render(request, "listings.html", rows=rows, user=user)


def _refresh_from_parcel(cad_num: str, data: dict[str, Any]) -> dict[str, Any]:
    """Факты, ЗОУИТ, рубрика и площадь — заново по участку из кэша; текст и заметки не трогаем.
    Если участок не достать (НСПД закрыт, кэш пуст) — данные остаются как были."""
    from landcheck.listing import ParcelFacts, build_draft

    try:
        parcel, _errors = _fetch_parcel_now(cad_num)
    except Exception as exc:  # noqa: BLE001 — пересборка текста не должна падать из-за НСПД
        log.warning("refresh parcel %s failed: %s", cad_num, exc)
        return data
    if not parcel.found:
        return data
    draft = build_draft(parcel, notes=data.get("notes") or "", price_rub=data.get("price_rub"))
    out = dict(data)
    out["facts"] = draft.facts
    out["zouit_notes"] = draft.zouit_notes
    out["land_area_sotka"] = draft.land_area_sotka
    out["address"] = draft.address or data.get("address")
    out["parcel_facts"] = ParcelFacts.from_parcel(parcel).to_dict()
    out["object_type"] = (
        draft.object_type if not draft.object_type else data.get("object_type") or draft.object_type
    )
    return out


def _compose_guards(pf: dict[str, Any], data: dict[str, Any]) -> tuple[list[str], list[str], str]:
    """Что модели нельзя: по фактам ЕГРН, а не по вере в промпт (прод 20.10)."""
    from landcheck.listing import normalize_category

    object_type = data.get("object_type") or ""
    if not normalize_category(pf.get("category")):
        object_type = ""  # рубрику не передаём: модель принимала её за назначение участка
    facts = [f for f in data.get("facts") or [] if "не установлен" not in f.lower()]
    forbidden: list[str] = []
    area = pf.get("best_area_m2")
    if area is not None and area < 50:
        # Площадь 1 м² в ЕГРН — ошибка данных: модель не должна её называть (прод 20.10:
        # «0,01 сотки» попало в текст и заблокировало выгрузку).
        facts = [f for f in facts if not f.startswith("Площадь")]
        forbidden.append("называть площадь участка в любом виде (в ЕГРН ошибка данных)")
    if not pf.get("permitted_use") and not pf.get("permitted_use_all"):
        # Модель брала «ИЖС» из рубрики Avito «Поселений (ИЖС)» (Клавдий, прод 20.10).
        forbidden.append(
            "называть назначение участка (ИЖС, садоводство, ЛПХ, «под дом»): "
            "ВРИ в ЕГРН не установлен"
        )
    return facts, forbidden, object_type


def _compose_with_ai(data: dict[str, Any]) -> dict[str, Any]:
    """Заголовок и текст через Gemini по фактам ЕГРН и заметкам; при ошибке данные не меняются."""
    from landcheck.packager import compose_listing

    if not settings.llm_enabled:
        return data
    pf = data.get("parcel_facts") or {}
    facts, forbidden, object_type = _compose_guards(pf, data)
    res = compose_listing(
        facts=facts,
        zouit_notes=list(data.get("zouit_notes") or []),
        forbidden=forbidden,
        notes=data.get("notes") or "",
        object_type=object_type,
        price_rub=data.get("price_rub"),
        fetched=(pf.get("fetched_at") or "")[:10],
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    if res.get("error"):
        log.warning("compose failed: %s", res["error"])
        data["composed_error"] = res["error"]
        return data
    if not object_type and not any(f.startswith("Площадь по ЕГРН ") for f in facts):
        # Ни рубрики, ни площади — модели нечего сказать в заголовке, она пишет
        # «Участок в районе с установленными границами» (прод 20.10). Заголовок — шаблонный.
        from landcheck.listing import _short_locality

        loc = _short_locality(data.get("address"))
        data["title"] = f"Земельный участок — {loc}" if loc else "Земельный участок"
    else:
        data["title"] = res["title"] or data.get("title")
    data["description"] = res["description"]
    data["composed_by"] = res["model"]
    data.pop("composed_error", None)
    data["warnings"] = _listing_warnings(data)
    return data


def _own_listing(db, user: User, listing_id: int):
    from landcheck.web.db import Listing as ListingRow

    row = db.get(ListingRow, listing_id)
    if row is None or (row.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    return row


@router.get("/listing/{listing_id}", response_class=HTMLResponse)
def listing_page(request: Request, user: UserDep, listing_id: int):
    with session() as db:
        row = _own_listing(db, user, listing_id)
        # Контроль противоречий пересчитывается при каждом просмотре: правила меняются,
        # а сохранённый список мог устареть (Клавдий, прод 19.10).
        fixed = _refresh_listing_data(row.data)
        changed = fixed is not None
        if fixed is not None:
            row.data = fixed
        fresh = _listing_warnings(row.data)
        if fresh != list(row.data.get("warnings") or []):
            row.data = {**row.data, "warnings": fresh}
            changed = True
        if changed:
            db.commit()
        return render(
            request,
            "listing.html",
            row=row,
            d=row.data,
            proofs=_proof_files(row.id),
            public_url=f"{settings.base_url}/l/{row.id}",
            err=request.query_params.get("err"),
            block_kind=_listing_block_kind(row.data),
            user=user,
        )


# --- подтверждение права (К2.3) и модерация ----------------------------------------------
PROOF_EXTS = {"pdf": b"%PDF", "jpg": b"\xff\xd8\xff", "png": b"\x89PNG\r\n\x1a\n"}
MAX_PROOF_BYTES = 10 * 1024 * 1024
MAX_PROOFS = 3


def _proofs_dir(listing_id: int) -> Path:
    return settings.db_path.parent / "proofs" / str(listing_id)


def _proof_kind_of(raw: bytes) -> str | None:
    if raw.startswith(b"%PDF"):
        return "pdf"
    kind = _image_kind(raw)
    return kind if kind in ("jpg", "png", "webp") else None


def _proof_files(listing_id: int) -> list[str]:
    d = _proofs_dir(listing_id)
    return sorted(p.name for p in d.iterdir() if p.is_file()) if d.exists() else []


def _delete_proofs(listing_id: int) -> None:
    d = _proofs_dir(listing_id)
    if d.exists():
        for p in d.iterdir():
            p.unlink()
        d.rmdir()


@router.post("/listing/{listing_id}/verify")
async def listing_verify(
    request: Request,
    user: UserDep,
    listing_id: int,
    proof_kind: Annotated[str, Form()] = "owner",
    proofs: Annotated[list[UploadFile] | None, File()] = None,
):
    """Продавец отправляет подтверждение права: файлы в приватное хранилище, статус «на проверке».

    Повторная отправка заменяет прежние файлы.
    """
    from datetime import UTC, datetime

    if proof_kind not in PROOF_KINDS:
        proof_kind = "owner"
    saved: list[str] = []
    skipped: list[str] = []
    with session() as db:
        row = _own_listing(db, user, listing_id)
        if row.status == "verified":
            return RedirectResponse(f"/listing/{listing_id}", status_code=303)
        _delete_proofs(row.id)
        d = _proofs_dir(row.id)
        for f in (proofs or [])[:MAX_PROOFS]:
            if not f.filename:
                continue
            raw = await f.read(MAX_PROOF_BYTES + 1)
            kind = _proof_kind_of(raw)
            if kind is None or len(raw) > MAX_PROOF_BYTES:
                skipped.append(f.filename)
                continue
            d.mkdir(parents=True, exist_ok=True)
            name = f"{len(saved) + 1:02d}.{kind}"
            (d / name).write_bytes(raw)
            saved.append(name)
        if not saved:
            msg = "Нужен хотя бы один файл PDF, JPG или PNG до 10 МБ."
            if skipped:
                msg += " Не приняты: " + ", ".join(skipped)
            return render(request, "error.html", message=msg, user=user, status_code=400)
        row.status = "pending"
        row.proof_kind = proof_kind
        row.submitted_at = datetime.now(UTC)
        row.moderation_note = None
        row.published = False
        data = dict(row.data)
        data["skipped_proofs"] = skipped
        row.data = data
        db.commit()
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


@router.get("/listing/{listing_id}/proof/{name}")
def listing_proof(user: UserDep, listing_id: int, name: str):
    """Файл подтверждения видят только владелец и модератор."""
    with session() as db:
        _own_listing(db, user, listing_id)
    safe = Path(name).name
    p = _proofs_dir(listing_id) / safe
    if safe != name or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@router.post("/listing/{listing_id}/publish")
def listing_publish(user: UserDep, listing_id: int, action: Annotated[str, Form()] = "on"):
    """Публикация в базу: только после подтверждения права и без противоречий с кадастром."""
    err = None
    with session() as db:
        row = _own_listing(db, user, listing_id)
        if action == "off":
            row.published = False
        elif row.status != "verified":
            err = "not_verified"
        elif _listing_warnings(row.data):
            err = "warnings"
        else:
            row.published = True
            _notify_buyers_about(db, row)
        db.commit()
    suffix = f"?err={err}" if err else ""
    return RedirectResponse(f"/listing/{listing_id}{suffix}", status_code=303)


def _notify_buyers_about(db, row) -> int:
    """Покупатели с подходящими запросами узнают о новом участке в базе (п.5 плана 20.10).
    Один участок — одно уведомление на запрос."""
    from landcheck.buyer import match_listing
    from landcheck.web.db import BuyerRequest
    from landcheck.web.notify import already_notified, notify

    owner = db.get(User, row.user_id)
    card = _market_card(row, owner)
    d = row.data or {}
    text = f"{d.get('description') or ''} {d.get('notes') or ''}"
    sent = 0
    for req in db.scalars(select(BuyerRequest).order_by(BuyerRequest.id.desc()).limit(500)):
        if req.user_id == row.user_id:
            continue
        m = match_listing(
            card,
            text=text,
            purpose=_purpose_of(req.purpose),
            budget_rub=req.budget_rub,
            min_area_m2=req.min_area_m2,
            max_area_m2=req.max_area_m2,
            region=req.region,
            answers=dict(req.answers or {}),
        )
        if not m:
            continue
        url = f"/search/{req.id}?l={row.id}"  # один участок — одно уведомление на запрос
        if already_notified(db, req.user_id, "match", url):
            continue
        price = ""
        if d.get("price_rub"):
            price = ", " + f"{d['price_rub']:,.0f}".replace(",", " ") + " ₽"
        verdict = (m.get("verdict") or {}).get("text") or m.get("level") or ""
        notify(
            db,
            req.user_id,
            "match",
            f"Новый участок под ваш запрос «{(req.text or '').strip()[:60] or 'без текста'}»: "
            f"{card.get('title') or row.cad_num}{price}."
            + (f" Под цель: {verdict}." if verdict else ""),
            url,
        )
        sent += 1
    return sent


def _market_card(row, owner: User | None) -> dict[str, Any]:
    d = row.data or {}
    pf = d.get("parcel_facts") or {}
    return {
        "id": row.id,
        "cad_num": row.cad_num,
        "title": d.get("title") or row.cad_num,
        "price_rub": d.get("price_rub"),
        "area_sotka": d.get("land_area_sotka"),
        "address": d.get("address"),
        "object_type": d.get("object_type"),
        "category": pf.get("category"),
        "permitted_use": pf.get("permitted_use"),
        "image": (d.get("images") or [None])[0],
        "verified_at": row.verified_at,
        "proof_kind": row.proof_kind,
        "seller": (owner.brand or owner.name) if owner else None,
        "price_per_sotka": (
            round(d["price_rub"] / d["land_area_sotka"])
            if d.get("price_rub") and d.get("land_area_sotka")
            else None
        ),
    }


def _public_listings(
    db,
    *,
    region: str = "",
    kind: str = "",
    price_max: float | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
):
    from landcheck.web.db import Listing as ListingRow

    rows = db.scalars(
        select(ListingRow)
        .where(ListingRow.status == "verified", ListingRow.published.is_(True))
        .order_by(ListingRow.verified_at.desc(), ListingRow.id.desc())
        .limit(500)
    ).all()
    owners = {u.id: u for u in db.scalars(select(User)).all()}
    cards = []
    for r in rows:
        c = _market_card(r, owners.get(r.user_id))
        hay = f"{c['address'] or ''} {c['title']}".lower()
        if region and region.lower() not in hay:
            continue
        if kind and (c["object_type"] or "") != kind:
            continue
        if price_max is not None and (c["price_rub"] is None or c["price_rub"] > price_max):
            continue
        if area_min is not None and (c["area_sotka"] is None or c["area_sotka"] < area_min):
            continue
        if area_max is not None and (c["area_sotka"] is None or c["area_sotka"] > area_max):
            continue
        cards.append(c)
    return cards


@router.get("/market", response_class=HTMLResponse)
def market(
    request: Request,
    region: str = "",
    kind: str = "",
    price_max: str = "",
    area_min: str = "",
    area_max: str = "",
    ref: str = "",
):
    """Витрина: только объявления с подтверждённым правом, публично, без входа."""
    from landcheck.inputs import parse_number
    from landcheck.listing import AVITO_OBJECT_TYPES

    def num(v: str) -> float | None:
        return parse_number(v) if v.strip() else None

    pm = num(price_max)
    if pm is not None and pm < 1000:
        pm *= 1_000_000
    with session() as db:
        cards = _public_listings(
            db,
            region=region.strip(),
            kind=kind,
            price_max=pm,
            area_min=num(area_min),
            area_max=num(area_max),
        )
    resp = render(
        request,
        "market.html",
        cards=cards,
        kinds=list(AVITO_OBJECT_TYPES.values()),
        f={
            "region": region,
            "kind": kind,
            "price_max": price_max,
            "area_min": area_min,
            "area_max": area_max,
        },
    )
    _remember_ref(request, resp, ref)
    return resp


@router.get("/l/{listing_id}", response_class=HTMLResponse)
def listing_public(request: Request, listing_id: int):
    """Публичная страница объявления: факты НСПД, фото, цена, продавец, отметка о праве."""
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        row = db.get(ListingRow, listing_id)
        if row is None or not row.is_public:
            raise HTTPException(404)
        owner = db.get(User, row.user_id)
        me = current_user(request)
        my_conn = None
        if me is not None and me.id != row.user_id:
            from landcheck.web.db import Connection

            my_conn = db.scalar(
                select(Connection).where(
                    Connection.listing_id == row.id, Connection.from_user_id == me.id
                )
            )
        return render(
            request,
            "listing_public.html",
            row=row,
            d=row.data,
            card=_market_card(row, owner),
            owner=owner,
            logo_url=logo_url(row.user_id) if owner else None,
            my_conn=my_conn,
            is_owner=bool(me is not None and me.id == row.user_id),
        )


# --- кабинет покупателя (К1, М4): запрос → уточняющие вопросы → подборка из базы -----------
def _request_ctx(db, user: User, req) -> dict[str, Any]:
    """Подборка под запрос из базы верифицированных объявлений."""
    from datetime import UTC, datetime

    from landcheck.buyer import rank
    from landcheck.web.db import Favorite
    from landcheck.web.db import Listing as ListingRow

    cards = _public_listings(db)
    texts = {}
    verified_at = {}
    for r in db.scalars(
        select(ListingRow).where(ListingRow.status == "verified", ListingRow.published.is_(True))
    ).all():
        d = r.data or {}
        texts[r.id] = f"{d.get('description') or ''} {d.get('notes') or ''}"
        verified_at[r.id] = r.verified_at
    matches = rank(
        cards,
        texts,
        purpose=_purpose_of(req.purpose),
        budget_rub=req.budget_rub,
        min_area_m2=req.min_area_m2,
        max_area_m2=req.max_area_m2,
        region=req.region,
        answers=dict(req.answers or {}),
    )
    seen = req.seen_at
    for m in matches:
        v = verified_at.get(m["id"])
        m["is_new"] = bool(seen and v and v > seen)
    saved = {
        f.listing_id for f in db.scalars(select(Favorite).where(Favorite.user_id == user.id)).all()
    }
    req.seen_at = datetime.now(UTC)
    db.commit()
    return {"matches": matches, "saved": saved}


def _purpose_of(value: str | None) -> Purpose:
    try:
        return Purpose(value or "any")
    except ValueError:
        return Purpose.ANY


@router.get("/search", response_class=HTMLResponse)
def search_form(request: Request, user: UserDep):
    from landcheck.web.db import BuyerRequest

    with session() as db:
        reqs = db.scalars(
            select(BuyerRequest)
            .where(BuyerRequest.user_id == user.id)
            .order_by(BuyerRequest.created_at.desc())
            .limit(20)
        ).all()
    return render(request, "search.html", purposes=PURPOSES, reqs=reqs, Purpose=Purpose, user=user)


@router.post("/search")
def search_create(
    user: UserDep,
    query: Annotated[str, Form()] = "",
    purpose: Annotated[str, Form()] = "any",
    budget: Annotated[str, Form()] = "",
    min_area: Annotated[str, Form()] = "",
    max_area: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
):
    from landcheck.buyer import region_from_text
    from landcheck.inputs import parse_number
    from landcheck.web.db import BuyerRequest

    req = parse_request(query) if query.strip() else CheckRequest()
    if purpose and purpose != Purpose.ANY.value:
        req.purpose = _purpose_of(purpose)
    if budget.strip():
        v = parse_number(budget)
        if v is not None and v < 1000:
            v *= 1_000_000
        req.budget_rub = v
    for name, val in (("min_area_m2", min_area), ("max_area_m2", max_area)):
        if val.strip():
            try:
                setattr(req, name, float(val.replace(",", ".")) * SOTKA_M2)
            except ValueError:
                pass
    reg = region.strip() or (region_from_text(query) or "")
    with session() as db:
        row = BuyerRequest(
            user_id=user.id,
            text=query.strip(),
            purpose=req.purpose.value,
            budget_rub=req.budget_rub,
            min_area_m2=req.min_area_m2,
            max_area_m2=req.max_area_m2,
            region=reg[:120] or None,
            answers={},
        )
        db.add(row)
        db.commit()
        rid = row.id
    return RedirectResponse(f"/search/{rid}", status_code=303)


def _own_request(db, user: User, rid: int):
    from landcheck.web.db import BuyerRequest

    row = db.get(BuyerRequest, rid)
    if row is None or (row.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    return row


@router.get("/search/{rid}", response_class=HTMLResponse)
def search_page(request: Request, user: UserDep, rid: int, edit: int = 0):
    from landcheck.buyer import questions_for

    with session() as db:
        req = _own_request(db, user, rid)
        purpose = _purpose_of(req.purpose)
        qs = questions_for(purpose)
        if edit or not req.answers:
            return render(
                request,
                "search_questions.html",
                req=req,
                purpose=purpose,
                questions=qs,
                answers=dict(req.answers or {}),
                user=user,
            )
        ctx = _request_ctx(db, user, req)
        return render(
            request,
            "search_results.html",
            req=req,
            purpose=purpose,
            questions=qs,
            answers=dict(req.answers or {}),
            user=user,
            **ctx,
        )


@router.post("/search/{rid}/answers")
async def search_answers(request: Request, user: UserDep, rid: int):
    from landcheck.buyer import questions_for

    form = await request.form()
    with session() as db:
        req = _own_request(db, user, rid)
        allowed = {
            q["key"]: {v for v, _ in q["options"]} for q in questions_for(_purpose_of(req.purpose))
        }
        answers = {}
        for key, values in allowed.items():
            v = form.get(key)
            if isinstance(v, str) and v in values:
                answers[key] = v
        req.answers = answers or {"skipped": "1"}
        db.commit()
    return RedirectResponse(f"/search/{rid}", status_code=303)


@router.post("/search/{rid}/delete")
def search_delete(user: UserDep, rid: int):
    with session() as db:
        req = _own_request(db, user, rid)
        db.delete(req)
        db.commit()
    return RedirectResponse("/search", status_code=303)


@router.post("/favorites/{listing_id}")
def favorite_toggle(user: UserDep, listing_id: int, back: Annotated[str, Form()] = "/saved"):
    from landcheck.web.db import Favorite
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        row = db.get(ListingRow, listing_id)
        if row is None or not row.is_public:
            raise HTTPException(404)
        fav = db.scalar(
            select(Favorite).where(Favorite.user_id == user.id, Favorite.listing_id == listing_id)
        )
        if fav is None:
            db.add(Favorite(user_id=user.id, listing_id=listing_id))
        else:
            db.delete(fav)
        db.commit()
    target = back if back.startswith("/") and not back.startswith("//") else "/saved"
    return RedirectResponse(target, status_code=303)


@router.get("/saved", response_class=HTMLResponse)
def saved_page(request: Request, user: UserDep, rid: int | None = None):
    """Сохранённые объекты и сравнение: цена, ₽/сот., площадь, вердикт под цель, что важно."""
    from landcheck.buyer import match_listing
    from landcheck.web.db import BuyerRequest, Favorite
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        favs = db.scalars(
            select(Favorite).where(Favorite.user_id == user.id).order_by(Favorite.created_at)
        ).all()
        req = db.get(BuyerRequest, rid) if rid else None
        if req is None:
            req = db.scalar(
                select(BuyerRequest)
                .where(BuyerRequest.user_id == user.id)
                .order_by(BuyerRequest.created_at.desc())
            )
        owners = {u.id: u for u in db.scalars(select(User)).all()}
        rows = []
        for f in favs:
            lr = db.get(ListingRow, f.listing_id)
            if lr is None or not lr.is_public:
                continue
            card = _market_card(lr, owners.get(lr.user_id))
            d = lr.data or {}
            m = match_listing(
                card,
                purpose=_purpose_of(req.purpose) if req else Purpose.ANY,
                budget_rub=None,
                min_area_m2=None,
                max_area_m2=None,
                region=None,
                answers=dict(req.answers or {}) if req else {},
                text=f"{d.get('description') or ''} {d.get('notes') or ''}",
            )
            rows.append(m or card)
        purpose = _purpose_of(req.purpose) if req else Purpose.ANY
    return render(request, "saved.html", rows=rows, req=req, purpose=purpose, user=user)


# --- инфопартнёр (К4): ссылка, два уровня, статистика ------------------------------------
@router.get("/partner", response_class=HTMLResponse)
def partner_page(request: Request, user: UserDep):
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        u = db.get(User, user.id)
        assert u is not None
        code = ensure_ref_code(db, u)
        level1 = db.scalars(select(User).where(User.referred_by == u.id)).all()
        l1_ids = [x.id for x in level1]
        level2 = (
            db.scalars(select(User).where(User.referred_by.in_(l1_ids))).all() if l1_ids else []
        )
        ids = l1_ids + [x.id for x in level2]
        checks = 0
        listings_verified = 0
        if ids:
            checks = db.scalar(select(func.count(Check.id)).where(Check.user_id.in_(ids))) or 0
            listings_verified = (
                db.scalar(
                    select(func.count(ListingRow.id)).where(
                        ListingRow.user_id.in_(ids), ListingRow.status == "verified"
                    )
                )
                or 0
            )

        def by_role(users):
            out: dict[str, int] = {}
            for x in users:
                out[x.role_label] = out.get(x.role_label, 0) + 1
            return out

        return render(
            request,
            "partner.html",
            code=code,
            link=f"{settings.base_url}/login?ref={code}",
            market_link=f"{settings.base_url}/market?ref={code}",
            level1=by_role(level1),
            level2=by_role(level2),
            n1=len(level1),
            n2=len(level2),
            checks=checks,
            listings_verified=listings_verified,
            user=user,
        )


# --- уведомления (план 20.10, п.5) ---------------------------------------------------------
@router.post("/tg/webhook/{secret}")
async def telegram_webhook(request: Request, secret: str):
    """Вебхук Telegram: адрес с секретом из токена; тело — update."""
    from landcheck.web.notify import handle_update, webhook_secret

    expected = webhook_secret()
    if not expected or secret != expected:
        raise HTTPException(404)
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}
    with session() as db:
        result = handle_update(db, update if isinstance(update, dict) else {})
    log.info("telegram update: %s", result)
    return {"ok": True}


@router.post("/profile/telegram/off")
def telegram_off(user: UserDep):
    with session() as db:
        u = db.get(User, user.id)
        if u is not None:
            u.telegram_chat_id = None
            db.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, user: UserDep):
    from landcheck.web.db import Notification
    from landcheck.web.notify import KIND_LABELS, mark_all_read

    with session() as db:
        rows = db.scalars(
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(Notification.id.desc())
            .limit(100)
        ).all()
        unread_ids = {n.id for n in rows if n.read_at is None}
        mark_all_read(db, user.id)
        db.commit()
    return render(
        request,
        "notifications.html",
        rows=rows,
        unread_ids=unread_ids,
        labels=KIND_LABELS,
        unread=0,
        user=user,
    )


# --- межагентский контур (К3): запрос контакта по объявлению ------------------------------
@router.post("/l/{listing_id}/connect")
def connection_create(user: UserDep, listing_id: int, note: Annotated[str, Form()] = ""):
    from landcheck.web.db import Connection
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        row = db.get(ListingRow, listing_id)
        if row is None or not row.is_public:
            raise HTTPException(404)
        if row.user_id == user.id:
            return RedirectResponse(f"/l/{listing_id}", status_code=303)
        exists = db.scalar(
            select(Connection).where(
                Connection.listing_id == listing_id, Connection.from_user_id == user.id
            )
        )
        if exists is None:
            db.add(
                Connection(
                    listing_id=listing_id,
                    from_user_id=user.id,
                    to_user_id=row.user_id,
                    note=note.strip()[:1000],
                )
            )
            from landcheck.web.notify import notify

            who = user.brand or user.name
            title = (row.data or {}).get("title") or row.cad_num
            note_clean = note.strip()[:200].rstrip(".!?… ")
            notify(
                db,
                row.user_id,
                "connect",
                f"{who} запрашивает контакт по объявлению «{title}»"
                + (f": «{note_clean}»" if note_clean else "")
                + ". Примите или отклоните в разделе «Запросы контакта».",
                "/connections",
            )
            db.commit()
    return RedirectResponse(f"/l/{listing_id}", status_code=303)


@router.post("/connections/{conn_id}/decide")
def connection_decide(user: UserDep, conn_id: int, decision: Annotated[str, Form()]):
    from datetime import UTC, datetime

    from landcheck.web.db import Connection

    with session() as db:
        conn = db.get(Connection, conn_id)
        if conn is None or conn.to_user_id != user.id:
            raise HTTPException(404)
        if decision in ("accepted", "declined"):
            conn.status = decision
            conn.decided_at = datetime.now(UTC)
            from landcheck.web.db import Listing as ListingRow
            from landcheck.web.notify import notify

            lst = db.get(ListingRow, conn.listing_id)
            title = ((lst.data or {}).get("title") if lst else None) or (
                lst.cad_num if lst else f"#{conn.listing_id}"
            )
            verdict = "принял" if decision == "accepted" else "отклонил"
            has_contact = bool(
                user.contact or user.email or (lst and (lst.data or {}).get("phone"))
            )
            if decision != "accepted":
                tail = "."
            elif has_contact:
                tail = ". Контакты открыты в разделе «Запросы контакта»."
            else:
                tail = ", но контакт в профиле не указан — мы попросили его заполнить."
            notify(
                db,
                conn.from_user_id,
                "decision",
                f"Владелец {verdict} ваш запрос контакта по объявлению «{title}»" + tail,
                "/connections",
            )
            if decision == "accepted" and not has_contact:
                # Принял, а показать нечего (прод 21.10: «Контакт: —») — подсказать владельцу
                notify(
                    db,
                    user.id,
                    "system",
                    "Вы приняли запрос контакта, но у вас не указаны ни контакт, ни email, "
                    "ни телефон в объявлении — второй стороне нечего показать. Заполните "
                    "контакт в профиле.",
                    "/profile",
                )
            db.commit()
    return RedirectResponse("/connections", status_code=303)


@router.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request, user: UserDep):
    from landcheck.web.db import Connection
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        incoming = db.scalars(
            select(Connection)
            .where(Connection.to_user_id == user.id)
            .order_by(Connection.created_at.desc())
        ).all()
        outgoing = db.scalars(
            select(Connection)
            .where(Connection.from_user_id == user.id)
            .order_by(Connection.created_at.desc())
        ).all()
        users = {u.id: u for u in db.scalars(select(User)).all()}
        listings = {r.id: r for r in db.scalars(select(ListingRow)).all()}
        return render(
            request,
            "connections.html",
            incoming=incoming,
            outgoing=outgoing,
            users=users,
            listings=listings,
            user=user,
        )


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(404)
    return user


AdminDep = Annotated[User, Depends(require_admin)]


def _aware(dt):
    from datetime import UTC

    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


@router.get("/admin", response_class=HTMLResponse)
def admin_overview(request: Request, admin: AdminDep):
    """Вся картина целиком: люди, проверки, объявления, запросы, связи, ИИ. Только администратор."""
    from collections import Counter
    from datetime import UTC, datetime, timedelta

    from landcheck.web.db import BuyerRequest, Connection, Favorite, Invite
    from landcheck.web.db import Listing as ListingRow

    now = datetime.now(UTC)
    week = now - timedelta(days=7)
    with session() as db:
        users = db.scalars(select(User).order_by(User.created_at.desc())).all()
        names = {u.id: (u.brand or u.name) for u in users}
        checks = db.scalars(select(Check).order_by(Check.created_at.desc())).all()
        listings = db.scalars(select(ListingRow).order_by(ListingRow.id.desc())).all()
        requests = db.scalars(select(BuyerRequest).order_by(BuyerRequest.created_at.desc())).all()
        connections = db.scalars(select(Connection).order_by(Connection.id.desc())).all()
        favorites = db.scalar(select(func.count(Favorite.id))) or 0
        items_total = db.scalar(select(func.count(CheckItem.id))) or 0
        invites_free = db.scalar(select(func.count(Invite.id)).where(Invite.used_by.is_(None))) or 0
    stats = {
        "users": len(users),
        "users_week": sum(1 for u in users if _aware(u.created_at) >= week),
        "users_by_role": Counter(u.role for u in users),
        "admins": sum(1 for u in users if u.is_admin),
        "checks": len(checks),
        "checks_week": sum(1 for c in checks if _aware(c.created_at) >= week),
        "checks_by_status": Counter(c.status for c in checks),
        "parcels": items_total,
        "listings": len(listings),
        "listings_by_status": Counter(r.status for r in listings),
        "published": sum(1 for r in listings if r.published),
        "composed_by": Counter((r.data or {}).get("composed_by") or "шаблон" for r in listings),
        "requests": len(requests),
        "requests_week": sum(1 for r in requests if _aware(r.created_at) >= week),
        "connections_by_status": Counter(c.status for c in connections),
        "favorites": favorites,
        "invites_free": invites_free,
    }
    return render(
        request,
        "admin_overview.html",
        stats=stats,
        names=names,
        users=users[:10],
        checks=checks[:12],
        listings=listings[:12],
        requests=requests[:10],
        connections=connections[:10],
        user=admin,
    )


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, admin: AdminDep):
    from collections import Counter

    from landcheck.web.db import BuyerRequest
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        users = db.scalars(select(User).order_by(User.created_at.desc())).all()
        checks = Counter(db.scalars(select(Check.user_id)).all())
        listings = Counter(db.scalars(select(ListingRow.user_id)).all())
        requests = Counter(db.scalars(select(BuyerRequest.user_id)).all())
        referred = Counter(u.referred_by for u in users if u.referred_by)
    return render(
        request,
        "admin_users.html",
        users=users,
        checks=checks,
        listings=listings,
        requests=requests,
        referred=referred,
        user=admin,
    )


@router.get("/admin/listings", response_class=HTMLResponse)
def admin_listings(request: Request, admin: AdminDep, status: str = "pending"):
    from landcheck.web.db import Listing as ListingRow

    if status not in LISTING_STATUSES and status != "all":
        status = "pending"
    with session() as db:
        q = select(ListingRow).order_by(ListingRow.submitted_at.desc(), ListingRow.id.desc())
        if status != "all":
            q = q.where(ListingRow.status == status)
        rows = db.scalars(q.limit(200)).all()
        owners = {u.id: u for u in db.scalars(select(User)).all()}
        counts = {
            st: db.scalar(select(func.count(ListingRow.id)).where(ListingRow.status == st)) or 0
            for st in LISTING_STATUSES
        }
    return render(
        request,
        "admin_listings.html",
        rows=rows,
        owners=owners,
        status=status,
        counts=counts,
        proofs={r.id: _proof_files(r.id) for r in rows},
        user=admin,
    )


@router.post("/admin/listings/{listing_id}/decide")
def admin_decide(
    admin: AdminDep,
    listing_id: int,
    decision: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
):
    """Решение модератора: право подтверждено или отклонено; файлы подтверждения удаляются."""
    from datetime import UTC, datetime

    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        row = db.get(ListingRow, listing_id)
        if row is None:
            raise HTTPException(404)
        if decision == "approve":
            row.status = "verified"
            row.verified_at = datetime.now(UTC)
        elif decision == "reject":
            row.status = "rejected"
            row.published = False
        else:
            raise HTTPException(400)
        row.moderation_note = note.strip()[:1000] or None
        from landcheck.web.notify import notify

        title = (row.data or {}).get("title") or row.cad_num
        if decision == "approve":
            msg = f"Право на участок «{title}» подтверждено: можно публиковать в базу."
        else:
            msg = f"Подтверждение права по «{title}» отклонено" + (
                f": {row.moderation_note}" if row.moderation_note else "."
            )
        notify(db, row.user_id, "moderation", msg, f"/listing/{row.id}")
        db.commit()
    _delete_proofs(listing_id)
    return RedirectResponse("/admin/listings", status_code=303)


@router.post("/listing/{listing_id}")
def listing_update(
    user: UserDep,
    listing_id: int,
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    price: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    from landcheck.inputs import parse_number

    with session() as db:
        row = _own_listing(db, user, listing_id)
        data = dict(row.data)
        data["title"] = title.strip() or data["title"]
        data["description"] = description.strip() or data["description"]
        if notes.strip():
            data["notes"] = notes.strip()
        if price.strip():
            v = parse_number(price)
            data["price_rub"] = v * 1_000_000 if v is not None and v < 1000 else v
        data["phone"] = phone.strip()
        data["warnings"] = _listing_warnings(data)
        row.data = data
        unpublished = bool(row.published and data["warnings"])
        if unpublished:
            row.published = False
        db.commit()
    suffix = "?err=unpublished" if unpublished else ""
    return RedirectResponse(f"/listing/{listing_id}{suffix}", status_code=303)


@router.get("/listing/{listing_id}/avito.xml")
def listing_xml(request: Request, user: UserDep, listing_id: int):
    from landcheck.listing import Draft, avito_xml

    with session() as db:
        row = _own_listing(db, user, listing_id)
        d = row.data
        problems = _listing_warnings(d)
        if problems:
            return render(
                request,
                "error.html",
                message=(
                    "Выгрузка заблокирована: в тексте есть утверждения, "
                    "противоречащие кадастру. " + " · ".join(problems)
                ),
                user=user,
                status_code=409,
            )
        draft = Draft(
            cad_num=row.cad_num,
            title=d["title"],
            description=d["description"],
            object_type=d["object_type"],
            land_area_sotka=d.get("land_area_sotka"),
            price_rub=d.get("price_rub"),
            address=d.get("address"),
        )
        xml = avito_xml(
            draft,
            ad_id=f"landcheck-{row.id}",
            phone=d.get("phone") or None,
            manager=user.name,
            image_urls=[settings.base_url + p for p in d.get("images", [])]
            if settings.base_url
            else None,
        )
    return Response(
        xml.encode("utf-8"),
        media_type="application/xml; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=avito-{row.cad_num.replace(':', '_')}.xml"
        },
    )


# --- голосовой ввод (ТЗ v2, М1.3) -------------------------------------------------------
@router.post("/asr")
async def asr(user: UserDep, audio: Annotated[UploadFile, File()]):
    """Расшифровка записи с микрофона: WAV/WebM/OGG до 60 с → текст для поля формы.

    Тот же ключ Gemini, что у модуля «Регион»; без ключа честно отвечаем, что выключено.
    Ответ всегда JSON {"text", "error"} — клиент показывает ошибку рядом с кнопкой.
    """
    from fastapi.responses import JSONResponse

    from landcheck.asr import MAX_AUDIO_BYTES, transcribe

    if not settings.asr_server_enabled:
        return JSONResponse(
            {
                "text": "",
                "error": "серверное распознавание выключено: нет ключа GROQ_API_KEY "
                "(бесплатно) и локальной модели",
            },
            status_code=503,
        )
    raw = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(raw) > MAX_AUDIO_BYTES:
        return JSONResponse({"text": "", "error": "запись слишком длинная"}, status_code=413)
    result = transcribe(
        raw,
        audio.content_type or "",
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    if result.get("error"):
        log.warning("asr failed for user %s: %s", user.id, result["error"])
        return JSONResponse({"text": "", "error": result["error"]}, status_code=502)
    if result.get("silent"):
        return {"text": "", "model": result["model"], "silent": True}
    return {"text": result["text"], "model": result["model"]}


@router.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__, "fixtures": settings.fixtures is not None}
