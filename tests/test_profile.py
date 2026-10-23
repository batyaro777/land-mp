"""Профиль брокера: имя, бренд, логотип в шапке публичного отчёта и печатной версии."""

from __future__ import annotations

import io
from pathlib import Path

from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_profile_logo_and_report_header(client, sample_csv: Path):  # noqa: F811
    login(client)
    assert "Профиль и логотип" in client.get("/").text
    assert client.get("/profile").status_code == 200
    # не картинка — ошибка, профиль всё равно сохраняется
    r = client.post(
        "/profile",
        data={"name": "Антон", "brand_name": "Земля МО"},
        files={"logo": ("x.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert "PNG, JPG" in client.get(r.headers["location"]).text
    # картинка — сохраняется, видна в профиле
    r = client.post(
        "/profile",
        data={"name": "Антон М.", "brand_name": "Алтай Земля", "contact": "@anton"},
        files={"logo": ("logo.png", io.BytesIO(PNG), "image/png")},
    )
    assert r.headers["location"] == "/profile?saved=1"
    prof = client.get("/profile?saved=1").text
    assert "/uploads/logos/1.png" in prof and "Алтай Земля" in prof and "Сохранено" in prof
    assert client.get("/uploads/logos/1.png").status_code == 200
    # шапка отчёта: публичная ссылка с логотипом и брендом, приватная — только для печати
    r = client.post("/checks", data={"numbers": sample_csv.read_text(encoding="utf-8")})
    url = r.headers["location"]
    rep = client.get(url + "/items/50:09:0060202:101").text
    assert 'class="report-brand print-only"' in rep and "Скачать PDF" in rep
    import re

    token = re.search(r"/r/([A-Za-z0-9_-]+)", rep).group(1)
    pub = client.get(f"/r/{token}").text
    assert '<div class="report-brand">' in pub
    assert "/uploads/logos/1.png" in pub and "Алтай Земля" in pub and "@anton" not in pub
    # удаление логотипа
    client.post(
        "/profile", data={"name": "Антон М.", "brand_name": "Алтай Земля", "remove_logo": "1"}
    )
    assert "/uploads/logos/1.png" not in client.get("/profile").text
    assert "/uploads/logos/" not in client.get(f"/r/{token}").text
