"""Точка входа бота: aiogram 3.x, long polling.

Запуск: ``python -m bot.main`` (или через ``run.bat`` из задачи T11).

Пока это каркас: диспетчер, логирование, чтение конфига и две служебные
команды для админов (/start и /ping). Парсеры, очередь и карточки постов
добавят следующие задачи. Без ``BOT_TOKEN`` бот не стартует, но завершается
понятным сообщением и ненулевым кодом возврата, без трейсбека.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.config import ConfigError, Settings, load_settings
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


def build_dispatcher(settings: Settings) -> Dispatcher:
    """Собрать диспетчер с минимальными служебными командами.

    Полноценные хендлеры появятся в ``bot/handlers/`` (T07-T08).
    """
    dispatcher = Dispatcher()
    dispatcher["settings"] = settings

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
            "Это каркас: новости и карточки появятся в следующих версиях."
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
    bot = Bot(token=settings.bot_token)
    dispatcher = build_dispatcher(settings)
    logger.info("Запуск long polling, администраторов: %d", len(settings.admin_ids))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types()
        )
    finally:
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
