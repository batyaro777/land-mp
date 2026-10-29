"""Приём материалов «как есть» (R12): типы файлов, факты из текста, папка Яндекс.Диска,
создание объявления из документов с автосверкой выписки."""

from __future__ import annotations

import base64
import io
import json
import struct
import zipfile

import httpx

from landcheck import intake
from landcheck.web.config import settings
from tests.test_egrn import EXTRACT
from tests.test_verification import _make_admin
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета


def _png(w: int, h: int, size: int = 100) -> bytes:
    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h)
    head += b"\x08\x02\x00\x00\x00" + b"\x00" * 4
    return head + b"\x00" * max(0, size - len(head))


def _jpeg(w: int, h: int, size: int = 100) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", h, w)
        + b"\x03"
        + b"\x00" * 9
    )
    head = b"\xff\xd8" + app0 + sof0
    return head + b"\x00" * max(0, size - len(head))


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        paras = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n"))
        z.writestr("word/document.xml", f"<w:document><w:body>{paras}</w:body></w:document>")
    return buf.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, raw in files.items():
            z.writestr(name, raw)
    return buf.getvalue()


# DER-оболочка CMS SignedData: достаточно для определения типа, подпись подменяется в тестах
SIG_DER = b"\x30\x82\x00\x20\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02" + b"\x00" * 20


def _pdf(text: str) -> bytes:
    """Минимальный PDF с текстовым слоем (латиница и цифры), который читает pypdf."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode("latin-1")
    objs[3] = (
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def _norm(s: str) -> str:
    return s.replace(" ", " ").replace("\xa0", " ")


# --- модуль ---------------------------------------------------------------------------------
def test_kind_of_by_signature_not_extension():
    assert intake.kind_of(_jpeg(10, 10), "x.png") == "jpg"
    assert intake.kind_of(_png(10, 10), "x.jpg") == "png"
    assert intake.kind_of(b"%PDF-1.4 ...", "scan.jpg") == "pdf"
    assert intake.kind_of(_docx("a"), "doc.docx") == "docx"
    assert intake.kind_of("Продаю участок".encode(), "notes.txt") == "txt"
    assert intake.kind_of("Продаю участок".encode(), "notes") == "txt"
    assert intake.kind_of(b"not an image at all", "fake.jpg") == "other"
    assert intake.kind_of(b"\x00\x01\x02", "bin.dat") == "other"
    assert intake.kind_of(b"PK\x03\x04junk", "a.zip") == "other"
    assert intake.kind_of(_zip({"a.txt": b"hi"}), "x.bin") == "zip"
    assert intake.kind_of(b'<?xml version="1.0"?><extract/>', "report.xml") == "xml"
    assert intake.kind_of(b"\xef\xbb\xbf<extract><a>1</a></extract>", "r") == "xml"
    assert intake.kind_of(SIG_DER, "report.xml.sig") == "sig"
    assert intake.kind_of(base64.b64encode(SIG_DER), "report.sig") == "sig"


def test_text_extraction_pdf_docx_txt():
    assert "50:09:0060202:101" in intake.pdf_text(_pdf("Cad 50:09:0060202:101 area 1000 sq m"))
    assert intake.pdf_text(b"%PDF-1.4 broken") == ""
    assert (
        intake.docx_text(_docx("Свет по границе\nГаз по улице")) == "Свет по границе\nГаз по улице"
    )
    m = intake.Material("n.txt", "Цена 2,5 млн руб.".encode(), "txt")
    assert intake.text_of(m) == "Цена 2,5 млн руб."


def test_extract_facts():
    f = intake.extract_facts(
        "Участок 50:09:0060202:101, рядом 50:09:0060202:102. Ещё раз 50:09:0060202:101.\n"
        "Цена 2,5 млн руб., торг. Площадь 12 соток. Тел. 8 (900) 123-45-67"
    )
    assert f.cad_nums == ["50:09:0060202:101", "50:09:0060202:102"]
    assert f.price_rub == 2_500_000 and f.area_sotka == 12 and f.phone == "+79001234567"
    f2 = intake.extract_facts("Площадь: 1000 +/- 11 кв. м. Кадастровая стоимость, руб.: 1 500 000")
    assert f2.area_sotka == 10 and f2.price_rub is None  # сумма без единицы после числа — не цена
    assert intake.extract_facts("").cad_nums == []


def test_image_size_and_document_heuristic():
    assert intake.image_size(_png(1240, 1754)) == (1240, 1754)
    assert intake.image_size(_jpeg(4000, 3000)) == (4000, 3000)
    assert intake.looks_like_document(_png(1240, 1754)) is True  # лист A4
    assert intake.looks_like_document(_jpeg(4000, 3000)) is False  # фото
    assert intake.looks_like_document(_jpeg(3000, 4000)) is False  # вертикальное фото 3:4
    assert intake.looks_like_document(_jpeg(1080, 1920)) is False  # вертикальное фото 9:16
    assert intake.looks_like_document(b"\xff\xd8\xff\x00") is None


def test_process_sorts_photos_documents_notes(monkeypatch):
    big = intake.Material("a.jpg", _jpeg(4000, 3000, 40_000), "jpg")
    dup = intake.Material("a-copy.jpg", big.raw, "jpg")
    small = intake.Material("thumb.jpg", _jpeg(400, 300, 2_000), "jpg")
    scan = intake.Material("scan.png", _png(1240, 1754, 30_000), "png")
    pdf = intake.Material("vypiska.pdf", b"%PDF-1.4 x", "pdf")
    note = intake.Material("o.txt", "Цена 3 млн руб., 15 соток".encode(), "txt")
    junk = intake.Material("x.bin", b"\x00\x01", "other")
    monkeypatch.setattr(intake, "pdf_text", lambda raw, max_pages=20: EXTRACT)
    res = intake.process([big, dup, small, scan, pdf, note, junk], api_key=None)
    assert [m.name for m in res.photos] == ["a.jpg"]
    assert [m.name for m in res.documents] == ["vypiska.pdf"]
    assert [m.name for m in res.notes] == ["o.txt"] and "3 млн" in res.notes_text
    assert dict(res.dropped) == {
        "a-copy.jpg": "дубль",
        "thumb.jpg": "слишком маленькое изображение",
        "x.bin": "неизвестный формат",
        "scan.png": intake.SCAN_REASON,
    }
    assert [m.name for m in res.scans] == ["scan.png"] and res.signatures == []
    assert res.facts.cad_nums == ["50:09:0060202:101"] and res.facts.price_rub == 3_000_000
    assert res.classified_by == "heuristic"
    d = res.to_dict()
    assert d["photos"] == 1 and d["documents"] == ["vypiska.pdf"]


def test_classify_images_via_gemini(monkeypatch):
    photo = intake.Material("p.jpg", _jpeg(4000, 3000, 20_000), "jpg")
    scan = intake.Material("s.jpg", _jpeg(4000, 3000, 20_000) + b"\x01", "jpg")
    answer = {
        "items": [
            {"n": 1, "kind": "photo", "text": "", "score": 4},
            {
                "n": 2,
                "kind": "document",
                "text": "Выписка из ЕГРН\nКадастровый номер: 50:09:0060202:101",
            },
        ]
    }
    seen = {}

    def fake_post(url, params=None, json=None, timeout=None):
        seen["url"] = url
        parts = json["contents"][0]["parts"]
        seen["images"] = sum(1 for p in parts if "inline_data" in p)
        body = {
            "candidates": [{"content": {"parts": [{"text": __import__("json").dumps(answer)}]}}]
        }
        return httpx.Response(200, json=body)

    monkeypatch.setattr(intake.httpx, "post", fake_post)
    assert intake.classify_images([photo, scan], api_key="k", model="gemini-test") == "gemini"
    assert seen["images"] == 2 and "gemini-test" in seen["url"]
    assert photo.is_document is False and photo.score == 4
    assert scan.is_document is True and "50:09:0060202:101" in scan.text


def test_classify_images_falls_back_on_error(monkeypatch):
    scan = intake.Material("s.png", _png(1240, 1754, 20_000), "png")
    monkeypatch.setattr(
        intake.httpx,
        "post",
        lambda *a, **k: httpx.Response(500, json={"error": {"status": "INTERNAL"}}),
    )
    assert intake.classify_images([scan], api_key="k") == "gemini+heuristic"
    assert scan.is_document is True


# --- Яндекс.Диск ----------------------------------------------------------------------------
def _yandex_transport(files: dict[str, bytes]):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "cloud-api.yandex.net" and req.url.path.endswith("/download"):
            path = req.url.params.get("path", "/")
            return httpx.Response(
                200, json={"href": f"https://downloader.test/get?p={path}", "method": "GET"}
            )
        if req.url.host == "cloud-api.yandex.net":
            path = req.url.params.get("path", "/")
            if path == "/":
                items = [{"name": "docs", "type": "dir", "path": "/docs"}] + [
                    {"name": n, "type": "file", "path": "/" + n, "size": len(b), "mime_type": "x"}
                    for n, b in files.items()
                    if not n.startswith("docs/")
                ]
            else:
                items = [
                    {"name": n.split("/")[-1], "type": "file", "path": "/" + n, "size": len(b)}
                    for n, b in files.items()
                    if n.startswith("docs/")
                ]
            return httpx.Response(
                200, json={"type": "dir", "_embedded": {"items": items, "total": len(items)}}
            )
        if req.url.host == "downloader.test":
            name = req.url.params["p"].lstrip("/")
            return httpx.Response(200, content=files[name])
        return httpx.Response(404, json={"message": "Ресурс не найден."})

    return httpx.MockTransport(handler)


def test_fetch_cloud_folder_recursive():
    files = {
        "photo.jpg": _jpeg(4000, 3000, 20_000),
        "note.txt": "Цена 2 млн руб".encode(),
        "docs/v.pdf": b"%PDF-1.4",
    }
    c = httpx.Client(transport=_yandex_transport(files))
    mats, errors = intake.fetch_cloud("https://disk.yandex.ru/d/AbCdEf", client=c)
    assert errors == []
    assert sorted((m.name, m.kind, m.source) for m in mats) == [
        ("note.txt", "txt", "cloud"),
        ("photo.jpg", "jpg", "cloud"),
        ("v.pdf", "pdf", "cloud"),
    ]
    assert intake.fetch_cloud("https://example.com/x", client=c)[1][0].startswith(
        "Ссылка не похожа"
    )
    assert intake.is_cloud_url("https://yadi.sk/d/abc") and not intake.is_cloud_url("")


# --- маршрут /listing -----------------------------------------------------------------------
def _register_named(c, email: str, name: str, role: str = "seller") -> None:
    r = c.post(
        "/register", data={"email": email, "password": "12345678", "name": name, "role": role}
    )
    assert r.status_code == 303


def _row(listing_url: str):
    from landcheck.web.db import Listing, session

    with session() as db:
        row = db.get(Listing, int(listing_url.rsplit("/", 1)[1]))
        return row.status, row.proof_kind, dict(row.data)


def test_listing_from_materials_with_auto_verification(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(intake, "pdf_text", lambda raw, max_pages=20: EXTRACT)
    _register_named(client, "ivanov@example.com", "Иванов Иван Иванович")
    r = client.post(
        "/listing",
        data={"cad_num": "", "price": "", "notes": "лес рядом"},
        files=[
            ("files", ("vypiska.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")),
            ("files", ("IMG_1.jpg", io.BytesIO(_jpeg(4000, 3000, 30_000)), "image/jpeg")),
            ("files", ("IMG_1 copy.jpg", io.BytesIO(_jpeg(4000, 3000, 30_000)), "image/jpeg")),
            ("files", ("scan.png", io.BytesIO(_png(1240, 1754, 30_000)), "image/png")),
            (
                "files",
                (
                    "about.txt",
                    io.BytesIO(
                        "Цена 2,5 млн руб. Тел. +7 900 123-45-67. Свет по границе.".encode()
                    ),
                    "text/plain",
                ),
            ),
            ("files", ("fake.jpg", io.BytesIO(b"not an image at all"), "image/jpeg")),
        ],
    )
    assert r.status_code == 303, r.text
    url = r.headers["location"]
    page = _norm(client.get(url).text)
    assert "50:09:0060202:101" in page and "на проверке" in page
    assert "Материалы разобраны" in page and "кадастровый номер, цену, телефон" in page
    assert "Автосверка выписки" in page and "совпадает" in page and "И***в И. И." in page
    assert (
        "Иванов Иван Иванович" not in page.split("Автосверка")[1]
    )  # ФИО из документа не показываем
    assert "Не приняты файлы" in page and "fake.jpg" in page and "Фото (1)" in page
    assert "дубль" in page and 'value="2500000"' in page
    status, kind, data = _row(url)
    assert status == "pending" and kind == "owner"
    assert data["phone"] == "+79001234567" and "Свет по границе" in data["notes"]
    assert data["intake"]["auto"] == {"cad_num": True, "price": True, "phone": True, "notes": True}
    assert data["proof_check"]["verdict"] == "ok" and data["proof_files"] == ["01.pdf"]
    lid = url.rsplit("/", 1)[1]
    assert (settings.db_path.parent / "proofs" / lid / "01.pdf").exists()
    assert not (settings.db_path.parent / "proofs" / lid / "02.png").exists()
    assert "скан документа: не подтверждение" in page
    assert data["images"] == [f"/uploads/{lid}/01.jpg"]
    # модератор видит сверку построчно; после решения остаётся только итог
    _register_named(client, "admin@example.com", "Админ", role="buyer")
    _make_admin("admin@example.com")
    admin_page = client.get("/admin/listings").text
    assert "Автосверка выписки" in admin_page and "✓ Кадастровый номер" in admin_page
    assert "✓ Правообладатель: И***в И. И." in admin_page
    r = client.post(f"/admin/listings/{lid}/decide", data={"decision": "approve"})
    assert r.status_code == 303
    status, _, data = _row(url)
    assert status == "verified"
    assert set(data["proof_check"]) == {"verdict", "label", "checked_at"}
    # после решения страницы продавца и модератора рендерятся с итогом без строк
    page = client.get(url).text
    assert "Автосверка выписки" in page and "Правообладатель:" not in page
    assert client.get("/admin/listings?status=verified").status_code == 200


def test_listing_from_materials_as_agent(client, monkeypatch):  # noqa: F811
    """Брокер с выпиской собственника: вид «представитель» из формы, имя не сверяется."""
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(intake, "pdf_text", lambda raw, max_pages=20: EXTRACT)
    _register_named(client, "broker@example.com", "Агент Брокеров", role="broker")
    r = client.post(
        "/listing",
        data={"cad_num": "", "proof_kind": "agent"},
        files=[("files", ("vypiska.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf"))],
    )
    assert r.status_code == 303, r.text
    status, kind, data = _row(r.headers["location"])
    assert status == "pending" and kind == "agent"
    assert data["proof_check"]["verdict"] == "ok" and "представител" in data["proof_check"]["label"]
    assert "представитель" in client.get(r.headers["location"]).text.lower()


def test_listing_requires_cad_from_form_or_documents(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gemini_api_key", None)
    _register_named(client, "s2@example.com", "Пётр Сидоров")
    r = client.post("/listing", data={"cad_num": "", "notes": "x"})
    assert r.status_code == 400 and "не указан и не найден" in r.text
    r = client.post("/listing", data={"cad_num": "50:09:0060202:101", "notes": "x"})
    assert r.status_code == 303
    assert _row(r.headers["location"])[0] == "draft"


def test_verify_runs_check_mismatch_then_agent(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(intake, "pdf_text", lambda raw, max_pages=20: EXTRACT)
    _register_named(client, "sidorov@example.com", "Пётр Сидоров")
    r = client.post("/listing", data={"cad_num": "50:09:0060202:108", "price": "1 800 000"})
    url = r.headers["location"]
    r = client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf"))],
    )
    assert r.status_code == 303
    page = client.get(url).text
    assert "расхождение" in page and "не совпадает с именем продавца" in page
    assert "в документе 50:09:0060202:101, в объявлении 50:09:0060202:108" in page
    _, _, data = _row(url)
    assert data["proof_check"]["verdict"] == "mismatch"
    # представитель: имя не сверяем, номер объявления должен совпасть
    r = client.post("/listing", data={"cad_num": "50:09:0060202:101"})
    url2 = r.headers["location"]
    r = client.post(
        url2 + "/verify",
        data={"proof_kind": "agent"},
        files=[("proofs", ("egrn.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf"))],
    )
    assert r.status_code == 303
    _, kind, data = _row(url2)
    assert kind == "agent" and data["proof_check"]["verdict"] == "ok"
    assert "представител" in data["proof_check"]["label"]


def test_verify_rejects_scans_requires_electronic(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gemini_api_key", None)
    _register_named(client, "s3@example.com", "Иван Иванов")
    r = client.post("/listing", data={"cad_num": "50:09:0060202:101"})
    url = r.headers["location"]
    r = client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("scan.png", io.BytesIO(_png(1240, 1754, 20_000)), "image/png"))],
    )
    assert r.status_code == 400
    assert "электронной выписки" in r.text and "scan.png (фото или скан не принимаются)" in r.text
    assert _row(url)[0] == "draft"
    # PDF без текстового слоя принимается, но сверка честно говорит «не прочитан»
    r = client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("x.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf"))],
    )
    assert r.status_code == 303
    _, _, data = _row(url)
    assert (
        data["proof_check"]["verdict"] == "unknown"
        and "не прочитан" in data["proof_check"]["label"]
    )


def test_verify_zip_with_xml_and_sig(client, monkeypatch):  # noqa: F811
    """Архив Росреестра: XML читается структурно, подпись проверяется, свежесть и НСПД сходятся."""
    from datetime import UTC, datetime, timedelta

    from landcheck import sig
    from landcheck.web import materials
    from tests.test_egrn_xml import build

    monkeypatch.setattr(settings, "gemini_api_key", None)
    calls = []

    def fake_verify(content, sig_raw, *, roots=None):
        calls.append((len(content), sig_raw[:4], roots))
        return sig.SigResult(
            "ok", signer="Росреестр (ФГИС ЕГРН)", issuer="УЦ", signed_at="2026-10-23T10:00:00Z"
        )

    monkeypatch.setattr(materials.sig, "verify_detached", fake_verify)
    _register_named(client, "ivanov2@example.com", "Иванов Иван Иванович")
    r = client.post("/listing", data={"cad_num": "50:09:0060202:101"})
    url = r.headers["location"]
    # время в выписке считается московским: +4 ч по UTC даёт «через час» по Москве
    issued = (datetime.now(UTC) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S")
    archive = _zip({"docs/report.xml": build(issued=issued), "docs/report.xml.sig": SIG_DER})
    r = client.post(
        url + "/verify",
        data={"proof_kind": "owner"},
        files=[("proofs", ("rosreestr.zip", io.BytesIO(archive), "application/zip"))],
    )
    assert r.status_code == 303, r.text
    status, kind, data = _row(url)
    assert status == "pending" and kind == "owner"
    pc = data["proof_check"]
    assert pc["verdict"] == "ok" and "подпись Росреестра проверена" in pc["label"], pc
    assert pc["source"] == "xml" and pc["document"] == "report.xml"
    by = {i["key"]: i for i in pc["items"]}
    assert by["sig"]["status"] == "ok" and "Росреестр" in by["sig"]["note"]
    assert (
        by["fresh"]["status"] == "ok"
        and by["category"]["status"] == "ok"
        and by["vri"]["status"] == "ok"
    )
    assert by["holder"]["status"] == "ok" and "И***в И. И." in by["holder"]["note"]
    assert calls and calls[0][1] == SIG_DER[:4]
    lid = url.rsplit("/", 1)[1]
    assert (settings.db_path.parent / "proofs" / lid / "01.xml").exists()
    assert (settings.db_path.parent / "proofs" / lid / "01.sig").exists()
    page = client.get(url).text
    assert "✓ Подпись" in page and "✓ Свежесть" in page and "01.xml" in page and "01.sig" in page


def test_listing_from_cloud_folder(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gemini_api_key", None)
    files = {"photo.jpg": _jpeg(4000, 3000, 20_000), "docs/about.txt": "Цена 3 млн руб".encode()}
    monkeypatch.setattr(
        intake, "_client", lambda timeout: httpx.Client(transport=_yandex_transport(files))
    )
    _register_named(client, "cloud@example.com", "Иван Иванов")
    r = client.post(
        "/listing",
        data={"cad_num": "50:09:0060202:101", "cloud_url": "https://disk.yandex.ru/d/AbCdEf"},
    )
    assert r.status_code == 303, r.text
    status, _, data = _row(r.headers["location"])
    assert status == "draft" and len(data["images"]) == 1
    assert data["price_rub"] == 3_000_000 and data["intake"]["sources"] == ["Яндекс.Диск (2)"]
    page = client.get(r.headers["location"]).text
    assert "Яндекс.Диск (2)" in page and "Фото (1)" in page
    # ошибка облака не валит создание
    r = client.post(
        "/listing",
        data={"cad_num": "50:09:0060202:101", "cloud_url": "https://example.com/not-a-disk"},
    )
    assert r.status_code == 303
    assert "Ссылка не похожа" in client.get(r.headers["location"]).text


def test_cloud_api_error_is_reported(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gemini_api_key", None)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Ресурс не найден."})

    monkeypatch.setattr(
        intake, "_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler))
    )
    mats, errors = intake.fetch_cloud("https://disk.yandex.ru/d/missing")
    assert mats == [] and errors == ["Яндекс.Диск: Ресурс не найден. (404)"]
    assert json.dumps(errors, ensure_ascii=False)
