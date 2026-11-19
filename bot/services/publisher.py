"""Публикация поста в канал (задача T10).

Модуль — единственное место, которое читает ``Settings.channel_id``:
служебные сообщения (карточка, ошибки, статусы) остаются в личке админов
и в канал не попадают (жёсткое требование владельца, п. 10 ТЗ).

Порядок важен:

1. проверить, что пост всё ещё перехвачен этим админом;
2. собрать текст поста (черновик, иначе оригинал), очистить его от ссылок,
   хэштегов и упоминаний и проверить длины по ТЗ;
3. отправить в канал (фото сверху + подпись или просто текст);
4. только после успешной отправки перевести пост в ``published``.

Если сначала отметить пост опубликованным, а отправка упадёт, пост
исчезнет из очереди, не попав в канал. Обратная ошибка (отправили, но
не успели записать статус) видна в логах — её безопаснее разбирать вручную.

``DRY_RUN`` не отправляет в канал ничего, но переводит пост в ``published``:
так весь цикл «очередь → карточка → рерайт → публикация» проверяется на ПК
без ключей и без риска что-то опубликовать.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Final

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile

from bot.config import Settings
from bot.db import Database, InvalidTransitionError
from bot.models import Post
from bot.services.cards import CARD_CAPTION_LIMIT, caption_len, image_file
from bot.services.format import clean_text, length_problems

logger = logging.getLogger(__name__)

#: Лимит текстового сообщения Telegram; пост по ТЗ в него укладывается с запасом.
MESSAGE_LIMIT: Final[int] = 4096


class PublishError(RuntimeError):
    """Публикация не выполнена; пост остался за админом.

    ``problems`` — конкретные претензии к тексту (длины), чтобы показать
    их админу списком, а не одной строкой.
    """

    def __init__(self, message: str, *, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Publication:
    """Что именно ушло (или ушло бы) в канал."""

    post: Post
    text: str
    dry_run: bool
    message_id: int | None = None


# ----------------------------------------------------------------------
# Текст поста
# ----------------------------------------------------------------------
def post_title(post: Post) -> str:
    """Заголовок публикации: черновик, иначе оригинал (без «Пост #N»)."""
    return (post.draft_title.strip() or post.title_original.strip())


def post_body(post: Post) -> str:
    """Тело публикации: черновик, иначе лид оригинала."""
    return post.draft_text.strip() or post.lead.strip()


def publication_text(title: str, body: str) -> str:
    """HTML поста: жирный заголовок и тело, без подписи канала."""
    return (
        f"<b>{html.escape(title, quote=False)}</b>\n\n"
        f"{html.escape(body, quote=False)}"
    )


def build_publication(post: Post) -> tuple[str, str, str]:
    """Собрать и проверить текст поста: ``(заголовок, тело, HTML)``.

    :raises PublishError: текста нет или он не соответствует ТЗ по длине.
    """
    title = clean_text(post_title(post))
    body = clean_text(post_body(post))
    if not title:
        raise PublishError("У поста нет заголовка — публиковать нечего.")
    if not body:
        raise PublishError("У поста нет текста — публиковать нечего.")

    problems = length_problems(title, body)
    if problems:
        raise PublishError(
            "Текст не соответствует требованиям ТЗ:\n- " + "\n- ".join(problems),
            problems=problems,
        )
    return title, body, publication_text(title, body)


# ----------------------------------------------------------------------
# Отправка
# ----------------------------------------------------------------------
def channel_target(settings: Settings) -> str | int:
    """Куда отправлять: числовой ID — числом, ``@username`` — строкой."""
    value = settings.channel_id.strip()
    return int(value) if value.lstrip("-").isdigit() else value


async def _send_to_channel(
    bot: Bot, settings: Settings, post: Post, text: str
) -> int | None:
    """Отправить пост в канал и вернуть ``message_id`` (``None`` — ошибка)."""
    target = channel_target(settings)
    silent = settings.publish_silent
    path = image_file(post)

    try:
        if path is not None:
            message = await bot.send_photo(
                chat_id=target,
                photo=FSInputFile(path),
                caption=text,
                parse_mode="HTML",
                disable_notification=silent,
            )
        else:
            message = await bot.send_message(
                chat_id=target,
                text=text,
                parse_mode="HTML",
                disable_notification=silent,
            )
    except TelegramForbiddenError as exc:
        raise PublishError(
            "Бот не может писать в канал: добавьте его администратором "
            "с правом публикации сообщений."
        ) from exc
    except TelegramRetryAfter as exc:
        raise PublishError(
            f"Telegram просит подождать {exc.retry_after} с — нажмите «Опубликовать» ещё раз."
        ) from exc
    except TelegramBadRequest as exc:
        raise PublishError(f"Telegram отклонил публикацию: {exc.message}") from exc
    except TelegramAPIError as exc:
        raise PublishError(f"Ошибка Telegram: {exc}") from exc

    return getattr(message, "message_id", None)


async def publish_post(
    db: Database,
    settings: Settings,
    post: Post,
    *,
    actor_id: int,
    bot: Bot,
) -> Publication:
    """Опубликовать пост в канал и отметить его ``published``.

    :raises PublishError: пост уже не у админа, текст не по ТЗ или Telegram
        отказал. В любом случае пост остаётся ``intercepted`` за владельцем.
    """
    fresh = await db.get_post(post.id)
    if fresh is None or not fresh.is_intercepted or fresh.owner_id != actor_id:
        raise PublishError("Пост уже не ваш — публикация отменена.")

    _title, _body, text = build_publication(fresh)

    # Подпись к фото ограничена 1024 единицами UTF-16, а HTML-экранирование
    # раздувает текст (& → &amp;). Проверяем итоговую разметку, чтобы Telegram
    # не отклонил уже готовую публикацию.
    if image_file(fresh) is not None and caption_len(text) > CARD_CAPTION_LIMIT:
        raise PublishError(
            "Текст с картинкой не влезает в подпись Telegram — сократите его "
            "или уберите картинку."
        )

    if settings.dry_run:
        logger.info("Пост #%s: DRY_RUN, в канал не отправляю", fresh.id)
        message_id: int | None = None
    else:
        if not settings.channel_id.strip():
            raise PublishError(
                "Не задан CHANNEL_ID — публикация невозможна. "
                "Заполните его в .env или включите DRY_RUN=1."
            )
        message_id = await _send_to_channel(bot, settings, fresh, text)
        logger.info(
            "Пост #%s опубликован в канал %s (message_id=%s, silent=%s)",
            fresh.id,
            settings.channel_id,
            message_id,
            settings.publish_silent,
        )

    try:
        published = await db.mark_published(fresh.id)
    except InvalidTransitionError as exc:
        raise PublishError(
            "Пост уже завершён другим действием — проверьте канал."
        ) from exc

    return Publication(
        post=published, text=text, dry_run=settings.dry_run, message_id=message_id
    )


__all__ = [
    "MESSAGE_LIMIT",
    "Publication",
    "PublishError",
    "build_publication",
    "channel_target",
    "post_body",
    "post_title",
    "publication_text",
    "publish_post",
]
