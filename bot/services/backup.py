"""Суточный бэкап БД: обёртка для планировщика.

Сам снимок делает :func:`bot.db.backup_database` (``VACUUM INTO``).
Здесь — политика: каталог ``data/backups``, хранение 7 снимков, пауза 24 ч
и гарантия, что ошибка бэкапа не роняет бота.

Подключение (T07/T11), например в ``bot.main``::

    asyncio.create_task(daily_backup_loop(settings))

Или разовый вызов ``await run_daily_backup(settings)`` из планировщика.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bot.config import Settings
from bot.db import backup_database_async

logger = logging.getLogger(__name__)

BACKUP_INTERVAL_HOURS = 24
BACKUP_KEEP = 7
BACKUP_PREFIX = "bot"


def backup_dir_for(settings: Settings) -> Path:
    """Каталог снимков, заданный через ``BACKUP_DIR``."""
    return settings.backup_dir


async def run_daily_backup(
    settings: Settings, *, keep: int = BACKUP_KEEP
) -> Path | None:
    """Сделать один снимок БД. Ошибки логируются, исключения не пробрасываются.

    :returns: путь к снимку или ``None``, если бэкап не удался.
    """
    try:
        path = await backup_database_async(
            settings.database_path,
            backup_dir_for(settings),
            prefix=BACKUP_PREFIX,
            keep=keep,
        )
    except Exception:
        logger.exception("Не удалось сделать бэкап БД %s", settings.database_path)
        return None
    return path


async def daily_backup_loop(
    settings: Settings,
    *,
    interval_hours: int = BACKUP_INTERVAL_HOURS,
    keep: int = BACKUP_KEEP,
    stop_event: asyncio.Event | None = None,
    run_immediately: bool = True,
) -> None:
    """Фоновая задача: снимок раз в ``interval_hours`` до остановки.

    :param stop_event: если задан, ожидание прерывается его установкой.
    :param run_immediately: делать ли снимок сразу при старте (иначе через интервал).
    """
    interval_sec = max(1, int(interval_hours)) * 3600
    if not run_immediately:
        await _sleep_or_stop(interval_sec, stop_event)

    while True:
        await run_daily_backup(settings, keep=keep)
        stopped = await _sleep_or_stop(interval_sec, stop_event)
        if stopped:
            logger.info("Цикл бэкапов остановлен.")
            return


async def _sleep_or_stop(timeout_sec: int, stop_event: asyncio.Event | None) -> bool:
    """Пауза; ``True`` — если пришла остановка."""
    if stop_event is None:
        await asyncio.sleep(timeout_sec)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return False
    return True


__all__ = [
    "BACKUP_INTERVAL_HOURS",
    "BACKUP_KEEP",
    "backup_dir_for",
    "daily_backup_loop",
    "run_daily_backup",
]
