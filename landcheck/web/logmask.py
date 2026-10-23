"""Секрет вебхука Telegram не должен попадать в журнал запросов (docker logs): uvicorn пишет
путь целиком — маскируем часть после /tg/webhook/."""

from __future__ import annotations

import logging
import re

_WEBHOOK_RE = re.compile(r"/tg/webhook/[^\s\"?#]+")


class MaskWebhookSecret(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _WEBHOOK_RE.sub("/tg/webhook/***", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str) and "/tg/webhook/" in record.msg:
            record.msg = _WEBHOOK_RE.sub("/tg/webhook/***", record.msg)
        return True


def install() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "landcheck.web"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, MaskWebhookSecret) for f in logger.filters):
            logger.addFilter(MaskWebhookSecret())
