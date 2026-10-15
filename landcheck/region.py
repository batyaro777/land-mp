"""Регион: инвестиции, дороги, риски и рынок по открытым источникам через Gemini с поиском Google.

Боль брокера (Антон М., 15.10.2026): «информация есть в открытом доступе, но её надо собирать,
структурировать и догадаться, что собирать». Модель с инструментом google_search собирает и
цитирует источники; мы показываем текст по фиксированным разделам и список ссылок.
Правила честности: без поиска (нет квоты, нет ключа) блок говорит об этом и НЕ отвечает из
«памяти» модели; каждый ответ помечен датой и моделью; один запрос на населённый пункт,
а не на участок (кэш по локации).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from landcheck.models import Purpose

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
SECTIONS = (
    "Развитие и инвестиции",
    "Дороги и инфраструктура",
    "Негативные факторы",
    "Рынок земли",
    "Статус территории",
    "Вывод для покупателя",
)
SRC = "Gemini с поиском Google по открытым источникам; ссылки ниже; не юридическое заключение"

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


def research_region(
    locality: str,
    purpose: Purpose,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Один запрос к Gemini с google_search. Ошибки — в поле error, исключений наружу нет."""
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
        return _failed(model, f"Gemini {status} ({r.status_code}){': ' + hint if hint else ''}")
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
