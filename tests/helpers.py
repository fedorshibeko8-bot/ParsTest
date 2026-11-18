"""Помощники тестов: запуск корутин и общая точка отсчёта времени."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, TypeVar

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

T = TypeVar("T")


def run(coro: Awaitable[T]) -> T:
    """Выполнить корутину до конца в отдельном event loop."""
    return asyncio.run(coro)


def offset(**kwargs: float) -> timedelta:
    """``offset(hours=49)`` → ``timedelta`` для проверок TTL/тайм-аутов."""
    return timedelta(**kwargs)
