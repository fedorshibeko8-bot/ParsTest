"""Карточка поста и клавиатуры меню владельца (задача T08).

Карточка показывает админу то, что потом уйдёт в канал: картинку, заголовок
и текст. До рерайта берутся оригинальные поля, после T09 — текущий черновик
(``draft_title``/``draft_text``), поэтому одна и та же функция обслуживает
все действия меню.

Ограничения Telegram: подпись к фото — 1024 символа. Длинный текст
обрезается по границе символа, HTML-теги не рвутся (экранирование и
обрезка считаются вместе).

Публикацией занимается ``bot.services.publisher`` (T10): здесь только
кнопка и предпросмотр. Меню, правка, снятие и показ источника работают
локально и не отправляют в канал ничего.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Final

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.models import DedupState, Post

logger = logging.getLogger(__name__)

# Лимит подписи к фото в Telegram.
CARD_CAPTION_LIMIT: Final[int] = 1024

POST_CALLBACK_PREFIX: Final[str] = "post:"

# Действия меню (callback data: ``post:<action>:<post_id>``).
ACTION_PUBLISH: Final[str] = "publish"
ACTION_AI: Final[str] = "ai"
ACTION_EDIT: Final[str] = "edit"
ACTION_DROP: Final[str] = "drop"
ACTION_SOURCE: Final[str] = "source"
ACTION_EDIT_TEXT: Final[str] = "text"
ACTION_EDIT_IMAGE: Final[str] = "image"
ACTION_REMOVE_IMAGE: Final[str] = "remove_image"
ACTION_BACK: Final[str] = "back"

BUTTONS: Final[dict[str, str]] = {
    ACTION_PUBLISH: "🚀 Опубликовать",
    ACTION_AI: "🤖 Доработать с ИИ",
    ACTION_EDIT: "✏️ Править вручную",
    ACTION_DROP: "❌ Не публиковать",
    ACTION_SOURCE: "🔗 Посмотреть источник",
    ACTION_EDIT_TEXT: "✏️ Изменить текст",
    ACTION_EDIT_IMAGE: "🖼 Изменить картинку",
    ACTION_REMOVE_IMAGE: "🚫 Убрать картинку",
    ACTION_BACK: "↩ Назад",
}


# ----------------------------------------------------------------------
# Callback-данные
# ----------------------------------------------------------------------
def post_callback(action: str, post_id: int) -> str:
    """``post:publish:12`` — действие меню для конкретного поста."""
    return f"{POST_CALLBACK_PREFIX}{action}:{int(post_id)}"


def parse_post_callback(data: str | None) -> tuple[str, int] | None:
    """Разобрать ``post:<action>:<id>``; чужие и битые данные → ``None``."""
    if not data or not data.startswith(POST_CALLBACK_PREFIX):
        return None
    rest = data[len(POST_CALLBACK_PREFIX) :]
    action, separator, raw_id = rest.rpartition(":")
    if not separator or not action or not raw_id.isdigit():
        return None
    post_id = int(raw_id)
    if post_id <= 0:
        return None
    return action, post_id


# ----------------------------------------------------------------------
# Текст карточки
# ----------------------------------------------------------------------
def caption_len(text: str) -> int:
    """Длина в единицах UTF-16: именно так Telegram считает лимит подписи.

    Эмодзи вне BMP занимают две единицы, поэтому ``len()`` занижает длину
    и с длинной подписью Telegram ответил бы отказом.
    """
    return len(text.encode("utf-16-le")) // 2


def _escaped_prefix(plain: str, budget: int) -> str:
    """Экранированный HTML-префикс текста, влезающий в ``budget`` единиц.

    Обрезка идёт по исходному тексту, а длина считается по экранированному:
    ``&`` → ``&amp;`` не вылезет за лимит и не разорвёт сущность.
    """
    if budget <= 0:
        return ""
    escaped = html.escape(plain, quote=False)
    if caption_len(escaped) <= budget:
        return escaped

    low, high = 0, len(plain)
    while low < high:
        middle = (low + high + 1) // 2
        if caption_len(html.escape(plain[:middle], quote=False)) <= budget - 1:
            low = middle
        else:
            high = middle - 1
    return html.escape(plain[:low].rstrip(), quote=False) + "…"


def card_title(post: Post) -> str:
    """Заголовок карточки: черновик, иначе оригинал, иначе номер поста."""
    title = post.draft_title.strip() or post.title_original.strip()
    return title or f"Пост #{post.id}"


def card_body(post: Post) -> str:
    """Текст карточки: черновик, иначе лид оригинала."""
    return post.draft_text.strip() or post.lead.strip()


def card_text(post: Post, *, limit: int = CARD_CAPTION_LIMIT) -> str:
    """HTML-текст карточки: при необходимости обрезан до лимита подписи.

    Для «серой зоны» сверху добавляется предупреждение о возможном дубле:
    решение всё равно принимает админ.
    """
    title = card_title(post)
    body = card_body(post)

    prefix = ""
    if post.dedup_state is DedupState.GRAY_ZONE:
        score = f" ({post.dedup_score:.0%})" if post.dedup_score is not None else ""
        prefix = f"⚠️ Похоже на уже опубликованное{score}\n\n"

    # 7 символов уходят на <b> и </b>, 2 — на пустую строку между частями.
    title_html = f"<b>{_escaped_prefix(title, limit - caption_len(prefix) - 7)}</b>"
    text = prefix + title_html
    if body and caption_len(text) + 2 < limit:
        text += "\n\n" + _escaped_prefix(
            body, limit - caption_len(text) - 2
        )
    return text


# ----------------------------------------------------------------------
# Клавиатуры
# ----------------------------------------------------------------------
def _button(action: str, post_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=BUTTONS[action], callback_data=post_callback(action, post_id)
    )


def card_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Основное меню владельца (п. 8 ТЗ)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(ACTION_PUBLISH, post_id),
                _button(ACTION_AI, post_id),
            ],
            [
                _button(ACTION_EDIT, post_id),
                _button(ACTION_DROP, post_id),
            ],
            [_button(ACTION_SOURCE, post_id)],
        ]
    )


def edit_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Подменю «Править вручную»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(ACTION_EDIT_TEXT, post_id)],
            [_button(ACTION_EDIT_IMAGE, post_id)],
            [_button(ACTION_REMOVE_IMAGE, post_id)],
            [_button(ACTION_BACK, post_id)],
        ]
    )


def keyboard_for(post_id: int, *, edit_menu: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура карточки: основная или подменю правки."""
    return edit_keyboard(post_id) if edit_menu else card_keyboard(post_id)


# ----------------------------------------------------------------------
# Отправка и обновление карточки
# ----------------------------------------------------------------------
def image_file(post: Post) -> Path | None:
    """Путь к существующему файлу картинки поста (``None``, если нет)."""
    if not post.image_path:
        return None
    path = Path(post.image_path)
    return path if path.is_file() else None


async def send_card(
    bot: Bot,
    chat_id: int,
    post: Post,
    *,
    edit_menu: bool = False,
) -> Message:
    """Отправить новую карточку в чат (фото сверху или текст)."""
    text = card_text(post)
    markup = keyboard_for(post.id, edit_menu=edit_menu)
    path = image_file(post)
    if path is not None:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile(path),
            caption=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=markup,
    )


async def refresh_card(
    bot: Bot,
    message: Message,
    post: Post,
    *,
    edit_menu: bool = False,
) -> Message | None:
    """Показать карточку после действия: по возможности отредактировать.

    Telegram не позволяет превратить фото-сообщение в текстовое и обратно,
    поэтому при смене типа старое сообщение удаляется и отправляется новое.
    """
    text = card_text(post)
    markup = keyboard_for(post.id, edit_menu=edit_menu)
    path = image_file(post)
    chat_id = message.chat.id

    if path is not None:
        if message.photo:
            try:
                await message.edit_caption(
                    caption=text, parse_mode="HTML", reply_markup=markup
                )
                return message
            except TelegramBadRequest as exc:
                if "not modified" not in str(exc):
                    logger.debug("Не удалось обновить подпись карточки: %s", exc)
                return message
        await _delete_message(message)
        return await send_card(bot, chat_id, post, edit_menu=edit_menu)

    if not message.photo:
        try:
            await message.edit_text(text=text, parse_mode="HTML", reply_markup=markup)
            return message
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                logger.debug("Не удалось обновить карточку: %s", exc)
            return message

    await _delete_message(message)
    return await send_card(bot, chat_id, post, edit_menu=edit_menu)


async def show_card_for_callback(
    callback,
    bot: Bot,
    post: Post,
    *,
    edit_menu: bool = False,
) -> None:
    """Обновить карточку, на которой нажата кнопка (учитывая тип сообщения)."""
    if isinstance(callback.message, Message):
        await refresh_card(bot, callback.message, post, edit_menu=edit_menu)


async def _delete_message(message: Message) -> None:
    """Удалить старое сообщение; ошибка (старое/удалённое) не критична."""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


__all__ = [
    "ACTION_AI",
    "ACTION_BACK",
    "ACTION_DROP",
    "ACTION_EDIT",
    "ACTION_EDIT_IMAGE",
    "ACTION_EDIT_TEXT",
    "ACTION_PUBLISH",
    "ACTION_REMOVE_IMAGE",
    "ACTION_SOURCE",
    "BUTTONS",
    "CARD_CAPTION_LIMIT",
    "POST_CALLBACK_PREFIX",
    "card_body",
    "card_keyboard",
    "card_text",
    "card_title",
    "caption_len",
    "edit_keyboard",
    "image_file",
    "keyboard_for",
    "parse_post_callback",
    "post_callback",
    "refresh_card",
    "send_card",
    "show_card_for_callback",
]
