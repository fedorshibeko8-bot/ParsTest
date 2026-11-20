"""Тесты конвейера новости (задача T11).

Проверяется порядок «фильтр → дедупликация → пост → картинка → анонс»,
ограничение первой загрузки лент и то, что сбой на любом шаге не теряет
уже созданный пост. Отдельно — рассылка пачкой: при всплеске новостей
админы получают не больше видимой очереди уведомлений за проход.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiogram.methods import SendMessage

from bot.config import Settings
from bot.db import Database
from bot.filters.dedup import DedupVerdict
from bot.models import DedupState, PostStatus
from bot.parsers.rss import FeedItem, PollOutcome
from bot.services import pipeline
from bot.services.images import save_image_bytes
from bot.services.pipeline import handle_feed_item, handle_feed_items
from bot.services.queue import take_callback
from tests.fakes import PNG, FakeBot
from tests.helpers import T0, make_settings, run

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


def test_source_published_at_is_kept_for_notification(db, settings) -> None:
    bot = FakeBot()

    post = run(
        handle_feed_item(bot, db, settings, make_item(published_at=T0))
    )

    assert post is not None
    assert post.source_published_at == T0
    assert "Оригинал опубликован: 18.11.2026 12:00 UTC" in bot.texts[0]


def test_news_is_queued_even_when_all_admins_are_busy(
    db, settings, make_post
) -> None:
    """Все админы заняты: пост создаётся, но уведомление не уходит никому."""
    busy = make_post(title_original="Аниме-фестиваль перенесли", lead="")
    run(db.mark_intercepted(busy.id, 1, now=T0))
    bot = FakeBot()

    post = run(
        handle_feed_item(
            bot,
            db,
            settings,
            make_item(url="https://example.com/gta6/busy", guid="vgc-busy"),
        )
    )

    assert post is not None
    assert post.status is PostStatus.PENDING
    # Новость сохранена в очереди, но личных сообщений нет.
    assert [pending.id for pending in run(db.list_pending())] == [post.id]
    assert bot.texts == []


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


# ----------------------------------------------------------------------
# Пачка новостей и ограничение первой загрузки
# ----------------------------------------------------------------------
@pytest.fixture()
def always_new(monkeypatch):
    """Не тратить время на дедупликацию: все записи считаются новыми."""

    async def fake_check(*args, **kwargs):
        return DedupVerdict(DedupState.NEW)

    monkeypatch.setattr(pipeline, "check_news", fake_check)


SOURCE_NAMES = ("VGC", "IGN", "Eurogamer", "GamesRadar+", "GameSpot")


def many_items(count: int, *, start: int = 1) -> list[FeedItem]:
    return [
        make_item(
            url=f"https://example.com/gta6/{index}",
            guid=f"vgc-{index}",
            source=SOURCE_NAMES[(index - 1) % len(SOURCE_NAMES)],
            title=f"GTA 6, свежая новость номер {index}",
        )
        for index in range(start, start + count)
    ]


def test_batch_announces_only_newest_visible_posts(
    db, settings, always_new
) -> None:
    bot = FakeBot()

    created = run(handle_feed_items(bot, db, settings, many_items(15)))

    assert len(created) == 15
    sent = bot.of(SendMessage)
    assert len(sent) == settings.queue_visible_limit
    visible = created[-settings.queue_visible_limit :]
    for message in sent:
        # Каждое уведомление указывает номер поста из видимой десятки.
        assert any(f"#{post.id}" in message.text for post in visible)


def test_first_pass_budget_caps_created_posts(db, settings, always_new) -> None:
    budget = pipeline.ImportBudget(3)
    bot = FakeBot()

    created = run(
        handle_feed_items(bot, db, settings, many_items(10), budget=budget)
    )

    assert len(created) == 3
    assert budget.left == 0
    assert run(db.count_pending()) == 3
    assert len(bot.of(SendMessage)) == 3


def test_import_budget_is_not_spent_on_irrelevant(db, settings) -> None:
    budget = pipeline.ImportBudget(1)
    bot = FakeBot()
    sport = make_item(
        url="https://example.com/sport",
        title="Футбол: новый сезон",
        lead="Матч перенесён на субботу.",
    )

    created = run(handle_feed_items(bot, db, settings, [sport], budget=budget))

    assert created == []
    assert budget.left == 1


def test_import_budget_is_not_spent_on_duplicate_url(
    db, settings, always_new
) -> None:
    budget = pipeline.ImportBudget(2)
    bot = FakeBot()

    run(handle_feed_items(bot, db, settings, [make_item()], budget=budget))
    assert budget.left == 1

    # Тот же URL: create_post отвергает дубль, место не тратится.
    again = run(handle_feed_items(bot, db, settings, [make_item()], budget=budget))

    assert again == []
    assert budget.left == 1
    assert run(db.count_pending()) == 1


def test_poll_news_loop_announces_batch_and_stops(
    db, settings, always_new, monkeypatch
) -> None:
    items = many_items(12)
    stop = asyncio.Event()

    async def fake_poll(db_, settings_, *, sources=None, on_item=None):
        for item in items:
            await on_item(item)
        stop.set()
        return [
            PollOutcome(source=source.name, entries=len(items))
            for source in sources
        ]

    monkeypatch.setattr(pipeline, "poll_all_sources", fake_poll)
    monkeypatch.setattr(pipeline, "jitter_delay", lambda settings_: 0.0)
    bot = FakeBot()

    async def scenario() -> None:
        await asyncio.wait_for(
            pipeline.poll_news_loop(bot, db, settings, stop_event=stop), timeout=5
        )

    run(scenario())

    # Первый проход ограничен видимой очередью — без сотни сообщений.
    assert run(db.count_pending()) == settings.queue_visible_limit
    assert len(bot.of(SendMessage)) == settings.queue_visible_limit
    assert stop.is_set()


def test_second_pass_is_not_limited_by_import_budget(
    db, settings, always_new, monkeypatch
) -> None:
    first = many_items(12)
    second = many_items(3, start=100)
    rounds = {"n": 0}
    stop = asyncio.Event()

    async def fake_poll(db_, settings_, *, sources=None, on_item=None):
        rounds["n"] += 1
        batch = first if rounds["n"] == 1 else second
        for item in batch:
            await on_item(item)
        if rounds["n"] == 2:
            stop.set()
        return [
            PollOutcome(source=source.name, entries=len(batch))
            for source in sources
        ]

    monkeypatch.setattr(pipeline, "poll_all_sources", fake_poll)
    monkeypatch.setattr(pipeline, "jitter_delay", lambda settings_: 0.0)
    bot = FakeBot()

    async def scenario() -> None:
        await asyncio.wait_for(
            pipeline.poll_news_loop(bot, db, settings, stop_event=stop), timeout=5
        )

    run(scenario())

    # 10 из первой пачки + все 3 из второй: история режется только один раз.
    assert rounds["n"] == 2
    assert run(db.count_pending()) == 13
    assert len(bot.of(SendMessage)) == 13


def test_retry_after_snoozes_only_that_source(
    db, settings, always_new, monkeypatch
) -> None:
    stop = asyncio.Event()
    calls: list[set[str]] = []

    async def fake_poll(db_, settings_, *, sources=None, on_item=None):
        calls.append({source.key for source in sources})
        if len(calls) == 1:
            return [
                PollOutcome(source="VGC", entries=0, error="429", retry_after=3600.0)
            ]
        stop.set()
        return [PollOutcome(source=source.name, entries=0) for source in sources]

    monkeypatch.setattr(pipeline, "poll_all_sources", fake_poll)
    monkeypatch.setattr(pipeline, "jitter_delay", lambda settings_: 0.0)

    async def scenario() -> None:
        await asyncio.wait_for(
            pipeline.poll_news_loop(FakeBot(), db, settings, stop_event=stop),
            timeout=5,
        )

    run(scenario())

    assert calls[0] == {"vgc", "ign", "eurogamer", "gamesradar", "gamespot"}
    # 429 от VGC не останавливает остальные ленты.
    assert "vgc" not in calls[1]
    assert {"ign", "eurogamer", "gamesradar", "gamespot"} <= calls[1]


def test_loop_without_enabled_sources_returns(
    db, settings, monkeypatch
) -> None:
    monkeypatch.setattr(pipeline, "SOURCES", ())

    async def scenario() -> None:
        await asyncio.wait_for(
            pipeline.poll_news_loop(FakeBot(), db, settings, stop_event=asyncio.Event()),
            timeout=5,
        )

    run(scenario())
