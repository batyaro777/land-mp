"""Связка приёма «как есть» с кабинетом: загрузки → материалы → фото в uploads/, документы
в proofs/, автосверка выписки ЕГРН → row.data["proof_check"]."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from landcheck import egrn, intake
from landcheck.web.config import settings

log = logging.getLogger(__name__)

MAX_UPLOAD_FILES = 25
MAX_PROOFS = 3


def proofs_dir(listing_id: int) -> Path:
    return settings.db_path.parent / "proofs" / str(listing_id)


def uploads_dir(listing_id: int) -> Path:
    return settings.db_path.parent / "uploads" / str(listing_id)


async def materials_from_uploads(files: list[UploadFile] | None) -> list[intake.Material]:
    out: list[intake.Material] = []
    for f in (files or [])[:MAX_UPLOAD_FILES]:
        if not f.filename:
            continue
        raw = await f.read(intake.MAX_FILE_BYTES + 1)
        out.append(intake.Material(name=f.filename, raw=raw, kind=intake.kind_of(raw, f.filename)))
    return out


def gather(uploads: list[intake.Material], cloud_url: str = "") -> tuple[intake.Intake, list[str]]:
    """Загрузки плюс облако → разобранный Intake; второй элемент — откуда брали."""
    sources = []
    materials = list(uploads)
    if uploads:
        sources.append(f"файлы ({len(uploads)})")
    errors: list[str] = []
    if cloud_url.strip():
        cloud, cloud_err = intake.fetch_cloud(cloud_url)
        materials.extend(cloud)
        errors.extend(cloud_err)
        sources.append(f"Яндекс.Диск ({len(cloud)})")
    res = intake.process(materials, api_key=settings.gemini_api_key, model=settings.gemini_model)
    res.errors = errors + res.errors
    return res, sources


def save_photos(listing_id: int, photos: list[intake.Material]) -> list[str]:
    paths: list[str] = []
    if not photos:
        return paths
    d = uploads_dir(listing_id)
    d.mkdir(parents=True, exist_ok=True)
    for i, m in enumerate(photos, start=1):
        target = d / f"{i:02d}.{m.kind}"
        target.write_bytes(m.raw)
        paths.append(f"/uploads/{listing_id}/{target.name}")
    return paths


def save_documents(listing_id: int, docs: list[intake.Material]) -> list[str]:
    """Сканы и PDF — в приватное хранилище подтверждений (до MAX_PROOFS, как в /verify)."""
    saved: list[str] = []
    d = proofs_dir(listing_id)
    for m in docs:
        if m.kind not in intake.DOC_KINDS or len(saved) >= MAX_PROOFS:
            continue
        d.mkdir(parents=True, exist_ok=True)
        name = f"{len(saved) + 1:02d}.{m.kind}"
        (d / name).write_bytes(m.raw)
        saved.append(name)
    return saved


def document_texts(docs: list[intake.Material]) -> list[str]:
    """Тексты документов; сканы без распознанного текста пропускаем."""
    return [m.text for m in docs if m.text and m.text.strip()]


def materials_from_proof_files(listing_id: int) -> list[intake.Material]:
    """Сохранённые файлы подтверждения → материалы с текстом (PDF-слой, сканы через Gemini)."""
    d = proofs_dir(listing_id)
    if not d.exists():
        return []
    out: list[intake.Material] = []
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        raw = p.read_bytes()
        m = intake.Material(name=p.name, raw=raw, kind=intake.kind_of(raw, p.name))
        if m.kind == "pdf":
            m.text = intake.pdf_text(raw)
        out.append(m)
    images = [m for m in out if m.is_image]
    if images:
        intake.classify_images(images, api_key=settings.gemini_api_key, model=settings.gemini_model)
    return out


def proof_check(
    docs: list[intake.Material],
    *,
    cad_num: str,
    area_m2: float | None,
    seller_name: str,
    proof_kind: str,
) -> dict[str, Any] | None:
    """Сверка лучшей выписки среди документов с объявлением и продавцом; None — нечего сверять."""
    texts = document_texts(docs)
    if not texts:
        if docs:
            return egrn.CheckResult(
                verdict="unknown",
                label="Текст документа не прочитан (скан без текстового слоя): сверьте вручную",
            ).to_dict()
        return None
    ex = egrn.best_extract(texts)
    if ex is None:
        return None
    return egrn.compare(
        ex, cad_num=cad_num, area_m2=area_m2, seller_name=seller_name, proof_kind=proof_kind
    ).to_dict()


def has_extract(docs: list[intake.Material]) -> bool:
    return any(egrn.parse_extract(t).is_extract for t in document_texts(docs))
