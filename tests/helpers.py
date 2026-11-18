"""Помощники тестов: запуск корутин, общая точка отсчёта времени, настройки."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, TypeVar

from bot.config import Settings

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

T = TypeVar("T")


def run(coro: Awaitable[T]) -> T:
    """Выполнить корутину до конца в отдельном event loop."""
    return asyncio.run(coro)


def offset(**kwargs: float) -> timedelta:
    """``offset(hours=49)`` → ``timedelta`` для проверок TTL/тайм-аутов."""
    return timedelta(**kwargs)


def make_settings(**overrides: Any) -> Settings:
    """Собрать ``Settings`` для тестов, не читая ``.env``.

    Все обязательные поля заполнены безопасными значениями; нужное
    переопределяется именованными аргументами (``poll_interval_min_sec=0``).
    """
    params: dict[str, Any] = {
        # Telegram
        "bot_token": "test-token",
        "channel_id": "",
        "admin_ids": (1,),
        "dry_run": True,
        # ИИ
        "gemini_api_key": "",
        "gemini_model": "gemini-test",
        "groq_api_key": "",
        "groq_model": "groq-test",
        # Логи и файлы
        "log_level": "INFO",
        "logs_dir": Path("logs"),
        "database_path": Path("data/bot.db"),
        "images_dir": Path("data/images"),
        # Опрос RSS
        "poll_interval_min_sec": 300,
        "poll_interval_max_sec": 600,
        "http_timeout_sec": 20,
        # Жизненный цикл поста
        "post_ttl_hours": 48,
        "intercept_timeout_minutes": 10,
        "queue_visible_limit": 10,
        # Дедупликация
        "dedup_duplicate_threshold": 0.85,
        "dedup_gray_zone_threshold": 0.70,
    }
    params.update(overrides)
    return Settings(**params)
