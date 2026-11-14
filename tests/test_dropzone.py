"""Загрузка файлов перетягиванием (Тони, 09.11): скрипт подключён на всех страницах, отдаётся
статикой, синтаксически цел; поля файлов несут подсказки для зоны."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_verification import _register
from tests.test_web import client  # noqa: F401 — фикстура клиента кабинета

JS = Path(__file__).resolve().parents[1] / "landcheck" / "web" / "static" / "dropzone.js"


def test_dropzone_script_served_and_included(client):  # noqa: F811
    r = client.get("/static/dropzone.js")
    assert r.status_code == 200 and "input[type=file]" in r.text and "DataTransfer" in r.text
    css = client.get("/static/app.css").text
    assert ".dropzone" in css and ".dz-input" in css
    _register(client, "dz@example.com")
    page = client.get("/listing/new").text
    assert "/static/dropzone.js" in page
    assert 'data-dz-text="Перетащите сюда фото участка' in page
    client.post("/logout")
    client.cookies.clear()
    page = client.get("/market").text
    assert "/static/dropzone.js" in page  # базовый шаблон — на всех страницах, и без входа


def test_dropzone_covers_every_file_input():
    templates = Path(__file__).resolve().parents[1] / "landcheck" / "web" / "templates"
    found = [p.name for p in templates.glob("*.html") if 'type="file"' in p.read_text("utf-8")]
    # все формы с файлами наследуют base.html, где подключён скрипт — зона появится на каждой
    assert set(found) >= {"listing_new.html", "listing.html", "report.html", "_gpzu.html"}
    for name in found:
        if name.startswith("_"):
            continue  # фрагменты вставляются в страницы с base.html
        assert 'extends "base.html"' in (templates / name).read_text("utf-8"), name


@pytest.mark.skipif(shutil.which("node") is None, reason="node не установлен")
def test_dropzone_js_syntax():
    subprocess.run(["node", "--check", str(JS)], check=True, timeout=30)
