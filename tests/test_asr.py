"""Голосовой ввод: разбор ответа Gemini, запасные модели, маршрут /asr."""

from __future__ import annotations

import io

import httpx

from landcheck import asr
from landcheck.asr import clean_text, normalize_mime, transcribe
from tests.test_web import client, login  # noqa: F401 — фикстура клиента кабинета


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _ok(text: str) -> _Resp:
    return _Resp(
        200,
        {
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"totalTokenCount": 900},
        },
    )


def test_transcribe_ok_and_cleanup(monkeypatch):
    calls: list[str] = []

    def fake_post(url, params, json, timeout):
        calls.append(url)
        assert json["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "audio/wav"
        return _ok(" «Построить дом  до 3 млн, участок 50:09:0060202:101» \n")

    monkeypatch.setattr(httpx, "post", fake_post)
    r = transcribe(b"RIFF....", "audio/wav;codecs=1", api_key="k", model="m1")
    assert r["error"] is None
    assert r["text"] == "Построить дом до 3 млн, участок 50:09:0060202:101"
    assert r["model"] == "m1" and len(calls) == 1 and "m1:generateContent" in calls[0]


def test_transcribe_falls_back_on_503(monkeypatch):
    seq = iter(
        [
            _Resp(503, {"error": {"status": "UNAVAILABLE"}}),
            _Resp(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}),
            _ok("дача"),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: next(seq))
    r = transcribe(b"x", "audio/webm", api_key="k", model="a", fallbacks=("b", "c"))
    assert r["text"] == "дача" and r["model"] == "c" and r["error"] is None


def test_transcribe_rejects_bad_input(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert "не поддерживается" in transcribe(b"x", "text/plain", api_key="k")["error"]
    assert transcribe(b"", "audio/wav", api_key="k")["error"] == "пустая запись"
    big = b"0" * (asr.MAX_AUDIO_BYTES + 1)
    assert "больше" in transcribe(big, "audio/wav", api_key="k")["error"]


def test_transcribe_permission_denied_is_final(monkeypatch):
    calls = []

    def fake_post(*a, **k):
        calls.append(1)
        return _Resp(403, {"error": {"status": "PERMISSION_DENIED"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    r = transcribe(b"x", "audio/wav", api_key="bad")
    assert "Gemini PERMISSION_DENIED" in r["error"] and len(calls) == 1


def _wav(samples, rate: int = 16000) -> bytes:
    import io
    import wave

    import numpy as np

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.asarray(samples) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def test_silence_is_not_sent_to_gemini(monkeypatch):
    import numpy as np

    calls: list[int] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1) or _ok("выдумка"))
    silent = _wav(np.zeros(16000 * 2))
    r = transcribe(silent, "audio/wav", api_key="k")
    assert r["text"] == "" and r.get("silent") and r["error"] is None and calls == []
    quiet = _wav(np.random.default_rng(1).normal(0, 0.002, 16000 * 2).clip(-1, 1))
    assert transcribe(quiet, "audio/wav", api_key="k").get("silent") and calls == []
    short = _wav(np.random.default_rng(1).normal(0, 0.1, 4000).clip(-1, 1))
    assert transcribe(short, "audio/wav", api_key="k").get("silent") and calls == []
    speech = _wav(np.random.default_rng(1).normal(0, 0.05, 16000 * 2).clip(-1, 1))
    r = transcribe(speech, "audio/wav", api_key="k")
    assert r["text"] == "выдумка" and calls == [1]
    # ответ «[нет речи]» превращается в пустую строку
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok("[нет речи]"))
    assert transcribe(speech, "audio/wav", api_key="k")["text"] == ""
    assert asr.wav_stats(b"not a wav") is None


def test_helpers():
    assert normalize_mime("audio/x-wav; rate=16000") == "audio/wav"
    assert normalize_mime(None) == ""
    assert clean_text('  "Хочу   дом"\n') == "Хочу дом"


def test_asr_route(client, monkeypatch):  # noqa: F811
    from landcheck.web.config import settings

    login(client)
    settings.gemini_api_key = None
    r = client.post("/asr", files={"audio": ("v.wav", io.BytesIO(b"RIFF"), "audio/wav")})
    assert r.status_code == 503 and "выключен" in r.json()["error"]
    # с ключом — на формах появляется кнопка, маршрут отдаёт текст
    settings.gemini_api_key = "test-key"
    try:
        assert 'class="mic' in client.get("/").text
        monkeypatch.setattr(
            asr,
            "transcribe",
            lambda *a, **k: {"text": "построить дом", "model": "m", "error": None},
        )
        r = client.post("/asr", files={"audio": ("v.wav", io.BytesIO(b"RIFF"), "audio/wav")})
        assert r.status_code == 200 and r.json() == {"text": "построить дом", "model": "m"}
        monkeypatch.setattr(
            asr,
            "transcribe",
            lambda *a, **k: {"text": "", "model": "m", "error": None, "silent": True},
        )
        r = client.post("/asr", files={"audio": ("v.wav", io.BytesIO(b"RIFF"), "audio/wav")})
        assert r.status_code == 200 and r.json() == {"text": "", "model": "m", "silent": True}
        monkeypatch.setattr(
            asr, "transcribe", lambda *a, **k: {"text": "", "model": "m", "error": "Gemini 500"}
        )
        r = client.post("/asr", files={"audio": ("v.wav", io.BytesIO(b"RIFF"), "audio/wav")})
        assert r.status_code == 502 and r.json()["error"] == "Gemini 500"
    finally:
        settings.gemini_api_key = None
    # без входа — редирект на логин
    client.cookies.clear()
    assert (
        client.post(
            "/asr", files={"audio": ("v.wav", io.BytesIO(b"RIFF"), "audio/wav")}
        ).status_code
        == 307
    )


def test_groq_provider_first_then_gemini(monkeypatch):
    import numpy as np

    speech = _wav(np.random.default_rng(2).normal(0, 0.05, 16000 * 2).clip(-1, 1))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls: list[str] = []

    def fake_post(url, *a, **k):
        calls.append(url)
        if "groq" in url:
            assert k["data"]["model"] == "whisper-large-v3-turbo" and k["data"]["language"] == "ru"
            return _Resp(200, {"text": "  Построить дом до 3 млн. "})
        raise AssertionError("gemini не должен вызываться")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert asr.providers_available() == ["groq"]
    r = transcribe(speech, "audio/wav", api_key="gem")
    assert r["text"] == "Построить дом до 3 млн." and r["model"] == "groq/whisper-large-v3-turbo"
    # Groq упал — запасной Gemini по переданному ключу
    seq = iter([_Resp(503, {}), _ok("дача у реки")])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: next(seq))
    r = transcribe(speech, "audio/wav", api_key="gem", model="m")
    assert r["text"] == "дача у реки" and r["model"] == "m"
    # без ключей вообще — честная ошибка без сетевых вызовов
    monkeypatch.delenv("GROQ_API_KEY")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    r = transcribe(speech, "audio/wav")
    assert "нет провайдера" in r["error"]


def test_elevenlabs_provider(monkeypatch):
    import numpy as np

    speech = _wav(np.random.default_rng(3).normal(0, 0.05, 16000 * 2).clip(-1, 1))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el")

    def fake_post(url, *a, **k):
        assert "elevenlabs" in url and k["headers"]["xi-api-key"] == "el"
        assert k["data"]["model_id"] == "scribe_v2" and k["data"]["language_code"] == "rus"
        return _Resp(200, {"text": "Участок 10 соток под дом.", "language_code": "rus"})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert asr.providers_available() == ["elevenlabs"]
    r = transcribe(speech, "audio/wav")
    assert r["text"] == "Участок 10 соток под дом." and r["model"] == "elevenlabs/scribe_v2"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(401, {}))
    assert "ElevenLabs HTTP 401" in transcribe(speech, "audio/wav")["error"]
