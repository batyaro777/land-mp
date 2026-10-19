"""Поиск по открытым источникам без платных API: SearXNG (свой экземпляр) или DuckDuckGo (ddgs).

Нужен модулю «Регион» вместо платного Gemini grounding: ищем, скачиваем страницы, извлекаем
текст (trafilatura) и отдаём модели только найденное — с номерами источников.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; landcheck/0.1; +https://land-mp.dnai.engineering)"
MAX_PAGE_CHARS = 3500
SKIP_HOSTS = ("vk.com", "vk.ru", "ok.ru", "t.me", "youtube.com", "instagram.com", "facebook.com")


def _searxng_url() -> str | None:
    return (os.environ.get("SEARXNG_URL") or "").rstrip("/") or None


def search(
    query: str, *, max_results: int = 8, news: bool = False, timeout: float = 15.0
) -> dict[str, Any]:
    """{"results": [{title,url,snippet,date,engine}], "engine": str, "error": str|None}."""
    url = _searxng_url()
    if url:
        res = _search_searxng(url, query, max_results=max_results, news=news, timeout=timeout)
        if not res.get("error"):
            return res
    return _search_ddgs(query, max_results=max_results, news=news)


def _search_searxng(base: str, query: str, *, max_results: int, news: bool, timeout: float) -> dict:
    params = {"q": query, "format": "json", "language": "ru-RU"}
    if news:
        params["categories"] = "news"
    try:
        r = httpx.get(
            f"{base}/search", params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout
        )
        if r.status_code != 200:
            return {"results": [], "engine": "searxng", "error": f"searxng HTTP {r.status_code}"}
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return {"results": [], "engine": "searxng", "error": f"searxng: {type(e).__name__}"}
    out = []
    for it in data.get("results") or []:
        out.append(
            {
                "title": it.get("title") or "",
                "url": it.get("url") or "",
                "snippet": it.get("content") or "",
                "date": (it.get("publishedDate") or "")[:10] or None,
                "engine": it.get("engine") or "searxng",
            }
        )
        if len(out) >= max_results:
            break
    return {"results": out, "engine": "searxng", "error": None}


def _search_ddgs(query: str, *, max_results: int, news: bool) -> dict:
    try:
        from ddgs import DDGS
    except ImportError:
        return {"results": [], "engine": "ddgs", "error": "пакет ddgs не установлен"}
    try:
        with DDGS() as d:
            raw = (
                d.news(query, region="ru-ru", max_results=max_results)
                if news
                else d.text(query, region="ru-ru", max_results=max_results)
            )
    except Exception as e:  # noqa: BLE001 — лимит 202, сеть
        return {"results": [], "engine": "ddgs", "error": f"ddgs: {type(e).__name__}: {e}"[:200]}
    out = []
    for it in raw or []:
        out.append(
            {
                "title": it.get("title") or "",
                "url": it.get("url") or it.get("href") or "",
                "snippet": it.get("body") or "",
                "date": (it.get("date") or "")[:10] or None,
                "engine": "ddgs",
            }
        )
    return {"results": out, "engine": "ddgs", "error": None}


def fetch_text(url: str, *, timeout: float = 10.0, max_chars: int = MAX_PAGE_CHARS) -> str:
    """Основной текст страницы или пустая строка; никаких исключений наружу."""
    host = urlparse(url).netloc.lower()
    if any(h in host for h in SKIP_HOSTS):
        return ""
    try:
        r = httpx.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
        )
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or ""):
            return ""
        html = r.text
    except Exception:  # noqa: BLE001
        return ""
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception:  # noqa: BLE001
        text = ""
    return text[:max_chars]


def fetch_many(urls: list[str], *, timeout: float = 10.0, workers: int = 6) -> dict[str, str]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        texts = list(ex.map(lambda u: fetch_text(u, timeout=timeout), urls))
    return dict(zip(urls, texts, strict=True))
