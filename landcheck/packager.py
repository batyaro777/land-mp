"""Автоупаковка объявления (ТЗ v2, К2.2): заголовок и текст из фактов ЕГРН и заметок продавца
через Gemini. Правила честности: факты ЕГРН — дословно и только они; обещаний, которых
кадастр не подтверждает, в тексте нет; результат всё равно проходит `listing.contradictions`.
Без ключа или при ошибке остаётся шаблонный текст `build_draft`.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from landcheck import llm
from landcheck.llm import parse_json_text
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

{rubric}
Цена: {price}.
Запрещено: {forbidden}.
Если категории или ВРИ нет в фактах — назначение участка не придумывай и не пиши, что что-то
«не указано» или «не установлено»: просто опиши участок без этого. Служебные фразы
(«в предоставленных данных», «не указан», «не установлена») в заголовке и тексте запрещены.
Площадь меньше одной сотки в фактах — вероятная ошибка данных: не называй её в заголовке.
Не добавляй характеристик, которых нет в фактах и заметках: постройки или их отсутствие, подъезд
и дорога, тишина, соседи, коммуникации, вид, лес, река — только если это написал продавец.
Если заметок мало, текст будет короче — это нормально. Каждую заметку продавца приводи один раз,
не повторяй её в заголовке, в первом абзаце и ещё раз в третьем.

Верни JSON с двумя полями:
- "title": заголовок до {title_max} знаков — только из фактов: площадь, назначение, населённый
  пункт, без слова «продам»; если ничего из этого в фактах нет — «Земельный участок» и населённый
  пункт, без описательных оборотов («в районе с установленными границами»);
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
    forbidden: list[str] | None = None,
) -> str:
    rules = ["обещать то, чего нет в фактах ЕГРН", *(forbidden or [])]
    rubric = (
        f"Рубрика площадки (это не назначение участка — назначение только из факта «ВРИ»): "
        f"{object_type}."
        if object_type
        else "Рубрика площадки не определена: категорию и назначение участка в тексте не упоминай."
    )
    return _PROMPT.format(
        facts="\n".join(f"- {f}" for f in facts) or "- нет",
        zouit="\n".join(f"- {z}" for z in zouit_notes) or "- нет",
        notes=notes.strip() or "- нет",
        rubric=rubric,
        price=_fmt_price(price_rub),
        title_max=TITLE_MAX,
        fetched=fetched,
        forbidden="; ".join(rules),
    )


_SERVICE_RE = re.compile(
    r"(не указан|не установлен|не определен|предоставленн\w* данн|исходн\w* данн|"
    r"в данных|нет данных|отсутствует в|не предоставил|дополнительн\w* сведен|сведений нет|"
    r"информации нет|информация отсутствует)",
    re.IGNORECASE,
)


def scrub_service_phrases(title: str, desc: str) -> tuple[str, str]:
    """Модель пишет покупателю «Точный населённый пункт не указан в предоставленных данных»
    и «категория не установлена» (прод 20.10) — такие предложения убираем кодом."""
    parts = [p.strip() for p in title.split(",")]
    title = ", ".join(p for p in parts if p and not _SERVICE_RE.search(p)) or "Земельный участок"
    paragraphs: list[str] = []
    for para in re.split(r"\n\s*\n", desc):
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", para.strip()) if x]
        kept = [x for x in sentences if not _SERVICE_RE.search(x)]
        if kept:
            paragraphs.append(" ".join(kept))
    return title, "\n\n".join(paragraphs)


def _finish(title: str, desc: str, model: str) -> dict[str, Any]:
    title, desc = scrub_service_phrases(title, desc)
    title = re.sub(r"\s+", " ", title).strip()[:TITLE_MAX].rstrip(" ,.;—-")
    return {"title": title, "description": desc.strip(), "model": model, "error": None}


def compose_listing(
    *,
    facts: list[str],
    zouit_notes: list[str],
    notes: str,
    object_type: str,
    price_rub: float | None,
    fetched: str,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 60.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
    providers: list[llm.Provider] | None = None,
    forbidden: list[str] | None = None,
) -> dict[str, Any]:
    """{"title", "description", "model", "error"} — исключений наружу нет.

    Сначала бесплатные провайдеры (`landcheck.llm`), Gemini — только если других нет и ключ есть.
    """
    prompt = build_prompt(
        facts=facts,
        zouit_notes=zouit_notes,
        notes=notes,
        object_type=object_type,
        price_rub=price_rub,
        fetched=fetched,
        forbidden=forbidden,
    )
    providers = llm.providers_from_env() if providers is None else providers
    if providers:
        res = llm.chat(
            [{"role": "user", "content": prompt}],
            providers=providers,
            json_object=True,
            temperature=0.4,
            timeout=timeout,
        )
        if res.get("error") or not res.get("json"):
            return {
                "title": "",
                "description": "",
                "model": "",
                "error": res.get("error") or "не JSON",
            }
        data = res["json"]
        if not str(data.get("description", "")).strip():
            return {"title": "", "description": "", "model": res["model"], "error": "пустой текст"}
        return _finish(
            str(data.get("title", "")),
            str(data["description"]),
            f"{res['provider']}/{res['model']}",
        )
    if not api_key:
        return {"title": "", "description": "", "model": "", "error": "нет провайдера LLM"}
    return _compose_gemini(
        prompt, api_key=api_key, model=model, timeout=timeout, fallbacks=fallbacks
    )


def _compose_gemini(
    prompt: str, *, api_key: str, model: str, timeout: float, fallbacks: tuple[str, ...]
) -> dict[str, Any]:
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
        return _finish(str(data.get("title", "")), str(data["description"]), m)
    return last
