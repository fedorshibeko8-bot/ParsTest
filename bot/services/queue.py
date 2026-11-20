"""Очередь новостей, уведомления и перехват поста (задача T07).

Правила из п. 5–7 ТЗ:

* новый ``pending``-пост анонсируется администраторам заголовком статьи
  (чтобы было понятно, о чём новость) без текста, с номером и кнопкой
  «Взять инициативу»; полный текст и перевод показываются уже в карточке
  после перехвата;
* уведомления идут в личку, по самому старому посту первым (FIFO);
* видимыми считаются до ``QUEUE_VISIBLE_LIMIT`` (10) самых свежих pending;
* админ с активным перехватом новых уведомлений не получает;
* первый нажавший становится владельцем (в БД это атомарно: уникальный
  индекс не даёт админу держать два поста);
* при перехвате остальные получают «Пост #N взял админ X»;
* 10 минут бездействия → пост возвращается в очередь, уведомление всем;
* 48 часов в очереди (TTL) → ``dropped``;
* служебные сообщения не уходят в канал: ``CHANNEL_ID`` здесь не читается.

Модуль ничего не знает о карточках и публикации (T08/T10): он отдаёт
:class:`TakeOutcome` и готовые уведомления. Хендлер кнопки —
``bot/handlers/queue.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Iterable, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    User,
)

from bot.config import Settings
from bot.db import Database, InvalidTransitionError, OwnerBusyError, PostNotFoundError
from bot.models import Post
from bot.services.format import cut_words

logger = logging.getLogger(__name__)

TAKE_CALLBACK_PREFIX = "take:"
TAKE_BUTTON_TEXT = "Взять инициативу"

# Как часто проверять TTL и тайм-ауты перехвата (ТЗ: 10 и 48 часов — запас огромный).
MAINTENANCE_INTERVAL_SEC = 30


# ----------------------------------------------------------------------
# Инлайн-кнопка и callback-данные
# ----------------------------------------------------------------------
def take_callback(post_id: int) -> str:
    """Callback-данные кнопки «Взять инициативу»."""
    return f"{TAKE_CALLBACK_PREFIX}{int(post_id)}"


def parse_take_callback(data: str | None) -> int | None:
    """Разобрать ``take:123`` → ``123``; чужие или битые данные → ``None``."""
    if not data or not data.startswith(TAKE_CALLBACK_PREFIX):
        return None
    raw = data[len(TAKE_CALLBACK_PREFIX) :]
    if not raw.isdigit():
        return None
    post_id = int(raw)
    return post_id if post_id > 0 else None


def take_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Клавиатура уведомления: одна кнопка, привязанная к посту."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=TAKE_BUTTON_TEXT, callback_data=take_callback(post_id)
                )
            ]
        ]
    )


# ----------------------------------------------------------------------
# Отправка уведомлений
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class NotifyResult:
    """Кому удалось отправить уведомление, а кому нет."""

    sent: tuple[int, ...] = ()
    forbidden: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()

    def merge(self, other: "NotifyResult") -> "NotifyResult":
        return NotifyResult(
            sent=self.sent + other.sent,
            forbidden=self.forbidden + other.forbidden,
            failed=self.failed + other.failed,
        )


async def send_to_admins(
    bot: Bot,
    settings: Settings,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    exclude: Iterable[int] = (),
) -> NotifyResult:
    """Отправить личное сообщение всем админам (кроме ``exclude``).

    Ошибка одного адресата не мешает остальным. Если админ не нажал
    ``/start`` (403 ``TelegramForbiddenError``), в лог пишется понятная
    строка «админ N не активировал бота» — это штатная ситуация.
    """
    excluded = set(exclude)
    sent: list[int] = []
    forbidden: list[int] = []
    failed: list[int] = []

    for admin_id in settings.admin_ids:
        if admin_id in excluded:
            continue
        try:
            await bot.send_message(
                chat_id=admin_id, text=text, reply_markup=reply_markup
            )
        except TelegramForbiddenError:
            forbidden.append(admin_id)
            logger.warning("Админ %s не активировал бота — уведомление не доставлено.", admin_id)
        except TelegramBadRequest as exc:
            failed.append(admin_id)
            logger.warning("Админу %s не отправлено уведомление: %s", admin_id, exc)
        except TelegramAPIError as exc:
            failed.append(admin_id)
            logger.warning("Ошибка Telegram при отправке админу %s: %s", admin_id, exc)
        except Exception:
            failed.append(admin_id)
            logger.exception("Не удалось отправить уведомление админу %s", admin_id)
        else:
            sent.append(admin_id)

    return NotifyResult(tuple(sent), tuple(forbidden), tuple(failed))


async def free_admin_ids(db: Database, settings: Settings) -> list[int]:
    """Админы без активного перехвата — только им шлём новые уведомления."""
    free: list[int] = []
    for admin_id in settings.admin_ids:
        if await db.find_active_post(admin_id) is None:
            free.append(admin_id)
    return free


def _new_post_message(post: Post) -> str:
    """Уведомление о новом посте: номер и заголовок статьи, без текста."""
    lines = ["🆕 Найдена новость", f"Пост #{post.id}"]
    headline = post_headline(post)
    if headline:
        lines.append(headline)
    if post.source:
        lines.append(f"Источник: {post.source}")
    lines.append(f"Нажмите «{TAKE_BUTTON_TEXT}», чтобы открыть карточку.")
    return "\n".join(lines)


def post_headline(post: Post, *, limit: int = 160) -> str:
    """Короткий заголовок статьи для служебных сообщений.

    Берётся оригинальный заголовок: до перехвата перевода ещё нет, а
    админу достаточно понять тему. Перевод появится в карточке после
    нажатия «Взять инициативу». Длинный заголовок обрезается по границе
    слова, чтобы уведомление оставалось компактным и без многоточия.
    """
    return cut_words(post.title_original, limit)


async def announce_posts(
    bot: Bot,
    db: Database,
    settings: Settings,
    posts: Sequence[Post],
) -> NotifyResult:
    """Разослать уведомления о новых ``pending``-постах.

    Посты отправляются по одному (каждый со своей кнопкой), в порядке
    ``created_at`` — oldest first, как требует п. 5 ТЗ. Берутся только
    ``pending`` и не больше ``queue_visible_limit`` самых свежих.
    """
    pending = [post for post in posts if post.is_pending]
    if not pending:
        return NotifyResult()

    ordered = sorted(pending, key=lambda post: (post.created_at, post.id))
    limit = max(1, int(settings.queue_visible_limit))
    if len(ordered) > limit:
        # Старые сверх лимита не анонсируем: в очереди видны 10 самых свежих.
        ordered = ordered[-limit:]

    recipients = await free_admin_ids(db, settings)
    if not recipients:
        # Владелец решил: новости продолжают приходить и создаваться, но если
        # все админы заняты, уведомление не уходит никому. Посты остаются
        # в pending и видны в очереди, когда кто-то освободится.
        logger.info(
            "Все админы заняты: %d новых постов остаются в очереди без уведомлений",
            len(ordered),
        )
        return NotifyResult()

    result = NotifyResult()
    for post in ordered:
        # Один и тот же список получателей: пока идёт рассылка, статусы не
        # меняются, а после перехвата админ сам станет занятым.
        partial = await send_to_admins(
            bot,
            settings,
            _new_post_message(post),
            reply_markup=take_keyboard(post.id),
            exclude=[admin_id for admin_id in settings.admin_ids if admin_id not in recipients],
        )
        result = result.merge(partial)
    return result


# ----------------------------------------------------------------------
# Перехват
# ----------------------------------------------------------------------
class TakeStatus(StrEnum):
    """Результат нажатия «Взять инициативу»."""

    TAKEN = "taken"                # пост стал вашим
    BUSY = "busy"                  # у админа уже есть активный пост
    ALREADY_TAKEN = "already_taken"  # пост уже забрал другой
    NOT_FOUND = "not_found"        # поста нет
    FORBIDDEN = "forbidden"        # пользователь не админ


@dataclass(frozen=True, slots=True)
class TakeOutcome:
    """Что показать нажавшему и что произошло с постом."""

    status: TakeStatus
    message: str
    post: Post | None = None

    @property
    def ok(self) -> bool:
        return self.status is TakeStatus.TAKEN


def admin_display_name(user: User) -> str:
    """Имя админа для служебных сообщений: имя, @username или числовой ID."""
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return f"ID {user.id}"


async def take_post(
    bot: Bot,
    db: Database,
    settings: Settings,
    *,
    post_id: int,
    user: User,
    now: datetime | None = None,
) -> TakeOutcome:
    """Сделать админа владельцем поста и уведомить остальных.

    Проверки и переход статуса делает БД (атомарно), поэтому два
    одновременных нажатия не создадут двух владельцев.
    """
    if not settings.is_admin(user.id):
        logger.warning("Не-админ %s пытался взять пост #%s", user.id, post_id)
        return TakeOutcome(TakeStatus.FORBIDDEN, "Доступ только для администраторов.")

    try:
        post = await db.mark_intercepted(post_id, user.id, now=now)
    except PostNotFoundError:
        return TakeOutcome(TakeStatus.NOT_FOUND, f"Пост #{post_id} не найден.")
    except OwnerBusyError:
        active = await db.find_active_post(user.id)
        if active is not None:
            message = (
                f"У вас уже есть активный пост #{active.id}. "
                "Сначала завершите его."
            )
        else:
            message = "У вас уже есть активный пост. Сначала завершите его."
        return TakeOutcome(TakeStatus.BUSY, message)
    except InvalidTransitionError:
        current = await db.get_post(post_id)
        if current is not None and current.is_intercepted:
            return TakeOutcome(
                TakeStatus.ALREADY_TAKEN,
                f"Пост #{post_id} уже взял другой админ.",
                current,
            )
        if current is not None and current.is_finished:
            return TakeOutcome(
                TakeStatus.ALREADY_TAKEN,
                f"Пост #{post_id} уже завершён.",
                current,
            )
        return TakeOutcome(TakeStatus.NOT_FOUND, f"Пост #{post_id} недоступен.")

    name = admin_display_name(user)
    logger.info("Пост #%s взял админ %s (%s)", post.id, name, user.id)
    await send_to_admins(
        bot,
        settings,
        f"Пост #{post.id} взял админ {name}.",
        exclude=(user.id,),
    )
    return TakeOutcome(
        TakeStatus.TAKEN,
        f"Пост #{post.id} теперь ваш. Тайм-аут бездействия — "
        f"{settings.intercept_timeout_minutes} мин.",
        post,
    )


async def register_owner_action(
    db: Database,
    post_id: int,
    *,
    actor_id: int,
    now: datetime | None = None,
) -> Post | None:
    """Сбросить тайм-аут после действия владельца (кнопка, текст, фото).

    Возвращает обновлённый пост или ``None``, если пост не перехвачен
    этим админом. Вызывать из хендлеров T08 перед каждым действием.
    """
    post = await db.get_post(post_id)
    if post is None or not post.is_intercepted or post.owner_id != actor_id:
        return None
    return await db.touch_post(post_id, now=now)


# ----------------------------------------------------------------------
# Тайм-ауты и TTL
# ----------------------------------------------------------------------
def _returned_message(post: Post) -> str:
    """Уведомление о возврате поста в очередь — тоже с заголовком статьи."""
    lines = [
        f"⏰ Пост #{post.id} снова в очереди: тайм-аут перехвата истёк.",
    ]
    headline = post_headline(post)
    if headline:
        lines.append(headline)
    lines.append("Можно брать.")
    return "\n".join(lines)


async def release_timed_out(
    bot: Bot,
    db: Database,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[Post]:
    """Вернуть в очередь посты с истёкшим тайм-аутом и уведомить всех.

    Владельцы фиксируются **до** обновления: после ``release_timed_out``
    в БД ``owner_id`` уже пуст.
    """
    expired = await db.list_timed_out(now=now)
    if not expired:
        return []

    released = await db.release_timed_out(now=now)
    for post in released:
        logger.info("Пост #%s вернулся в очередь по тайм-ауту", post.id)
        await send_to_admins(
            bot,
            settings,
            _returned_message(post),
            reply_markup=take_keyboard(post.id),
        )
    return released


async def expire_stale_pending(
    db: Database, *, now: datetime | None = None
) -> list[Post]:
    """Снять pending-посты с истёкшим TTL (48 ч). Возвращает снятые."""
    expired = await db.expire_pending(now=now)
    if expired:
        logger.info("Снято по TTL: %d пост(ов)", len(expired))
    return expired


async def visible_pending(db: Database, settings: Settings) -> list[Post]:
    """До ``queue_visible_limit`` самых свежих pending — для меню T08."""
    return await db.list_pending(
        limit=settings.queue_visible_limit, newest_first=True
    )


async def run_maintenance_once(
    bot: Bot,
    db: Database,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> tuple[list[Post], list[Post]]:
    """Один проход обслуживания: TTL, затем тайм-ауты.

    :returns: ``(снятые по TTL, возвращённые в очередь)``.
    """
    expired = await expire_stale_pending(db, now=now)
    released = await release_timed_out(bot, db, settings, now=now)
    return expired, released


async def maintenance_loop(
    bot: Bot,
    db: Database,
    settings: Settings,
    *,
    interval_sec: float = MAINTENANCE_INTERVAL_SEC,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Фоновая проверка TTL и тайм-аутов (подключается в ``bot.main``)."""
    delay = max(1.0, float(interval_sec))
    while True:
        try:
            await run_maintenance_once(bot, db, settings)
        except Exception:
            # Сбой обслуживания не должен останавливать бота и опрос лент.
            logger.exception("Ошибка обслуживания очереди")
        if stop_event is None:
            await asyncio.sleep(delay)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            continue
        logger.info("Цикл обслуживания очереди остановлен.")
        return


__all__ = [
    "MAINTENANCE_INTERVAL_SEC",
    "NotifyResult",
    "TAKE_BUTTON_TEXT",
    "TAKE_CALLBACK_PREFIX",
    "TakeOutcome",
    "TakeStatus",
    "admin_display_name",
    "announce_posts",
    "expire_stale_pending",
    "free_admin_ids",
    "maintenance_loop",
    "parse_take_callback",
    "post_headline",
    "register_owner_action",
    "release_timed_out",
    "run_maintenance_once",
    "send_to_admins",
    "take_callback",
    "take_keyboard",
    "take_post",
    "visible_pending",
]
