"""Языковая модель через цепочку OpenAI-совместимых провайдеров: бесплатный стек по умолчанию.

Решение Тони 19.10.2026: Gemini платный — используем бесплатные уровни с максимумом качества.
Порядок: Groq (gpt-oss-120b, Qwen 3.8 27B; бесплатно, без карты) → OpenRouter (модели :free)
→ Mistral (бесплатный уровень) → Gemini (только если ключ оставлен). Каждый провайдер — обычный
`/chat/completions`; при 429/5xx идём к следующему. Ошибки — в поле error, исключений наружу нет.
"""

from __future__ import annotations

import json
import logging
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
    # Бесплатные модели OpenRouter, проверены 20.10.2026 на промпте «Региона» с выключенными
    # рассуждениями и JSON-режимом: lightning 8 с, super 16 с, ultra 21 с, все отдают 6 разделов;
    # gemma-4 :free отвечает 429 (общий пул перегружен), minimax не даёт выключить рассуждения.
    # Список меняется: openrouter.ai/api/v1/models, pricing.prompt == "0".
    # Порядок: super (16 с, чистый русский) → ultra (21 с) → lightning (8 с, но огрехи в тексте
    # вроде «permanente», «сети, сети») → маршрутизатор.
    "openrouter": (
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3.5-lightning:free",
        "openrouter/free",
    ),
    "mistral": ("mistral-medium-latest", "mistral-small-latest"),
    "gemini": ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash"),
    # DeepSeek напрямую: ~$0.3/M входных и $1.2/M выходных токенов на Flash (пик), «Регион»
    # обходится в доли цента; JSON-режим поддерживается. Первый в цепочке, если есть ключ.
    "deepseek": ("deepseek-flash", "deepseek-v4-pro"),
}
BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    # Gemini через OpenAI-совместимый endpoint — платный уровень, только если явно включён
    # в LANDCHECK_LLM (например, «groq,gemini»).
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}
log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def _models_from_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(f"{name.upper()}_MODELS") or os.environ.get(f"{name.upper()}_MODEL")
    if raw:
        return tuple(m.strip() for m in raw.split(",") if m.strip())
    return DEFAULT_MODELS[name]


def providers_from_env(order: str | None = None) -> list[Provider]:
    """Провайдеры с ключами в окружении, в порядке LANDCHECK_LLM.

    По умолчанию deepseek,groq,openrouter,mistral; без ключа провайдер пропускается.
    """
    order = order or os.environ.get("LANDCHECK_LLM") or "deepseek,groq,openrouter,mistral"
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
                name=name, base_url=BASE_URLS[name], api_key=key, models=_models_from_env(name)
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
            headers = {"Authorization": f"Bearer {p.api_key}"}
            if p.name == "openrouter":
                headers["HTTP-Referer"] = "https://land-mp.dnai.engineering"
                headers["X-Title"] = "landcheck"
            r = None
            # Сначала строгий режим: без «рассуждений» (иначе reasoning-модели тратят на них все
            # токены и отдают пустой ответ) и с JSON-форматом; если модель его не принимает (400),
            # повторяем один раз без этих полей.
            for strict in (True, False) if p.json_mode else (False,):
                body: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if strict:
                    if json_object:
                        body["response_format"] = {"type": "json_object"}
                    if p.name == "openrouter":
                        body["reasoning"] = {"enabled": False}
                    if p.name == "deepseek":
                        # V4.1 Flash по умолчанию «думает» (reasoning_tokens даже на простом
                        # вопросе); без размышлений ответ на 35 % быстрее и дешевле (замер 20.10).
                        body["thinking"] = {"type": "disabled"}
                try:
                    r = httpx.post(
                        f"{p.base_url}/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=timeout,
                    )
                except Exception as e:  # noqa: BLE001 — сеть
                    r = None
                    last_error = f"{p.name}/{model}: {type(e).__name__}: {e}"
                    log.warning("llm %s", last_error)
                    break
                if r.status_code != 400 or not strict:
                    break
            if r is None:
                continue
            if r.status_code != 200:
                last_error = f"{p.name}/{model}: HTTP {r.status_code}"
                log.warning("llm %s: %s", last_error, (r.text or "")[:300].replace("\n", " "))
                if r.status_code in RETRY_STATUSES or r.status_code in (400, 404):
                    continue
                break
            try:
                message = r.json()["choices"][0]["message"]
                text = message.get("content") or ""
            except (ValueError, KeyError, IndexError, TypeError):
                last_error = f"{p.name}/{model}: ответ без choices"
                continue
            if not text.strip():
                last_error = f"{p.name}/{model}: пустой ответ" + (
                    " (все токены ушли в рассуждения)" if message.get("reasoning") else ""
                )
                log.warning("llm %s", last_error)
                continue
            data = parse_json_text(text) if json_object else None
            if json_object and data is None:
                last_error = f"{p.name}/{model}: ответ не JSON"
                log.warning("llm %s: %s", last_error, text[:200].replace("\n", " "))
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
