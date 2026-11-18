"""Общие фикстуры тестов.

Тесты работают только с временными файлами ``pytest`` (``%TEMP%``),
реальная ``data/bot.db`` и ``.env`` не читаются и не изменяются.
Асинхронные сценарии запускаются через ``asyncio.run`` в синхронных тестах —
так не нужен ``pytest-asyncio`` и меньше зависимостей.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from bot.db import Database
from bot.models import Post
from tests.helpers import T0, run


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """Пустая БД во временном каталоге с коротким лимитом очереди."""
    database = Database(
        tmp_path / "test.db",
        queue_visible_limit=3,
        post_ttl_hours=48,
        intercept_timeout_minutes=10,
        dedup_history_days=7,
    )
    run(database.initialize())
    return database


@pytest.fixture()
def make_post(db: Database) -> Callable[..., Post]:
    """Фабрика постов с уникальными URL/GUID; любые поля можно переопределить."""
    counter = {"n": 0}

    def factory(**overrides: Any) -> Post:
        counter["n"] += 1
        number = counter["n"]
        params: dict[str, Any] = {
            "url": f"https://example.com/news/{number}",
            "guid": f"guid-{number}",
            "source": "VGC",
            "title_original": f"Новость номер {number}",
            "lead": "Короткий лид новости.",
        }
        params.update(overrides)
        return run(db.create_post(**params))

    return factory


@pytest.fixture()
def at() -> Callable[[timedelta], datetime]:
    """Метка времени относительно T0 — удобно проверять TTL и тайм-ауты."""

    def factory(offset: timedelta = timedelta()) -> datetime:
        return T0 + offset

    return factory
