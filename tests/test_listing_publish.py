"""Публикация объявления (прод 09.11, объявление №3): замена выписки «на проверке», уведомление
модератору, OCR сканов PDF, форма не теряется при ошибке."""

from __future__ import annotations

import io

from sqlalchemy import select

from landcheck import intake
from landcheck.web import materials
from tests.test_egrn import EXTRACT
from tests.test_verification import PDF, _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

CAD = "50:09:0060202:101"


def _listing(c) -> str:
    r = c.post("/listing", data={"cad_num": CAD, "price": "2500000", "notes": "лес рядом"})
    assert r.status_code == 303
    return r.headers["location"]


def test_pending_with_unreadable_proof_offers_replacement(client):  # noqa: F811
    _register(client, "seller@example.com")
    url = _listing(client)
    page = client.get(url).text
    assert "Заменить файлы" not in page  # черновик: обычная форма
    r = client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("brandbook.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    assert r.status_code == 303
    page = client.get(url).text
    assert "на проверке" in page
    assert "Текст файла не прочитан" in page and "Заменить файлы и отправить снова" in page
    assert page.count('action="' + url + '/verify"') == 1
    # замена файла в статусе «на проверке» принимается и остаётся на проверке
    r = client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF + b"1"), "application/pdf"))],
    )
    assert r.status_code == 303
    assert "на проверке" in client.get(url).text


def test_pending_with_non_extract_says_so(client, monkeypatch):  # noqa: F811
    # прод 09.11: вместо выписки — брендбук; OCR читает текст, но это не выписка
    monkeypatch.setattr(intake, "pdf_text", lambda raw, max_pages=20: "")
    monkeypatch.setattr(
        intake, "pdf_scan_text", lambda raw: "ФИРМЕННЫЙ ЗНАК И ЛОГОТИП\nЦВЕТА БРЕНДА"
    )
    _register(client, "seller4@example.com")
    url = _listing(client)
    client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("brandbook.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    page = client.get(url).text
    assert "не похож на выписку ЕГРН" in page and "Заменить файлы и отправить снова" in page


def test_moderators_notified_when_listing_goes_pending(client):  # noqa: F811
    from landcheck.web.db import Notification, User, session

    _register(client, "admin@example.com", role="broker")
    _make_admin("admin@example.com")
    client.post("/logout")
    client.cookies.clear()
    _register(client, "seller2@example.com")
    url = _listing(client)
    with session() as db:
        admin = db.scalar(select(User).where(User.email == "admin@example.com"))
        assert not db.scalars(select(Notification).where(Notification.user_id == admin.id)).all()
    client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    with session() as db:
        admin = db.scalar(select(User).where(User.email == "admin@example.com"))
        seller = db.scalar(select(User).where(User.email == "seller2@example.com"))
        got = db.scalars(select(Notification).where(Notification.user_id == admin.id)).all()
        mine = db.scalars(select(Notification).where(Notification.user_id == seller.id)).all()
    assert len(got) == 1 and got[0].kind == "moderation" and got[0].url == "/admin/listings"
    assert "Объявление на проверке" in got[0].text and CAD in got[0].text
    assert "не сверено" in got[0].text
    assert not [n for n in mine if n.kind == "moderation"]  # продавцу — не модераторское


def test_create_without_number_keeps_form(client):  # noqa: F811
    _register(client, "seller3@example.com")
    r = client.post(
        "/listing",
        data={"cad_num": "", "price": "2 500 000", "notes": "свет по границе", "phone": "+7 900"},
        files=[("files", ("scan.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    assert r.status_code == 400
    assert "не найден в материалах" in r.text and "прикрепите их заново" in r.text
    assert 'value="2 500 000"' in r.text and "свет по границе</textarea>" in r.text
    assert 'value="+7 900"' in r.text and 'action="/listing"' in r.text


def test_scanned_pdf_read_by_ocr(monkeypatch):
    monkeypatch.setattr(intake, "pdf_text", lambda raw, max_pages=20: "")
    monkeypatch.setattr(intake, "pdf_scan_text", lambda raw: EXTRACT)
    res = intake.process([intake.Material(name="scan.pdf", raw=PDF, kind="pdf")])
    assert res.documents and res.documents[0].ocr
    assert CAD in res.facts.cad_nums  # номер берётся из скана — форма не отвечает «не найден»
    assert res.to_dict()["ocr"] == ["scan.pdf"]
    check = materials.proof_check(
        res.documents,
        cad_num=CAD,
        area_m2=1000.0,
        seller_name="Иванов Иван Иванович",
        proof_kind="owner",
    )
    assert check["ocr"] is True and check["label"].startswith("Скан, текст распознан")


def test_text_layer_pdf_not_marked_ocr(monkeypatch):
    calls: list[bytes] = []
    monkeypatch.setattr(intake, "pdf_text", lambda raw, max_pages=20: EXTRACT)
    monkeypatch.setattr(intake, "pdf_scan_text", lambda raw: calls.append(raw) or "")
    m = intake.Material(name="egrn.pdf", raw=PDF, kind="pdf")
    intake.fill_pdf_text(m)
    assert m.text == EXTRACT and not m.ocr and not calls  # OCR не зовётся, если есть слой


def test_pdf_scan_text_without_tesseract(monkeypatch):
    from landcheck import ocr

    monkeypatch.setattr(ocr, "available", lambda: False)
    assert intake.pdf_scan_text(PDF) == ""


def test_ocr_capped_per_upload(monkeypatch):
    calls: list[bytes] = []
    monkeypatch.setattr(intake, "pdf_text", lambda raw, max_pages=20: "")
    monkeypatch.setattr(intake, "pdf_scan_text", lambda raw: calls.append(raw) or EXTRACT)
    docs = [
        intake.Material(name=f"s{i}.pdf", raw=PDF + str(i).encode(), kind="pdf") for i in range(5)
    ]
    res = intake.process(docs)
    assert len(res.documents) == 5 and len(calls) == intake.OCR_SCAN_DOCS
    assert sum(d.ocr for d in res.documents) == intake.OCR_SCAN_DOCS
