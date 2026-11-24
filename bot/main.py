"""Точка входа бота: aiogram 3.x, long polling.

Запуск: ``python -m bot.main`` (или через ``run.bat`` из задачи T11).

Собирает всё вместе: диспетчер, логирование, БД, опрос RSS (T03–T06),
очередь с перехватом (T07), карточку владельца (T08), рерайт (T09) и
публикацию (T10), фоновое обслуживание TTL и тайм-аутов. Без ``BOT_TOKEN``
бот не стартует, но завершается понятным сообщением и ненулевым кодом
возврата, без трейсбека.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from bot.config import ConfigError, Settings, load_settings
from bot.db import Database
from bot.handlers.cards import router as cards_router
from bot.handlers.queue import router as queue_router
from bot.services.backup import daily_backup_loop
from bot.services.pipeline import poll_news_loop
from bot.services.queue import maintenance_loop
from bot.utils.logging import setup_logging

logger = logging.getLogger(__name__)

# Коды возврата процесса (пригодятся run.bat и Планировщику задач Windows).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3


def _configure_console() -> None:
    """Не падать, если символ вне кодировки консоли (CP866 на Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def build_dispatcher(settings: Settings, db: Database) -> Dispatcher:
    """Собрать диспетчер: служебные команды, перехват и карточку поста.

    ``db`` и ``settings`` кладутся в workflow data — aiogram подставит их
    в хендлеры по именам аргументов.
    """
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["settings"] = settings
    dispatcher["db"] = db
    dispatcher.include_router(queue_router)
    dispatcher.include_router(cards_router)

    async def deny(message: Message) -> None:
        user = message.from_user
        logger.warning("Отказ в доступе: user_id=%s", user.id if user else None)
        await message.answer("Доступ только для администраторов канала.")

    @dispatcher.message(CommandStart())
    async def handle_start(message: Message) -> None:
        if not settings.is_admin(message.from_user.id if message.from_user else None):
            await deny(message)
            return
        mode = "DRY_RUN (заглушки, публикации нет)" if settings.dry_run else "боевой"
        await message.answer(
            "Бот на связи.\n"
            f"Режим: {mode}.\n"
            f"Администраторов в конфиге: {len(settings.admin_ids)}.\n"
            "Источники опрашиваются автоматически, уведомления о новостях "
            "и кнопка «Взять инициативу» работают."
        )

    @dispatcher.message(Command("ping"))
    async def handle_ping(message: Message) -> None:
        if not settings.is_admin(message.from_user.id if message.from_user else None):
            await deny(message)
            return
        await message.answer("pong")

    return dispatcher


async def run_polling(settings: Settings) -> None:
    """Подключиться к Telegram и слушать обновления (long polling)."""
    db = Database.from_settings(settings)
    await db.initialize()

    bot = Bot(token=settings.bot_token)
    dispatcher = build_dispatcher(settings, db)
    stop_event = asyncio.Event()
    maintenance = asyncio.create_task(
        maintenance_loop(bot, db, settings, stop_event=stop_event)
    )

    polling = asyncio.create_task(
        poll_news_loop(bot, db, settings, stop_event=stop_event)
    )
    backups = asyncio.create_task(daily_backup_loop(settings, stop_event=stop_event))
    tasks = (polling, maintenance, backups)

    logger.info("Запуск long polling, администраторов: %d", len(settings.admin_ids))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            handle_signals=True,
        )
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await db.close()
        await bot.session.close()
        logger.info("Соединение с Telegram закрыто.")


def main() -> int:
    """Подготовить окружение и запустить бота. Возвращает код выхода."""
    _configure_console()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print("Не удалось запустить бота: ошибка в настройках.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print(
            "Образец настроек - .env.example. Скопируйте его в .env и заполните.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    setup_logging(level=settings.log_level, logs_dir=settings.logs_dir)
    for warning in settings.warnings:
        logger.warning(warning)

    try:
        settings.ensure_directories()
    except OSError:
        logger.exception("Не удалось создать рабочие каталоги (data/, logs/)")
        return EXIT_ERROR

    if settings.dry_run:
        logger.warning(
            "DRY_RUN=1: реальная публикация в канал и запросы к ИИ отключены."
        )

    try:
        asyncio.run(run_polling(settings))
    except KeyboardInterrupt:
        logger.info("Получен Ctrl+C, бот остановлен.")
    except TelegramUnauthorizedError:
        logger.error(
            "Telegram отклонил BOT_TOKEN. Проверьте токен в .env (его выдаёт @BotFather)."
        )
        return EXIT_AUTH
    except Exception:
        logger.exception("Критическая ошибка в работе бота")
        return EXIT_ERROR

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
