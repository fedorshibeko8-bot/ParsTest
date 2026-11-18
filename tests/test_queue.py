"""Тесты очереди, уведомлений и перехвата (задача T07).

Бот подменяется ``FakeBot``: сообщения никуда не уходят, но видно, кому,
в каком порядке и с какой клавиатурой они отправлены. Проверяются FIFO,
лимит видимых постов, занятые админы, 403, перехват, тайм-аут и TTL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup, User

from bot.models import PostStatus
from bot.services.queue import (
    NotifyResult,
    TakeStatus,
    announce_posts,
    free_admin_ids,
    maintenance_loop,
    parse_take_callback,
    register_owner_action,
    release_timed_out,
    run_maintenance_once,
    send_to_admins,
    take_callback,
    take_keyboard,
    take_post,
    visible_pending,
)
from tests.helpers import T0, make_settings, offset, run


@dataclass(frozen=True)
class SentMessage:
    chat_id: int
    text: str
    reply_markup: InlineKeyboardMarkup | None


class FakeBot:
    """Минимальная замена ``aiogram.Bot`` для проверки рассылки."""

    def __init__(
        self,
        *,
        forbidden: tuple[int, ...] = (),
        bad_request: tuple[int, ...] = (),
    ) -> None:
        self.forbidden = set(forbidden)
        self.bad_request = set(bad_request)
        self.sent: list[SentMessage] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=chat_id, text=text),
                message="Forbidden: bot was blocked by the user",
            )
        if chat_id in self.bad_request:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=chat_id, text=text),
                message="Bad Request: chat not found",
            )
        self.sent.append(SentMessage(chat_id, text, reply_markup))


def make_user(user_id: int = 1, first_name: str = "Фёдор") -> User:
    return User(id=user_id, is_bot=False, first_name=first_name)


# ----------------------------------------------------------------------
# Callback-данные и клавиатура
# ----------------------------------------------------------------------
def test_take_callback_roundtrip() -> None:
    assert take_callback(42) == "take:42"
    assert parse_take_callback("take:42") == 42


@pytest.mark.parametrize(
    "data", [None, "", "take:", "take:abc", "take:0", "take:-1", "other:5"]
)
def test_parse_take_callback_rejects_bad_data(data: str | None) -> None:
    assert parse_take_callback(data) is None


def test_take_keyboard_has_one_button_for_post() -> None:
    markup = take_keyboard(7)
    button = markup.inline_keyboard[0][0]
    assert button.callback_data == "take:7"
    assert button.text


# ----------------------------------------------------------------------
# Рассылка
# ----------------------------------------------------------------------
def test_send_to_admins_reaches_everyone() -> None:
    settings = make_settings(admin_ids=(1, 2, 3))
    bot = FakeBot()

    result = run(send_to_admins(bot, settings, "текст"))

    assert result == NotifyResult(sent=(1, 2, 3))
    assert [message.chat_id for message in bot.sent] == [1, 2, 3]
    assert all(message.text == "текст" for message in bot.sent)


def test_send_to_admins_skips_excluded() -> None:
    settings = make_settings(admin_ids=(1, 2))
    bot = FakeBot()

    result = run(send_to_admins(bot, settings, "текст", exclude=(2,)))

    assert result.sent == (1,)
    assert [message.chat_id for message in bot.sent] == [1]


def test_forbidden_admin_does_not_block_others() -> None:
    settings = make_settings(admin_ids=(1, 2, 3))
    bot = FakeBot(forbidden=(2,))

    result = run(send_to_admins(bot, settings, "текст"))

    assert result.sent == (1, 3)
    assert result.forbidden == (2,)
    assert [message.chat_id for message in bot.sent] == [1, 3]


def test_bad_request_is_logged_and_others_receive() -> None:
    settings = make_settings(admin_ids=(1, 2))
    bot = FakeBot(bad_request=(1,))

    result = run(send_to_admins(bot, settings, "текст"))

    assert result.sent == (2,)
    assert result.failed == (1,)


# ----------------------------------------------------------------------
# Анонсы новых постов
# ----------------------------------------------------------------------
def test_announce_posts_goes_oldest_first_fifo(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,))
    oldest = make_post(created_at=T0 - offset(hours=2))
    middle = make_post(created_at=T0 - offset(hours=1))
    newest = make_post(created_at=T0)
    bot = FakeBot()

    run(announce_posts(bot, db, settings, [newest, oldest, middle]))

    texts = [message.text for message in bot.sent]
    assert len(texts) == 3
    assert f"#{oldest.id}" in texts[0]
    assert f"#{middle.id}" in texts[1]
    assert f"#{newest.id}" in texts[2]


def test_announce_has_no_content_and_has_take_button(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,))
    post = make_post(title_original="Секретный заголовок", lead="Секретный лид")
    bot = FakeBot()

    run(announce_posts(bot, db, settings, [post]))

    message = bot.sent[0]
    assert f"#{post.id}" in message.text
    assert "Секретный" not in message.text
    assert message.reply_markup is not None
    button = message.reply_markup.inline_keyboard[0][0]
    assert button.callback_data == take_callback(post.id)


def test_announce_limits_to_visible_posts(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,), queue_visible_limit=10)
    posts = [
        make_post(created_at=T0 + offset(minutes=index)) for index in range(12)
    ]
    bot = FakeBot()

    run(announce_posts(bot, db, settings, posts))

    assert len(bot.sent) == 10
    announced_ids = [
        int(message.text.split("Пост #", 1)[1].split(".", 1)[0])
        for message in bot.sent
    ]
    # Старые два (id 1, 2) за пределами «10 самых свежих» не анонсируются.
    assert announced_ids == [post.id for post in posts[2:]]


def test_busy_admin_gets_no_new_announcements(db, make_post) -> None:
    settings = make_settings(admin_ids=(1, 2))
    busy_post = make_post()
    run(db.mark_intercepted(busy_post.id, 2, now=T0))
    fresh = make_post(created_at=T0 + offset(minutes=1))
    bot = FakeBot()

    run(announce_posts(bot, db, settings, [fresh]))

    assert [message.chat_id for message in bot.sent] == [1]
    assert run(free_admin_ids(db, settings)) == [1]


def test_announce_skips_finished_posts(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,))
    post = make_post()
    run(db.mark_published(post.id))
    bot = FakeBot()

    stored = run(db.get_post(post.id))
    assert stored is not None
    result = run(announce_posts(bot, db, settings, [stored]))

    assert result == NotifyResult()
    assert bot.sent == []


def test_announce_does_not_use_channel(db, make_post) -> None:
    settings = make_settings(admin_ids=(1, 2), channel_id="-1004371911262")
    bot = FakeBot()

    run(announce_posts(bot, db, settings, [make_post()]))

    assert all(message.chat_id in settings.admin_ids for message in bot.sent)
    assert all(message.chat_id != int(settings.channel_id) for message in bot.sent)


# ----------------------------------------------------------------------
# Перехват
# ----------------------------------------------------------------------
def test_take_post_sets_owner_and_notifies_others(db, make_post) -> None:
    settings = make_settings(admin_ids=(1, 2))
    post = make_post()
    bot = FakeBot()

    outcome = run(
        take_post(bot, db, settings, post_id=post.id, user=make_user(1, "Иван"), now=T0)
    )

    assert outcome.status is TakeStatus.TAKEN
    assert outcome.ok
    assert f"#{post.id}" in outcome.message

    stored = run(db.get_post(post.id))
    assert stored is not None
    assert stored.status is PostStatus.INTERCEPTED
    assert stored.owner_id == 1
    assert stored.intercept_deadline == T0 + offset(minutes=10)

    assert [message.chat_id for message in bot.sent] == [2]
    assert "Иван" in bot.sent[0].text
    assert f"Пост #{post.id}" in bot.sent[0].text


def test_take_post_rejects_non_admin(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,))
    post = make_post()
    bot = FakeBot()

    outcome = run(
        take_post(bot, db, settings, post_id=post.id, user=make_user(99), now=T0)
    )

    assert outcome.status is TakeStatus.FORBIDDEN
    assert bot.sent == []
    assert run(db.get_post(post.id)).status is PostStatus.PENDING


def test_take_post_blocks_second_active_post(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,))
    first = make_post()
    second = make_post()
    run(db.mark_intercepted(first.id, 1, now=T0))
    bot = FakeBot()

    outcome = run(
        take_post(bot, db, settings, post_id=second.id, user=make_user(1), now=T0)
    )

    assert outcome.status is TakeStatus.BUSY
    assert f"#{first.id}" in outcome.message
    assert bot.sent == []
    assert run(db.get_post(second.id)).status is PostStatus.PENDING


def test_take_post_already_taken(db, make_post) -> None:
    settings = make_settings(admin_ids=(1, 2))
    post = make_post()
    run(db.mark_intercepted(post.id, 1, now=T0))
    bot = FakeBot()

    outcome = run(
        take_post(bot, db, settings, post_id=post.id, user=make_user(2), now=T0)
    )

    assert outcome.status is TakeStatus.ALREADY_TAKEN
    assert f"#{post.id}" in outcome.message
    assert run(db.get_post(post.id)).owner_id == 1


def test_take_post_not_found(db) -> None:
    settings = make_settings(admin_ids=(1,))
    bot = FakeBot()

    outcome = run(
        take_post(bot, db, settings, post_id=999, user=make_user(1), now=T0)
    )

    assert outcome.status is TakeStatus.NOT_FOUND


# ----------------------------------------------------------------------
# Действия владельца сбрасывают таймер
# ----------------------------------------------------------------------
def test_owner_action_extends_deadline(db, make_post) -> None:
    post = make_post()
    run(db.mark_intercepted(post.id, 1, now=T0))

    updated = run(
        register_owner_action(db, post.id, actor_id=1, now=T0 + offset(minutes=5))
    )

    assert updated is not None
    assert updated.intercept_deadline == T0 + offset(minutes=15)
    assert updated.last_activity_at == T0 + offset(minutes=5)


def test_owner_action_from_stranger_is_ignored(db, make_post) -> None:
    post = make_post()
    run(db.mark_intercepted(post.id, 1, now=T0))

    result = run(
        register_owner_action(db, post.id, actor_id=2, now=T0 + offset(minutes=5))
    )

    assert result is None
    stored = run(db.get_post(post.id))
    assert stored is not None
    assert stored.intercept_deadline == T0 + offset(minutes=10)


@pytest.mark.parametrize("status", ["pending", "published", "dropped"])
def test_owner_action_requires_intercepted_post(db, make_post, status: str) -> None:
    post = make_post()
    if status == "published":
        run(db.mark_published(post.id, now=T0))
    elif status == "dropped":
        run(db.mark_dropped(post.id, reason="тест", now=T0))

    assert run(register_owner_action(db, post.id, actor_id=1, now=T0)) is None


# ----------------------------------------------------------------------
# Тайм-аут перехвата
# ----------------------------------------------------------------------
def test_timeout_returns_post_to_queue_and_notifies_all(db, make_post) -> None:
    settings = make_settings(admin_ids=(1, 2))
    post = make_post()
    run(db.mark_intercepted(post.id, 1, now=T0))
    bot = FakeBot()

    released = run(
        release_timed_out(bot, db, settings, now=T0 + offset(minutes=11))
    )

    assert [item.id for item in released] == [post.id]
    stored = run(db.get_post(post.id))
    assert stored is not None
    assert stored.status is PostStatus.PENDING
    assert stored.owner_id is None
    assert stored.intercept_deadline is None

    assert [message.chat_id for message in bot.sent] == [1, 2]
    assert all(f"#{post.id}" in message.text for message in bot.sent)
    assert all(message.reply_markup is not None for message in bot.sent)


def test_timeout_not_released_before_deadline(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,))
    post = make_post()
    run(db.mark_intercepted(post.id, 1, now=T0))
    bot = FakeBot()

    released = run(
        release_timed_out(bot, db, settings, now=T0 + offset(minutes=9))
    )

    assert released == []
    assert bot.sent == []
    assert run(db.get_post(post.id)).status is PostStatus.INTERCEPTED


# ----------------------------------------------------------------------
# TTL и общий проход обслуживания
# ----------------------------------------------------------------------
def test_old_pending_is_dropped_by_ttl(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,))
    old = make_post(created_at=T0 - offset(hours=49))
    fresh = make_post(created_at=T0 - offset(hours=1))

    expired, released = run(
        run_maintenance_once(FakeBot(), db, settings, now=T0)
    )

    assert [post.id for post in expired] == [old.id]
    assert released == []
    old_stored = run(db.get_post(old.id))
    assert old_stored is not None
    assert old_stored.status is PostStatus.DROPPED
    assert old_stored.drop_reason == "ttl"
    assert run(db.get_post(fresh.id)).status is PostStatus.PENDING


def test_maintenance_handles_ttl_and_timeout_together(db, make_post) -> None:
    settings = make_settings(admin_ids=(1, 2))
    old = make_post(created_at=T0 - offset(hours=50))
    taken = make_post(created_at=T0)
    run(db.mark_intercepted(taken.id, 1, now=T0))
    bot = FakeBot()

    expired, released = run(
        run_maintenance_once(bot, db, settings, now=T0 + offset(minutes=11))
    )

    assert [post.id for post in expired] == [old.id]
    assert [post.id for post in released] == [taken.id]
    assert len(bot.sent) == 2  # уведомление о возврате обоим админам


def test_visible_pending_returns_newest_up_to_limit(db, make_post) -> None:
    settings = make_settings(queue_visible_limit=10)
    posts = [
        make_post(created_at=T0 + offset(minutes=index)) for index in range(12)
    ]

    visible = run(visible_pending(db, settings))

    assert len(visible) == 10
    assert [post.id for post in visible] == [post.id for post in reversed(posts[2:])]


def test_maintenance_loop_stops_by_event(db, make_post) -> None:
    settings = make_settings(admin_ids=(1,))
    make_post(created_at=T0 - offset(hours=49))
    stop_event = asyncio.Event()
    stop_event.set()
    bot = FakeBot()

    run(maintenance_loop(bot, db, settings, interval_sec=1, stop_event=stop_event))

    # Один проход успел выполниться до остановки.
    assert run(db.count_pending()) == 0


# ----------------------------------------------------------------------
# Хендлер кнопки
# ----------------------------------------------------------------------
class FakeCallback:
    """Минимальная замена ``CallbackQuery``: фиксируем ответы бота."""

    def __init__(self, data: str, user: User) -> None:
        self.data = data
        self.from_user = user
        self.message = None
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def test_handler_take_sets_owner(db, make_post) -> None:
    from bot.handlers.queue import handle_take

    settings = make_settings(admin_ids=(1, 2))
    post = make_post()
    bot = FakeBot()
    callback = FakeCallback(take_callback(post.id), make_user(1))

    run(handle_take(callback, bot, db, settings))

    stored = run(db.get_post(post.id))
    assert stored is not None and stored.owner_id == 1
    assert callback.answers == [("Пост ваш.", False)]
    assert [message.chat_id for message in bot.sent] == [2]


def test_handler_take_reports_busy_admin(db, make_post) -> None:
    from bot.handlers.queue import handle_take

    settings = make_settings(admin_ids=(1,))
    first = make_post()
    second = make_post()
    run(db.mark_intercepted(first.id, 1, now=T0))
    callback = FakeCallback(take_callback(second.id), make_user(1))

    run(handle_take(callback, FakeBot(), db, settings))

    assert callback.answers
    assert callback.answers[0][1] is True  # показываем всплывающим окном
    assert f"#{first.id}" in callback.answers[0][0]


def test_handler_take_rejects_broken_callback(db) -> None:
    from bot.handlers.queue import handle_take

    settings = make_settings(admin_ids=(1,))
    callback = FakeCallback("take:abc", make_user(1))

    run(handle_take(callback, FakeBot(), db, settings))

    assert callback.answers and callback.answers[0][1] is True
