"""Настройка логирования: консоль + файл с ротацией по дням.

Файл логов — ``logs/bot.log`` в UTF-8, ротация в полночь, хранится 30 дней.
Если файл открыть не удалось (нет прав, занят другим процессом), бот
продолжает работу с логированием только в консоль.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "bot.log"
BACKUP_DAYS = 30
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def _build_console_handler() -> logging.Handler:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_build_formatter())
    return handler


def _build_file_handler(logs_dir: Path) -> logging.Handler:
    """Суточная ротация: файл bot.log + архивы bot.log.ГГГГ-ММ-ДД (30 дней)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        filename=logs_dir / LOG_FILE_NAME,
        when="midnight",
        interval=1,
        backupCount=BACKUP_DAYS,
        encoding="utf-8",
        delay=True,
        utc=False,
    )
    handler.setFormatter(_build_formatter())
    return handler


def setup_logging(level: str = "INFO", logs_dir: str | Path = "logs") -> None:
    """Настроить корневой логгер (вызывается один раз при старте).

    :param level: уровень логирования (``DEBUG``, ``INFO``, ``WARNING``, ...).
    :param logs_dir: каталог для ``bot.log``; создаётся при необходимости.
    """
    numeric_level = logging.getLevelName(str(level).upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(numeric_level)

    root.addHandler(_build_console_handler())
    try:
        root.addHandler(_build_file_handler(Path(logs_dir)))
    except OSError as exc:  # логгер ещё не готов — пишем напрямую в консоль
        print(
            f"Предупреждение: не удалось открыть файл логов в {logs_dir}: {exc}. "
            "Логи только в консоль.",
            file=sys.stderr,
        )

    logging.captureWarnings(True)
    # aiogram пишет каждый апдейт на INFO — оставляем только предупреждения.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
