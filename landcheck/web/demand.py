"""Спрос (R11): запрос покупателя — объект рынка. Витрина обезличенных запросов для продавцов
и брокеров, отклик «предложить участок под запрос» с вердиктом под цель покупателя, контакты
обеих сторон после согласия покупателя. Контакты покупателей — крючок монетизации
(`LANDCHECK_DEMAND_PAID`)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from landcheck.buyer import match_listing, questions_for
from landcheck.models import SOTKA_M2, Purpose
from landcheck.web.config import settings
from landcheck.web.db import BuyerRequest, Listing, Offer, User
from landcheck.web.notify import notify

ACTIVE_DAYS = 90  # запрос старше — не показываем: покупатель, скорее всего, уже решил вопрос
# В тексте запроса покупатель мог оставить телефон или почту — на витрине их не показываем
_CONTACT_RE = re.compile(r"(\+?\d[\d\s\-()]{8,}\d|[\w.+-]+@[\w-]+\.[\w.]+)")


def purpose_of(value: str | None) -> Purpose:
    try:
        return Purpose(value or "any")
    except ValueError:
        return Purpose.ANY


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def wants(req: BuyerRequest) -> list[str]:
    """Ответы на уточняющие вопросы словами: «Газ: обязателен», «Для чего: постоянно»."""
    out: list[str] = []
    answers = req.answers or {}
    for q in questions_for(purpose_of(req.purpose)):
        v = answers.get(q["key"])
        if not v:
            continue
        for val, label in q["options"]:
            if val == v:
                out.append(f"{q['text'].rstrip('?')}: {label.lower()}")
    return out


def request_card(req: BuyerRequest) -> dict[str, Any]:
    """Обезличенная карточка запроса: ни имени, ни контакта покупателя."""
    purpose = purpose_of(req.purpose)
    return {
        "id": req.id,
        "user_id": req.user_id,
        "purpose": purpose,
        "label": purpose.label,
        "activity": purpose.activity,
        "budget_rub": req.budget_rub,
        "min_sotka": round(req.min_area_m2 / SOTKA_M2, 1) if req.min_area_m2 else None,
        "max_sotka": round(req.max_area_m2 / SOTKA_M2, 1) if req.max_area_m2 else None,
        "region": req.region,
        "text": _CONTACT_RE.sub("…", req.text or "")[:200],
        "wants": wants(req),
        "created_at": req.created_at,
    }


def active_requests(
    db,
    *,
    exclude_user_id: int | None = None,
    region: str = "",
    purpose: str = "",
    days: int = ACTIVE_DAYS,
) -> list[BuyerRequest]:
    since = datetime.now(UTC) - timedelta(days=days)
    out: list[BuyerRequest] = []
    for r in db.scalars(select(BuyerRequest).order_by(BuyerRequest.created_at.desc()).limit(500)):
        created = _aware(r.created_at)
        if created is not None and created < since:
            continue
        if exclude_user_id is not None and r.user_id == exclude_user_id:
            continue
        hay = f"{r.region or ''} {r.text or ''}".lower()
        if region and region.strip().lower() not in hay:
            continue
        if purpose and r.purpose != purpose:
            continue
        out.append(r)
    return out


def fit(req: BuyerRequest, card: dict[str, Any], text: str = "") -> dict[str, Any] | None:
    """Проходит ли объявление фильтры запроса; вердикт под цель покупателя."""
    return match_listing(
        card,
        purpose=purpose_of(req.purpose),
        budget_rub=req.budget_rub,
        min_area_m2=req.min_area_m2,
        max_area_m2=req.max_area_m2,
        region=req.region,
        answers=dict(req.answers or {}),
        text=text,
    )


def matching_requests(
    db, card: dict[str, Any], text: str, *, exclude_user_id: int | None
) -> list[tuple[BuyerRequest, dict[str, Any]]]:
    """Активные запросы, под которые подходит объявление (для счётчика спроса у продавца)."""
    out = []
    for req in active_requests(db, exclude_user_id=exclude_user_id):
        m = fit(req, card, text)
        if m is not None:
            out.append((req, m))
    out.sort(key=lambda x: -x[1]["score"])
    return out


def my_public_listings(db, user: User) -> list[Listing]:
    return list(
        db.scalars(
            select(Listing)
            .where(
                Listing.user_id == user.id,
                Listing.status == "verified",
                Listing.published.is_(True),
            )
            .order_by(Listing.id.desc())
        )
    )


def listing_text(listing: Listing) -> str:
    d = listing.data or {}
    return f"{d.get('description') or ''} {d.get('notes') or ''}"


def contacts_allowed(user: User) -> bool:
    """Крючок монетизации: контакты покупателей продавцам — в тарифе. Пока флаг выключен — всем."""
    return (not settings.demand_paid) or bool(user.is_admin)


def create_offer(
    db,
    req: BuyerRequest,
    listing: Listing,
    from_user: User,
    note: str,
    card: dict[str, Any],
) -> tuple[Offer, bool]:
    """Отклик на запрос; повторный по той же паре запрос+объявление не создаётся."""
    existing = db.scalar(
        select(Offer).where(Offer.request_id == req.id, Offer.listing_id == listing.id)
    )
    if existing is not None:
        return existing, False
    m = fit(req, card, listing_text(listing))
    offer = Offer(
        request_id=req.id,
        listing_id=listing.id,
        from_user_id=from_user.id,
        note=note.strip()[:1000],
        fit=m is not None,
        verdict=(m or {}).get("verdict") or {},
    )
    db.add(offer)
    db.flush()
    who = from_user.brand or from_user.name
    title = card.get("title") or listing.cad_num
    what = (req.text or purpose_of(req.purpose).activity)[:80]
    if m is not None:
        v = m["verdict"]
        tail = f"{v['level']} — {v['text']}"
    else:
        tail = "вне параметров вашего запроса (цена, площадь или район)"
    notify(
        db,
        req.user_id,
        "offer",
        f"{who} предлагает участок «{title}» под ваш запрос «{what}»: {tail}. "
        "Посмотреть и ответить — в подборке по запросу.",
        f"/search/{req.id}#offers",
    )
    return offer, True


def decide_offer(
    db, offer: Offer, buyer: User, decision: str, title: str, listing: Listing | None = None
) -> None:
    offer.status = decision
    offer.decided_at = datetime.now(UTC)
    verdict = "принял" if decision == "accepted" else "отклонил"
    tail = "."
    if decision == "accepted":
        if buyer.contact or buyer.email:
            tail = ". Контакты открыты в разделе «Спрос»."
        else:
            tail = ", но контакт в профиле не указан — мы попросили его заполнить."
            notify(
                db,
                buyer.id,
                "system",
                "Вы приняли предложение, но у вас не указаны ни контакт, ни email — продавцу "
                "нечего показать. Заполните контакт в профиле.",
                "/profile",
            )
        seller = db.get(User, offer.from_user_id)
        phone = ((listing.data or {}).get("phone") if listing is not None else None) or ""
        if seller is not None and not (seller.contact or seller.email or phone):
            # симметрично покупателю: принял, а показать нечего (Клавдий, прод 23.10)
            notify(
                db,
                seller.id,
                "system",
                "Покупатель принял ваше предложение, но у вас не указаны ни контакт, ни email, "
                "ни телефон в объявлении — покупателю нечего показать. Заполните контакт "
                "в профиле.",
                "/profile",
            )
    notify(
        db,
        offer.from_user_id,
        "offer_decision",
        f"Покупатель {verdict} ваше предложение «{title}»{tail}",
        "/demand",
    )


def offers_for_request(db, req: BuyerRequest) -> list[dict[str, Any]]:
    """Предложения под запрос для страницы покупателя: карточка, вердикт, контакт после согласия."""
    out: list[dict[str, Any]] = []
    rows = db.scalars(
        select(Offer).where(Offer.request_id == req.id).order_by(Offer.created_at.desc())
    ).all()
    for o in rows:
        lst = db.get(Listing, o.listing_id)
        seller = db.get(User, o.from_user_id)
        d = (lst.data or {}) if lst else {}
        contact = None
        if o.status == "accepted" and seller is not None:
            contact = seller.contact or seller.email or d.get("phone")
        out.append(
            {
                "offer": o,
                "listing": lst,
                "title": d.get("title") or (lst.cad_num if lst else f"#{o.listing_id}"),
                "price_rub": d.get("price_rub"),
                "area_sotka": d.get("land_area_sotka"),
                "address": d.get("address"),
                "seller": (seller.brand or seller.name) if seller else "",
                "contact": contact,
                "verdict": o.verdict or {},
                "fit": o.fit,
            }
        )
    return out


def my_offers(db, user: User) -> list[dict[str, Any]]:
    """Мои предложения: запрос, объявление, статус, контакт покупателя после согласия."""
    out: list[dict[str, Any]] = []
    rows = db.scalars(
        select(Offer).where(Offer.from_user_id == user.id).order_by(Offer.created_at.desc())
    ).all()
    allowed = contacts_allowed(user)
    for o in rows:
        req = db.get(BuyerRequest, o.request_id)
        lst = db.get(Listing, o.listing_id)
        buyer = db.get(User, req.user_id) if req else None
        contact = None
        if o.status == "accepted" and buyer is not None and allowed:
            contact = buyer.contact or buyer.email
        out.append(
            {
                "offer": o,
                "request": request_card(req) if req else None,
                "title": ((lst.data or {}).get("title") if lst else None)
                or (lst.cad_num if lst else f"#{o.listing_id}"),
                "listing_id": o.listing_id,
                "contact": contact,
                "contact_paid": o.status == "accepted" and not allowed,
            }
        )
    return out


def notify_sellers_about(db, req: BuyerRequest, *, card_fn) -> int:
    """Владельцам опубликованных объявлений, под которые подходит новый запрос, — «появился
    покупатель под ваш участок». Одно уведомление на владельца и запрос (дедуп по url)."""
    from landcheck.web.notify import already_notified
    from landcheck.web.serialize import fmt_money

    rows = db.scalars(
        select(Listing).where(Listing.status == "verified", Listing.published.is_(True))
    ).all()
    by_owner: dict[int, list[str]] = {}
    for lst in rows:
        if lst.user_id == req.user_id:
            continue
        owner = db.get(User, lst.user_id)
        card = card_fn(lst, owner)
        if fit(req, card, listing_text(lst)) is None:
            continue
        by_owner.setdefault(lst.user_id, []).append(card.get("title") or lst.cad_num)
    c = request_card(req)
    params = [c["activity"].lower()]
    if c["budget_rub"]:
        params.append(f"до {fmt_money(c['budget_rub'])}")
    if c["min_sotka"] or c["max_sotka"]:
        params.append(f"{c['min_sotka'] or ''}–{c['max_sotka'] or ''} сот.".replace("–ы", ""))
    if c["region"]:
        params.append(c["region"])
    sent = 0
    for owner_id, titles in by_owner.items():
        url = f"/demand#r{req.id}"
        if already_notified(db, owner_id, "demand", url):
            continue
        what = ", ".join(f"«{t}»" for t in titles[:3])
        if len(titles) > 3:
            what += f" и ещё {len(titles) - 3}"
        notify(
            db,
            owner_id,
            "demand",
            f"Появился покупатель под ваш участок {what}: {'; '.join(params)}. "
            "Предложить участок — в разделе «Спрос».",
            url,
        )
        sent += 1
    return sent


def demand_counts(db, listings: list[Listing], *, card_fn) -> dict[int, int]:
    """Сколько активных запросов подходит под каждое объявление (список объявлений продавца)."""
    reqs = active_requests(db)
    out: dict[int, int] = {}
    for lst in listings:
        owner = db.get(User, lst.user_id)
        card = card_fn(lst, owner)
        text = listing_text(lst)
        out[lst.id] = sum(
            1 for r in reqs if r.user_id != lst.user_id and fit(r, card, text) is not None
        )
    return out
