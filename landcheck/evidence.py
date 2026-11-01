"""Первоисточники до страницы (ТЗ v3, п. 2.5, этап 2).

Тони, 01.11: «продукт не выдумывает — ссылка на конкретную страницу, где строка или пункт
первоисточника». Андрей (зум 28.10, 01:00:53): «ссылки не хватает на первоисточник… объяснить,
почему это важно». Здесь документы администрации (проекты межевания, ПЗЗ, постановления), найденные
реестром и автопоиском, скачиваются, из них достаётся текст по страницам, и ищутся упоминания самого
участка, его кадастрового квартала и населённого пункта. Результат — цитата со ссылкой на документ и
страницу (`#page=N` для PDF). Упоминание номера участка или квартала в ПМТ/постановлении — флаг R25.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import io
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from landcheck import ocr, ruproxy, websearch
from landcheck.inputs import CAD_RE

log = logging.getLogger(__name__)

MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 400
MAX_DOCS = 12
CONTEXT = 160
MAX_QUOTES_PER_TERM = 3
# Тайминги (прод 01.11: скачивание через RU-шлюз держало фон дольше 6 минут): на один документ —
# не больше минуты скачивания и минуты разбора, вся проверка — 3 минуты, документы параллельно.
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
DOWNLOAD_DEADLINE_S = 60.0
ATTEMPT_DEADLINE_S = 45.0  # попытка целиком (рукопожатие с шлюзом не покрывается таймаутами httpx)
PARSE_DEADLINE_S = 60.0
BUDGET_S = 180.0
WORKERS = 4
# Страницы разделов администрации («Проекты планировок», «Документы») обычно ведут на вложенные
# PDF — их читаем второй волной, по подсказкам в тексте ссылки (прод 01.11: на индексных страницах
# упоминаний 0, сами проекты лежат файлами).
NESTED_KINDS = ("registry", "pmt", "npa", "notice", "hearings")
MAX_LINKS_PER_PAGE = 40
MAX_NESTED_PER_PAGE = 5
MAX_NESTED = 10
NESTED_MIN_BUDGET_S = 30.0
NESTED_HINT_RE = re.compile(
    r"межеван|планировк|постановлен|распоряжен|извещен|слушан|проект|решени|схем|аукцион|торг", re.I
)
_HREF_RE = re.compile(r"<a\s[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_SIZE_RE = re.compile(
    r"^\s*[\d.,]+\s*(?:[КМГ]б|[КМГ]Б|[KMG]B|байт)\b[\s,;:—-]*|[\s(]*[\d.,]+\s*(?:[КМГ]б|[КМГ]Б|[KMG]B)\)?\s*$",
    re.I,
)
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w-]+)", re.I)
# Хосты, закрытые для дата-центров (диагностика Клавдия 01.11: с RUVDS и из NL SYN-таймаут, трасса
# глохнет в сети Ростелекома перед подсетью «Электронного правительства»): не тратим 45 с,
# честно говорим, что нужен ручной просмотр. Переопределяется LANDCHECK_UNREACHABLE_HOSTS.
UNREACHABLE_HOSTS = ("gosweb.gosuslugi.ru", "do.gosuslugi.ru", "www.gosuslugi.ru", "gosuslugi.ru")
DOC_KEY_VERSION = "doc2"  # doc → doc2: до 01.11 HTML читался только как utf-8, кэш был битым
UA = "Mozilla/5.0 (compatible; land-mp/1.0; +https://land-mp.dnai.engineering)"
# документы, где упоминание участка — обременение или занятость (для флага R25)
STRONG_KINDS = ("pmt", "npa", "notice", "registry", "hearings")
KIND_ORDER = ("registry", "pmt", "npa", "notice", "pzz", "hearings", "genplan")
SKIP_KINDS = ("gisogd",)
LOCALITY_RE = re.compile(
    r"(?:\b(?:д|дер|деревня|с|село|п|пос|посёлок|поселок|х|хутор|ст|станица|аул)\.?\s+)"
    r"([А-ЯЁ][а-яё-]{2,}(?:\s[А-ЯЁ][а-яё-]{2,})?)"
)


def doc_key(url: str) -> str:
    return f"{DOC_KEY_VERSION}:" + hashlib.sha1(url.encode("utf-8")).hexdigest()


def quarter_of(cad_num: str | None) -> str | None:
    parts = (cad_num or "").split(":")
    return ":".join(parts[:3]) if len(parts) == 4 else None


def locality_terms(address: str | None, locality: str | None) -> list[str]:
    """Название населённого пункта из адреса НСПД («д. Дешовки», «с. Элекмонар»)."""
    out: list[str] = []
    for text in (address or "", locality or ""):
        for m in LOCALITY_RE.finditer(text):
            name = m.group(1).strip()
            if name and name not in out:
                out.append(name)
    return out[:2]


def terms_for(parcel: dict[str, Any], locality: str | None) -> list[dict[str, str]]:
    cad = parcel.get("cad_num") or ""
    terms: list[dict[str, str]] = []
    if cad:
        terms.append({"kind": "cad", "value": cad, "label": "кадастровый номер участка"})
    q = quarter_of(cad)
    if q:
        terms.append({"kind": "quarter", "value": q, "label": "кадастровый квартал"})
    for name in locality_terms(parcel.get("address"), locality):
        terms.append({"kind": "locality", "value": name, "label": "населённый пункт"})
    return terms


# --- загрузка и текст --------------------------------------------------------------------------
OFFICE_EXT = (".pdf", ".docx", ".zip")
ZIP_MAX_MEMBERS = 5


def _is_pdf(content_type: str, url: str, head: bytes) -> bool:
    return (
        "pdf" in content_type.lower()
        or urlparse(url).path.lower().endswith(".pdf")
        or head[:5] == b"%PDF-"
    )


def _format_of(content_type: str, url: str, raw: bytes) -> str:
    """pdf | docx | zip | html | other — по заголовку, расширению и сигнатуре файла."""
    if _is_pdf(content_type, url, raw[:8]):
        return "pdf"
    path = urlparse(url).path.lower()
    ctype = content_type.lower()
    if raw[:2] == b"PK":
        if (
            path.endswith(".docx")
            or "wordprocessingml" in ctype
            or _zip_has(raw, "word/document.xml")
        ):
            return "docx"
        return "zip"
    if "html" in ctype or raw[:300].lower().lstrip().startswith((b"<!doctype", b"<html")):
        return "html"
    return "other"


def _zip_has(raw: bytes, name: str) -> bool:
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            return name in zf.namelist()
    except zipfile.BadZipFile:
        return False


def docx_text(raw: bytes) -> str:
    """Текст .docx без сторонних библиотек: абзацы word/document.xml."""
    import zipfile

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", " ", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return html_mod.unescape(re.sub(r"[ \t\xa0]+", " ", text))


def _zip_pages(raw: bytes) -> tuple[list[dict[str, Any]], str | None, int]:
    """Страницы файлов внутри архива (pdf/docx, до ZIP_MAX_MEMBERS); у страниц метка файла."""
    import zipfile

    pages: list[dict[str, Any]] = []
    notes: list[str] = []
    members = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        infos = [i for i in zf.infolist() if i.filename.lower().endswith((".pdf", ".docx"))]
        skipped = len(infos) - min(len(infos), ZIP_MAX_MEMBERS)
        for info in infos[:ZIP_MAX_MEMBERS]:
            if info.file_size > MAX_BYTES:
                notes.append(f"{info.filename}: больше 25 МБ")
                continue
            data = zf.read(info)
            name = info.filename.rsplit("/", 1)[-1]
            members += 1
            if info.filename.lower().endswith(".docx"):
                text = docx_text(data)
                if text.strip():
                    pages.append({"n": None, "label": f"файл {name}", "text": text})
                continue
            inner, partial = _pdf_pages(data)
            if not inner and ocr.available():
                inner, partial = ocr.pdf_ocr_pages(data)
                if inner:
                    notes.append(f"{name}: распознано OCR")
            if not inner:
                notes.append(f"{name}: без текстового слоя")
            for pg in inner:
                pg["label"] = f"файл {name}, стр. {pg['n']}"
                pages.append(pg)
            if partial:
                notes.append(f"{name}: {partial}")
        if skipped:
            notes.append(f"ещё {skipped} файлов в архиве не прочитаны (лимит {ZIP_MAX_MEMBERS})")
    return pages, ("; ".join(notes) or None), members


def _pdf_pages(raw: bytes) -> tuple[list[dict[str, Any]], str | None]:
    """Страницы с текстом и пометка, если прочитаны не все (лимит страниц или времени)."""
    pages, partial, _ = pdf_text_pages(raw)
    return pages, partial


def pdf_page_count(raw: bytes) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:  # noqa: BLE001
        return 0


def pdf_text_pages(raw: bytes) -> tuple[list[dict[str, Any]], str | None, int]:
    """Страницы с текстом, пометка о неполном чтении и общее число страниц документа."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    total = len(reader.pages)
    pages: list[dict[str, Any]] = []
    started = time.monotonic()
    partial = None
    for n, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        if time.monotonic() - started > PARSE_DEADLINE_S:
            partial = f"прочитаны первые {n - 1} из {total} страниц (лимит времени разбора)"
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — одна битая страница не должна валить документ
            text = ""
        text = re.sub(r"[ \t\xa0]+", " ", text)
        if text.strip():
            pages.append({"n": n, "text": text})
    else:
        if total > MAX_PAGES:
            partial = f"прочитаны первые {MAX_PAGES} из {total} страниц"
    return pages, partial, total


def _html_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return None
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
    return html_mod.unescape(title)[:160] or None


def decode_html(raw: bytes, content_type: str) -> str:
    """Кодировка из заголовка или <meta>, иначе utf-8, иначе windows-1251 (base.garant.ru,
    старые сайты администраций)."""
    declared: list[str] = []
    m = _CHARSET_RE.search(content_type or "")
    if m:
        declared.append(m.group(1))
    m = _CHARSET_RE.search(raw[:4096].decode("ascii", errors="ignore"))
    if m:
        declared.append(m.group(1))
    for enc in [*declared, "utf-8", "cp1251"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def page_links(html: str, base_url: str) -> list[dict[str, str]]:
    """Ссылки на PDF со страницы (адрес и текст ссылки) — кандидаты второй волны."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, anchor in _HREF_RE.findall(html):
        href = html_mod.unescape(href.strip())
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.path.lower().endswith(OFFICE_EXT):
            continue
        if url in seen:
            continue
        seen.add(url)
        text = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", anchor))).strip()
        text = _SIZE_RE.sub("", text).strip()  # «55.19 КБ Решение …» → «Решение …»
        out.append({"url": url, "text": text[:200]})
        if len(out) >= MAX_LINKS_PER_PAGE:
            break
    return out


def _explain(exc: BaseException) -> str:
    """Причина сбоя скачивания словами пользователя, не именем исключения."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "сайт не отвечает (таймаут соединения)"
    if isinstance(exc, httpx.ReadTimeout):
        return "сайт не прислал данные за 20 с"
    if isinstance(exc, httpx.ProxyError):
        return "шлюз не смог подключиться к сайту"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        if "getaddrinfo" in text or "name or service" in text or "nodename" in text:
            return "адрес сайта не найден (DNS)"
        return "соединение с сайтом не установлено"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "сайт оборвал соединение"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    text = str(exc).lower()
    if "socks" in (type(exc).__module__ or "").lower() or "malformed reply" in text:
        return "шлюз не получил ответ от сайта (обрыв SOCKS)"
    return type(exc).__name__


def _attempt(url: str, proxy: str | None) -> tuple[bytes, str, str, int]:
    """Одна попытка скачать (напрямую или через шлюз)."""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    started = time.monotonic()
    with httpx.Client(
        proxy=proxy,
        timeout=TIMEOUT,
        follow_redirects=True,
        headers=headers,
        verify=ruproxy.verify_context() if proxy else True,
    ) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code in (403, 451) and proxy is None and ruproxy.enabled():
                raise _Blocked(resp.status_code)
            resp.raise_for_status()
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    raise ValueError("больше 25 МБ")
                if time.monotonic() - started > DOWNLOAD_DEADLINE_S:
                    raise TimeoutError("скачивание дольше минуты")
            return bytes(buf), resp.headers.get("content-type", ""), str(resp.url), resp.status_code


class _Blocked(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status} напрямую")
        self.status = status


def unreachable_hosts() -> tuple[str, ...]:
    import os

    raw = os.environ.get("LANDCHECK_UNREACHABLE_HOSTS")
    if raw is None:
        return UNREACHABLE_HOSTS
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def is_unreachable(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in unreachable_hosts())


def residential_proxy() -> str | None:
    """Резидентный/мобильный российский прокси только для сайтов, закрытых для хостингов
    (gosweb.gosuslugi.ru): `LANDCHECK_RU_RESIDENTIAL_PROXY=http://user:pass@host:port`."""
    import os

    return os.environ.get("LANDCHECK_RU_RESIDENTIAL_PROXY") or None


def _download(url: str) -> tuple[bytes, str, str, int]:
    """(bytes, content_type, final_url, status). Напрямую, при блокировке или сетевом сбое — через
    RU-шлюз. Каждая попытка ограничена по стенным часам: прод 01.11 — соединение через шлюз с
    недоступным сайтом держало поток 145 с, таймауты httpx на рукопожатие SOCKS не действуют."""
    errors: list[str] = []
    if is_unreachable(url):
        residential = residential_proxy()
        if not residential:
            raise ConnectionError(
                "сайт закрыт для серверов хостинга (портал госуслуг не отвечает ни напрямую, ни "
                "через RU-шлюз) — откройте ссылку вручную"
            )
        attempts: list[str | None] = [residential]
    else:
        attempts = [None] + ([ruproxy.proxy_url()] if ruproxy.enabled() else [])
    for proxy in attempts:
        suffix = (
            " через резидентный прокси"
            if proxy and proxy == residential_proxy() and is_unreachable(url)
            else (" через шлюз" if proxy else "")
        )
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="landcheck-download")
        fut = pool.submit(_attempt, url, proxy)
        try:
            return fut.result(timeout=ATTEMPT_DEADLINE_S)
        except FuturesTimeout:
            errors.append(f"нет ответа за {int(ATTEMPT_DEADLINE_S)} с{suffix}")
            continue
        except _Blocked as exc:
            errors.append(str(exc))
            continue
        except (
            TimeoutError,
            ValueError,
        ) as exc:  # медленный канал или большой файл — шлюз не поможет
            errors.append(f"{exc}{suffix}")
            break
        except httpx.HTTPStatusError:
            raise
        except Exception as exc:  # noqa: BLE001 — сетевые сбои, в т. ч. socksio «Malformed reply»
            why = _explain(exc)
            errors.append(why if "шлюз" in why else f"{why}{suffix}")
            continue
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    raise ConnectionError("; ".join(errors) or "не удалось скачать")


def fetch_document(url: str) -> dict[str, Any]:
    """Документ по ссылке → страницы текста. Ошибки — внутри результата."""
    out: dict[str, Any] = {
        "url": url,
        "final_url": url,
        "title": None,
        "content_type": None,
        "is_pdf": False,
        "format": None,
        "ocr": False,
        "page_count": None,
        "pages": [],
        "partial": None,
        "error": None,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        raw, ctype, final, _ = _download(url)
    except httpx.HTTPStatusError as exc:
        out["error"] = f"не скачан: HTTP {exc.response.status_code}"
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"не скачан: {exc}"
        return out
    out["final_url"], out["content_type"] = final, ctype
    try:
        fmt = _format_of(ctype, final, raw)
        out["format"] = fmt
        if fmt == "pdf":
            out["is_pdf"] = True
            out["pages"], out["partial"], out["page_count"] = pdf_text_pages(raw)
            if not out["pages"] and ocr.available():
                out["pages"], out["partial"] = ocr.pdf_ocr_pages(raw)
                out["ocr"] = bool(out["pages"])
            if not out["pages"]:
                out["error"] = (
                    "PDF без текстового слоя (скан): OCR не дал текста, нужен просмотр глазами"
                    if ocr.available()
                    else "PDF без текстового слоя (скан): нужен просмотр глазами"
                )
        elif fmt == "docx":
            text = docx_text(raw)
            if text.strip():
                out["pages"] = [{"n": None, "text": text}]
            else:
                out["error"] = "в .docx нет текста"
        elif fmt == "zip":
            out["pages"], out["partial"], members = _zip_pages(raw)
            out["ocr"] = "распознано OCR" in (out["partial"] or "")
            if not members:
                out["error"] = "в архиве нет PDF или .docx"
            elif not out["pages"]:
                out["error"] = "файлы архива без текстового слоя: " + (out["partial"] or "")
        elif fmt == "html":
            html = decode_html(raw, ctype)
            out["title"] = _html_title(html)
            out["links"] = page_links(html, final)
            text = websearch.extract_text(html)
            if text.strip():
                out["pages"] = [{"n": None, "text": text}]
            else:
                out["error"] = "текст страницы не извлечён"
        else:
            out["error"] = f"формат не разбирается ({ctype.split(';')[0] or 'неизвестен'})"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"не разобран: {type(exc).__name__}"
    return out


# --- поиск упоминаний ----------------------------------------------------------------------------
def _pattern(term: str) -> re.Pattern[str]:
    if CAD_RE.fullmatch(term) or re.fullmatch(r"\d+:\d+:\d+", term):
        # номер может быть записан с пробелами вокруг двоеточий или перенесён
        parts = [re.escape(p) for p in term.split(":")]
        body = r"\s*:\s*".join(parts)
        return re.compile(r"(?<![\d:])" + body + r"(?![\d:])")
    stem = term[:-1] if len(term) > 5 else term
    return re.compile(r"(?<![а-яё])" + re.escape(stem) + r"[а-яё]{0,3}(?![а-яё])", re.I)


def find_mentions(doc: dict[str, Any], terms: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for term in terms:
        rx = _pattern(term["value"])
        count = 0
        for page in doc.get("pages") or []:
            text = page["text"]
            for m in rx.finditer(text):
                a, b = max(0, m.start() - CONTEXT), min(len(text), m.end() + CONTEXT)
                quote = re.sub(r"\s+", " ", text[a:b]).strip()
                out.append(
                    {
                        "term": term["value"],
                        "term_kind": term["kind"],
                        "term_label": term["label"],
                        "page": page["n"],
                        "label": page.get("label"),
                        "quote": ("…" if a > 0 else "") + quote + ("…" if b < len(text) else ""),
                    }
                )
                count += 1
                if count >= MAX_QUOTES_PER_TERM:
                    break
            if count >= MAX_QUOTES_PER_TERM:
                break
    return out


def page_url(url: str, page: int | None) -> str:
    return f"{url}#page={page}" if page else url


def candidate_docs(
    admin_docs: dict[str, Any] | None, max_docs: int = MAX_DOCS
) -> list[dict[str, Any]]:
    """Что скачивать: ссылки реестра муниципалитета, затем найденное автопоиском по видам."""
    ad = admin_docs or {}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    registry = (ad.get("registry") or {}).get("links") or []
    for link in registry:
        url = link.get("url")
        if url and url not in seen:
            seen.add(url)
            out.append({"url": url, "title": link.get("title"), "kind": "registry"})
    found = (ad.get("found") or {}).get("docs") or {}
    for kind in KIND_ORDER:
        for d in found.get(kind) or []:
            if kind in SKIP_KINDS or d["url"] in seen:
                continue
            seen.add(d["url"])
            out.append({"url": d["url"], "title": d.get("title"), "kind": kind})
    return out[:max_docs]


def collect(
    parcel: dict[str, Any],
    admin_docs: dict[str, Any] | None,
    locality: str | None,
    *,
    fetch: Callable[[str], dict[str, Any]] = fetch_document,
    max_docs: int = MAX_DOCS,
    workers: int = WORKERS,
    budget_s: float = BUDGET_S,
) -> dict[str, Any]:
    """Скачать документы, найти упоминания участка/квартала/населённого пункта."""
    terms = terms_for(parcel, locality)
    cands = candidate_docs(admin_docs, max_docs=max_docs)
    started = time.monotonic()
    docs, timed_out = _fetch_wave(cands, fetch, workers, budget_s)
    nested_skipped = 0
    nested = nested_candidates(cands, docs, terms)
    if nested:
        left = budget_s - (time.monotonic() - started)
        if left >= NESTED_MIN_BUDGET_S:
            more, more_timed_out = _fetch_wave(nested, fetch, workers, left)
            docs.extend(more)
            timed_out += more_timed_out
            cands = cands + nested
        else:
            nested_skipped = len(nested)
    docs_out: list[dict[str, Any]] = []
    hits = 0
    strong = 0
    for i, cand in enumerate(cands):
        doc = docs[i]
        mentions = find_mentions(doc, terms) if doc.get("pages") else []
        for m in mentions:
            m["url"] = page_url(
                doc.get("final_url") or cand["url"], m["page"] if doc.get("is_pdf") else None
            )
        row = {
            "url": cand["url"],
            "final_url": doc.get("final_url") or cand["url"],
            "title": doc.get("title") or cand.get("title") or cand["url"],
            "kind": cand["kind"],
            "is_pdf": doc.get("is_pdf", False),
            "format": doc.get("format"),
            "ocr": doc.get("ocr", False),
            "pages": len(doc.get("pages") or []),
            "page_count": doc.get("page_count"),
            "partial": doc.get("partial"),
            "via": cand.get("via"),
            "error": doc.get("error"),
            "mentions": mentions,
            "strong": any(m["term_kind"] in ("cad", "quarter") for m in mentions)
            and cand["kind"] in STRONG_KINDS,
        }
        hits += len(mentions)
        strong += 1 if row["strong"] else 0
        docs_out.append(row)
    note = (
        "Проверены текстовые слои документов по ссылкам из блока выше; сканы распознаются OCR "
        "(до 15 страниц, в тексте возможны ошибки распознавания — сверяйте по ссылке). Файлы "
        "больше 25 МБ не читаются — такие помечены. Цитата ведёт на страницу документа "
        "(для PDF — #page=N); в браузере откроется сам первоисточник."
    )
    if timed_out:
        note += f" Не успели за {int(budget_s // 60)} минуты: {timed_out} — «перечитать документы»."
    if nested_skipped:
        note += f" Вложенные файлы разделов ({nested_skipped}) не проверены: лимит времени."
    return {
        "terms": terms,
        "docs": docs_out,
        "hits": hits,
        "strong": strong,
        "checked": len(docs_out),
        "fetched_ok": sum(1 for d in docs_out if not d["error"]),
        "timed_out": timed_out,
        "nested": sum(1 for d in docs_out if d.get("via")),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": note,
    }


def _fetch_wave(
    cands: list[dict[str, Any]],
    fetch: Callable[[str], dict[str, Any]],
    workers: int,
    budget_s: float,
) -> tuple[list[dict[str, Any]], int]:
    """Скачать кандидатов параллельно в пределах бюджета; не успевшие — помечены."""
    docs: dict[int, dict[str, Any]] = {}
    if not cands:
        return [], 0
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(cands))))
    futures = {pool.submit(_safe_fetch, fetch, c["url"]): i for i, c in enumerate(cands)}
    try:
        for fut in as_completed(futures, timeout=max(budget_s, 0.01)):
            docs[futures[fut]] = fut.result()
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    timed_out = 0
    for i, c in enumerate(cands):
        if i not in docs:
            timed_out += 1
            docs[i] = {
                "url": c["url"],
                "pages": [],
                "error": f"не проверен: лимит времени проверки ({int(BUDGET_S // 60)} мин)",
            }
    return [docs[i] for i in range(len(cands))], timed_out


def _link_score(link: dict[str, str], terms: list[dict[str, str]]) -> int:
    text = f"{link.get('text') or ''} {urlparse(link['url']).path}".lower()
    score = 0
    if NESTED_HINT_RE.search(text):
        score += 2
    for t in terms:
        value = t["value"].lower()
        needle = value[:-1] if t["kind"] == "locality" and len(value) > 5 else value
        if needle and needle in text:
            score += 3
    return score


def nested_candidates(
    cands: list[dict[str, Any]], docs: list[dict[str, Any]], terms: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """PDF, на которые ведут прочитанные страницы разделов: по подсказкам в тексте ссылки
    (межевание, планировка, постановление, номер участка, населённый пункт)."""
    seen = {c["url"] for c in cands}
    out: list[dict[str, Any]] = []
    for cand, doc in zip(cands, docs, strict=False):
        if cand["kind"] not in NESTED_KINDS or not doc.get("links"):
            continue
        fresh = [link for link in doc["links"] if link["url"] not in seen]
        scored = sorted(fresh, key=lambda link: -_link_score(link, terms))
        picked = [link for link in scored if _link_score(link, terms) > 0][:MAX_NESTED_PER_PAGE]
        for link in picked:
            seen.add(link["url"])
            out.append(
                {
                    "url": link["url"],
                    "title": link["text"] or urlparse(link["url"]).path.rsplit("/", 1)[-1],
                    "kind": cand["kind"],
                    "via": doc.get("final_url") or cand["url"],
                }
            )
            if len(out) >= MAX_NESTED:
                return out
    return out


def _safe_fetch(fetch: Callable[[str], dict[str, Any]], url: str) -> dict[str, Any]:
    try:
        return fetch(url)
    except Exception as exc:  # noqa: BLE001 — один документ не должен валить проверку
        log.warning("evidence fetch %s: %s", url, exc)
        return {"url": url, "pages": [], "error": f"не разобран: {type(exc).__name__}"}


def r25_flag(evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Флаг «участок упомянут в документе администрации» — только по номеру участка или квартала
    и только в проектах межевания, постановлениях, извещениях и документах реестра."""
    strong_docs = [d for d in evidence.get("docs") or [] if d.get("strong")]
    if not strong_docs:
        return None
    d = strong_docs[0]
    m = next(x for x in d["mentions"] if x["term_kind"] in ("cad", "quarter"))
    where = m.get("label") or (f"стр. {m['page']}" if m.get("page") else "страница документа")
    what = (
        f"участок {m['term']}"
        if m["term_kind"] == "cad"
        else f"кадастровый квартал {m['term']} (в нём участок)"
    )
    more = f" и ещё в {len(strong_docs) - 1}" if len(strong_docs) > 1 else ""
    return {
        "rule_id": "R25",
        "level": "проверить",
        "message": (
            f"{what} упомянут в документе администрации «{(d['title'] or '')[:90]}» "
            f"({where}){more}: открыть по ссылке и прочитать пункт — проект межевания, извещение "
            "или постановление может означать распределение, резервирование или изъятие, которых "
            "в ЕГРН ещё нет"
        ),
        "source": m["url"],
        "details": {
            "docs": [
                {"url": x["final_url"], "title": x["title"], "kind": x["kind"]} for x in strong_docs
            ],
            "quote": m["quote"][:400],
        },
    }
