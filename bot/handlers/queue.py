"""Хендлер кнопки «Взять инициативу» (задача T07).

Карточка и меню владельца — следующая задача (T08). Здесь только захват
поста: проверка прав, атомарный переход в ``intercepted``, сообщение
остальным и понятный ответ нажавшему.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.db import Database
from bot.services.cards import refresh_card
from bot.services.queue import (
    TAKE_CALLBACK_PREFIX,
    parse_take_callback,
    take_post,
)

logger = logging.getLogger(__name__)

router = Router(name="queue")


@router.callback_query(F.data.startswith(TAKE_CALLBACK_PREFIX))
async def handle_take(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    settings: Settings,
) -> None:
    """Первый нажавший становится владельцем поста."""
    post_id = parse_take_callback(callback.data)
    if post_id is None:
        await callback.answer("Кнопка устарела, попробуйте новую.", show_alert=True)
        return

    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    outcome = await take_post(
        bot, db, settings, post_id=post_id, user=user
    )

    if not outcome.ok:
        await callback.answer(outcome.message, show_alert=True)
        return

    await callback.answer("Пост ваш.")
    if isinstance(callback.message, Message) and outcome.post is not None:
        # Превращаем уведомление в карточку: текст — редактируем, фото — новое.
        await refresh_card(bot, callback.message, outcome.post)


__all__ = ["handle_take", "router"]
