"""Тесты конвейера новости (задача T11).

Проверяется порядок «фильтр → дедупликация → пост → картинка → анонс»
и то, что сбой на любом шаге не теряет уже созданный пост.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiogram.methods import SendMessage

from bot.config import Settings
from bot.db import Database
from bot.filters.dedup import DedupVerdict
from bot.models import DedupState, PostStatus
from bot.parsers.rss import FeedItem
from bot.services import pipeline
from bot.services.images import save_image_bytes
from bot.services.pipeline import handle_feed_item
from bot.services.queue import take_callback
from tests.fakes import PNG, FakeBot
from tests.helpers import make_settings, run

NEWS_TITLE = "Rockstar показала новый кадр GTA 6 и дату релиза"
NEWS_LEAD = "Студия Rockstar Games показала новый кадр из GTA 6 и дату выхода."


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return make_settings(
        admin_ids=(1,), images_dir=tmp_path / "images", queue_visible_limit=10
    )


def make_item(**overrides) -> FeedItem:
    params = {
        "url": "https://example.com/gta6/1",
        "source": "VGC",
        "title": NEWS_TITLE,
        "guid": "vgc-1",
        "lead": NEWS_LEAD,
        "image_url": None,
    }
    params.update(overrides)
    return FeedItem(**params)


def callback_datas(markup) -> list[str]:
    return [
        button.callback_data for row in markup.inline_keyboard for button in row
    ]


# ----------------------------------------------------------------------
# Фильтр
# ----------------------------------------------------------------------
def test_irrelevant_news_is_skipped(db, settings) -> None:
    bot = FakeBot()
    item = make_item(title="Футбол: новый сезон", lead="Матч перенесён.")

    result = run(handle_feed_item(bot, db, settings, item))

    assert result is None
    assert run(db.list_pending()) == []
    assert bot.methods == []


def test_gta_news_reaches_queue(db, settings) -> None:
    bot = FakeBot()

    post = run(handle_feed_item(bot, db, settings, make_item()))

    assert post is not None
    assert post.status is PostStatus.PENDING
    assert post.source == "VGC"
    assert post.title_original == NEWS_TITLE
    assert post.dedup_state is DedupState.NEW
    assert run(db.list_pending()) == [post]


# ----------------------------------------------------------------------
# Дедупликация
# ----------------------------------------------------------------------
def test_same_news_twice_is_not_queued_twice(db, settings) -> None:
    bot = FakeBot()
    item = make_item()

    first = run(handle_feed_item(bot, db, settings, item))
    second = run(
        handle_feed_item(bot, db, settings, make_item(url="https://example.com/gta6/2"))
    )

    assert first is not None
    assert second is None
    assert len(run(db.list_pending())) == 1


def test_existing_url_is_ignored_even_with_new_title(db, settings, monkeypatch) -> None:
    bot = FakeBot()
    run(handle_feed_item(bot, db, settings, make_item()))
    run(db.mark_url_seen("https://example.com/gta6/1", guid="vgc-1", source="VGC"))

    result = run(
        handle_feed_item(
            bot,
            db,
            settings,
            make_item(url="https://example.com/gta6/1", title="Совсем другой заголовок GTA 6"),
        )
    )

    assert result is None
    assert len(run(db.list_pending())) == 1


def test_gray_zone_is_still_queued_with_score(db, settings, monkeypatch) -> None:
    verdict = DedupVerdict(DedupState.GRAY_ZONE, score=0.78)
    called: dict = {}

    async def fake_check(db_, settings_, **kwargs):
        called.update(kwargs)
        return verdict

    monkeypatch.setattr(pipeline, "check_news", fake_check)
    bot = FakeBot()

    post = run(handle_feed_item(bot, db, settings, make_item()))

    assert post is not None
    assert post.dedup_state is DedupState.GRAY_ZONE
    assert post.dedup_score == 0.78
    # Гайд: дедупликация получает оригинальные поля записи.
    assert called["url"] == "https://example.com/gta6/1"
    assert called["guid"] == "vgc-1"
    assert called["title"] == NEWS_TITLE
    assert called["lead"] == NEWS_LEAD


def test_duplicate_verdict_is_skipped(db, settings, monkeypatch) -> None:
    async def fake_check(*args, **kwargs):
        return DedupVerdict(DedupState.DUPLICATE)

    monkeypatch.setattr(pipeline, "check_news", fake_check)
    bot = FakeBot()

    assert run(handle_feed_item(bot, db, settings, make_item())) is None
    assert run(db.list_pending()) == []


# ----------------------------------------------------------------------
# Картинка
# ----------------------------------------------------------------------
def test_image_is_saved_to_post(db, settings, monkeypatch) -> None:
    saved = run(save_image_bytes(PNG, settings))
    seen: dict = {}

    async def fake_download(url, settings_, **kwargs):
        seen["url"] = url
        return saved

    monkeypatch.setattr(pipeline, "try_download_image", fake_download)
    bot = FakeBot()

    post = run(
        handle_feed_item(
            bot, db, settings, make_item(image_url="https://cdn.example.com/a.png")
        )
    )

    assert post is not None
    assert post.image_path == str(saved.path)
    assert post.image_url == "https://cdn.example.com/a.png"
    assert seen["url"] == "https://cdn.example.com/a.png"


def test_image_failure_keeps_news(db, settings, monkeypatch) -> None:
    async def fake_download(url, settings_, **kwargs):
        return None

    monkeypatch.setattr(pipeline, "try_download_image", fake_download)
    bot = FakeBot()

    post = run(
        handle_feed_item(
            bot, db, settings, make_item(image_url="https://cdn.example.com/a.png")
        )
    )

    assert post is not None
    assert post.image_path is None
    assert len(run(db.list_pending())) == 1


# ----------------------------------------------------------------------
# Анонс
# ----------------------------------------------------------------------
def test_admin_gets_announcement_with_take_button(db, settings) -> None:
    bot = FakeBot()

    post = run(handle_feed_item(bot, db, settings, make_item()))

    assert post is not None
    sent = bot.of(SendMessage)
    assert len(sent) == 1
    assert sent[0].chat_id == 1
    assert take_callback(post.id) in callback_datas(sent[0].reply_markup)
    # В канал ничего не ушло: адресат — только админ.
    assert {method.chat_id for method in bot.methods if hasattr(method, "chat_id")} == {1}


def test_announcement_failure_keeps_post_pending(db, settings, monkeypatch) -> None:
    async def broken_announce(*args, **kwargs):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(pipeline, "announce_posts", broken_announce)
    bot = FakeBot()

    post = run(handle_feed_item(bot, db, settings, make_item()))

    assert post is not None
    assert run(db.get_post(post.id)).status is PostStatus.PENDING
