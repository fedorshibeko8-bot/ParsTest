"""Хендлер кнопки «Взять инициативу» (задача T07).

После перехвата пост сразу показывается в карточке. Если у поста ещё нет
черновика, хендлер вызывает рерайт через ИИ (T09): админ видит новость
уже переведённой на русский, а не английский оригинал. В ``DRY_RUN``
сети нет — карточка показывает оригинал, как и раньше.

Ошибка перевода не отменяет перехват: пост остаётся у админа, карточка
показывает оригинал, а в личку приходит пояснение.
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

TRANSLATING_TEXT = "⏳ Перевожу новость на русский…"


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

    # Черновик уже есть (например, пост возвращали в очередь) или включён
    # DRY_RUN — переводить нечего/нечем: показываем карточку как есть.
    if settings.dry_run or post.draft_title.strip():
        await callback.answer("Пост ваш.")
        await refresh_card(bot, message, post)
        return

    await callback.answer("Пост ваш. Перевожу на русский…")
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
    «Перевожу…», чтобы админ видел, что бот работает. Если ИИ недоступен,
    перехват сохраняется, показывается оригинал и приходит пояснение.
    """
    try:
        await message.edit_text(TRANSLATING_TEXT)
    except TelegramBadRequest as exc:
        # Сообщение могло устареть — не мешает переводу и карточке.
        logger.debug("Не удалось показать «Перевожу…»: %s", exc)

    try:
        outcome = await rewrite_post(db, settings, post, actor_id=actor_id)
    except RewriteError as exc:
        logger.warning("Перевод поста #%s не удался: %s", post.id, exc)
        await refresh_card(bot, message, post)
        try:
            await message.answer(
                f"Пост #{post.id} ваш, но перевести не удалось: {exc}\n"
                "Показываю оригинал. Можно нажать «Доработать с ИИ» ещё раз "
                "или поправить текст вручную."
            )
        except Exception:
            logger.debug("Не удалось сообщить об ошибке перевода #%s", post.id)
        return

    await refresh_card(bot, message, outcome.post)


__all__ = ["TRANSLATING_TEXT", "handle_take", "router"]
