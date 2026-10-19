"""Автоупаковка объявления (ТЗ v2, К2.2): заголовок и текст из фактов ЕГРН и заметок продавца
через Gemini. Правила честности: факты ЕГРН — дословно и только они; обещаний, которых
кадастр не подтверждает, в тексте нет; результат всё равно проходит `listing.contradictions`.
Без ключа или при ошибке остаётся шаблонный текст `build_draft`.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from landcheck.region import DEFAULT_MODEL, FALLBACK_MODELS, GEMINI_URL

TITLE_MAX = 50

_PROMPT = """Ты редактор объявлений о продаже земельных участков в России. Собери объявление.

Факты из ЕГРН (используй дословно, ничего не добавляй и не меняй):
{facts}

Ограничения из публичных данных (ЗОУИТ), если есть:
{zouit}

Заметки продавца (могут содержать неточности — бери только то, что не противоречит фактам ЕГРН;
не обещай «ИЖС», если ВРИ другой; не пиши «без обременений», если есть ЗОУИТ; не давай юридических
гарантий):
{notes}

Тип для площадки: {object_type}. Цена: {price}.
Если категория или ВРИ в фактах не установлены — так и пиши, назначение участка не придумывай.
Площадь меньше одной сотки в фактах — вероятная ошибка данных: не называй её в заголовке.

Верни JSON с двумя полями:
- "title": заголовок до {title_max} знаков — площадь, назначение, населённый пункт,
  без слова «продам»;
- "description": текст 600–1100 знаков, 3–5 абзацев, спокойный деловой тон, без капслока,
  эмодзи и восклицаний; первый абзац — что это и где; второй — факты ЕГРН; третий — что есть
  на участке и рядом по заметкам продавца; затем ограничения, если есть; в конце одна строка
  «Данные НСПД от {fetched}. Документы покажем при осмотре.»
Только JSON, без пояснений."""


def _fmt_price(price_rub: float | None) -> str:
    if not price_rub:
        return "не указана"
    return f"{price_rub:,.0f} ₽".replace(",", " ")


def build_prompt(
    *,
    facts: list[str],
    zouit_notes: list[str],
    notes: str,
    object_type: str,
    price_rub: float | None,
    fetched: str,
) -> str:
    return _PROMPT.format(
        facts="\n".join(f"- {f}" for f in facts) or "- нет",
        zouit="\n".join(f"- {z}" for z in zouit_notes) or "- нет",
        notes=notes.strip() or "- нет",
        object_type=object_type,
        price=_fmt_price(price_rub),
        title_max=TITLE_MAX,
        fetched=fetched,
    )


def parse_json_text(text: str) -> dict[str, Any] | None:
    """JSON из ответа модели, в том числе внутри ```json … ```."""
    t = text.strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def compose_listing(
    *,
    facts: list[str],
    zouit_notes: list[str],
    notes: str,
    object_type: str,
    price_rub: float | None,
    fetched: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 60.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
) -> dict[str, Any]:
    """{"title", "description", "model", "error"} — исключений наружу нет."""
    prompt = build_prompt(
        facts=facts,
        zouit_notes=zouit_notes,
        notes=notes,
        object_type=object_type,
        price_rub=price_rub,
        fetched=fetched,
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
    }
    last: dict[str, Any] = {"title": "", "description": "", "model": model, "error": "не вызван"}
    for m in (model, *[f for f in fallbacks if f != model]):
        try:
            r = httpx.post(
                GEMINI_URL.format(model=m), params={"key": api_key}, json=body, timeout=timeout
            )
        except Exception as e:  # noqa: BLE001 — сеть
            return {"title": "", "description": "", "model": m, "error": f"{type(e).__name__}: {e}"}
        if r.status_code != 200:
            try:
                status = (r.json().get("error") or {}).get("status") or str(r.status_code)
            except ValueError:
                status = str(r.status_code)
            last = {"title": "", "description": "", "model": m, "error": f"Gemini {status}"}
            if status in ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "NOT_FOUND"):
                continue
            return last
        cand = (r.json().get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        data = parse_json_text("".join(p.get("text", "") for p in parts if isinstance(p, dict)))
        if not data or not str(data.get("description", "")).strip():
            return {"title": "", "description": "", "model": m, "error": "модель вернула не JSON"}
        title = re.sub(r"\s+", " ", str(data.get("title", ""))).strip()[:TITLE_MAX].rstrip(" ,.;—-")
        desc = str(data["description"]).strip()
        return {"title": title, "description": desc, "model": m, "error": None}
    return last
