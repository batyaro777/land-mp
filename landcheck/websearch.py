"""Поиск по открытым источникам без платных API: SearXNG (свой экземпляр) или DuckDuckGo (ddgs).

Нужен модулю «Регион» вместо платного Gemini grounding: ищем, скачиваем страницы, извлекаем
текст (trafilatura) и отдаём модели только найденное — с номерами источников.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from typing import Any
from urllib.parse import urlparse

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; landcheck/0.1; +https://land-mp.dnai.engineering)"
MAX_PAGE_CHARS = 3500
MAX_HTML_CHARS = 1_500_000  # больше — не статья; и память воркера
EXTRACT_TIMEOUT = 45.0  # вместе с ожиданием в очереди: 6 потоков загрузки делят 2 воркера
log = logging.getLogger(__name__)
SKIP_HOSTS = ("vk.com", "vk.ru", "ok.ru", "t.me", "youtube.com", "instagram.com", "facebook.com")


def _searxng_url() -> str | None:
    return (os.environ.get("SEARXNG_URL") or "").rstrip("/") or None


def search(
    query: str,
    *,
    max_results: int = 8,
    news: bool = False,
    recent: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """{"results": [{title,url,snippet,date,engine}], "engine": str, "error": str|None}.

    recent=True ограничивает выдачу последним годом (планы и стройки, а не новости 2007 года).
    """
    url = _searxng_url()
    if url:
        res = _search_searxng(
            url, query, max_results=max_results, news=news, recent=recent, timeout=timeout
        )
        if not res.get("error"):
            return res
    return _search_ddgs(query, max_results=max_results, news=news, recent=recent)


def _search_searxng(
    base: str,
    query: str,
    *,
    max_results: int,
    news: bool,
    recent: bool = False,
    timeout: float,
) -> dict:
    params = {"q": query, "format": "json", "language": "ru-RU"}
    if news:
        params["categories"] = "news"
    if recent:
        params["time_range"] = "year"
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


def _search_ddgs(query: str, *, max_results: int, news: bool, recent: bool = False) -> dict:
    try:
        from ddgs import DDGS
    except ImportError:
        return {"results": [], "engine": "ddgs", "error": "пакет ddgs не установлен"}
    kwargs: dict[str, Any] = {"region": "ru-ru", "max_results": max_results}
    if recent:
        kwargs["timelimit"] = "y"
    try:
        with DDGS() as d:
            raw = d.news(query, **kwargs) if news else d.text(query, **kwargs)
    except Exception as e:  # noqa: BLE001 — лимит 202, сеть, «No results found»
        if recent:
            # Без ограничения по дате DuckDuckGo чаще что-то находит; дату проверит модель.
            return _search_ddgs(query, max_results=max_results, news=news, recent=False)
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


_POOL: ProcessPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _extract(html: str) -> str:
    import trafilatura

    return trafilatura.extract(html, include_comments=False, include_tables=False) or ""


def _inprocess() -> bool:
    return os.environ.get("LANDCHECK_EXTRACT_INPROCESS", "") == "1"


def _pool() -> ProcessPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ProcessPoolExecutor(
                max_workers=2, mp_context=multiprocessing.get_context("spawn")
            )
        return _POOL


def _reset_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def extract_text(html: str) -> str:
    """Основной текст из HTML. В проде — в отдельном процессе: trafilatura/lxml в шести
    потоках роняли контейнер без traceback (прод 21.10, «Регион» по Чепошу); падение или
    зависание воркера теперь стоит одной страницы, а не сервера. `LANDCHECK_EXTRACT_INPROCESS=1`
    — извлекать в том же процессе (тесты)."""
    html = html[:MAX_HTML_CHARS]
    if _inprocess():
        try:
            return _extract(html)
        except Exception:  # noqa: BLE001
            return ""
    try:
        return _pool().submit(_extract, html).result(timeout=EXTRACT_TIMEOUT)
    except BrokenProcessPool:
        log.warning("extract worker crashed, pool reset")
        _reset_pool()
        return ""
    except FutureTimeout:
        # пул не трогаем: сброс с cancel_futures отменил бы соседние страницы этого прогона,
        # а зависший воркер он всё равно не убивает (Клавдий, прод 21.10)
        log.warning("extract timeout after %ss", EXTRACT_TIMEOUT)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("extract failed: %s", type(exc).__name__)
        return ""


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
    return extract_text(html)[:max_chars]


def fetch_many(urls: list[str], *, timeout: float = 10.0, workers: int = 6) -> dict[str, str]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        texts = list(ex.map(lambda u: fetch_text(u, timeout=timeout), urls))
    return dict(zip(urls, texts, strict=True))
