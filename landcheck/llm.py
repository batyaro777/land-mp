"""Языковая модель через цепочку OpenAI-совместимых провайдеров: бесплатный стек по умолчанию.

Решение Тони 19.10.2026: Gemini платный — используем бесплатные уровни с максимумом качества.
Порядок: Groq (gpt-oss-120b, Qwen 3.8 27B; бесплатно, без карты) → OpenRouter (модели :free)
→ Mistral (бесплатный уровень) → Gemini (только если ключ оставлен). Каждый провайдер — обычный
`/chat/completions`; при 429/5xx идём к следующему. Ошибки — в поле error, исключений наружу нет.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    models: tuple[str, ...]
    json_mode: bool = True  # поддерживает response_format json_object


DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    "groq": ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"),
    # Бесплатные модели OpenRouter на 20.10.2026 (список меняется: openrouter.ai/api/v1/models,
    # pricing.prompt == "0"); последним — маршрутизатор по любой свободной модели.
    "openrouter": (
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "minimax/minimax-m3.1-flash:free",
        "openrouter/free",
    ),
    "mistral": ("mistral-medium-latest", "mistral-small-latest"),
    "gemini": ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash"),
}
BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    # Gemini через OpenAI-совместимый endpoint — платный уровень, только если явно включён
    # в LANDCHECK_LLM (например, «groq,gemini»).
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def _models_from_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(f"{name.upper()}_MODELS") or os.environ.get(f"{name.upper()}_MODEL")
    if raw:
        return tuple(m.strip() for m in raw.split(",") if m.strip())
    return DEFAULT_MODELS[name]


def providers_from_env(order: str | None = None) -> list[Provider]:
    """Провайдеры с ключами в окружении, в порядке LANDCHECK_LLM.

    По умолчанию groq,openrouter,mistral; без ключа провайдер пропускается.
    """
    order = order or os.environ.get("LANDCHECK_LLM") or "groq,openrouter,mistral"
    out: list[Provider] = []
    for name in (n.strip().lower() for n in order.split(",")):
        if name not in BASE_URLS:
            continue
        key = os.environ.get(f"{name.upper()}_API_KEY")
        if name == "gemini":
            key = key or os.environ.get("GOOGLE_API_KEY")
        if not key:
            continue
        out.append(
            Provider(
                name=name,
                base_url=BASE_URLS[name],
                api_key=key,
                models=_models_from_env(name),
                json_mode=name != "openrouter",
            )
        )
    return out


def parse_json_text(text: str) -> dict[str, Any] | None:
    """JSON из ответа модели, в том числе внутри ```json … ``` или с преамбулой."""
    t = (text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def chat(
    messages: list[dict[str, str]],
    *,
    providers: list[Provider] | None = None,
    json_object: bool = False,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """{"text", "json", "provider", "model", "error", "tried"}: первый удачный ответ в цепочке."""
    providers = providers_from_env() if providers is None else providers
    tried: list[str] = []
    last_error = "нет ни одного провайдера LLM: задайте GROQ_API_KEY (бесплатно) или другой ключ"
    for p in providers:
        for model in p.models:
            tried.append(f"{p.name}/{model}")
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if json_object and p.json_mode:
                body["response_format"] = {"type": "json_object"}
            headers = {"Authorization": f"Bearer {p.api_key}"}
            if p.name == "openrouter":
                headers["HTTP-Referer"] = "https://land-mp.dnai.engineering"
                headers["X-Title"] = "landcheck"
            try:
                r = httpx.post(
                    f"{p.base_url}/chat/completions", json=body, headers=headers, timeout=timeout
                )
            except Exception as e:  # noqa: BLE001 — сеть
                last_error = f"{p.name}/{model}: {type(e).__name__}"
                continue
            if r.status_code != 200:
                last_error = f"{p.name}/{model}: HTTP {r.status_code}"
                if r.status_code in RETRY_STATUSES or r.status_code in (400, 404):
                    # 400/404 — модель недоступна или не поддерживает json_object: пробуем дальше
                    continue
                break
            try:
                text = r.json()["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError):
                last_error = f"{p.name}/{model}: ответ без choices"
                continue
            data = parse_json_text(text) if json_object else None
            if json_object and data is None:
                last_error = f"{p.name}/{model}: ответ не JSON"
                continue
            return {
                "text": text,
                "json": data,
                "provider": p.name,
                "model": model,
                "error": None,
                "tried": tried,
            }
    return {
        "text": "",
        "json": None,
        "provider": None,
        "model": None,
        "error": last_error,
        "tried": tried,
    }


def available() -> bool:
    return bool(providers_from_env())
