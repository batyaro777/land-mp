"""Голосовой ввод: расшифровка речи через Gemini (аудио как inline-часть запроса).

ТЗ v2, М1.3: цель и запрос можно проговорить. Выбор движка (19.10.2026): тот же ключ Gemini, что
и у модуля «Регион», без нового провайдера и секрета; проверено на реальной русской речи —
30 с за 3,9 с, 750 аудио-токенов, с пунктуацией. Текст показывается пользователю для правки
перед отправкой формы, сам по себе ничего не запускает.
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from landcheck.region import DEFAULT_MODEL, FALLBACK_MODELS, GEMINI_URL

MAX_AUDIO_BYTES = 4 * 1024 * 1024  # 60 с WAV 16 кГц моно 16 бит ≈ 1,9 МБ; запас на другие кодеки
MAX_SECONDS = 60
ALLOWED_MIME = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/aac",
    "audio/flac",
}

_PROMPT = (
    "Расшифруй речь дословно на русском языке. Верни только текст расшифровки, без пояснений, "
    "без кавычек и без заголовков. Убери междометия и запинки («э-э», «ну вот»). Числа пиши "
    "цифрами; кадастровые номера — цифрами через двоеточия, например 50:09:0060202:101; "
    "суммы — числом с единицей (2,5 млн). Если речи нет или она неразборчива, верни пустую строку."
)


def normalize_mime(mime: str | None) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if m in ("audio/x-wav", "audio/wave"):
        return "audio/wav"
    return m


def clean_text(text: str) -> str:
    t = text.strip().strip("«»\"'")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def transcribe(
    audio: bytes,
    mime: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 60.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
) -> dict[str, Any]:
    """Ответ {"text", "model", "error", "usage"}; ошибки — в поле error, исключений наружу нет."""
    mime = normalize_mime(mime)
    if mime not in ALLOWED_MIME:
        return _failed(model, f"формат аудио не поддерживается: {mime or 'не указан'}")
    if not audio:
        return _failed(model, "пустая запись")
    if len(audio) > MAX_AUDIO_BYTES:
        return _failed(model, f"запись больше {MAX_AUDIO_BYTES // (1024 * 1024)} МБ")
    result: dict[str, Any] | None = None
    for m in (model, *[f for f in fallbacks if f != model]):
        result = _once(audio, mime, api_key=api_key, model=m, timeout=timeout)
        if not result.pop("retry_next", False):
            break
    assert result is not None
    return result


def _once(audio: bytes, mime: str, *, api_key: str, model: str, timeout: float) -> dict[str, Any]:
    body = {
        "contents": [
            {
                "parts": [
                    {"text": _PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(audio).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {"temperature": 0},
    }
    try:
        r = httpx.post(
            GEMINI_URL.format(model=model), params={"key": api_key}, json=body, timeout=timeout
        )
    except Exception as e:  # noqa: BLE001 — сеть
        return _failed(model, f"{type(e).__name__}: {e}")
    if r.status_code != 200:
        try:
            err = r.json().get("error") or {}
        except ValueError:
            err = {}
        status = err.get("status") or str(r.status_code)
        failed = _failed(model, f"Gemini {status} ({r.status_code})")
        failed["retry_next"] = status in ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "NOT_FOUND")
        return failed
    return parse_response(r.json(), model=model)


def parse_response(data: dict[str, Any], *, model: str) -> dict[str, Any]:
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = clean_text("".join(p.get("text", "") for p in parts if isinstance(p, dict)))
    return {"text": text, "model": model, "error": None, "usage": data.get("usageMetadata")}


def _failed(model: str, error: str) -> dict[str, Any]:
    return {"text": "", "model": model, "error": error, "usage": None}
