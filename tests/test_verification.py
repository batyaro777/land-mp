"""Кабинет продавца (К2): подтверждение права, модерация, удаление файлов после решения."""

from __future__ import annotations

import io

from landcheck.web.config import settings
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

PDF = b"%PDF-1.4\n%fake\n" + b"0" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _register(c, email: str, role: str = "seller") -> None:
    r = c.post(
        "/register", data={"email": email, "password": "12345678", "name": email, "role": role}
    )
    assert r.status_code == 303


def _make_admin(email: str) -> None:
    from sqlalchemy import select

    from landcheck.web.db import User, session

    with session() as db:
        u = db.scalar(select(User).where(User.email == email))
        u.is_admin = True
        db.commit()


def test_verification_flow(client):  # noqa: F811
    _register(client, "seller@example.com")
    r = client.post(
        "/listing",
        data={"cad_num": "50:09:0060202:101", "price": "2500000", "notes": "лес рядом"},
    )
    url = r.headers["location"]
    page = client.get(url).text
    assert "Подтверждение права" in page and "черновик" in page and "Отправить на проверку" in page
    # без файлов — ошибка
    r = client.post(url + "/verify", data={"proof_kind": "owner"})
    assert r.status_code == 400 and "хотя бы один файл" in r.text
    # не документ — пропускается, PDF принимается
    r = client.post(
        url + "/verify",
        data={"proof_kind": "agent"},
        files=[
            ("proofs", ("x.txt", io.BytesIO(b"hello"), "text/plain")),
            ("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf")),
        ],
    )
    assert r.status_code == 303
    page = client.get(url).text
    assert "на проверке" in page and "01.pdf" in page and "Не приняты файлы" in page
    assert client.get(url + "/proof/01.pdf").status_code == 200
    assert client.get(url + "/proof/../01.pdf").status_code in (400, 404)
    listing_id = int(url.rsplit("/", 1)[1])
    assert (settings.db_path.parent / "proofs" / str(listing_id) / "01.pdf").exists()
    # не админ — модерации нет; чужой файл — 404
    assert client.get("/admin/listings").status_code == 404
    client.post("/logout")
    _register(client, "other@example.com", role="buyer")
    assert client.get(url + "/proof/01.pdf").status_code == 404
    client.post("/logout")
    # админ видит очередь и подтверждает
    _register(client, "admin@example.com", role="broker")
    _make_admin("admin@example.com")
    assert "Модерация" in client.get("/").text
    q = client.get("/admin/listings").text
    assert "50:09:0060202:101" in q and "seller@example.com" in q and "01.pdf" in q
    assert "агентский" in q.lower()
    r = client.post(
        f"/admin/listings/{listing_id}/decide", data={"decision": "approve", "note": "ок"}
    )
    assert r.status_code == 303
    assert not (settings.db_path.parent / "proofs" / str(listing_id)).exists()
    assert "право подтверждено" in client.get("/admin/listings?status=verified").text
    client.post("/logout")
    # владелец видит статус
    client.post("/login/password", data={"email": "seller@example.com", "password": "12345678"})
    page = client.get(url).text
    assert "Право подтверждено" in page and "Отправить на проверку" not in page
    assert "право подтверждено" in client.get("/listings").text


def test_rejection_allows_resubmit(client):  # noqa: F811
    _register(client, "seller2@example.com")
    url = client.post("/listing", data={"cad_num": "50:09:0060202:104"}).headers["location"]
    client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("doc.png", io.BytesIO(PNG), "image/png"))],
    )
    listing_id = int(url.rsplit("/", 1)[1])
    client.post("/logout")
    _register(client, "admin2@example.com", role="broker")
    _make_admin("admin2@example.com")
    client.post(
        f"/admin/listings/{listing_id}/decide",
        data={"decision": "reject", "note": "в выписке другой кадастровый номер"},
    )
    client.post("/logout")
    client.post("/login/password", data={"email": "seller2@example.com", "password": "12345678"})
    page = client.get(url).text
    assert "Отклонено: в выписке другой кадастровый номер" in page
    assert "Отправить на проверку" in page
