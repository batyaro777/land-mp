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
    # Спрос (R11): контакты покупателей продавцам и брокерам — платно (1) или всем (0, пилот)
    demand_paid: bool = field(
        default_factory=lambda: os.environ.get("LANDCHECK_DEMAND_PAID", "") == "1"
    )
    # Слежение за участками (R9): период перепроверки в часах, 0 — выключено
    watch_hours: float = field(
        default_factory=lambda: float(os.environ.get("LANDCHECK_WATCH_HOURS", "24"))
    )
    # Слежение за документами администрации по участку (ТЗ v3, п. 2.5, этап 3): период
    # перечитки в часах (по умолчанию неделя), 0 — выключено
    watch_docs_hours: float = field(
        default_factory=lambda: float(os.environ.get("LANDCHECK_WATCH_DOCS_HOURS", "168"))
    )
    debug: bool = field(default_factory=lambda: os.environ.get("LANDCHECK_DEBUG", "") == "1")
    # Автоподтверждение права при совпадении выписки (зум 09.11); LANDCHECK_AUTO_VERIFY=0 — выкл.
    auto_verify: bool = field(
        default_factory=lambda: os.environ.get("LANDCHECK_AUTO_VERIFY", "1") != "0"
    )
    # RU-шлюз (ТЗ v3, п. 9): SOCKS5 на российский сервер для torgi.gov.ru и набор
    # сертификатов с корнем Минцифры. Пусто — слой торгов честно пишет «не настроен».
    ru_proxy: str | None = field(
        default_factory=lambda: os.environ.get("LANDCHECK_RU_PROXY") or None
    )
    ru_ca_bundle: str | None = field(
        default_factory=lambda: os.environ.get("LANDCHECK_RU_CA_BUNDLE") or None
    )
    torgi_ttl_days: float = field(
        default_factory=lambda: float(os.environ.get("LANDCHECK_TORGI_TTL_DAYS", "1"))
    )
    # Анализатор торгов: глубина истории в месяцах, сколько карточек завершённых лотов и
    # участков НСПД читать за один проход (torgi пропускает запрос раз в 6 с), период
    # автосбора в часах (0 — только по кнопке).
    torgi_scan_months: int = field(
        default_factory=lambda: int(os.environ.get("LANDCHECK_TORGI_SCAN_MONTHS", "12"))
    )
    torgi_scan_cards: int = field(
        default_factory=lambda: int(os.environ.get("LANDCHECK_TORGI_SCAN_CARDS", "300"))
    )
    torgi_scan_nspd: int = field(
        default_factory=lambda: int(os.environ.get("LANDCHECK_TORGI_SCAN_NSPD", "300"))
    )
    torgi_scan_hours: float = field(
        default_factory=lambda: float(os.environ.get("LANDCHECK_TORGI_SCAN_HOURS", "24"))
    )
    # Документы администрации (ТЗ v3, п. 2.5): кэш автопоиска по муниципалитету
    admin_docs_ttl_days: float = field(
        default_factory=lambda: float(os.environ.get("LANDCHECK_ADMIN_DOCS_TTL_DAYS", "7"))
    )
    # Модуль «Регион»: Gemini с поиском Google. Без ключа блок честно говорит, что выключен.
    gemini_api_key: str | None = field(
        default_factory=lambda: (
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None
        )
    )
    gemini_model: str = field(
        default_factory=lambda: os.environ.get("LANDCHECK_GEMINI_MODEL") or "gemini-3.8-flash"
    )
    # PEM с корневыми сертификатами Минцифры (ГУЦ) для цепочки подписи выписок ЕГРН; без файла
    # проверяется только сама подпись (статус chain_unverified).
    gost_roots: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["LANDCHECK_GOST_ROOTS"])
            if os.environ.get("LANDCHECK_GOST_ROOTS")
            else None
        )
    )
    # Доставка уведомлений: smtp://user:pass@host:587?from=land@example.com (STARTTLS),
    # smtps://…:465 — TLS сразу; Telegram — токен бота, chat_id пользователь вводит в профиле.
    smtp_url: str | None = field(
        default_factory=lambda: os.environ.get("LANDCHECK_SMTP_URL") or None
    )
    telegram_bot_token: str | None = field(
        default_factory=lambda: os.environ.get("LANDCHECK_TG_BOT_TOKEN") or None
    )

    @property
    def llm_enabled(self) -> bool:
        """Есть хотя бы один бесплатный провайдер (Groq, OpenRouter, Mistral) или ключ Gemini."""
        from landcheck import llm

        return bool(llm.providers_from_env()) or bool(self.gemini_api_key)

    @property
    def region_enabled(self) -> bool:
        from landcheck import llm
        from landcheck.region import region_mode

        if region_mode() == "gemini":
            return bool(self.gemini_api_key)
        return bool(llm.providers_from_env())

    @property
    def asr_server_enabled(self) -> bool:
        from landcheck.asr import providers_available

        return bool(providers_available()) or bool(self.gemini_api_key)

    def __post_init__(self) -> None:
        if not self.secret:
            # Без секрета сессии не переживут перезапуск, но для локального запуска это допустимо.
            import secrets

            self.secret = secrets.token_hex(32)


settings = Settings()
