"""Голосовой ввод: расшифровка речи цепочкой провайдеров, бесплатный стек по умолчанию.

ТЗ v2, М1.3. Решение Тони 19.10.2026: Gemini платный — используем бесплатные уровни.
Порядок (LANDCHECK_ASR, по умолчанию ``groq,elevenlabs,local,gemini``):
- ``groq`` — Whisper large-v3-turbo на Groq (бесплатный уровень без карты: 20 запросов/мин,
  2 000/день, 28 800 аудио-секунд/день);
- ``elevenlabs`` — Scribe v2 (подписка Тони; часы включены в план), лучшая точность на русском;
- ``local`` — faster-whisper на сервере (extra ``asr-local``), только WAV, без внешних сервисов;
- ``gemini`` — прежний путь, если ключ оставлен.
В браузере первым работает Web Speech API (см. voice.js); сюда приходит запасной путь.
Текст показывается пользователю для правки перед отправкой формы, сам по себе ничего не запускает.
"""

from __future__ import annotations

import base64
import io
import os
import re
import threading
import wave
from typing import Any

import httpx
import numpy as np

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
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_STT_MODEL") or "scribe_v2"
LOCAL_MODEL = os.environ.get("LANDCHECK_WHISPER_MODEL") or "large-v3-turbo"

# Тишина: на пустой записи модель выдумывает текст («Да, добрый день», «277» — проверено на
# проде 19.10.2026), поэтому до запроса меряем уровень сигнала. Речь в реальной записи даёт
# RMS 0,02–0,13 по секундам, комнатный шум микрофона — тысячные.
SILENCE_RMS = 0.005
MIN_SECONDS = 0.5
NO_SPEECH = "[нет речи]"

_PROMPT = (
    "Расшифруй речь дословно на русском языке. Верни только текст расшифровки, без пояснений, "
    "без кавычек и без заголовков. Убери междометия и запинки («э-э», «ну вот»). Числа пиши "
    "цифрами; кадастровые номера — цифрами через двоеточия, например 50:09:0060202:101; "
    f"суммы — числом с единицей (2,5 млн). Если речи нет, она неразборчива или это не русская "
    f"речь, верни ровно {NO_SPEECH} и ничего больше."
)
# Подсказка Whisper задаёт стиль: пунктуация, цифры, формат кадастрового номера.
_WHISPER_PROMPT = (
    "Построить дом для семьи до 3 млн, участок 8–12 соток, кадастровый номер 50:09:0060202:101, "
    "газ по границе, Истра."
)


def wav_stats(audio: bytes) -> tuple[float, float] | None:
    """(длительность, RMS 0..1) для WAV PCM 16 бит; None — не WAV или не разобрать."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            if w.getsampwidth() != 2:
                return None
            rate, frames = w.getframerate(), w.readframes(w.getnframes())
    except (wave.Error, EOFError, ValueError):
        return None
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size == 0 or rate <= 0:
        return (0.0, 0.0)
    return (samples.size / rate, float(np.sqrt(np.mean(samples**2))))


def normalize_mime(mime: str | None) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if m in ("audio/x-wav", "audio/wave"):
        return "audio/wav"
    return m


def clean_text(text: str) -> str:
    t = text.strip().strip("«»\"'")
    t = re.sub(r"\s+", " ", t)
    if t.strip("[]«» ").lower() in ("нет речи", "no speech"):
        return ""
    return t.strip()


def providers_available(order: str | None = None) -> list[str]:
    """Провайдеры из LANDCHECK_ASR, у которых есть ключ или установленный пакет."""
    order = order or os.environ.get("LANDCHECK_ASR") or "groq,elevenlabs,local,gemini"
    out: list[str] = []
    for name in (n.strip().lower() for n in order.split(",")):
        if name == "groq" and os.environ.get("GROQ_API_KEY"):
            out.append(name)
        elif name == "elevenlabs" and os.environ.get("ELEVENLABS_API_KEY"):
            out.append(name)
        elif name == "local" and _local_available():
            out.append(name)
        elif name == "gemini" and (
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        ):
            out.append(name)
    return out


def _local_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def transcribe(
    audio: bytes,
    mime: str,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 60.0,
    fallbacks: tuple[str, ...] = FALLBACK_MODELS,
    providers: list[str] | None = None,
) -> dict[str, Any]:
    """Ответ {"text", "model", "error", "usage"}; ошибки — в поле error, исключений наружу нет.

    `api_key`/`model`/`fallbacks` относятся к Gemini и сохранены для совместимости.
    """
    mime = normalize_mime(mime)
    if mime not in ALLOWED_MIME:
        return _failed("", f"формат аудио не поддерживается: {mime or 'не указан'}")
    if not audio:
        return _failed("", "пустая запись")
    if len(audio) > MAX_AUDIO_BYTES:
        return _failed("", f"запись больше {MAX_AUDIO_BYTES // (1024 * 1024)} МБ")
    if mime == "audio/wav":
        stats = wav_stats(audio)
        if stats is not None and (stats[0] < MIN_SECONDS or stats[1] < SILENCE_RMS):
            return {
                "text": "",
                "model": "",
                "error": None,
                "usage": None,
                "silent": True,
                "seconds": round(stats[0], 2),
                "rms": round(stats[1], 4),
            }
    providers = providers_available() if providers is None else providers
    if api_key and "gemini" not in providers:
        providers = [*providers, "gemini"]
    errors: list[str] = []
    for name in providers:
        if name == "groq":
            res = _transcribe_groq(audio, mime, timeout=timeout)
        elif name == "elevenlabs":
            res = _transcribe_elevenlabs(audio, mime, timeout=timeout)
        elif name == "local":
            res = _transcribe_local(audio, mime)
        elif name == "gemini":
            key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            res = _transcribe_gemini(
                audio, mime, api_key=key or "", model=model, timeout=timeout, fallbacks=fallbacks
            )
        else:
            continue
        if not res.get("error"):
            return res
        errors.append(f"{name}: {res['error']}")
    if not providers:
        return _failed("", "нет провайдера распознавания: задайте GROQ_API_KEY (бесплатно)")
    return _failed("", "; ".join(errors))


# --- Groq Whisper --------------------------------------------------------------------------
def _transcribe_groq(audio: bytes, mime: str, *, timeout: float) -> dict[str, Any]:
    key = os.environ.get("GROQ_API_KEY") or ""
    ext = {"audio/wav": "wav", "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a"}.get(
        mime, "wav"
    )
    try:
        r = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (f"voice.{ext}", audio, mime)},
            data={
                "model": GROQ_MODEL,
                "language": "ru",
                "response_format": "json",
                "temperature": "0",
                "prompt": _WHISPER_PROMPT,
            },
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 — сеть
        return _failed(GROQ_MODEL, f"{type(e).__name__}: {e}")
    if r.status_code != 200:
        return _failed(GROQ_MODEL, f"Groq HTTP {r.status_code}")
    try:
        text = r.json().get("text") or ""
    except ValueError:
        return _failed(GROQ_MODEL, "Groq: ответ не JSON")
    return {"text": clean_text(text), "model": f"groq/{GROQ_MODEL}", "error": None, "usage": None}


# --- ElevenLabs Scribe --------------------------------------------------------------------
def _transcribe_elevenlabs(audio: bytes, mime: str, *, timeout: float) -> dict[str, Any]:
    key = os.environ.get("ELEVENLABS_API_KEY") or ""
    ext = {"audio/wav": "wav", "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a"}.get(
        mime, "wav"
    )
    try:
        r = httpx.post(
            ELEVENLABS_URL,
            headers={"xi-api-key": key},
            files={"file": (f"voice.{ext}", audio, mime)},
            data={
                "model_id": ELEVENLABS_MODEL,
                "language_code": "rus",
                "tag_audio_events": "false",
                "diarize": "false",
            },
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 — сеть
        return _failed(ELEVENLABS_MODEL, f"{type(e).__name__}: {e}")
    if r.status_code != 200:
        return _failed(ELEVENLABS_MODEL, f"ElevenLabs HTTP {r.status_code}")
    try:
        text = r.json().get("text") or ""
    except ValueError:
        return _failed(ELEVENLABS_MODEL, "ElevenLabs: ответ не JSON")
    return {
        "text": clean_text(text),
        "model": f"elevenlabs/{ELEVENLABS_MODEL}",
        "error": None,
        "usage": None,
    }


# --- локальный faster-whisper ---------------------------------------------------------------
_local_model = None
_local_lock = threading.Lock()


def _get_local_model():
    global _local_model
    with _local_lock:
        if _local_model is None:
            from faster_whisper import WhisperModel

            _local_model = WhisperModel(
                LOCAL_MODEL,
                device="cpu",
                compute_type="int8",
                cpu_threads=int(os.environ.get("LANDCHECK_WHISPER_THREADS") or "4"),
            )
        return _local_model


def _transcribe_local(audio: bytes, mime: str) -> dict[str, Any]:
    if mime != "audio/wav":
        return _failed(LOCAL_MODEL, "локальная модель принимает только WAV")
    try:
        with wave.open(io.BytesIO(audio)) as w:
            rate, ch, frames = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    except (wave.Error, EOFError, ValueError) as e:
        return _failed(LOCAL_MODEL, f"WAV не разобран: {e}")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        samples = samples.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        idx = np.linspace(0, samples.size - 1, int(samples.size * 16000 / rate))
        samples = np.interp(idx, np.arange(samples.size), samples).astype(np.float32)
    try:
        model = _get_local_model()
        segments, _info = model.transcribe(
            samples,
            language="ru",
            beam_size=5,
            vad_filter=True,
            initial_prompt=_WHISPER_PROMPT,
            condition_on_previous_text=False,
        )
        text = " ".join(seg.text.strip() for seg in segments)
    except Exception as e:  # noqa: BLE001 — модель, память
        return _failed(LOCAL_MODEL, f"{type(e).__name__}: {e}")
    return {"text": clean_text(text), "model": f"local/{LOCAL_MODEL}", "error": None, "usage": None}


# --- Gemini (опция) ---------------------------------------------------------------------------
def _transcribe_gemini(
    audio: bytes,
    mime: str,
    *,
    api_key: str,
    model: str,
    timeout: float,
    fallbacks: tuple[str, ...],
) -> dict[str, Any]:
    if not api_key:
        return _failed(model, "нет ключа Gemini")
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
