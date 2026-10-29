"""Связка приёма «как есть» с кабинетом: загрузки → материалы → фото в uploads/, документы
в proofs/, автосверка выписки ЕГРН → row.data["proof_check"].

Политика R13: подтверждение права — только электронная выписка (PDF с Госуслуг или XML из
Росреестра, по возможности с файлом подписи .sig). Фото и сканы документов не принимаются.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from landcheck import egrn, egrn_xml, gpzu, intake, sig
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
    """Электронные документы (pdf/xml) и их подписи — в приватное хранилище (до MAX_PROOFS)."""
    saved: list[str] = []
    d = proofs_dir(listing_id)
    n = 0
    for m in docs:
        if m.kind not in intake.DOC_KINDS or n >= MAX_PROOFS:
            continue
        n += 1
        d.mkdir(parents=True, exist_ok=True)
        name = f"{n:02d}.{m.kind}"
        (d / name).write_bytes(m.raw)
        saved.append(name)
        if m.sig:
            (d / f"{n:02d}.sig").write_bytes(m.sig)
            saved.append(f"{n:02d}.sig")
    return saved


def proof_materials(uploads: list[intake.Material]) -> tuple[list[intake.Material], list[str]]:
    """Файлы формы «Отправить на проверку» → электронные документы с подписями; остальное —
    в список непринятых с причиной."""
    docs: list[intake.Material] = []
    sigs: list[intake.Material] = []
    skipped: list[str] = []
    queue = list(uploads)
    while queue:
        m = queue.pop(0)
        if m.size > intake.MAX_FILE_BYTES or not m.raw:
            skipped.append(f"{m.name} (пустой или больше 10 МБ)")
        elif m.kind == "zip":
            inner, errs = intake.unpack_zip(m)
            queue[:0] = inner
            skipped.extend(errs)
        elif m.kind in intake.DOC_KINDS:
            if m.kind == "pdf":
                m.text = intake.pdf_text(m.raw)
            docs.append(m)
        elif m.kind == "sig":
            sigs.append(m)
        elif m.is_image:
            skipped.append(f"{m.name} (фото или скан не принимаются)")
        else:
            skipped.append(f"{m.name} (не PDF/XML/SIG)")
    free = intake.pair_signatures(docs, sigs)
    skipped.extend(f"{m.name} (подпись без документа)" for m in free)
    return docs, skipped


def has_electronic(docs: list[intake.Material]) -> bool:
    return any(m.kind in intake.DOC_KINDS for m in docs)


def materials_from_proof_files(listing_id: int) -> list[intake.Material]:
    """Сохранённые файлы подтверждения → документы с текстом PDF и подобранными подписями."""
    d = proofs_dir(listing_id)
    if not d.exists():
        return []
    files = {p.name: p for p in sorted(d.iterdir()) if p.is_file()}
    out: list[intake.Material] = []
    for name, p in files.items():
        if name.endswith(".sig"):
            continue
        raw = p.read_bytes()
        m = intake.Material(name=name, raw=raw, kind=intake.kind_of(raw, name))
        if m.kind == "pdf":
            m.text = intake.pdf_text(raw)
        sig_path = files.get(name.rsplit(".", 1)[0] + ".sig")
        if sig_path is not None:
            m.sig = sig_path.read_bytes()
        out.append(m)
    return out


def extract_of(m: intake.Material) -> egrn.Extract | None:
    """Поля выписки из документа: XML структурно, PDF по текстовому слою."""
    if m.kind == "xml":
        return egrn_xml.parse_xml_extract(m.raw)
    if m.kind == "pdf":
        # текст уже прочитан при приёме (intake.process / materials_from_proof_files):
        # повторное чтение битого PDF давало по три одинаковые строки в логе (Клавдий, 23.10)
        return egrn.parse_extract(m.text) if m.text.strip() else None
    return None


def proof_check(
    docs: list[intake.Material],
    *,
    cad_num: str,
    area_m2: float | None,
    seller_name: str,
    proof_kind: str,
    created_at: datetime | None = None,
    category: str | None = None,
    permitted_use: str | None = None,
) -> dict[str, Any] | None:
    """Сверка лучшей выписки среди документов (XML предпочтительнее PDF) с объявлением,
    продавцом, подписью и свежестью; None — нечего сверять."""
    best: tuple[egrn.Extract, intake.Material] | None = None
    for m in docs:
        ex = extract_of(m)
        if ex is None:
            continue
        key = (ex.is_extract, ex.source == "xml", ex.filled)
        if best is None or key > (best[0].is_extract, best[0].source == "xml", best[0].filled):
            best = (ex, m)
    if best is None:
        if docs:
            return egrn.CheckResult(
                verdict="unknown",
                label="Текст документа не прочитан (PDF без текстового слоя): нужна электронная "
                "выписка с Госуслуг или XML из Росреестра",
            ).to_dict()
        return None
    ex, doc = best
    signature = None
    if doc.sig:
        try:
            signature = sig.verify_detached(doc.raw, doc.sig, roots=settings.gost_roots).to_dict()
        except Exception as e:  # noqa: BLE001 — проверка подписи не должна валить отправку
            log.warning("sig verify failed: %s: %s", type(e).__name__, e)
            signature = {"status": "unavailable", "detail": type(e).__name__}
    res = egrn.compare(
        ex,
        cad_num=cad_num,
        area_m2=area_m2,
        seller_name=seller_name,
        proof_kind=proof_kind,
        signature=signature,
        created_at=created_at,
        category=category,
        permitted_use=permitted_use,
    ).to_dict()
    res["source"] = ex.source
    res["document"] = doc.name
    return res


def gpzu_of(docs: list[intake.Material]) -> dict[str, Any] | None:
    """ГПЗУ среди документов объявления: лучший по числу прочитанных полей, с именем файла."""
    best: tuple[int, gpzu.Gpzu, str] | None = None
    for m in docs:
        if m.kind != "pdf" or not m.text.strip():
            continue
        g = gpzu.parse(m.text)
        if g.is_gpzu and (best is None or g.filled > best[0]):
            best = (g.filled, g, m.name)
    if best is None:
        return None
    out = best[1].to_dict()
    out["document"] = best[2]
    return out
