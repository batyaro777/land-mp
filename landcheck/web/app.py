"""FastAPI-приложение кабинета брокера."""

from __future__ import annotations

import io
import logging
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
    ROLES,
    Check,
    CheckItem,
    User,
    authenticate,
    init_db,
    normalize_email,
    redeem_invite,
    register_user,
    session,
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
    ROLES=ROLES, PROOF_KINDS=PROOF_KINDS, LISTING_STATUSES=LISTING_STATUSES
)


def create_app() -> FastAPI:
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
    ctx.setdefault("voice_enabled", bool(settings.gemini_api_key))
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
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None, tab: str = "password"):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=error, tab=tab, user=None)


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
):
    """Регистрация без кода (ТЗ v2, R3): email, пароль, имя, роль."""
    if (resp := _throttled(request)) is not None:
        return resp
    with session() as db:
        user, error = register_user(db, email, password, name, role, brand_name)
    if user is None:
        record_failed_attempt(request.client.host if request.client else "?")
        return render(request, "login.html", error=error, tab="register", user=None)
    return _login_response(user)


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --- dashboard & checks -------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: UserDep):
    if user.role == "seller":
        return RedirectResponse("/listings", status_code=303)
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
        "location_url": f"/checks/{check.id}/items/{item.cad_num}/location",
        "region": region,
        "region_url": f"/checks/{check.id}/items/{item.cad_num}/region",
        "region_enabled": bool(settings.gemini_api_key),
        "checklist": CHECKLIST,
        "price": price_orientation(data, (check.stats or {}).get("median_price_per_sotka")),
        "price_pos": price_position(data, median),
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
            data["location"] = loc
            item.data = data
            db.commit()
        return render(
            request,
            "_location.html",
            loc=loc,
            public=False,
            location_url=f"/checks/{check.id}/items/{cad_num}/location",
            region=data.get("region"),
            user=user,
            **_loc_extras(loc, data.get("region")),
        )


@router.get("/checks/{check_id}/items/{cad_num}/region", response_class=HTMLResponse)
def report_region(request: Request, user: UserDep, check_id: int, cad_num: str, refresh: int = 0):
    """Блок «Регион» по запросу (HTMX): Gemini с поиском Google по локации участка."""
    from landcheck.region import locality_from_address

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
        locality = locality_from_address(parcel.get("address"))
        if region is None or refresh or region.get("error"):
            if locality is None:
                region = {
                    "sections": [],
                    "sources": [],
                    "error": "у участка нет адреса в НСПД, локацию не определить",
                    "locality": None,
                }
            else:
                region = jobs.fetch_region(locality, _check_purpose(check), refresh=bool(refresh))
                region["locality"] = locality
            data["region"] = region
            item.data = data
            db.commit()
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
    return render(
        request, "profile.html", user=user, logo_url=logo_url(user.id), saved=saved, error=error
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
        if role in ROLES:
            u.role = role
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
def single_form(request: Request, user: UserDep):
    return render(request, "single.html", purposes=PURPOSES, user=user)


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
    from landcheck.listing import ParcelFacts, contradictions, static_warnings

    pf_raw = data.get("parcel_facts")
    if not pf_raw:
        return list(data.get("warnings") or [])
    pf = ParcelFacts.from_dict(pf_raw)
    text = f"{data.get('title') or ''}\n{data.get('description') or ''}"
    return contradictions(pf, text, data.get("price_rub")) + static_warnings(pf)


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
    """Пересобрать текст ИИ по новым заметкам продавца."""
    with session() as db:
        row = _own_listing(db, user, listing_id)
        data = dict(row.data)
        data["notes"] = notes.strip() or data.get("notes") or ""
        row.data = _compose_with_ai(data)
        db.commit()
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


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


def _compose_with_ai(data: dict[str, Any]) -> dict[str, Any]:
    """Заголовок и текст через Gemini по фактам ЕГРН и заметкам; при ошибке данные не меняются."""
    from landcheck.packager import compose_listing

    if not settings.gemini_api_key:
        return data
    pf = data.get("parcel_facts") or {}
    res = compose_listing(
        facts=list(data.get("facts") or []),
        zouit_notes=list(data.get("zouit_notes") or []),
        notes=data.get("notes") or "",
        object_type=data.get("object_type") or "",
        price_rub=data.get("price_rub"),
        fetched=(pf.get("fetched_at") or "")[:10],
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    if res.get("error"):
        log.warning("compose failed: %s", res["error"])
        data["composed_error"] = res["error"]
        return data
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
        return render(
            request,
            "listing.html",
            row=row,
            d=row.data,
            proofs=_proof_files(row.id),
            public_url=f"{settings.base_url}/l/{row.id}",
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
    with session() as db:
        row = _own_listing(db, user, listing_id)
        if action == "off":
            row.published = False
        elif row.status == "verified" and not _listing_warnings(row.data):
            row.published = True
        db.commit()
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


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
    return render(
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


@router.get("/l/{listing_id}", response_class=HTMLResponse)
def listing_public(request: Request, listing_id: int):
    """Публичная страница объявления: факты НСПД, фото, цена, продавец, отметка о праве."""
    from landcheck.web.db import Listing as ListingRow

    with session() as db:
        row = db.get(ListingRow, listing_id)
        if row is None or not row.is_public:
            raise HTTPException(404)
        owner = db.get(User, row.user_id)
        return render(
            request,
            "listing_public.html",
            row=row,
            d=row.data,
            card=_market_card(row, owner),
            owner=owner,
            logo_url=logo_url(row.user_id) if owner else None,
        )


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(404)
    return user


AdminDep = Annotated[User, Depends(require_admin)]


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
        db.commit()
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


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

    if not settings.gemini_api_key:
        return JSONResponse(
            {"text": "", "error": "голосовой ввод выключен: нет ключа Gemini"}, status_code=503
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
