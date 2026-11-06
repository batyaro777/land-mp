"""RU-шлюз: HTTP-клиент для российских госсайтов, закрытых для зарубежных IP (ТЗ v3, п. 9).

torgi.gov.ru (и при блокировке — nspd.gov.ru) с сервера в Нидерландах не отвечают: гео-блок.
Запросы к ним идут через SOCKS5-туннель на RU-сервер (`LANDCHECK_RU_PROXY`), а TLS
проверяется по набору сертификатов с корнем Минцифры (`LANDCHECK_RU_CA_BUNDLE`):
госсайты подписаны «Russian Trusted CA», которого нет в системных корнях. Проверку
сертификатов не отключаем. Без переменных окружения модуль честно говорит «не настроен».
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path

import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) LandMP/1.0 (+https://land-mp.dnai.engineering)"
DEFAULT_TIMEOUT = 25.0


def proxy_url() -> str | None:
    """`socks5h://host:port` из окружения; пусто — модуль выключен."""
    return os.environ.get("LANDCHECK_RU_PROXY") or None


def ca_bundle() -> str | None:
    """Путь к PEM-набору с корнем Минцифры, если файл есть."""
    path = os.environ.get("LANDCHECK_RU_CA_BUNDLE") or ""
    return path if path and Path(path).is_file() else None


def enabled() -> bool:
    return proxy_url() is not None


def status() -> dict[str, str | bool | None]:
    """Для диагностики в отчёте: настроен ли шлюз и есть ли бандл сертификатов."""
    return {"enabled": enabled(), "proxy": proxy_url(), "ca_bundle": ca_bundle()}


def verify_context() -> ssl.SSLContext | bool:
    bundle = ca_bundle()
    if bundle is None:
        return True
    return ssl.create_default_context(cafile=bundle)


def client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """httpx-клиент через RU-шлюз (или напрямую, если шлюз не задан — для тестов и dev)."""
    return httpx.Client(
        proxy=proxy_url(),
        verify=verify_context(),
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*"},
    )
