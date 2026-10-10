"""Настройки веб-приложения из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass
class Settings:
    db_path: Path = field(
        default_factory=lambda: _env_path("LANDCHECK_DB", "data/landcheck.sqlite")
    )
    cache_path: Path = field(
        default_factory=lambda: _env_path("LANDCHECK_CACHE", "data/nspd-cache.sqlite")
    )
    secret: str = field(default_factory=lambda: os.environ.get("LANDCHECK_SECRET", ""))
    proxy: str | None = field(default_factory=lambda: os.environ.get("LANDCHECK_PROXY") or None)
    fixtures: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["LANDCHECK_FIXTURES"]).expanduser()
            if os.environ.get("LANDCHECK_FIXTURES")
            else None
        )
    )
    workers: int = field(default_factory=lambda: int(os.environ.get("LANDCHECK_WORKERS", "2")))
    nspd_timeout: int = field(
        default_factory=lambda: int(os.environ.get("LANDCHECK_TIMEOUT", "40"))
    )
    cache_ttl_days: float = field(
        default_factory=lambda: float(os.environ.get("LANDCHECK_TTL_DAYS", "7"))
    )
    max_items: int = field(
        default_factory=lambda: int(os.environ.get("LANDCHECK_MAX_ITEMS", "300"))
    )
    brand: str = field(
        default_factory=lambda: os.environ.get("LANDCHECK_BRAND", "Проверка участков")
    )
    base_url: str = field(default_factory=lambda: os.environ.get("LANDCHECK_BASE_URL", ""))
    debug: bool = field(default_factory=lambda: os.environ.get("LANDCHECK_DEBUG", "") == "1")

    def __post_init__(self) -> None:
        if not self.secret:
            # Без секрета сессии не переживут перезапуск, но для локального запуска это допустимо.
            import secrets

            self.secret = secrets.token_hex(32)


settings = Settings()
