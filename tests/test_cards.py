"""Тесты карточки поста и меню владельца (задача T08).

Хендлеры вызываются напрямую, без диспетчера: ``FakeBot`` записывает
методы Telegram (``SendMessage``, ``EditMessageText`` и т.д.), а объекты
``Message``/``CallbackQuery`` — настоящие, aiogram-овские, поэтому
проверяется и поведение самих типов (``edit_text``, ``answer``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageCaption,
    EditMessageText,
    SendMessage,
    SendPhoto,
)

from bot.config import Settings
from bot.db import Database
from bot.handlers.cards import (
    EditStates,
    handle_cancel,
    handle_image_wrong_type,
    handle_new_photo,
    handle_new_text,
    handle_post_action,
)
from bot.models import DedupState, PostStatus
from bot.services.cards import (
    ACTION_AI,
    ACTION_BACK,
    ACTION_DROP,
    ACTION_EDIT,
    ACTION_EDIT_IMAGE,
    ACTION_EDIT_TEXT,
    ACTION_NEXT,
    ACTION_PREV,
    ACTION_PUBLISH,
    ACTION_REMOVE_IMAGE,
    ACTION_SOURCE,
    CARD_CAPTION_LIMIT,
    caption_len,
    card_body,
    card_keyboard,
    card_text,
    card_title,
    distinct_draft_versions,
    edit_keyboard,
    image_file,
    keyboard_for,
    parse_post_callback,
    post_callback,
)
from bot.services.images import ImageTooLargeError, ImageTypeError, save_image_bytes
from tests.fakes import (
    PNG,
    FakeBot,
    FakeState,
    callback_datas,
    make_callback,
    make_message,
)
from tests.helpers import T0, make_settings, run


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Настройки с отдельной папкой картинок и двумя админами."""
    return make_settings(admin_ids=(1, 2), images_dir=tmp_path / "images")


def take(db: Database, post, owner_id: int = 1):
    """Перехватить пост и вернуть свежую копию из БД."""
    run(db.mark_intercepted(post.id, owner_id, now=T0))
    stored = run(db.get_post(post.id))
    assert stored is not None
    return stored


# ----------------------------------------------------------------------
# Callback-данные
# ----------------------------------------------------------------------
def test_post_callback_round_trip() -> None:
    assert post_callback(ACTION_PUBLISH, 12) == "post:publish:12"
    assert parse_post_callback("post:publish:12") == (ACTION_PUBLISH, 12)


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "take:1",
        "post:",
        "post:edit",
        "post:edit:0",
        "post:edit:-3",
        "post:edit:abc",
        "post::5",
        ":5",
    ],
)
def test_parse_post_callback_rejects_bad_data(data: str | None) -> None:
    assert parse_post_callback(data) is None


# ----------------------------------------------------------------------
# Текст карточки
# ----------------------------------------------------------------------
def test_card_prefers_draft_and_falls_back_to_original(db, make_post) -> None:
    post = make_post(title_original="Оригинальный заголовок", lead="Оригинальный лид.")
    assert card_title(post) == "Оригинальный заголовок"
    assert card_body(post) == "Оригинальный лид."

    run(
        db.add_draft_version(
            post.id, title="Черновик заголовка", body="Черновик текста."
        )
    )
    stored = run(db.get_post(post.id))
    assert stored is not None
    assert card_title(stored) == "Черновик заголовка"
    assert card_body(stored) == "Черновик текста."


def test_card_title_without_text_shows_id(make_post) -> None:
    post = make_post(title_original="", lead="")
    assert card_title(post) == f"Пост #{post.id}"


def test_card_text_escapes_html(make_post) -> None:
    post = make_post(title_original="GTA <6> & Co", lead="A & B < C")
    text = card_text(post)
    assert "<b>GTA &lt;6&gt; &amp; Co</b>" in text
    assert "A &amp; B &lt; C" in text
    # Никаких «сырых» тегов из пользовательского текста.
    assert "<6>" not in text


def test_card_text_marks_gray_zone(make_post) -> None:
    post = make_post(
        title_original="Похожая новость",
        lead="Лид.",
        dedup_state=DedupState.GRAY_ZONE,
        dedup_score=0.78,
    )
    text = card_text(post)
    assert text.startswith("⚠️ Похоже на уже опубликованное (78%)")
    assert "<b>Похожая новость</b>" in text


def test_card_text_is_cut_to_caption_limit(make_post) -> None:
    post = make_post(title_original="Заголовок", lead="Я" * 5000)
    text = card_text(post)
    assert caption_len(text) <= CARD_CAPTION_LIMIT
    assert "…" not in text
    # Ни одного законченного предложения в лиде нет — незавершённый
    # обрыв вообще не показывается.
    assert text == "<b>Заголовок</b>"
    assert text.count("<b>") == 1 and text.count("</b>") == 1


def test_card_text_cuts_body_at_complete_sentence(make_post) -> None:
    post = make_post(
        title_original="Заголовок",
        lead="Игроки получили обновление. " * 100,
    )
    text = card_text(post)
    assert caption_len(text) <= CARD_CAPTION_LIMIT
    assert "…" not in text
    assert text.endswith(".")


def test_card_text_counts_emoji_as_two_units(make_post) -> None:
    post = make_post(title_original="🎮" * 200, lead="🎮" * 2000)
    text = card_text(post)
    assert caption_len(text) <= CARD_CAPTION_LIMIT
    # В кодовых точках текст короче: Telegram считает UTF-16.
    assert len(text) < CARD_CAPTION_LIMIT


def test_card_text_without_body_is_title_only(make_post) -> None:
    post = make_post(title_original="Только заголовок", lead="")
    assert card_text(post) == "<b>Только заголовок</b>"


# ----------------------------------------------------------------------
# Клавиатуры
# ----------------------------------------------------------------------
def test_card_keyboard_has_all_menu_buttons(make_post) -> None:
    post = make_post()
    assert callback_datas(card_keyboard(post.id)) == [
        post_callback(ACTION_PUBLISH, post.id),
        post_callback(ACTION_AI, post.id),
        post_callback(ACTION_EDIT, post.id),
        post_callback(ACTION_DROP, post.id),
        post_callback(ACTION_PREV, post.id),
        post_callback(ACTION_NEXT, post.id),
        post_callback(ACTION_SOURCE, post.id),
    ]


def test_edit_keyboard_has_back(make_post) -> None:
    post = make_post()
    assert callback_datas(edit_keyboard(post.id)) == [
        post_callback(ACTION_EDIT_TEXT, post.id),
        post_callback(ACTION_EDIT_IMAGE, post.id),
        post_callback(ACTION_REMOVE_IMAGE, post.id),
        post_callback(ACTION_BACK, post.id),
    ]
    assert callback_datas(keyboard_for(post.id, edit_menu=True)) == callback_datas(
        edit_keyboard(post.id)
    )
    assert callback_datas(keyboard_for(post.id)) == callback_datas(
        card_keyboard(post.id)
    )


# ----------------------------------------------------------------------
# Файл картинки и сохранение присланного фото
# ----------------------------------------------------------------------
def test_image_file_returns_none_without_file(make_post, tmp_path: Path) -> None:
    post = make_post(image_path=str(tmp_path / "missing.png"))
    assert image_file(post) is None
    empty = make_post(image_path=None)
    assert image_file(empty) is None


def test_image_file_returns_existing_path(make_post, tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    path.write_bytes(PNG)
    post = make_post(image_path=str(path))
    assert image_file(post) == path


def test_save_image_bytes_writes_file(settings: Settings) -> None:
    saved = run(save_image_bytes(PNG, settings))
    assert saved.mime_type == "image/png"
    assert saved.size_bytes == len(PNG)
    assert saved.path.read_bytes() == PNG
    assert saved.path.parent == settings.images_dir


def test_save_image_bytes_rejects_non_image(settings: Settings) -> None:
    with pytest.raises(ImageTypeError):
        run(save_image_bytes(b"not an image at all", settings))


def test_save_image_bytes_rejects_empty(settings: Settings) -> None:
    with pytest.raises(ImageTypeError):
        run(save_image_bytes(b"", settings))


def test_save_image_bytes_rejects_oversize() -> None:
    small = make_settings(image_max_mb=1)
    with pytest.raises(ImageTooLargeError):
        run(save_image_bytes(PNG + b"\x00" * (2 * 1024 * 1024), small))


# ----------------------------------------------------------------------
# Доступ к меню
# ----------------------------------------------------------------------
def test_foreign_admin_cannot_use_menu(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    message = make_message(bot)
    callback = make_callback(
        bot, post_callback(ACTION_EDIT, post.id), message, user_id=2
    )

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    answers = bot.of(AnswerCallbackQuery)
    assert answers and answers[0].show_alert is True
    assert "не ваш" in (answers[0].text or "")
    assert not bot.of(EditMessageText)
    assert not bot.of(EditMessageCaption)
    assert run(db.get_post(post.id)).owner_id == 1


def test_pending_post_cannot_be_opened(db, make_post, settings) -> None:
    post = make_post()
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_DROP, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    assert bot.of(AnswerCallbackQuery)[0].show_alert is True
    assert run(db.get_post(post.id)).status is PostStatus.PENDING


def test_broken_callback_is_reported(db, settings) -> None:
    bot = FakeBot()
    callback = make_callback(bot, "post:edit:abc", make_message(bot))

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    answers = bot.of(AnswerCallbackQuery)
    assert answers and answers[0].show_alert is True
    assert "устарела" in (answers[0].text or "")


# ----------------------------------------------------------------------
# Действия меню
# ----------------------------------------------------------------------
def test_edit_action_opens_edit_menu(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_EDIT, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    edits = bot.of(EditMessageText)
    assert len(edits) == 1
    assert post_callback(ACTION_EDIT_TEXT, post.id) in callback_datas(
        edits[0].reply_markup
    )


def test_back_returns_main_menu(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_BACK, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    edits = bot.of(EditMessageText)
    assert len(edits) == 1
    assert post_callback(ACTION_PUBLISH, post.id) in callback_datas(
        edits[0].reply_markup
    )


def test_any_action_extends_timeout(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_SOURCE, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    stored = run(db.get_post(post.id))
    assert stored.intercept_deadline > post.intercept_deadline
    assert stored.last_activity_at > post.last_activity_at


def test_source_is_shown_only_to_owner(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    message = make_message(bot, chat_id=555)
    callback = make_callback(bot, post_callback(ACTION_SOURCE, post.id), message)

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    assert bot.texts == [f"Источник: {post.url}"]
    assert {method.chat_id for method in bot.of(SendMessage)} == {555}
    assert run(db.get_post(post.id)).status is PostStatus.INTERCEPTED


# ----------------------------------------------------------------------
# Стрелки вариантов черновика
# ----------------------------------------------------------------------
def _press(
    bot: FakeBot,
    db: Database,
    settings: Settings,
    post,
    action: str,
):
    """Нажать кнопку карточки и вернуть обновлённый пост."""
    callback = make_callback(
        bot, post_callback(action, post.id), make_message(bot)
    )
    run(handle_post_action(callback, bot, db, settings, FakeState()))
    return run(db.get_post(post.id))


def _answer_texts(bot: FakeBot) -> list[str]:
    return [answer.text or "" for answer in bot.of(AnswerCallbackQuery)]


def test_arrows_choose_between_edits(db, make_post, settings) -> None:
    post = take(db, make_post())
    run(db.add_draft_version(post.id, title="Первый", body="Первый вариант."))
    run(db.add_draft_version(post.id, title="Второй", body="Второй вариант."))

    bot = FakeBot()
    stored = _press(bot, db, settings, post, ACTION_PREV)

    assert stored.draft_title == "Первый"
    assert stored.draft_text == "Первый вариант."
    assert "Вариант 1 из 2." in _answer_texts(bot)
    assert "Первый вариант." in (bot.of(EditMessageText)[-1].text or "")

    stored = _press(bot, db, settings, post, ACTION_NEXT)

    assert stored.draft_title == "Второй"
    assert stored.draft_text == "Второй вариант."
    assert "Вариант 2 из 2." in _answer_texts(bot)


def test_arrows_ignore_rollback_copies(db, make_post, settings) -> None:
    """Откат создаёт запись истории, но не превращается в лишний шаг."""
    post = take(db, make_post())
    run(db.add_draft_version(post.id, title="A", body="Вариант A."))
    run(db.add_draft_version(post.id, title="B", body="Вариант B."))

    bot = FakeBot()
    _press(bot, db, settings, post, ACTION_PREV)

    versions = run(db.get_draft_versions(post.id))
    variants = distinct_draft_versions(versions)
    assert len(versions) == 3
    assert [variant.body for variant in variants] == ["Вариант A.", "Вариант B."]

    stored = _press(bot, db, settings, post, ACTION_NEXT)
    assert stored.draft_text == "Вариант B."


def test_arrows_report_boundaries(db, make_post, settings) -> None:
    post = take(db, make_post())
    run(db.add_draft_version(post.id, title="Первый", body="Первый вариант."))
    run(db.add_draft_version(post.id, title="Второй", body="Второй вариант."))

    bot = FakeBot()
    _press(bot, db, settings, post, ACTION_PREV)  # уже на первом

    stored = _press(bot, db, settings, post, ACTION_PREV)
    assert stored.draft_text == "Первый вариант."
    assert "Это первый вариант текста." in _answer_texts(bot)

    _press(bot, db, settings, post, ACTION_NEXT)  # на втором
    stored = _press(bot, db, settings, post, ACTION_NEXT)
    assert stored.draft_text == "Второй вариант."
    assert "Это последний вариант текста." in _answer_texts(bot)


def test_arrows_with_single_variant_explain_nothing_to_switch(
    db, make_post, settings
) -> None:
    post = take(db, make_post())
    run(db.add_draft_version(post.id, title="Единственный", body="Один текст."))

    bot = FakeBot()
    stored = _press(bot, db, settings, post, ACTION_NEXT)

    assert stored.draft_text == "Один текст."
    assert "У поста пока один вариант текста." in _answer_texts(bot)
    assert not bot.of(EditMessageText)


def test_drop_marks_post_dropped_and_edits_message(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    message = make_message(bot, chat_id=777)
    callback = make_callback(bot, post_callback(ACTION_DROP, post.id), message)

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    stored = run(db.get_post(post.id))
    assert stored.status is PostStatus.DROPPED
    assert stored.drop_reason == "manual"
    assert stored.intercept_deadline is None

    edits = bot.of(EditMessageText)
    assert len(edits) == 1
    assert f"Пост #{post.id} снят" in (edits[0].text or "")
    # В канал ничего не ушло: все адресаты — чат владельца.
    assert {method.chat_id for method in bot.methods if hasattr(method, "chat_id")} == {
        777
    }
    assert not bot.of(SendPhoto)


def test_drop_card_with_photo_uses_caption(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    message = make_message(bot, photo=True)
    callback = make_callback(bot, post_callback(ACTION_DROP, post.id), message)

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    captions = bot.of(EditMessageCaption)
    assert len(captions) == 1
    assert f"Пост #{post.id} снят" in (captions[0].caption or "")


def test_remove_image_deletes_file(db, make_post, settings) -> None:
    saved = run(save_image_bytes(PNG, settings))
    post = take(db, make_post(image_path=str(saved.path)))
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_REMOVE_IMAGE, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    assert not saved.path.exists()
    stored = run(db.get_post(post.id))
    assert stored.image_path is None
    edits = bot.of(EditMessageText)
    assert edits and post_callback(ACTION_PUBLISH, post.id) in callback_datas(
        edits[0].reply_markup
    )


def test_edit_text_action_asks_for_text(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    state = FakeState()
    callback = make_callback(
        bot, post_callback(ACTION_EDIT_TEXT, post.id), make_message(bot, chat_id=9)
    )

    run(handle_post_action(callback, bot, db, settings, state))

    assert state.state == EditStates.text.state
    assert state.data["post_id"] == post.id
    assert any("Пришлите новый текст" in text for text in bot.texts)
    assert {m.chat_id for m in bot.of(SendMessage)} == {9}


def test_edit_image_action_uses_configured_limit(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    state = FakeState()
    callback = make_callback(
        bot, post_callback(ACTION_EDIT_IMAGE, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, settings, state))

    assert state.state == EditStates.image.state
    assert any(f"до {settings.image_max_mb} МБ" in text for text in bot.texts)


# ----------------------------------------------------------------------
# Правка текста
# ----------------------------------------------------------------------
def test_new_text_saves_manual_draft(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    message = make_message(bot, text="  Свежий текст новости.  ", chat_id=42)
    state = FakeState(data={"post_id": post.id})

    run(handle_new_text(message, state, bot, db, settings))

    stored = run(db.get_post(post.id))
    assert stored.draft_text == "Свежий текст новости."
    assert stored.rewrite_rounds == 0
    assert stored.intercept_deadline > post.intercept_deadline
    assert state.cleared
    assert len(bot.of(SendMessage)) == 1
    assert "Свежий текст новости." in bot.texts[0]
    assert bot.of(SendMessage)[0].chat_id == 42


def test_new_text_too_long_is_rejected(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    message = make_message(bot, text="Я" * 351)

    run(handle_new_text(message, FakeState(data={"post_id": post.id}), bot, db, settings))

    assert run(db.get_post(post.id)).draft_text == ""
    assert any("длиннее 350" in text for text in bot.texts)


def test_new_text_from_stranger_is_ignored(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    message = make_message(bot, text="Чужой текст", user_id=999)
    state = FakeState(data={"post_id": post.id})

    run(handle_new_text(message, state, bot, db, settings))

    assert run(db.get_post(post.id)).draft_text == ""
    assert state.cleared
    assert bot.methods == []


def test_new_text_for_lost_post_is_cancelled(db, make_post, settings) -> None:
    post = make_post()  # не перехвачен
    bot = FakeBot()
    message = make_message(bot, text="Текст")
    state = FakeState(data={"post_id": post.id})

    run(handle_new_text(message, state, bot, db, settings))

    assert state.cleared
    assert run(db.get_post(post.id)).draft_text == ""
    assert any("уже не ваш" in text for text in bot.texts)


# ----------------------------------------------------------------------
# Замена картинки
# ----------------------------------------------------------------------
def test_new_photo_replaces_image(db, make_post, settings) -> None:
    old = run(save_image_bytes(PNG, settings))
    post = take(db, make_post(image_path=str(old.path)))
    bot = FakeBot(payload=PNG)
    message = make_message(bot, photo=True, chat_id=13)
    state = FakeState(state=EditStates.image.state, data={"post_id": post.id})

    run(handle_new_photo(message, state, bot, db, settings))

    stored = run(db.get_post(post.id))
    assert stored.image_path is not None
    new_path = Path(stored.image_path)
    assert new_path.exists()
    assert new_path.read_bytes() == PNG
    assert not old.path.exists()
    assert stored.intercept_deadline > post.intercept_deadline
    assert state.cleared
    assert len(bot.of(SendPhoto)) == 1
    assert bot.of(SendPhoto)[0].chat_id == 13


def test_new_photo_too_large_is_rejected(db, make_post, settings) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    # Telegram сообщает размер заранее — файл даже не скачиваем.
    message = make_message(
        bot, photo=True, file_size=settings.image_max_bytes + 1
    )

    run(
        handle_new_photo(
            message, FakeState(data={"post_id": post.id}), bot, db, settings
        )
    )

    assert run(db.get_post(post.id)).image_path is None
    assert any("больше" in text for text in bot.texts)
    assert not bot.of(SendPhoto)


def test_new_photo_without_owner_is_cancelled(db, make_post, settings) -> None:
    post = make_post()
    bot = FakeBot()
    state = FakeState(data={"post_id": post.id})

    run(handle_new_photo(make_message(bot, photo=True), state, bot, db, settings))

    assert state.cleared
    assert run(db.get_post(post.id)).image_path is None
    assert any("уже не ваш" in text for text in bot.texts)


def test_image_state_accepts_only_photo(db) -> None:
    bot = FakeBot()

    run(handle_image_wrong_type(make_message(bot, text="просто текст")))

    assert bot.texts and "именно фото" in bot.texts[0]


# ----------------------------------------------------------------------
# Отмена правки
# ----------------------------------------------------------------------
def test_cancel_leaves_edit_mode(db) -> None:
    bot = FakeBot()
    state = FakeState(state=EditStates.text.state, data={"post_id": 5})

    run(handle_cancel(make_message(bot, text="/cancel"), state))

    assert state.cleared
    assert bot.texts == ["Правка отменена."]


def test_cancel_outside_edit_mode_is_silent(db) -> None:
    bot = FakeBot()
    state = FakeState()

    run(handle_cancel(make_message(bot, text="/cancel"), state))

    assert state.cleared
    assert bot.methods == []


# ----------------------------------------------------------------------
# Карточка без картинки и с картинкой отправляется разными методами
# ----------------------------------------------------------------------
def test_send_card_uses_message_without_image(db, make_post, settings) -> None:
    from bot.services.cards import send_card

    post = make_post()
    bot = FakeBot()

    run(send_card(bot, 5, post))

    assert bot.of(SendMessage)[0].chat_id == 5
    assert not bot.of(SendPhoto)


def test_send_card_uses_photo_when_file_exists(db, make_post, settings) -> None:
    from bot.services.cards import send_card

    saved = run(save_image_bytes(PNG, settings))
    post = make_post(image_path=str(saved.path))
    bot = FakeBot()

    run(send_card(bot, 5, post))

    photos = bot.of(SendPhoto)
    assert len(photos) == 1
    assert photos[0].caption.startswith("<b>")
    assert not bot.of(SendMessage)


def test_refresh_card_switches_photo_to_text(db, make_post, settings) -> None:
    from bot.services.cards import refresh_card

    post = make_post()  # без картинки
    bot = FakeBot()
    message = make_message(bot, photo=True)  # старое сообщение было с фото

    run(refresh_card(bot, message, post))

    assert bot.of(DeleteMessage), "фото-сообщение должно быть удалено"
    assert bot.of(SendMessage)
    assert not bot.of(EditMessageCaption)
