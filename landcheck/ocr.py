"""OCR сканов PDF для «первоисточников» (ТЗ v3, п. 2.5, этап 2): документы администраций часто
выложены сканами без текстового слоя (Сочи, приёмка 01.11). Страницы рендерятся pypdfium2 и
распознаются tesseract (rus+eng) с лимитом страниц и времени; текст помечается как OCR — в нём
возможны ошибки распознавания, ссылка ведёт на саму страницу документа."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

log = logging.getLogger("landcheck.ocr")

OCR_MAX_PAGES = 15
OCR_DEADLINE_S = 60.0
OCR_DPI = 200
OCR_LANG = "rus+eng"
OCR_PAGE_TIMEOUT_S = 40
_O_RE = re.compile(r"(?<=[\d:])[OoОо]+(?=[\d:])")  # буквы О внутри кадастрового номера → нули


def available() -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        import pypdfium2  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def normalize(text: str) -> str:
    """Типичные ошибки OCR в номерах: О вместо 0, пробелы у двоеточий, двойные пробелы."""
    text = _O_RE.sub(lambda m: "0" * len(m.group(0)), text)
    text = re.sub(r"(?<=\d)\s*:\s*(?=\d)", ":", text)
    return re.sub(r"[ \t\xa0]+", " ", text)


def render_png(pdf: Any, index: int, dpi: int = OCR_DPI) -> bytes:
    import io

    page = pdf[index]
    try:
        image = page.render(scale=dpi / 72).to_pil()
    finally:
        page.close()
    buf = io.BytesIO()
    image.convert("L").save(buf, format="PNG")
    return buf.getvalue()


def tesseract_env() -> dict[str, str]:
    """Один поток на процесс: документы распознаются параллельно (несколько tesseract сразу), и
    многопоточный OpenMP на 4 ядрах давал таймауты страниц (прод 01.11: 0 страниц за 80 с против
    всех страниц за 24 с с OMP_THREAD_LIMIT=1)."""
    env = dict(os.environ)
    env.setdefault("OMP_THREAD_LIMIT", "1")
    return env


def recognize_png(png: bytes, lang: str = OCR_LANG) -> str:
    """tesseract из stdin в stdout; без временных файлов."""
    proc = subprocess.run(  # noqa: S603 — фиксированная команда, данные только через stdin
        ["tesseract", "stdin", "stdout", "-l", lang, "--psm", "4"],
        input=png,
        capture_output=True,
        timeout=OCR_PAGE_TIMEOUT_S,
        check=False,
        env=tesseract_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-200:] or "tesseract")
    return proc.stdout.decode("utf-8", errors="replace")


def pdf_ocr_pages(
    raw: bytes,
    *,
    max_pages: int = OCR_MAX_PAGES,
    deadline_s: float = OCR_DEADLINE_S,
) -> tuple[list[dict[str, Any]], str | None]:
    """Страницы с распознанным текстом и пометка, если распознаны не все."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(raw)
    try:
        total = len(pdf)
        pages: list[dict[str, Any]] = []
        started = time.monotonic()
        done = 0
        for index in range(min(total, max_pages)):
            if time.monotonic() - started > deadline_s:
                break
            try:
                text = normalize(recognize_png(render_png(pdf, index)))
            except Exception as exc:  # noqa: BLE001 — одна страница не должна валить документ
                log.warning("ocr page %s: %s", index + 1, exc)
                text = ""
            done = index + 1
            if text.strip():
                pages.append({"n": index + 1, "text": text})
        partial = None
        if done < total:
            why = "лимит времени" if done < min(total, max_pages) else f"лимит {max_pages} страниц"
            partial = f"распознаны OCR первые {done} из {total} страниц ({why})"
        return pages, partial
    finally:
        pdf.close()
