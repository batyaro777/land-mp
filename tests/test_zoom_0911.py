"""Замечания с зума 09.11 (Тони, Антон М., Влад): номер с точками, пояснение Avito XML,
подсказки по Яндекс.Диску, «Предложить участок» в спросе, пропуск вопросов, удаление запросов,
сортировка и регион по умолчанию на витрине, регион в профиле, «Рядом продаётся»."""

from __future__ import annotations

import io

import httpx
from sqlalchemy import select

from landcheck import intake
from landcheck.inputs import normalize_cad
from tests.test_verification import PDF, _make_admin, _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

CAD = "50:09:0060202:101"


def test_normalize_cad_accepts_dots_commas_spaces():
    assert normalize_cad("50:09:0060202:101") == CAD
    assert normalize_cad(" 50 : 09 : 0060202 : 101 ") == CAD
    assert normalize_cad("50.09.0060202.101") == CAD
    assert normalize_cad("50,09,0060202,101") == CAD
    assert normalize_cad("50 09 0060202 101") == CAD
    assert normalize_cad("50/09/0060202/101") == CAD
    assert normalize_cad("2,5 млн") is None
    assert normalize_cad("") is None
    assert normalize_cad("12.05.2005 50 сот") is None  # дата и площадь — не номер


def test_single_and_listing_accept_dotted_number(client):  # noqa: F811
    _register(client, "dots@example.com")
    r = client.post("/single", data={"cad_num": "50.09.0060202.101", "purpose": "any"})
    assert r.status_code == 303
    r = client.post("/listing", data={"cad_num": "50.09.0060202.101", "price": "2500000"})
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert CAD in page
    assert "Что такое Avito XML" in page and "файл не нужен" in page


def test_yandex_closed_folder_gets_plain_hint():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Не удалось найти запрошенный ресурс."})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    files, errors = intake.yandex_list("https://disk.yandex.ru/d/abc", client=c)
    assert not files and len(errors) == 1
    assert "Поделиться" in errors[0] and "Скопировать ссылку" in errors[0]
    page_hint = "«Поделиться» → «Скопировать ссылку»"
    assert page_hint in errors[0]


def test_demand_explains_missing_offer_button(client):  # noqa: F811
    from landcheck.web.db import BuyerRequest, User, session

    _register(client, "buyer@example.com", role="buyer")
    with session() as db:
        b = db.scalar(select(User).where(User.email == "buyer@example.com"))
        db.add(BuyerRequest(user_id=b.id, text="дом для семьи", region="синтетический"))
        db.commit()
    client.post("/logout")
    client.cookies.clear()
    _register(client, "seller@example.com")
    page = client.get("/demand").text
    assert "Предлагать участки пока нечем" in page and "У вас нет объявлений" in page
    assert 'disabled title="Нужно опубликованное объявление' in page
    url = client.post("/listing", data={"cad_num": CAD, "price": "2500000"}).headers["location"]
    client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    page = client.get("/demand").text
    assert "на проверке" in page and "загрузите электронную выписку" in page
    assert "Предлагать участки пока нечем" in page


def test_skip_questions_really_skips(client):  # noqa: F811
    _register(client, "skip@example.com", role="buyer")
    r = client.post("/search", data={"query": "участок под дом в Истре", "purpose": "izhs"})
    url = r.headers["location"]
    page = client.get(url).text
    assert 'name="skip" value="1"' in page and "Пропустить вопросы" in page
    r = client.post(url + "/answers", data={"skip": "1", "kids": "yes"})
    assert r.status_code == 303
    page = client.get(url).text
    assert "Подборка под ваш запрос" in page  # не вопросы снова
    assert "Подходящих участков пока нет" in page and "Запрос сохранён" in page
    assert "по «Истре»" in page or "по «Истра»" in page or "Истр" in page
    # удалить запрос из списка «Мои запросы»
    rid = int(url.rsplit("/", 1)[1])
    page = client.get("/search").text
    assert f'action="/search/{rid}/delete"' in page
    assert client.post(f"/search/{rid}/delete").status_code == 303
    assert f'action="/search/{rid}/delete"' not in client.get("/search").text


def _publish(c, email: str, cad: str, price: str, notes: str = "") -> int:
    _register(c, email)
    url = c.post(
        "/listing", data={"cad_num": cad, "price": price, "notes": notes, "phone": "+7 900"}
    ).headers["location"]
    c.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    c.post("/logout")
    c.cookies.clear()
    return int(url.rsplit("/", 1)[1])


def test_market_sort_region_default_and_nearby(client):  # noqa: F811
    id1 = _publish(client, "s1@example.com", "50:09:0060202:101", "2500000")
    id2 = _publish(client, "s2@example.com", "50:09:0060202:102", "900000")
    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    for i in (id1, id2):
        client.post(f"/admin/listings/{i}/decide", data={"decision": "approve"})
    client.post("/logout")
    client.cookies.clear()
    for email, i in (("s1@example.com", id1), ("s2@example.com", id2)):
        client.post("/login/password", data={"email": email, "password": "12345678"})
        client.post(f"/listing/{i}/publish", data={"action": "on"})
        client.post("/logout")
        client.cookies.clear()
    pub = [i for i in (id1, id2) if client.get(f"/l/{i}").status_code == 200]
    assert id1 in pub  # второе может не пройти из-за противоречий текста — не важно для сортировки
    m = client.get("/market?applied=1&sort=price_asc").text
    assert 'value="price_asc" selected' in m and "дешевле за сотку" in m
    if len(pub) == 2:
        asc = client.get("/market?applied=1&sort=price_asc").text
        desc = client.get("/market?applied=1&sort=price_desc").text
        assert asc.index(f"/l/{id2}") < asc.index(f"/l/{id1}")
        assert desc.index(f"/l/{id1}") < desc.index(f"/l/{id2}")
        # «Рядом продаётся» — другой участок той же местности на публичной странице
        page = client.get(f"/l/{id1}").text
        assert "Рядом продаётся" in page and f"/l/{id2}" in page
    # регион из профиля подставляется, пока фильтр не применяли; applied=1 сбрасывает
    _register(client, "buyer@example.com", role="buyer")
    client.post(
        "/profile", data={"name": "Покупатель", "role": "buyer", "region": "Алтайский край"}
    )
    page = client.get("/market").text
    assert 'value="Алтайский край"' in page and "по «Алтайский край»" in page
    assert "Показать все регионы" in page
    page = client.get("/market?applied=1").text
    assert 'value="Алтайский край"' not in page
    # и в спросе, и при создании запроса без региона
    assert 'value="Алтайский край"' in client.get("/demand").text
    r = client.post("/search", data={"query": "участок под дом", "purpose": "izhs"})
    from landcheck.web.db import BuyerRequest, session

    with session() as db:
        req = db.get(BuyerRequest, int(r.headers["location"].rsplit("/", 1)[1]))
        assert req.region == "Алтайский край"


def test_register_with_region(client):  # noqa: F811
    r = client.post(
        "/register",
        data={
            "email": "reg@example.com",
            "password": "12345678",
            "name": "Р",
            "role": "buyer",
            "region": "Ставропольский край",
        },
    )
    assert r.status_code == 303
    page = client.get("/profile").text
    assert 'value="Ставропольский край"' in page and 'list="region-names"' in page
    client.post("/logout")
    client.cookies.clear()
    assert "Ставропольский край" in client.get("/login?tab=register").text  # datalist


# --- пакет B: автоподтверждение, переписка, Google Диск ---------------------------------------


def _register_named(c, email: str, name: str, role: str = "seller") -> None:
    r = c.post(
        "/register", data={"email": email, "password": "12345678", "name": name, "role": role}
    )
    assert r.status_code == 303


def _extract_for(cad: str, area_m2: float, holder: str) -> str:
    from tests.test_egrn import EXTRACT

    return (
        EXTRACT.replace("50:09:0060202:101", cad)
        .replace("Площадь: 1000 +/- 11 кв. м", f"Площадь: {area_m2:g} +/- 11 кв. м")
        .replace("Иванов Иван Иванович", holder)
    )


def test_auto_verify_when_extract_matches(client, monkeypatch):  # noqa: F811
    from landcheck.web.db import Listing, Notification, User, session

    _register(client, "adm@example.com", role="broker")
    _make_admin("adm@example.com")
    client.post("/logout")
    client.cookies.clear()
    _register_named(client, "owner@example.com", "Иванов Иван Иванович")
    url = client.post("/listing", data={"cad_num": CAD, "price": "2500000"}).headers["location"]
    lid = int(url.rsplit("/", 1)[1])
    with session() as db:
        area = (db.get(Listing, lid).data.get("parcel_facts") or {}).get("best_area_m2") or 1000
    monkeypatch.setattr(
        intake,
        "pdf_text",
        lambda raw, max_pages=20: _extract_for(CAD, area, "Иванов Иван Иванович"),
    )
    r = client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    assert r.status_code == 303
    with session() as db:
        row = db.get(Listing, lid)
        assert row.status == "verified" and row.verified_at is not None
        assert "автоматически" in (row.moderation_note or "")
        assert row.data["proof_check"]["verdict"] == "ok" and "items" not in row.data["proof_check"]
        owner = db.scalar(select(User).where(User.email == "owner@example.com"))
        adm = db.scalar(select(User).where(User.email == "adm@example.com"))
        mine = [
            n.text for n in db.scalars(select(Notification).where(Notification.user_id == owner.id))
        ]
        theirs = [
            n.text for n in db.scalars(select(Notification).where(Notification.user_id == adm.id))
        ]
    assert any("подтверждено автоматически" in t for t in mine)
    assert any("Автоподтверждение права" in t for t in theirs)
    from landcheck.web.config import settings

    assert not (settings.db_path.parent / "proofs" / str(lid)).exists()  # файлы удалены
    page = client.get(url).text
    assert "право подтверждено" in page and "Опубликовать в базе" in page
    assert client.post(url + "/publish", data={"action": "on"}).status_code == 303
    assert client.get(f"/l/{lid}").status_code == 200


def test_no_auto_verify_on_holder_mismatch(client, monkeypatch):  # noqa: F811
    from landcheck.web.db import Listing, session

    _register_named(client, "owner2@example.com", "Петров Пётр Петрович")
    url = client.post("/listing", data={"cad_num": CAD, "price": "2500000"}).headers["location"]
    lid = int(url.rsplit("/", 1)[1])
    with session() as db:
        area = (db.get(Listing, lid).data.get("parcel_facts") or {}).get("best_area_m2") or 1000
    monkeypatch.setattr(
        intake,
        "pdf_text",
        lambda raw, max_pages=20: _extract_for(CAD, area, "Иванов Иван Иванович"),
    )
    client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    with session() as db:
        row = db.get(Listing, lid)
        assert row.status == "pending" and row.data["proof_check"]["verdict"] == "mismatch"
    assert "расхождение" in client.get(url).text


def test_auto_verify_can_be_disabled(client, monkeypatch):  # noqa: F811
    from landcheck.web.config import settings
    from landcheck.web.db import Listing, session

    monkeypatch.setattr(settings, "auto_verify", False)
    _register_named(client, "owner3@example.com", "Иванов Иван Иванович")
    url = client.post("/listing", data={"cad_num": CAD, "price": "2500000"}).headers["location"]
    lid = int(url.rsplit("/", 1)[1])
    with session() as db:
        area = (db.get(Listing, lid).data.get("parcel_facts") or {}).get("best_area_m2") or 1000
    monkeypatch.setattr(
        intake,
        "pdf_text",
        lambda raw, max_pages=20: _extract_for(CAD, area, "Иванов Иван Иванович"),
    )
    client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(PDF), "application/pdf"))],
    )
    with session() as db:
        assert db.get(Listing, lid).status == "pending"


def test_chat_between_buyer_and_seller(client):  # noqa: F811
    from landcheck.web.db import Message, Notification, User, session

    lid = _publish(client, "sell@example.com", CAD, "2500000")
    _register(client, "adm2@example.com", role="broker")
    _make_admin("adm2@example.com")
    client.post(f"/admin/listings/{lid}/decide", data={"decision": "approve"})
    client.post("/logout")
    client.cookies.clear()
    client.post("/login/password", data={"email": "sell@example.com", "password": "12345678"})
    client.post(f"/listing/{lid}/publish", data={"action": "on"})
    client.post("/logout")
    client.cookies.clear()
    # покупатель просит контакт с запиской — это первое сообщение переписки
    _register(client, "buy@example.com", role="buyer")
    r = client.post(f"/l/{lid}/connect", data={"note": "Здравствуйте! Торг уместен?"})
    assert r.status_code == 303
    with session() as db:
        conn_id = db.scalar(select(Message.connection_id))
        seller = db.scalar(select(User).where(User.email == "sell@example.com"))
    page = client.get(f"/l/{lid}").text
    assert f"/connections/{conn_id}" in page and "написать продавцу" in page
    page = client.get(f"/connections/{conn_id}").text
    assert "Торг уместен?" in page and "контакты не открыты" in page
    r = client.post(
        f"/connections/{conn_id}/messages", data={"text": "Когда можно посмотреть участок?"}
    )
    assert r.status_code == 303
    # посторонний не видит переписку
    client.post("/logout")
    client.cookies.clear()
    _register(client, "stranger@example.com", role="buyer")
    assert client.get(f"/connections/{conn_id}").status_code == 404
    assert client.post(f"/connections/{conn_id}/messages", data={"text": "x"}).status_code == 404
    client.post("/logout")
    client.cookies.clear()
    # продавец: уведомление, счётчик новых, ответ
    client.post("/login/password", data={"email": "sell@example.com", "password": "12345678"})
    with session() as db:
        kinds = [
            n.kind
            for n in db.scalars(select(Notification).where(Notification.user_id == seller.id))
        ]
    assert "connect" in kinds and "message" in kinds
    page = client.get("/connections").text
    assert "Переписка (2" in page and "новых 2" in page
    page = client.get(f"/connections/{conn_id}").text
    assert "Когда можно посмотреть участок?" in page and "Принять и открыть контакты" in page
    assert "новых" not in client.get("/connections").text  # прочитано
    r = client.post(f"/connections/{conn_id}/messages", data={"text": "Завтра после 15:00."})
    assert r.status_code == 303 and r.headers["location"].endswith("?sent=1")
    client.post("/logout")
    client.cookies.clear()
    client.post("/login/password", data={"email": "buy@example.com", "password": "12345678"})
    with session() as db:
        buyer = db.scalar(select(User).where(User.email == "buy@example.com"))
        texts = [
            n.text
            for n in db.scalars(
                select(Notification).where(
                    Notification.user_id == buyer.id, Notification.kind == "message"
                )
            )
        ]
    assert any("Завтра после 15:00" in t for t in texts)
    assert "новых 1" in client.get("/connections").text
    # отклонённый запрос закрывает переписку
    client.post("/logout")
    client.cookies.clear()
    client.post("/login/password", data={"email": "sell@example.com", "password": "12345678"})
    client.post(f"/connections/{conn_id}/decide", data={"decision": "declined"})
    assert client.post(f"/connections/{conn_id}/messages", data={"text": "x"}).status_code == 403
    assert "переписка закрыта" in client.get(f"/connections/{conn_id}").text


def test_google_drive_folder_import(monkeypatch, tmp_path):
    from pathlib import Path

    assert intake.is_cloud_url("https://drive.google.com/drive/folders/1AbC?usp=sharing")
    assert intake.is_cloud_url("https://disk.yandex.ru/d/abc")
    assert not intake.is_cloud_url("https://example.com/x")

    def fake_download(url: str, out_dir: str) -> None:
        d = Path(out_dir) / "Участок"
        d.mkdir()
        (d / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 4000)
        (d / "egrn.pdf").write_bytes(PDF)
        (Path(out_dir) / "notes.txt").write_text("газ по границе", encoding="utf-8")

    monkeypatch.setattr(intake, "_google_download", fake_download)
    mats, errors = intake.fetch_cloud("https://drive.google.com/drive/folders/1AbC?usp=sharing")
    assert errors == [] and sorted(m.name for m in mats) == ["egrn.pdf", "notes.txt", "photo.jpg"]
    assert all(m.source == "cloud" for m in mats)

    def broken(url: str, out_dir: str) -> None:
        raise RuntimeError("Cannot retrieve the folder information from the link")

    monkeypatch.setattr(intake, "_google_download", broken)
    mats, errors = intake.fetch_cloud("https://drive.google.com/drive/folders/closed")
    assert mats == [] and "Все, у кого есть ссылка" in errors[0]
    mats, errors = intake.fetch_cloud("https://example.com/not-cloud")
    assert mats == [] and "Google Диска" in errors[0]
