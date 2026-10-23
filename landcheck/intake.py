"""Приём материалов «как есть» (К2, ТЗ v2): файлы любого вида и публичная папка Яндекс.Диска →
фото участка, сканы документов, факты для объявления.

Продавец не сортирует ничего сам: мы различаем фото и документы, убираем дубли и мелочь,
достаём из текста кадастровый номер, цену, площадь и телефон. Картинки различает Gemini
(один запрос на пачку); без ключа работает эвристика по размеру и пропорциям листа.
Исключений наружу нет: всё, что не удалось, попадает в `dropped`/`errors` с причиной.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import struct
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from landcheck.inputs import CAD_RE, parse_number
from landcheck.llm import parse_json_text
from landcheck.region import DEFAULT_MODEL, FALLBACK_MODELS, GEMINI_URL

log = logging.getLogger(__name__)
logging.getLogger("pypdf").setLevel(logging.ERROR)  # битые PDF от продавцов — не предупреждения

YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources"
CLOUD_HOSTS = ("disk.yandex.", "yadi.sk", "disk.360.yandex.")
MAX_CLOUD_FILES = 30
MAX_FILE_BYTES = 10 * 1024 * 1024
MIN_PHOTO_BYTES = 15 * 1024  # мельче — иконки, превью, обрезки
MAX_PHOTOS = 10
IMAGE_KINDS = ("jpg", "png", "webp")
DOC_KINDS = ("pdf", "jpg", "png")  # что принимаем как подтверждение права
TEXT_KINDS = ("txt", "docx")

_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"%PDF", "pdf"),
)


@dataclass
class Material:
    """Один файл из загрузки или облака с тем, что мы о нём поняли."""

    name: str
    raw: bytes
    kind: str  # jpg|png|webp|pdf|docx|txt|other
    source: str = "upload"  # upload|cloud
    text: str = ""  # текст документа: PDF/DOCX/TXT или распознанный скан
    is_document: bool | None = None  # для картинок: скан документа (True) или фото (False)
    score: int = 0  # качество фото 1–5 по оценке модели; 0 — не оценивалось

    @property
    def is_image(self) -> bool:
        return self.kind in IMAGE_KINDS

    @property
    def size(self) -> int:
        return len(self.raw)


@dataclass
class Facts:
    """Что удалось вытащить из текстов: кадастровые номера по частоте, цена, площадь, телефон."""

    cad_nums: list[str] = field(default_factory=list)
    price_rub: float | None = None
    area_sotka: float | None = None
    phone: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cad_nums": self.cad_nums,
            "price_rub": self.price_rub,
            "area_sotka": self.area_sotka,
            "phone": self.phone,
        }


@dataclass
class Intake:
    """Итог разбора: фото в порядке показа, документы на проверку права, факты, что отброшено."""

    photos: list[Material] = field(default_factory=list)
    documents: list[Material] = field(default_factory=list)
    notes: list[Material] = field(default_factory=list)  # txt/docx продавца — в заметки
    facts: Facts = field(default_factory=Facts)
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (имя, причина)
    errors: list[str] = field(default_factory=list)
    classified_by: str = "none"  # gemini|heuristic|none

    @property
    def notes_text(self) -> str:
        return "\n\n".join(m.text.strip() for m in self.notes if m.text.strip())[:4000]

    def to_dict(self) -> dict[str, Any]:
        return {
            "photos": len(self.photos),
            "documents": [m.name for m in self.documents],
            "notes": [m.name for m in self.notes],
            "facts": self.facts.to_dict(),
            "dropped": [f"{n} — {why}" for n, why in self.dropped],
            "errors": self.errors,
            "classified_by": self.classified_by,
        }


# --- тип и текст файла ------------------------------------------------------------------------
def kind_of(raw: bytes, name: str = "") -> str:
    """Тип по сигнатуре, а не по расширению: расширения в облаке врут."""
    for sig, kind in _SIGNATURES:
        if raw[: len(sig)] == sig:
            return kind
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            return "other"
        return "other"
    low = name.lower()
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    # текст принимаем по расширению или без расширения; «фото» с текстом внутри — мусор
    if raw and (ext in ("txt", "md", "text") or not ext) and _looks_like_text(raw):
        return "txt"
    return "other"


def _looks_like_text(raw: bytes) -> bool:
    sample = raw[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def docx_text(raw: bytes) -> str:
    """Текст из DOCX без зависимостей: абзацы word/document.xml."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return _clean(text)


def pdf_text(raw: bytes, max_pages: int = 20) -> str:
    """Текстовый слой PDF (выписки из Госуслуг и Росреестра его содержат). Скан без слоя → ''."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover — зависимость в pyproject
        log.warning("pypdf не установлен: текст PDF недоступен")
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        parts = []
        for page in reader.pages[:max_pages]:
            parts.append(page.extract_text() or "")
    except Exception as e:  # noqa: BLE001 — битые PDF не должны валить приём
        log.info("pdf_text: %s: %s", type(e).__name__, e)
        return ""
    return _clean("\n".join(parts))


def text_of(m: Material) -> str:
    if m.kind == "pdf":
        return pdf_text(m.raw)
    if m.kind == "docx":
        return docx_text(m.raw)
    if m.kind == "txt":
        return _clean(m.raw.decode("utf-8", "ignore"))
    return ""


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


# --- размер картинки без Pillow (эвристика «лист документа») ----------------------------------
def image_size(raw: bytes) -> tuple[int, int] | None:
    """(ширина, высота) для JPEG/PNG/WEBP по заголовку; None — не разобрали."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            w, h = struct.unpack(">II", raw[16:24])
            return (w, h) if w and h else None
        if raw[:3] == b"\xff\xd8\xff":
            return _jpeg_size(raw)
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return _webp_size(raw)
    except (struct.error, IndexError):
        return None
    return None


def _jpeg_size(raw: bytes) -> tuple[int, int] | None:
    i = 2
    n = len(raw)
    while i + 9 < n:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", raw[i + 5 : i + 9])
            return (w, h) if w and h else None
        i += 2 + seg_len
    return None


def _webp_size(raw: bytes) -> tuple[int, int] | None:
    chunk = raw[12:16]
    if chunk == b"VP8 " and len(raw) >= 30:
        w, h = struct.unpack("<HH", raw[26:30])
        return (w & 0x3FFF, h & 0x3FFF)
    if chunk == b"VP8L" and len(raw) >= 25:
        b = raw[21:25]
        w = 1 + (((b[1] & 0x3F) << 8) | b[0])
        h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return (w, h)
    if chunk == b"VP8X" and len(raw) >= 30:
        w = 1 + int.from_bytes(raw[24:27], "little")
        h = 1 + int.from_bytes(raw[27:30], "little")
        return (w, h)
    return None


def looks_like_document(raw: bytes) -> bool | None:
    """Без модели: вертикальный лист с пропорцией A4 (1:1.414 ± 0.05) — почти наверняка скан.

    Фото участка снимают горизонтально или с пропорциями камеры 3:4 (1.33) и 9:16 (1.78),
    обе вне полосы. None — размер не разобран, решение за вызывающим.
    """
    size = image_size(raw)
    if not size:
        return None
    w, h = size
    if h <= w:
        return False
    ratio = h / w
    return 1.37 <= ratio <= 1.47


# --- факты из текста --------------------------------------------------------------------------
_MONEY_RE = re.compile(r"(\d[\d\s  ]*(?:[.,]\d+)?)\s*(млн|тыс\.?|руб\w*|₽|р\.)", re.I)
_AREA_RE = re.compile(
    r"(\d[\d\s  ]*(?:[.,]\d+)?)\s*(?:\+/-\s*\d+\s*)?(сот\w*|га\b|кв\.?\s*м|м2|м²|кв\.м)",
    re.I,
)
_PHONE_RE = re.compile(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b")


def extract_facts(text: str) -> Facts:
    """Кадастровые номера (по частоте), цена (самая крупная сумма), площадь, телефон."""
    f = Facts()
    if not text:
        return f
    nums = [re.sub(r"\s+", "", m.group(0)) for m in CAD_RE.finditer(text)]
    f.cad_nums = [n for n, _ in Counter(nums).most_common()]
    prices = []
    for m in _MONEY_RE.finditer(text):
        v = parse_number(m.group(1) + " " + m.group(2))
        if v is not None and 100_000 <= v <= 5_000_000_000:
            prices.append(v)
    if prices:
        f.price_rub = max(prices)
    for m in _AREA_RE.finditer(text):
        v = parse_number(m.group(1))
        if v is None or v <= 0:
            continue
        unit = m.group(2).lower()
        if unit.startswith("сот"):
            sotka = v
        elif unit.startswith("га"):
            sotka = v * 100
        else:
            sotka = v / 100
        if 0.5 <= sotka <= 100_000:
            f.area_sotka = round(sotka, 2)
            break
    pm = _PHONE_RE.search(text)
    if pm:
        f.phone = re.sub(r"[^\d+]", "", pm.group(0))
        if f.phone.startswith("8"):
            f.phone = "+7" + f.phone[1:]
    return f


# --- Яндекс.Диск: публичная папка --------------------------------------------------------------
def is_cloud_url(url: str) -> bool:
    host = (urlparse(url.strip()).hostname or "").lower()
    return bool(url.strip()) and any(host.startswith(h) or h in host for h in CLOUD_HOSTS)


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True)


def yandex_list(
    public_url: str,
    *,
    client: httpx.Client,
    limit: int = MAX_CLOUD_FILES,
    _path: str = "/",
    depth=0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Файлы публичной папки (рекурсивно до 2 уровней): name, path, size, mime_type."""
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    params: dict[str, Any] = {"public_key": public_url, "limit": 100}
    if _path != "/":
        params["path"] = _path
    try:
        r = client.get(YANDEX_API, params=params)
    except httpx.HTTPError as e:
        return files, [f"Яндекс.Диск недоступен: {type(e).__name__}"]
    if r.status_code != 200:
        try:
            msg = r.json().get("message") or r.json().get("description") or str(r.status_code)
        except ValueError:
            msg = str(r.status_code)
        return files, [f"Яндекс.Диск: {msg} ({r.status_code})"]
    data = r.json()
    if data.get("type") == "file":
        files.append(_item(data, data.get("path") or "/"))
        return files, errors
    for it in (data.get("_embedded") or {}).get("items") or []:
        if len(files) >= limit:
            errors.append(f"в папке больше {limit} файлов: взяли первые {limit}")
            break
        if it.get("type") == "dir":
            if depth >= 2:
                continue
            sub, sub_err = yandex_list(
                public_url,
                client=client,
                limit=limit - len(files),
                _path=it.get("path") or "/",
                depth=depth + 1,
            )
            files.extend(sub)
            errors.extend(sub_err)
        elif it.get("type") == "file":
            files.append(_item(it, it.get("path") or ""))
    return files, errors


def _item(it: dict[str, Any], path: str) -> dict[str, Any]:
    return {
        "name": it.get("name") or path.rsplit("/", 1)[-1],
        "path": path,
        "size": int(it.get("size") or 0),
        "mime_type": it.get("mime_type") or "",
    }


def yandex_download(public_url: str, path: str, *, client: httpx.Client) -> bytes | None:
    params: dict[str, Any] = {"public_key": public_url}
    if path and path != "/":
        params["path"] = path
    try:
        r = client.get(YANDEX_API + "/download", params=params)
        if r.status_code != 200:
            return None
        href = r.json().get("href")
        if not href:
            return None
        with client.stream("GET", href) as resp:
            if resp.status_code != 200:
                return None
            buf = io.BytesIO()
            for chunk in resp.iter_bytes():
                buf.write(chunk)
                if buf.tell() > MAX_FILE_BYTES:
                    return None
            return buf.getvalue()
    except (httpx.HTTPError, ValueError):
        return None


def fetch_cloud(
    public_url: str, *, client: httpx.Client | None = None, timeout: float = 30.0
) -> tuple[list[Material], list[str]]:
    """Все файлы публичной папки как материалы. Ошибки — текстом, без исключений."""
    url = public_url.strip()
    if not is_cloud_url(url):
        return [], ["Ссылка не похожа на публичную папку Яндекс.Диска (disk.yandex.ru/d/…)"]
    own = client is None
    c = client or _client(timeout)
    try:
        files, errors = yandex_list(url, client=c)
        out: list[Material] = []
        for f in files:
            if f["size"] > MAX_FILE_BYTES:
                errors.append(f"{f['name']} — больше 10 МБ, пропущен")
                continue
            raw = yandex_download(url, f["path"], client=c)
            if raw is None:
                errors.append(f"{f['name']} — не скачался")
                continue
            out.append(
                Material(name=f["name"], raw=raw, kind=kind_of(raw, f["name"]), source="cloud")
            )
        return out, errors
    finally:
        if own:
            c.close()


# --- Gemini: скан или фото, текст скана, оценка фото ------------------------------------------
_VISION_PROMPT = (
    "Ниже {n} изображений, пронумерованных по порядку (1 — первое). Это материалы продавца "
    "земельного участка. Для каждого определи:\n"
    'kind: "document" — скан или фото документа/экрана с текстом (выписка ЕГРН, договор, '
    'свидетельство, скриншот, карта с надписями); "photo" — фотография местности, участка, '
    'дороги, построек, видов; "other" — остальное (логотипы, мемы, случайные картинки).\n'
    'text: для document — весь читаемый текст дословно, с сохранением строк; иначе "".\n'
    "score: для photo — насколько кадр годится первым фото объявления, 1–5 "
    "(5 — ясный ракурс участка в хорошую погоду, 1 — тёмное, размытое, случайное); иначе 0.\n"
    'Верни только JSON: {{"items": [{{"n": 1, "kind": "photo", "text": "", "score": 4}}]}}'
)
_BATCH_BYTES = 12 * 1024 * 1024
_BATCH_ITEMS = 8
_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def classify_images(
    images: list[Material],
    *,
    api_key: str | None,
    model: str = DEFAULT_MODEL,
    timeout: float = 90.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
) -> str:
    """Заполняет is_document/text/score у картинок. Возвращает, чем классифицировали.

    Без ключа или при ошибке модели — эвристика по пропорциям листа (см. looks_like_document).
    """
    if not images:
        return "none"
    used = "heuristic"
    if api_key:
        ok_all = True
        for batch in _batches(images):
            res = _gemini_vision(
                batch, api_key=api_key, model=model, timeout=timeout, fallbacks=fallbacks
            )
            if res is None:
                ok_all = False
                continue
            for m, item in zip(batch, res, strict=False):
                if item is None:
                    continue
                kind = str(item.get("kind") or "").lower()
                if kind == "document":
                    m.is_document = True
                    m.text = _clean(str(item.get("text") or ""))
                elif kind == "photo":
                    m.is_document = False
                    try:
                        m.score = max(0, min(5, int(item.get("score") or 0)))
                    except (TypeError, ValueError):
                        m.score = 0
                elif kind == "other":
                    m.is_document = None
                    m.score = -1
        used = "gemini" if ok_all else "gemini+heuristic"
    for m in images:
        if m.is_document is None and m.score != -1:
            m.is_document = looks_like_document(m.raw)
    return used


def _batches(images: list[Material]) -> list[list[Material]]:
    out: list[list[Material]] = []
    cur: list[Material] = []
    size = 0
    for m in images:
        if cur and (len(cur) >= _BATCH_ITEMS or size + m.size > _BATCH_BYTES):
            out.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += m.size
    if cur:
        out.append(cur)
    return out


def _gemini_vision(
    batch: list[Material], *, api_key: str, model: str, timeout: float, fallbacks: tuple[str, ...]
) -> list[dict[str, Any] | None] | None:
    parts: list[dict[str, Any]] = [{"text": _VISION_PROMPT.format(n=len(batch))}]
    for i, m in enumerate(batch, start=1):
        parts.append({"text": f"Изображение {i}:"})
        parts.append(
            {
                "inline_data": {
                    "mime_type": _MIME.get(m.kind, "image/jpeg"),
                    "data": base64.b64encode(m.raw).decode("ascii"),
                }
            }
        )
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    for mdl in (model, *[f for f in fallbacks if f != model]):
        try:
            r = httpx.post(
                GEMINI_URL.format(model=mdl), params={"key": api_key}, json=body, timeout=timeout
            )
        except httpx.HTTPError as e:
            log.warning("vision %s: %s", mdl, type(e).__name__)
            return None
        if r.status_code != 200:
            try:
                status = (r.json().get("error") or {}).get("status") or str(r.status_code)
            except ValueError:
                status = str(r.status_code)
            log.warning("vision %s: Gemini %s", mdl, status)
            if status in ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "NOT_FOUND"):
                continue
            return None
        cand = (r.json().get("candidates") or [{}])[0]
        text = "".join(
            p.get("text", "")
            for p in (cand.get("content") or {}).get("parts") or []
            if isinstance(p, dict)
        )
        data = parse_json_text(text)
        items = (data or {}).get("items")
        if not isinstance(items, list):
            return None
        by_n: dict[int, dict[str, Any]] = {}
        for it in items:
            if isinstance(it, dict):
                try:
                    by_n[int(it.get("n"))] = it
                except (TypeError, ValueError):
                    continue
        return [by_n.get(i) for i in range(1, len(batch) + 1)]
    return None


# --- сборка ------------------------------------------------------------------------------------
def process(
    materials: list[Material],
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 90.0,
) -> Intake:
    """Дубли и мусор — в dropped; картинки — на фото и сканы; тексты — в факты и заметки."""
    res = Intake()
    seen: set[str] = set()
    images: list[Material] = []
    for m in materials:
        if not m.raw:
            res.dropped.append((m.name, "пустой файл"))
            continue
        if m.size > MAX_FILE_BYTES:
            res.dropped.append((m.name, "больше 10 МБ"))
            continue
        h = hashlib.sha1(m.raw).hexdigest()
        if h in seen:
            res.dropped.append((m.name, "дубль"))
            continue
        seen.add(h)
        if m.kind == "other":
            res.dropped.append((m.name, "неизвестный формат"))
            continue
        if m.is_image:
            images.append(m)
        elif m.kind == "pdf":
            m.text = m.text or text_of(m)
            res.documents.append(m)
        else:  # txt/docx — слова продавца
            m.text = m.text or text_of(m)
            res.notes.append(m)
    if images:
        res.classified_by = classify_images(images, api_key=api_key, model=model, timeout=timeout)
    photos: list[Material] = []
    for m in images:
        if m.is_document:
            res.documents.append(m)
        elif m.score == -1:
            res.dropped.append((m.name, "не фото участка"))
        elif m.size < MIN_PHOTO_BYTES:
            res.dropped.append((m.name, "слишком маленькое изображение"))
        else:
            photos.append(m)
    photos.sort(key=lambda m: (-m.score, -m.size))
    res.photos = photos[:MAX_PHOTOS]
    for m in photos[MAX_PHOTOS:]:
        res.dropped.append((m.name, f"больше {MAX_PHOTOS} фото"))
    text = "\n".join(m.text for m in (*res.documents, *res.notes) if m.text)
    res.facts = extract_facts(text)
    return res
