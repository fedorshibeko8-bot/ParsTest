"""Карточка поста и меню владельца (задача T08).

Один обработчик ``post:<action>:<id>`` обслуживает всё меню: доступ
проверяется по владельцу в БД, затем действие сбрасывает 10-минутный
тайм-аут (``register_owner_action``) и возвращает обновлённый предпросмотр.

Рерайт (кнопка «Доработать с ИИ») выполняет ``bot.services.rewrite``:
провайдеры, промпт и лимиты длины живут там, а хендлер только обновляет
карточку и сообщает админу о замечаниях. Публикация (кнопка «Опубликовать»)
вызывает ``bot.services.publisher`` — единственный модуль, который знает
``CHANNEL_ID``. Служебные сообщения уходят только в личку админам.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.db import Database
from bot.models import DraftOrigin, Post
from bot.services.cards import (
    ACTION_AI,
    ACTION_BACK,
    ACTION_DROP,
    ACTION_EDIT,
    ACTION_EDIT_IMAGE,
    ACTION_EDIT_TEXT,
    ACTION_PUBLISH,
    ACTION_REMOVE_IMAGE,
    ACTION_SOURCE,
    POST_CALLBACK_PREFIX,
    parse_post_callback,
    send_card,
    show_card_for_callback,
)
from bot.services.images import ImageError, remove_image_file, save_image_bytes
from bot.services.publisher import PublishError, publish_post
from bot.services.queue import admin_display_name, register_owner_action
from bot.services.rewrite import RewriteError, rewrite_post

logger = logging.getLogger(__name__)

router = Router(name="cards")


class EditStates(StatesGroup):
    """Ожидание текста или фото от владельца поста."""

    text = State()
    image = State()


@router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext) -> None:
    """Выйти из режима правки, если он был активен."""
    current = await state.get_state()
    await state.clear()
    if current is not None:
        await message.answer("Правка отменена.")


# ----------------------------------------------------------------------
# Общие проверки
# ----------------------------------------------------------------------
async def _owned_post(
    db: Database, *, post_id: int, user_id: int
) -> Post | None:
    """Пост, если он перехвачен именно этим админом."""
    post = await db.get_post(post_id)
    if post is None or not post.is_intercepted or post.owner_id != user_id:
        return None
    return post


async def _require_owned(
    callback: CallbackQuery, db: Database
) -> tuple[str, Post] | None:
    """Разобрать callback и проверить владельца; иначе ответить и вернуть ``None``."""
    parsed = parse_post_callback(callback.data)
    if parsed is None:
        await callback.answer("Кнопка устарела, откройте карточку заново.", show_alert=True)
        return None
    action, post_id = parsed

    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return None

    post = await _owned_post(db, post_id=post_id, user_id=user.id)
    if post is None:
        await callback.answer(
            "Пост уже не ваш: тайм-аут истёк или его завершил другой админ.",
            show_alert=True,
        )
        return None
    return action, post


# ----------------------------------------------------------------------
# Действия меню
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith(POST_CALLBACK_PREFIX))
async def handle_post_action(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Диспетчер кнопок меню владельца."""
    required = await _require_owned(callback, db)
    if required is None:
        return
    action, post = required

    # Любое действие владельца продлевает перехват (п. 7 ТЗ).
    await register_owner_action(db, post.id, actor_id=callback.from_user.id)

    if action == ACTION_PUBLISH:
        await _publish_post(callback, bot, db, settings, post)
        return

    if action == ACTION_AI:
        await _rewrite_post(callback, bot, db, settings, post)
        return

    if action == ACTION_EDIT:
        await callback.answer()
        await show_card_for_callback(callback, bot, post, edit_menu=True)
        return

    if action == ACTION_BACK:
        await callback.answer()
        await show_card_for_callback(callback, bot, post, edit_menu=False)
        return

    if action == ACTION_SOURCE:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"Источник: {post.url}")
        return

    if action == ACTION_DROP:
        await _drop_post(callback, db, post)
        return

    if action == ACTION_EDIT_TEXT:
        await _ask_for_text(callback, state, post)
        return

    if action == ACTION_EDIT_IMAGE:
        await _ask_for_image(callback, state, post, settings)
        return

    if action == ACTION_REMOVE_IMAGE:
        await _remove_image(callback, bot, db, settings, post)
        return

    logger.warning("Неизвестное действие карточки %r для поста #%s", action, post.id)
    await callback.answer("Неизвестное действие.", show_alert=True)


async def _rewrite_post(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    settings: Settings,
    post: Post,
) -> None:
    """Переписать пост через ИИ и показать обновлённый черновик."""
    user = callback.from_user
    if user is None:  # pragma: no cover - проверено в _require_owned
        return

    await callback.answer("Переписываю текст.")
    try:
        outcome = await rewrite_post(db, settings, post, actor_id=user.id)
    except RewriteError as exc:
        logger.warning("Рерайт поста #%s не удался: %s", post.id, exc)
        if isinstance(callback.message, Message):
            await callback.message.answer(f"Не удалось переписать: {exc}")
        return

    await show_card_for_callback(callback, bot, outcome.post)
    if not isinstance(callback.message, Message):
        return
    if outcome.result.warnings:
        await callback.message.answer(
            "Замечания к тексту: " + "; ".join(outcome.result.warnings)
        )
    if outcome.hint:
        await callback.message.answer(outcome.hint)


async def _publish_post(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    settings: Settings,
    post: Post,
) -> None:
    """Опубликовать пост в канал и показать итог владельцу."""
    user = callback.from_user
    if user is None:  # pragma: no cover - проверено в _require_owned
        return

    await callback.answer("Публикую в канал.")
    try:
        publication = await publish_post(
            db, settings, post, actor_id=user.id, bot=bot
        )
    except PublishError as exc:
        logger.warning("Публикация поста #%s не удалась: %s", post.id, exc)
        if isinstance(callback.message, Message):
            await callback.message.answer(f"Не удалось опубликовать: {exc}")
        return

    message = callback.message if isinstance(callback.message, Message) else None
    if publication.dry_run:
        logger.info("Пост #%s: публикация сымитирована (DRY_RUN)", publication.post.id)
        if message is not None:
            await message.answer(
                f"DRY_RUN: пост #{publication.post.id} помечен опубликованным, "
                "в канал ничего не отправлено. Проверьте текст карточки."
            )

    # Карточка больше не нужна: меню убираем, чтобы пост нельзя было
    # опубликовать второй раз.
    if message is None:
        return
    note = (
        f"✅ Пост #{publication.post.id} опубликован."
        if not publication.dry_run
        else f"✅ Пост #{publication.post.id} опубликован (DRY_RUN, без отправки)."
    )
    try:
        if message.photo:
            await message.edit_caption(caption=note)
        else:
            await message.edit_text(text=note)
    except Exception:
        logger.debug("Не удалось обновить карточку после публикации #%s", post.id)


async def _drop_post(callback: CallbackQuery, db: Database, post: Post) -> None:
    """Снять пост: в канал ничего не уходит."""
    try:
        dropped = await db.mark_dropped(post.id, reason="manual")
    except Exception:
        logger.exception("Не удалось снять пост #%s", post.id)
        await callback.answer("Не удалось снять пост, попробуйте ещё раз.", show_alert=True)
        return

    logger.info(
        "Пост #%s снят админом %s",
        dropped.id,
        admin_display_name(callback.from_user) if callback.from_user else "?",
    )
    await callback.answer("Пост снят.")
    if not isinstance(callback.message, Message):
        return
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=f"❌ Пост #{dropped.id} снят.")
        else:
            await callback.message.edit_text(text=f"❌ Пост #{dropped.id} снят.")
    except Exception:
        logger.debug("Не удалось обновить сообщение снятого поста #%s", dropped.id)


async def _ask_for_text(
    callback: CallbackQuery, state: FSMContext, post: Post
) -> None:
    """Перевести админа в режим ожидания нового текста."""
    await state.set_state(EditStates.text)
    await state.update_data(post_id=post.id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Пришлите новый текст одним сообщением.\n"
            "Заголовок останется прежним; чтобы выйти из правки — /cancel."
        )


async def _ask_for_image(
    callback: CallbackQuery, state: FSMContext, post: Post, settings: Settings
) -> None:
    """Перевести админа в режим ожидания фото."""
    await state.set_state(EditStates.image)
    await state.update_data(post_id=post.id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"Пришлите новое фото (до {settings.image_max_mb} МБ).\n"
            "Чтобы выйти из правки — /cancel."
        )


async def _remove_image(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    settings: Settings,
    post: Post,
) -> None:
    """Убрать картинку из поста и вернуть карточку в основное меню."""
    if post.image_path:
        remove_image_file(post.image_path, settings.images_dir)
    updated = await db.set_image(post.id, image_path=None)
    await callback.answer("Картинка убрана.")
    await show_card_for_callback(callback, bot, updated, edit_menu=False)


# ----------------------------------------------------------------------
# Правка текста
# ----------------------------------------------------------------------
@router.message(EditStates.text)
async def handle_new_text(
    message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings
) -> None:
    """Заменить текст черновика присланным сообщением."""
    data = await state.get_data()
    post_id = int(data.get("post_id", 0))
    user = message.from_user
    if user is None or not settings.is_admin(user.id):
        await state.clear()
        return

    post = await _owned_post(db, post_id=post_id, user_id=user.id)
    if post is None:
        await state.clear()
        await message.answer("Пост уже не ваш: правка отменена.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст не сохранён. Пришлите текст ещё раз или /cancel.")
        return
    if len(text) > 350:
        await message.answer(
            "Текст длиннее 350 символов — для публикации это слишком много. "
            "Сократите и пришлите снова или /cancel."
        )
        return

    # Текстовое сообщение — тоже действие владельца: продлеваем перехват.
    await register_owner_action(db, post.id, actor_id=user.id)
    await state.clear()
    try:
        await db.add_draft_version(
            post.id,
            title=post.draft_title,
            body=text,
            origin=DraftOrigin.MANUAL,
        )
    except Exception:
        logger.exception("Не удалось сохранить ручной текст поста #%s", post.id)
        await message.answer("Не удалось сохранить текст, попробуйте ещё раз.")
        return

    updated = await db.get_post(post.id)
    if updated is not None:
        await send_card(bot, message.chat.id, updated)


# ----------------------------------------------------------------------
# Замена картинки
# ----------------------------------------------------------------------
@router.message(EditStates.image, F.photo)
async def handle_new_photo(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db: Database,
    settings: Settings,
) -> None:
    """Сохранить присланное фото и обновить карточку."""
    data = await state.get_data()
    user = message.from_user
    if user is None or not settings.is_admin(user.id):
        await state.clear()
        return

    post_id = int(data.get("post_id", 0))
    post = await _owned_post(db, post_id=post_id, user_id=user.id)
    if post is None:
        await state.clear()
        await message.answer("Пост уже не ваш: замена картинки отменена.")
        return

    photo = message.photo[-1] if message.photo else None
    if photo is None:
        return
    if photo.file_size and photo.file_size > settings.image_max_bytes:
        await message.answer(
            f"Фото больше {settings.image_max_mb} МБ — уменьшите и пришлите снова."
        )
        return

    try:
        buffer = await bot.download(photo)
        payload = buffer.getvalue() if hasattr(buffer, "getvalue") else bytes(buffer)
        saved = await save_image_bytes(payload, settings)
    except ImageError as exc:
        logger.warning("Фото от админа не сохранено: %s", exc)
        await message.answer(f"Не удалось сохранить фото: {exc}")
        return
    except Exception:
        logger.exception("Ошибка скачивания фото из Telegram для поста #%s", post.id)
        await message.answer("Не удалось скачать фото из Telegram, попробуйте ещё раз.")
        return

    await state.clear()
    # Присланное фото тоже продлевает перехват.
    await register_owner_action(db, post.id, actor_id=user.id)
    old_path = post.image_path
    await db.set_image(post.id, image_path=str(saved.path))
    if old_path:
        remove_image_file(old_path, settings.images_dir)

    updated = await db.get_post(post.id)
    if updated is not None:
        await send_card(bot, message.chat.id, updated)


@router.message(EditStates.image)
async def handle_image_wrong_type(message: Message) -> None:
    """В режиме замены картинки принимаем только фото."""
    await message.answer("Нужно именно фото (не файл и не текст). Попробуйте ещё раз.")


__all__ = ["EditStates", "handle_post_action", "router"]
