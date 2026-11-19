"""Тесты публикации в канал (задача T10).

Проверяются три вещи, которые легко сломать незаметно:

* в канал уходит только сам пост (заголовок + тело), без служебных
  сообщений, ссылок, хэштегов и подписи канала;
* кнопка публикует пост независимо от соответствия текста длинам из ТЗ;
* при отказе Telegram пост остаётся за админом, а не теряется.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageCaption,
    EditMessageText,
    SendMessage,
    SendPhoto,
)

from bot.config import Settings, load_settings
from bot.db import Database
from bot.handlers.cards import handle_post_action
from bot.models import PostStatus
from bot.services.cards import ACTION_PUBLISH, post_callback
from bot.services.format import (
    BODY_MAX,
    BODY_MIN,
    TITLE_MAX,
    TITLE_MIN,
    TOTAL_MAX,
    clean_text,
    cut_words,
    length_problems,
    post_lengths,
)
from bot.services.images import save_image_bytes
from bot.services.publisher import (
    Publication,
    PublishError,
    build_publication,
    channel_target,
    post_body,
    post_title,
    publication_text,
    publish_post,
)
from tests.fakes import PNG, FakeBot, FakeState, make_callback, make_message
from tests.helpers import T0, make_settings, run

CHANNEL = "-1004371911262"
TITLE = "Rockstar показала новый кадр GTA 6 и дату релиза"
BODY = (
    "Студия Rockstar Games показала новый кадр из GTA 6. В сообщении "
    "подтверждена дата выхода игры. Подробности обещают раскрыть позже."
)


class FailingBot(FakeBot):
    """Бот, который отказывает на любой отправке (права, лимиты)."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def send_message(self, *args, **kwargs):
        raise self.error

    async def send_photo(self, *args, **kwargs):
        raise self.error


@pytest.fixture()
def live_settings(tmp_path: Path) -> Settings:
    """Боевые настройки: канал задан, DRY_RUN выключен."""
    return make_settings(
        admin_ids=(1, 2),
        dry_run=False,
        channel_id=CHANNEL,
        gemini_api_key="gemini-key",
        groq_api_key="groq-key",
        images_dir=tmp_path / "images",
    )


@pytest.fixture()
def dry_settings(tmp_path: Path) -> Settings:
    return make_settings(
        admin_ids=(1, 2), dry_run=True, channel_id="", images_dir=tmp_path / "images"
    )


def take(db: Database, post, owner_id: int = 1):
    """Перехватить пост и вернуть свежую копию из БД."""
    run(db.mark_intercepted(post.id, owner_id, now=T0))
    stored = run(db.get_post(post.id))
    assert stored is not None
    return stored


def ready_post(make_post, **overrides):
    """Пост с текстом, который проходит требования ТЗ по длине."""
    params = {"title_original": TITLE, "lead": BODY}
    params.update(overrides)
    return make_post(**params)


# ----------------------------------------------------------------------
# Лимиты и очистка текста
# ----------------------------------------------------------------------
def test_sample_text_fits_limits() -> None:
    assert TITLE_MIN <= len(TITLE) <= TITLE_MAX
    assert BODY_MIN <= len(BODY) <= BODY_MAX
    assert length_problems(TITLE, BODY) == ()


def test_length_problems_describes_each_violation() -> None:
    problems = length_problems("коротко", "мало")
    assert any("заголовок короче" in problem for problem in problems)
    assert any("текст короче" in problem for problem in problems)


def test_total_length_is_checked() -> None:
    title = "Я" * TITLE_MAX
    body = "Я" * BODY_MAX
    assert post_lengths(title, body) == (TITLE_MAX, BODY_MAX, TOTAL_MAX)
    assert length_problems(title, body) == ()


def test_clean_text_removes_links_tags_and_mentions() -> None:
    dirty = "Новость #GTA6 @channel https://example.com/news про игру."

    cleaned = clean_text(dirty)

    assert "http" not in cleaned.lower()
    assert "#" not in cleaned
    assert "@" not in cleaned
    assert "Новость" in cleaned and "про игру." in cleaned


# ----------------------------------------------------------------------
# Сборка текста поста
# ----------------------------------------------------------------------
def test_post_title_and_body_prefer_draft(db, make_post) -> None:
    post = ready_post(make_post, title_original="Оригинал", lead="Оригинальный лид.")
    run(db.add_draft_version(post.id, title="Черновик", body="Текст черновика."))
    stored = run(db.get_post(post.id))
    assert stored is not None

    assert post_title(stored) == "Черновик"
    assert post_body(stored) == "Текст черновика."


def test_post_title_without_text_is_empty(db, make_post) -> None:
    post = make_post(title_original="", lead="")
    assert post_title(post) == ""
    assert post_body(post) == ""


def test_publication_text_escapes_html(make_post) -> None:
    text = publication_text("GTA <6> & Co", "A & B < C")

    assert text.startswith("<b>GTA &lt;6&gt; &amp; Co</b>")
    assert "A &amp; B &lt; C" in text
    assert "\n\n" in text


def test_build_publication_returns_valid_text(make_post) -> None:
    post = ready_post(make_post)

    title, body, text = build_publication(post)

    assert (title, body) == (TITLE, BODY)
    assert text == f"<b>{TITLE}</b>\n\n{BODY}"


def test_build_publication_strips_forbidden_elements(make_post) -> None:
    post = ready_post(
        make_post,
        lead=BODY + " #GTA6 @channel https://example.com/gta",
    )

    _title, body, text = build_publication(post)

    assert "http" not in text.lower()
    assert "#" not in text
    assert "@" not in text


def test_build_publication_rejects_fully_empty_text(make_post) -> None:
    post = make_post(title_original="", lead="")

    with pytest.raises(PublishError, match="пустой"):
        build_publication(post)


def test_build_publication_uses_body_as_title_when_title_missing(make_post) -> None:
    post = ready_post(make_post, title_original="", lead=BODY)

    title, body, _text = build_publication(post)

    assert title == cut_words(BODY, TITLE_MAX)
    assert body == BODY
    assert "…" not in title


def test_build_publication_uses_title_as_body_when_lead_missing(make_post) -> None:
    post = ready_post(make_post, lead="")

    title, body, text = build_publication(post)

    assert (title, body) == (TITLE, TITLE)
    assert text == f"<b>{TITLE}</b>\n\n{TITLE}"


@pytest.mark.parametrize(
    "title",
    ["Короткий заголовок", "Я" * (TITLE_MAX + 1)],
)
def test_build_publication_accepts_any_title_length(make_post, title) -> None:
    post = ready_post(make_post, title_original=title)

    built_title, _body, text = build_publication(post)

    assert built_title == title
    assert f"<b>{title}</b>" in text


@pytest.mark.parametrize(
    "body",
    ["Слишком коротко.", "Я" * (BODY_MAX + 1)],
)
def test_build_publication_accepts_any_body_length(make_post, body) -> None:
    post = ready_post(make_post, lead=body)

    _title, built_body, text = build_publication(post)

    assert built_body == body
    assert text.endswith(body)


# ----------------------------------------------------------------------
# Адрес канала
# ----------------------------------------------------------------------
def test_channel_target_converts_numeric_id(live_settings) -> None:
    assert channel_target(live_settings) == int(CHANNEL)


def test_channel_target_keeps_username(tmp_path) -> None:
    settings = make_settings(channel_id="@fowgsl", images_dir=tmp_path / "images")
    assert channel_target(settings) == "@fowgsl"


# ----------------------------------------------------------------------
# Настройка PUBLISH_SILENT
# ----------------------------------------------------------------------
def _read_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str):
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("DRY_RUN", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings(env_path=tmp_path / "missing.env")


def test_publish_silent_is_off_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PUBLISH_SILENT", raising=False)
    assert _read_settings(monkeypatch, tmp_path).publish_silent is False


def test_publish_silent_is_read_from_env(monkeypatch, tmp_path: Path) -> None:
    settings = _read_settings(monkeypatch, tmp_path, PUBLISH_SILENT="1")
    assert settings.publish_silent is True


# ----------------------------------------------------------------------
# Публикация
# ----------------------------------------------------------------------
def test_publish_sends_text_and_marks_published(db, make_post, live_settings) -> None:
    post = take(db, ready_post(make_post))
    bot = FakeBot()

    publication = run(
        publish_post(db, live_settings, post, actor_id=1, bot=bot)
    )

    assert isinstance(publication, Publication)
    assert publication.dry_run is False
    sent = bot.of(SendMessage)
    assert len(sent) == 1
    assert sent[0].chat_id == int(CHANNEL)
    assert sent[0].parse_mode == "HTML"
    assert sent[0].text == f"<b>{TITLE}</b>\n\n{BODY}"
    assert not bot.of(SendPhoto)
    assert publication.post.status is PostStatus.PUBLISHED
    assert publication.post.published_at is not None


def test_publish_with_image_uses_caption(
    db, make_post, live_settings, tmp_path: Path
) -> None:
    saved = run(save_image_bytes(PNG, live_settings))
    post = take(db, ready_post(make_post, image_path=str(saved.path)))
    bot = FakeBot()

    publication = run(publish_post(db, live_settings, post, actor_id=1, bot=bot))

    photos = bot.of(SendPhoto)
    assert len(photos) == 1
    assert photos[0].chat_id == int(CHANNEL)
    assert photos[0].caption == f"<b>{TITLE}</b>\n\n{BODY}"
    assert not bot.of(SendMessage)
    # Файл не удаляется сразу: очистка идёт через IMAGE_KEEP_HOURS.
    assert saved.path.exists()
    assert publication.post.image_path == str(saved.path)


def test_publish_respects_silent_flag(db, make_post, tmp_path) -> None:
    settings = make_settings(
        admin_ids=(1,),
        dry_run=False,
        channel_id=CHANNEL,
        publish_silent=True,
        images_dir=tmp_path / "images",
    )
    post = take(db, ready_post(make_post))
    bot = FakeBot()

    run(publish_post(db, settings, post, actor_id=1, bot=bot))

    assert bot.of(SendMessage)[0].disable_notification is True


def test_publish_dry_run_sends_nothing(db, make_post, dry_settings) -> None:
    post = take(db, ready_post(make_post))
    bot = FakeBot()

    publication = run(publish_post(db, dry_settings, post, actor_id=1, bot=bot))

    assert publication.dry_run is True
    assert publication.message_id is None
    assert bot.methods == []
    assert publication.post.status is PostStatus.PUBLISHED


def test_publish_rejects_foreign_admin(db, make_post, live_settings) -> None:
    post = take(db, ready_post(make_post), owner_id=1)
    bot = FakeBot()

    with pytest.raises(PublishError):
        run(publish_post(db, live_settings, post, actor_id=2, bot=bot))

    assert bot.methods == []
    assert run(db.get_post(post.id)).status is PostStatus.INTERCEPTED


def test_publish_rejects_pending_post(db, make_post, live_settings) -> None:
    post = ready_post(make_post)  # не перехвачен
    bot = FakeBot()

    with pytest.raises(PublishError):
        run(publish_post(db, live_settings, post, actor_id=1, bot=bot))

    assert bot.methods == []


def test_publish_accepts_short_text(db, make_post, live_settings) -> None:
    post = take(db, make_post(title_original="Коротко", lead="Мало текста."))
    bot = FakeBot()

    publication = run(
        publish_post(db, live_settings, post, actor_id=1, bot=bot)
    )

    assert bot.of(SendMessage)
    assert publication.post.status is PostStatus.PUBLISHED


def test_publish_with_overlong_caption_sends_text_only(
    db, make_post, live_settings
) -> None:
    saved = run(save_image_bytes(PNG, live_settings))
    # После экранирования «&» → «&amp;» подпись не влезает в 1024 единицы.
    # Это лимит Telegram, а не ТЗ: пост всё равно уходит в канал текстом.
    post = take(
        db,
        ready_post(
            make_post,
            title_original=TITLE,
            lead="&" * 300,
            image_path=str(saved.path),
        ),
    )
    bot = FakeBot()

    publication = run(publish_post(db, live_settings, post, actor_id=1, bot=bot))

    assert bot.of(SendMessage)
    assert not bot.of(SendPhoto)
    assert publication.post.status is PostStatus.PUBLISHED


def test_publish_without_photo_allows_many_ampersands(
    db, make_post, live_settings
) -> None:
    # Без картинки ограничение 4096 символов, экранирование не мешает.
    post = take(db, ready_post(make_post, lead="&" * 300))
    bot = FakeBot()

    run(publish_post(db, live_settings, post, actor_id=1, bot=bot))

    assert bot.of(SendMessage), "текст должен уйти в канал"


def test_publish_requires_channel_id(db, make_post, tmp_path) -> None:
    settings = make_settings(
        admin_ids=(1,), dry_run=False, channel_id="", images_dir=tmp_path / "images"
    )
    post = take(db, ready_post(make_post))
    bot = FakeBot()

    with pytest.raises(PublishError) as excinfo:
        run(publish_post(db, settings, post, actor_id=1, bot=bot))

    assert "CHANNEL_ID" in str(excinfo.value)
    assert bot.methods == []


def test_publish_forbidden_is_explained(db, make_post, live_settings) -> None:
    post = take(db, ready_post(make_post))
    bot = FailingBot(
        TelegramForbiddenError(
            method=SendMessage(chat_id=CHANNEL, text="x"),
            message="Forbidden: bot is not a member of the channel chat",
        )
    )

    with pytest.raises(PublishError) as excinfo:
        run(publish_post(db, live_settings, post, actor_id=1, bot=bot))

    assert "администратор" in str(excinfo.value)
    assert run(db.get_post(post.id)).status is PostStatus.INTERCEPTED


def test_publish_bad_request_is_explained(db, make_post, live_settings) -> None:
    post = take(db, ready_post(make_post))
    bot = FailingBot(
        TelegramBadRequest(
            method=SendMessage(chat_id=CHANNEL, text="x"),
            message="Bad Request: chat not found",
        )
    )

    with pytest.raises(PublishError) as excinfo:
        run(publish_post(db, live_settings, post, actor_id=1, bot=bot))

    assert "chat not found" in str(excinfo.value)
    assert run(db.get_post(post.id)).status is PostStatus.INTERCEPTED


# ----------------------------------------------------------------------
# Кнопка «Опубликовать»
# ----------------------------------------------------------------------
def test_publish_button_marks_post_and_removes_menu(
    db, make_post, dry_settings
) -> None:
    post = take(db, ready_post(make_post))
    bot = FakeBot()
    message = make_message(bot, chat_id=55)
    callback = make_callback(
        bot, post_callback(ACTION_PUBLISH, post.id), message
    )

    run(handle_post_action(callback, bot, db, dry_settings, FakeState()))

    stored = run(db.get_post(post.id))
    assert stored.status is PostStatus.PUBLISHED
    edits = bot.of(EditMessageText)
    assert edits and "опубликован" in (edits[0].text or "")
    assert edits[0].reply_markup is None
    assert any(
        "DRY_RUN" in (answer.text or "") for answer in bot.of(AnswerCallbackQuery)
    ) or any("DRY_RUN" in text for text in bot.texts)
    # В канал ничего не ушло: единственный адресат — админ.
    assert {method.chat_id for method in bot.methods if hasattr(method, "chat_id")} <= {55}


def test_publish_button_with_photo_edits_caption(db, make_post, dry_settings) -> None:
    post = take(db, ready_post(make_post))
    bot = FakeBot()
    message = make_message(bot, chat_id=55, photo=True)
    callback = make_callback(
        bot, post_callback(ACTION_PUBLISH, post.id), message
    )

    run(handle_post_action(callback, bot, db, dry_settings, FakeState()))

    captions = bot.of(EditMessageCaption)
    assert captions and "опубликован" in (captions[0].caption or "")
    assert run(db.get_post(post.id)).status is PostStatus.PUBLISHED


def test_publish_button_accepts_short_text(db, make_post, dry_settings) -> None:
    post = take(db, make_post(title_original="Коротко", lead="Мало."))
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_PUBLISH, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, dry_settings, FakeState()))

    assert not any("Не удалось опубликовать" in text for text in bot.texts)
    assert run(db.get_post(post.id)).status is PostStatus.PUBLISHED


def test_publish_button_second_click_is_refused(db, make_post, dry_settings) -> None:
    post = take(db, ready_post(make_post))
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_PUBLISH, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, dry_settings, FakeState()))
    bot.methods.clear()
    run(handle_post_action(callback, bot, db, dry_settings, FakeState()))

    answers = bot.of(AnswerCallbackQuery)
    assert answers and answers[0].show_alert is True
    assert "не ваш" in (answers[0].text or "")
    assert not bot.of(EditMessageText)
