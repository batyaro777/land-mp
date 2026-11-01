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
import io
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from landcheck import ruproxy, websearch
from landcheck.inputs import CAD_RE

log = logging.getLogger(__name__)

MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 400
MAX_DOCS = 12
CONTEXT = 160
MAX_QUOTES_PER_TERM = 3
TIMEOUT_S = 40.0
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
    return "doc:" + hashlib.sha1(url.encode("utf-8")).hexdigest()


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
def _is_pdf(content_type: str, url: str, head: bytes) -> bool:
    return (
        "pdf" in content_type.lower()
        or urlparse(url).path.lower().endswith(".pdf")
        or head[:5] == b"%PDF-"
    )


def _pdf_pages(raw: bytes) -> list[dict[str, Any]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    pages: list[dict[str, Any]] = []
    for n, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — одна битая страница не должна валить документ
            text = ""
        text = re.sub(r"[ \t\xa0]+", " ", text)
        if text.strip():
            pages.append({"n": n, "text": text})
    return pages


def _html_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return None
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
    return title[:160] or None


def _download(url: str) -> tuple[bytes, str, str, int]:
    """(bytes, content_type, final_url, status). Напрямую, при блокировке — через RU-шлюз."""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    errors: list[str] = []
    attempts = [None] + ([ruproxy.proxy_url()] if ruproxy.enabled() else [])
    for proxy in attempts:
        try:
            with httpx.Client(
                proxy=proxy,
                timeout=TIMEOUT_S,
                follow_redirects=True,
                headers=headers,
                verify=ruproxy.verify_context() if proxy else True,
            ) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code in (403, 451) and proxy is None and len(attempts) > 1:
                        errors.append(f"HTTP {resp.status_code} напрямую")
                        continue
                    resp.raise_for_status()
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_BYTES:
                            raise ValueError("слишком большой файл")
                    return (
                        bytes(buf),
                        resp.headers.get("content-type", ""),
                        str(resp.url),
                        resp.status_code,
                    )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            errors.append(f"{type(exc).__name__}{' через шлюз' if proxy else ''}")
            continue
    raise ConnectionError("; ".join(errors) or "не удалось скачать")


def fetch_document(url: str) -> dict[str, Any]:
    """Документ по ссылке → страницы текста. Ошибки — внутри результата."""
    out: dict[str, Any] = {
        "url": url,
        "final_url": url,
        "title": None,
        "content_type": None,
        "is_pdf": False,
        "pages": [],
        "error": None,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        raw, ctype, final, _ = _download(url)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"не скачан: {exc}"
        return out
    out["final_url"], out["content_type"] = final, ctype
    try:
        if _is_pdf(ctype, final, raw[:8]):
            out["is_pdf"] = True
            out["pages"] = _pdf_pages(raw)
            if not out["pages"]:
                out["error"] = "PDF без текстового слоя (скан): нужен просмотр глазами"
        elif "html" in ctype.lower() or raw[:300].lower().lstrip().startswith(
            (b"<!doctype", b"<html")
        ):
            html = raw.decode("utf-8", errors="replace")
            out["title"] = _html_title(html)
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
) -> dict[str, Any]:
    """Скачать документы, найти упоминания участка/квартала/населённого пункта."""
    terms = terms_for(parcel, locality)
    docs_out: list[dict[str, Any]] = []
    hits = 0
    strong = 0
    for cand in candidate_docs(admin_docs, max_docs=max_docs):
        doc = fetch(cand["url"])
        mentions = find_mentions(doc, terms) if doc.get("pages") else []
        for m in mentions:
            m["url"] = page_url(doc.get("final_url") or cand["url"], m["page"])
        row = {
            "url": cand["url"],
            "final_url": doc.get("final_url") or cand["url"],
            "title": doc.get("title") or cand.get("title") or cand["url"],
            "kind": cand["kind"],
            "is_pdf": doc.get("is_pdf", False),
            "pages": len(doc.get("pages") or []),
            "error": doc.get("error"),
            "mentions": mentions,
            "strong": any(m["term_kind"] in ("cad", "quarter") for m in mentions)
            and cand["kind"] in STRONG_KINDS,
        }
        hits += len(mentions)
        strong += 1 if row["strong"] else 0
        docs_out.append(row)
    return {
        "terms": terms,
        "docs": docs_out,
        "hits": hits,
        "strong": strong,
        "checked": len(docs_out),
        "fetched_ok": sum(1 for d in docs_out if not d["error"]),
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": (
            "Проверены текстовые слои документов по ссылкам из блока выше. Скан без текста и файлы "
            "больше 25 МБ не читаются — такие помечены. Цитата ведёт на страницу документа "
            "(для PDF — #page=N); в браузере откроется сам первоисточник."
        ),
    }


def r25_flag(evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Флаг «участок упомянут в документе администрации» — только по номеру участка или квартала
    и только в проектах межевания, постановлениях, извещениях и документах реестра."""
    strong_docs = [d for d in evidence.get("docs") or [] if d.get("strong")]
    if not strong_docs:
        return None
    d = strong_docs[0]
    m = next(x for x in d["mentions"] if x["term_kind"] in ("cad", "quarter"))
    where = f"стр. {m['page']}" if m.get("page") else "страница документа"
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
