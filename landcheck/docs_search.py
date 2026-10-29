"""Документы и упоминания участка в открытых источниках по кадастровому номеру.

Суды, торги, постановления и акты, новости, объявления о продаже — через тот же поиск,
что и «Регион» (SearXNG → ddgs). Страницы-справочники кадастра (повторяют карточку НСПД)
отбрасываются. Это не юридическая проверка: ссылки для ручной сверки.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

KINDS: dict[str, str] = {
    "court": "Суды",
    "torgi": "Торги и аукционы",
    "gov": "Акты, постановления, публичные слушания",
    "news": "Новости и упоминания",
    "ads": "Объявления о продаже",
}
_ORDER = ("court", "torgi", "gov", "news", "ads")

# Справочники кадастра: показывают ту же карточку участка, информации сверх НСПД не дают
_LOOKUP_HOSTS = (
    "nspd.gov.ru",
    "rosreestr.gov.ru",
    "rosreestr.net",
    "pkk.",
    "egrp365",
    "egrn",
    "kadastrmap",
    "kadastr.ru",
    "kadastr-",
    "kadastrovaya",
    "publichnaya",
    "reestr-online",
    "zemelyabot",
    "geo-risk",
    "zembaza",
    "domclick.ru/kadastr",
    "ktotam",
    "reformagkh",
    "dom.mingkh",
    "e-ecolog",
    "egrpo",
    "kadnet",
    "rosreestr-",
    "выписка",
    "vypiska",
)
_PRIMARY_HOSTS = re.compile(
    r"(sudact|sudrf|arbitr|torgi\.gov|gosuslugi|gov\.ru|\.gov\b|\.рф)", re.I
)
_COURT = re.compile(
    r"(sudact|sudrf|судебн|\bсуд[аеу]?\b|решени[ея] по делу|дело №|арбитраж|mos-gorsud|"
    r"kad\.arbitr)",
    re.I,
)
_TORGI = re.compile(r"(torgi\.gov|торг|аукцион|лот\b|продажа имущества|конкурсн|банкрот)", re.I)
_GOV = re.compile(
    r"(gosuslugi|gov\.ru|\.gov\b|постановлен|распоряжен|администраци|публичн\w* слушан|"
    r"генплан|пзз|правил\w* землепольз|межевани|проект планировки)",
    re.I,
)
_ADS = re.compile(
    r"(avito|cian|domclick|youla|mirkvartir|move\.ru|olan\.ru|n1\.ru|etagi|realty\.yandex|"
    r"продам|продаётся|продается|объявлени)",
    re.I,
)


def is_lookup(url: str, title: str = "", cad_num: str | None = None) -> bool:
    """Страница-справочник: известный хост или кадастровый номер прямо в адресе страницы
    (`/e-cad/04:05:050201:306` — карточка участка, не документ), кроме судов, торгов и
    официальных сайтов."""
    from urllib.parse import unquote

    u = urlparse(url)
    host = u.netloc.lower()
    path = u.path.lower()
    hay = f"{host} {path} {title.lower()}"
    if any(h in hay for h in _LOOKUP_HOSTS):
        return True
    if cad_num and not _PRIMARY_HOSTS.search(host):
        if cad_num in unquote(path) or cad_num.replace(":", "") in path:
            return True
    return False


def classify(url: str, title: str, snippet: str = "") -> str:
    hay = f"{url} {title} {snippet}"
    if _COURT.search(hay):
        return "court"
    if _TORGI.search(hay):
        return "torgi"
    if _ADS.search(hay):
        return "ads"
    if _GOV.search(hay):
        return "gov"
    return "news"


def build_queries(cad_num: str) -> list[str]:
    return [f'"{cad_num}"', f"{cad_num} суд OR торги OR постановление"]


def search_documents(
    cad_num: str, *, search_fn=None, max_results: int = 10, limit: int = 15
) -> dict[str, Any]:
    from landcheck import websearch

    search_fn = search_fn or websearch.search
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    queries = build_queries(cad_num)
    for q in queries:
        res = search_fn(q, max_results=max_results, news=False, recent=False)
        if res.get("error"):
            errors.append(res["error"])
            continue
        for it in res.get("results") or []:
            url = (it.get("url") or "").split("#")[0]
            title = (it.get("title") or "").strip()
            snippet = (it.get("snippet") or "").strip()
            if not url or url in seen or is_lookup(url, title, cad_num):
                continue
            # страница должна упоминать номер (поисковики любят подсовывать похожие)
            if cad_num not in f"{title} {snippet}":
                continue
            seen.add(url)
            items.append(
                {
                    "url": url,
                    "title": title or url,
                    "snippet": snippet[:300],
                    "date": it.get("date"),
                    "kind": classify(url, title, snippet),
                    "host": urlparse(url).netloc.lower().removeprefix("www."),
                }
            )
    items.sort(key=lambda d: _ORDER.index(d["kind"]))
    groups = [
        {"kind": k, "title": KINDS[k], "items": [d for d in items if d["kind"] == k]}
        for k in _ORDER
    ]
    return {
        "items": items[:limit],
        "groups": [g for g in groups if g["items"]],
        "queries": queries,
        "fetched_at": datetime.now(UTC).isoformat(),
        "error": "; ".join(dict.fromkeys(errors)) if errors and not items else None,
    }
