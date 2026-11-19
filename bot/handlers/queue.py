"""Хендлер кнопки «Взять инициативу» (задача T07).

После перехвата пост сразу показывается в карточке. Если у поста ещё нет
черновика, хендлер вызывает рерайт через ИИ (T09): админ видит новость
уже переведённой на русский, а не английский оригинал. ``DRY_RUN``
запрещает только отправку в канал, поэтому не отменяет перевод — в нём
используется предсказуемый русский текст-заглушка.

Ошибка перевода не отменяет перехват: пост остаётся у админа, но оригинал
показывается как неготовый текст с явным пояснением в личку — публиковать
его нельзя, пока перевод не выполнен.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.db import Database
from bot.models import Post
from bot.services.cards import refresh_card
from bot.services.queue import (
    TAKE_CALLBACK_PREFIX,
    parse_take_callback,
    take_post,
)
from bot.services.rewrite import RewriteError, rewrite_post

logger = logging.getLogger(__name__)

router = Router(name="queue")

TRANSLATING_TEXT = "⏳ Перевожу новость на русский язык."


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

    message = callback.message if isinstance(callback.message, Message) else None
    post = outcome.post
    if message is None or post is None:
        await callback.answer("Пост ваш.")
        return

    # Черновик уже есть (например, пост возвращали в очередь) — переводить
    # нечего: показываем карточку как есть. DRY_RUN перевод не отменяет.
    if post.draft_title.strip():
        await callback.answer("Пост ваш.")
        await refresh_card(bot, message, post)
        return

    await callback.answer("Пост ваш. Перевожу на русский язык.")
    await _translate_and_show(
        bot, db, settings, message=message, post=post, actor_id=user.id
    )


async def _translate_and_show(
    bot: Bot,
    db: Database,
    settings: Settings,
    *,
    message: Message,
    post: Post,
    actor_id: int,
) -> None:
    """Перевести новость через ИИ и показать карточку с черновиком.

    Пока идёт запрос к модели, уведомление превращается в строку
    «Перевожу на русский язык», чтобы админ видел, что бот работает.
    Если ИИ недоступен, перехват сохраняется, но оригинал помечается как
    неготовый: в личку приходит явная причина и способ повторить перевод.
    """
    try:
        await message.edit_text(TRANSLATING_TEXT)
    except TelegramBadRequest as exc:
        # Сообщение могло устареть — не мешает переводу и карточке.
        logger.debug("Не удалось показать статус перевода: %s", exc)

    try:
        outcome = await rewrite_post(db, settings, post, actor_id=actor_id)
    except RewriteError as exc:
        logger.warning("Перевод поста #%s не удался: %s", post.id, exc)
        await refresh_card(bot, message, post)
        try:
            await message.answer(
                f"Пост #{post.id} ваш, но перевод на русский не выполнен: {exc}\n"
                "Карточка ниже показывает оригинал — его нельзя публиковать. "
                "Нажмите «Доработать с ИИ», чтобы повторить попытку, или "
                "внесите текст вручную. Если причина — регион, включите "
                "зарубежный IP или задайте GROQ_API_KEY."
            )
        except Exception:
            logger.debug("Не удалось сообщить об ошибке перевода #%s", post.id)
        return

    await refresh_card(bot, message, outcome.post)


__all__ = ["TRANSLATING_TEXT", "handle_take", "router"]
