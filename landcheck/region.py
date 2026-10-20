"""Регион: инвестиции, дороги, риски и рынок по открытым источникам.

Боль брокера (Антон М., 15.10.2026): «информация есть в открытом доступе, но её надо собирать,
структурировать и догадаться, что собирать».

Два режима (LANDCHECK_REGION):
- ``search`` (по умолчанию, бесплатный стек, решение Тони 19.10): поиск SearXNG/DuckDuckGo →
  страницы → текст → языковая модель через бесплатных провайдеров (``landcheck.llm``) собирает
  шесть разделов, ссылаясь на номера источников;
- ``gemini``: прежний путь через Gemini с инструментом google_search (платный уровень).

Правила честности: без найденных источников блок не отвечает «из памяти» модели; каждый
ответ помечен датой, моделью и запросами; один запрос на населённый пункт (кэш по локации).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from landcheck.models import Purpose

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
# Запасные модели, если основная перегружена (503) или без квоты (429): проверено 15.10.2026.
FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite")
SECTIONS = (
    "Развитие и инвестиции",
    "Дороги и инфраструктура",
    "Негативные факторы",
    "Рынок земли",
    "Статус территории",
    "Вывод для покупателя",
)
SRC = "по открытым источникам; ссылки ниже; не юридическое заключение"
MAX_DOCS = 10
MIN_DOC_CHARS = 200

_PROMPT = """Ты аналитик рынка земли в России. Локация: {locality}.
Цель покупателя: {activity}.
Найди в открытых источниках актуальные сведения и изложи по-русски строго по разделам с заголовками:
## Развитие и инвестиции
## Дороги и инфраструктура
## Негативные факторы
## Рынок земли
## Статус территории
## Вывод для покупателя
Правила: только факты из найденных источников, с датами и, где есть, суммами; интересуют планы
на 1–5 лет (инвестпроекты, ОЭЗ и ТОР, курортные кластеры, новые дороги и мосты, газификация,
полигоны отходов, промышленные объекты, застройка). В разделе «Рынок земли» — цены за сотку по
типам (ИЖС, садоводство, сельхоз, рекреация) и динамика за год, если источники их приводят.
Где данных нет — пиши «данных не найдено». Никаких предположений и общих слов. В каждом
разделе 2–5 коротких пунктов, каждый начинается с «- ». В конце список источников не добавляй.
"""

_RAG_SYSTEM = (
    "Ты аналитик рынка земли в России. Отвечай только по приложенным документам, "
    "по-русски, кратко и конкретно, с датами и суммами, где они есть. Ничего не выдумывай: "
    "если в документах факта нет — пиши «данных не найдено». После каждого пункта ставь номера "
    "документов-источников в квадратных скобках, например [2] или [1, 4]. Сегодня {today}: "
    "важны планы и события последних 1–3 лет и на 1–5 лет вперёд; событие старше трёх лет "
    "упоминай только с годом и не выдавай за актуальное; рекламу санаториев, путеводители и "
    "туристические обзоры не пересказывай."
)

_RAG_PROMPT = """Локация: {locality}. Цель покупателя: {activity}.
Собери сводку строго из документов ниже. Верни JSON вида
{{"sections": [{{"title": "<название раздела>", "items": ["<пункт> [n]", ...]}}, ...],
 "used": [n, ...]}}
с шестью разделами ровно в таком порядке и с такими названиями:
{sections}
В каждом разделе 2–5 коротких пунктов (или один пункт «данных не найдено»). Интересуют планы на
1–5 лет: инвестпроекты, ОЭЗ и ТОР, курортные кластеры, дороги и мосты, газификация, полигоны
отходов, промышленные объекты, застройка; в «Рынке земли» — цены за сотку по типам (ИЖС,
садоводство, сельхоз, рекреация) и динамика. «Вывод для покупателя» — 2–3 пункта по цели.
Поле "used" — номера документов, на которые ты сослался.

Документы:
{docs}
"""


def locality_from_address(address: str | None) -> str | None:
    """«Российская Федерация, Республика Алтай, Чемальский район, с. Элекмонар, ул. …, з/у 5»
    → «Республика Алтай, Чемальский район, с. Элекмонар» (до улицы, дома, участка)."""
    if not address:
        return None
    parts = [p.strip() for p in address.split(",") if p.strip()]
    parts = [p for p in parts if p.lower() not in ("российская федерация", "россия", "рф")]
    stop = re.compile(
        r"^(ул\.?|улица|пер\.?|переулок|пр-?кт|проспект|ш\.?|шоссе|д\.?\s*\d|дом|з/у|участок|уч\.?|"
        r"стр\.?|строение|кв\.?|тер\.?|снт|тсн|днп|мкр|квартал|линия|проезд|\d)",
        re.IGNORECASE,
    )
    kept: list[str] = []
    for p in parts:
        if stop.match(p):
            break
        kept.append(p)
    kept = kept[:4]
    return ", ".join(kept) if kept else None


def locality_key(locality: str) -> str:
    return re.sub(r"\s+", " ", locality.lower().replace("ё", "е")).strip()


def _split_sections(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^#{1,3}\s*(.+?)\s*$", s)
        if m:
            current = {"title": m.group(1).strip("*: "), "items": []}
            out.append(current)
            continue
        if not s or current is None:
            continue
        s = re.sub(r"^[-*•]\s*", "", s)
        s = re.sub(r"\[(\d+(?:,\s*\d+)*)\]", "", s)  # [1, 2] — сноски модели
        s = re.sub(r"\s+([.,;:!?])", r"\1", s).strip()
        if s:
            current["items"].append(s)
    return out


def parse_response(data: dict[str, Any], *, model: str) -> dict[str, Any]:
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    gm = cand.get("groundingMetadata") or {}
    sources = []
    seen: set[str] = set()
    for ch in gm.get("groundingChunks") or []:
        web = ch.get("web") or {}
        uri, title = web.get("uri"), web.get("title")
        if uri and uri not in seen:
            seen.add(uri)
            sources.append({"title": title or uri, "url": uri})
    return {
        "model": model,
        "fetched_at": datetime.now(UTC).isoformat(),
        "sections": _split_sections(text),
        "text": text,
        "sources": sources,
        "queries": list(gm.get("webSearchQueries") or []),
        "grounded": bool(sources),
        "usage": data.get("usageMetadata"),
        "error": None,
        "source": SRC,
    }


def region_mode() -> str:
    return (os.environ.get("LANDCHECK_REGION") or "search").strip().lower()


def research_region(
    locality: str,
    purpose: Purpose,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
    mode: str | None = None,
) -> dict[str, Any]:
    """Сводка по локации: бесплатный поиск + модель (search) или Gemini grounding (gemini)."""
    mode = mode or region_mode()
    if mode == "gemini" and api_key:
        return research_region_gemini(
            locality, purpose, api_key=api_key, model=model, timeout=timeout, fallbacks=fallbacks
        )
    return research_region_search(locality, purpose)


# --- бесплатный путь: поиск → страницы → модель ------------------------------------------
def build_queries(locality: str, purpose: Purpose) -> list[dict[str, Any]]:
    short = locality.split(",")[-1].strip() or locality
    year = datetime.now(UTC).year
    return [
        {"q": f"{short} инвестиции проект {year}", "news": False, "recent": True},
        {"q": f"{short} строительство дорога газификация {year}", "news": False, "recent": True},
        {"q": f"{short} свалка полигон промзона кладбище", "news": False, "recent": False},
        {"q": f"{short} земельный участок цена за сотку {year}", "news": False, "recent": True},
        {"q": f"{short} генплан застройка мастер-план", "news": False, "recent": False},
        {"q": f"{short} инвестиции строительство", "news": True, "recent": True},
    ]


def collect_documents(
    locality: str, purpose: Purpose, *, search_fn=None, fetch_fn=None, max_docs: int = MAX_DOCS
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Уникальные страницы по запросам: (документы, запросы, ошибки поиска)."""
    from landcheck import websearch

    search_fn = search_fn or websearch.search
    fetch_fn = fetch_fn or websearch.fetch_many
    seen: set[str] = set()
    docs: list[dict[str, Any]] = []
    queries: list[str] = []
    errors: list[str] = []
    for spec in build_queries(locality, purpose):
        queries.append(spec["q"])
        res = search_fn(
            spec["q"], max_results=6, news=spec["news"], recent=spec.get("recent", False)
        )
        if res.get("error"):
            errors.append(res["error"])
            continue
        for it in res["results"]:
            url = (it.get("url") or "").split("#")[0]
            if not url or url in seen:
                continue
            seen.add(url)
            docs.append(
                {
                    "url": url,
                    "title": it.get("title") or url,
                    "snippet": it.get("snippet") or "",
                    "date": it.get("date"),
                    "engine": it.get("engine"),
                }
            )
    docs = docs[: max_docs * 2]
    texts = fetch_fn([d["url"] for d in docs])
    kept = []
    for d in docs:
        text = texts.get(d["url"]) or ""
        d["text"] = text if len(text) >= MIN_DOC_CHARS else d["snippet"]
        if d["text"]:
            kept.append(d)
        if len(kept) >= max_docs:
            break
    return kept, queries, errors


def _render_docs(docs: list[dict[str, Any]]) -> str:
    chunks = []
    for i, d in enumerate(docs, start=1):
        head = f"[{i}] {d['title']}"
        if d.get("date"):
            head += f" ({d['date']})"
        chunks.append(f"{head}\n{d['url']}\n{d['text']}")
    return "\n\n".join(chunks)


def research_region_search(
    locality: str, purpose: Purpose, *, search_fn=None, fetch_fn=None, chat_fn=None
) -> dict[str, Any]:
    from landcheck import llm

    if chat_fn is None and not llm.providers_from_env():
        return _failed(
            "search", "нет провайдера LLM: задайте бесплатный GROQ_API_KEY (или OPENROUTER_API_KEY)"
        )
    chat_fn = chat_fn or llm.chat
    docs, queries, errors = collect_documents(
        locality, purpose, search_fn=search_fn, fetch_fn=fetch_fn
    )
    if not docs:
        return _failed(
            "search",
            "по локации ничего не найдено в открытых источниках"
            + (": " + "; ".join(errors) if errors else ""),
        )
    prompt = _RAG_PROMPT.format(
        locality=locality,
        activity=purpose.activity.lower(),
        sections="\n".join(f"- {s}" for s in SECTIONS),
        docs=_render_docs(docs),
    )
    system = _RAG_SYSTEM.format(today=datetime.now(UTC).strftime("%d.%m.%Y"))
    res = chat_fn(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        json_object=True,
        temperature=0.2,
        max_tokens=2500,
    )
    if res.get("error") or not res.get("json"):
        return _failed("search", res.get("error") or "модель не вернула JSON")
    data = res["json"]
    sections: list[dict[str, Any]] = []
    cited: set[int] = set()
    by_title = {
        str(s.get("title", "")).strip().lower(): s
        for s in data.get("sections") or []
        if isinstance(s, dict)
    }
    for title in SECTIONS:
        sec = by_title.get(title.lower()) or {}
        items = []
        for it in sec.get("items") or []:
            text = re.sub(r"\s+", " ", str(it)).strip()
            if not text:
                continue
            for m in re.finditer(r"\[(\d+(?:\s*,\s*\d+)*)\]", text):
                cited.update(int(x) for x in re.findall(r"\d+", m.group(1)))
            items.append(text)
        sections.append({"title": title, "items": items or ["данных не найдено"]})
    for n in data.get("used") or []:
        if isinstance(n, int):
            cited.add(n)
    sources = [
        {"n": i, "title": d["title"], "url": d["url"], "date": d.get("date")}
        for i, d in enumerate(docs, start=1)
    ]
    grounded = bool(cited & {s["n"] for s in sources})
    text = "\n".join(
        f"## {s['title']}\n" + "\n".join(f"- {i}" for i in s["items"]) for s in sections
    )
    out = {
        "model": f"{res['provider']}/{res['model']}",
        "fetched_at": datetime.now(UTC).isoformat(),
        "sections": sections if grounded else [],
        "text": text,
        "sources": sources,
        "queries": queries,
        "grounded": grounded,
        "usage": None,
        "error": None
        if grounded
        else "модель не сослалась ни на один источник — результат не показываем",
        "source": SRC,
        "search_errors": errors,
    }
    return out


# --- платный путь: Gemini с google_search (оставлен как опция) ----------------------------
def research_region_gemini(
    locality: str,
    purpose: Purpose,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
) -> dict[str, Any]:
    """Запрос к Gemini с google_search; при 503/429 пробуем запасные модели по очереди."""
    tried: list[str] = []
    result: dict[str, Any] | None = None
    for m in (model, *[f for f in fallbacks if f != model]):
        result = _research_once(locality, purpose, api_key=api_key, model=m, timeout=timeout)
        tried.append(m)
        if not result.get("retry_next"):
            break
    assert result is not None
    result.pop("retry_next", None)
    if len(tried) > 1:
        result["tried"] = tried
    return result


def _research_once(
    locality: str, purpose: Purpose, *, api_key: str, model: str, timeout: float
) -> dict[str, Any]:
    prompt = _PROMPT.format(locality=locality, activity=purpose.activity.lower())
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }
    url = GEMINI_URL.format(model=model)
    try:
        r = httpx.post(url, params={"key": api_key}, json=body, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — сеть
        return _failed(model, f"{type(e).__name__}: {e}")
    if r.status_code != 200:
        try:
            err = r.json().get("error") or {}
        except ValueError:
            err = {}
        status = err.get("status") or str(r.status_code)
        hint = {
            "RESOURCE_EXHAUSTED": "нет квоты на поиск Google у проекта Gemini: нужен платный "
            "уровень (биллинг в AI Studio)",
            "UNAVAILABLE": "модель перегружена, повторить позже",
            "NOT_FOUND": "модель недоступна этому ключу, сменить LANDCHECK_GEMINI_MODEL",
            "PERMISSION_DENIED": "ключ отклонён",
            "INVALID_ARGUMENT": "ключ или запрос отклонены",
        }.get(status, "")
        failed = _failed(model, f"Gemini {status} ({r.status_code}){': ' + hint if hint else ''}")
        failed["retry_next"] = status in ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "NOT_FOUND")
        return failed
    data = r.json()
    parsed = parse_response(data, model=model)
    if not parsed["grounded"]:
        # Без ссылок ответ — это «память» модели, а не открытые источники. Не показываем.
        parsed["sections"] = []
        parsed["error"] = "модель ответила без ссылок на источники — результат не показываем"
    return parsed


def _failed(model: str, error: str) -> dict[str, Any]:
    return {
        "model": model,
        "fetched_at": datetime.now(UTC).isoformat(),
        "sections": [],
        "text": "",
        "sources": [],
        "queries": [],
        "grounded": False,
        "usage": None,
        "error": error,
        "source": SRC,
    }
