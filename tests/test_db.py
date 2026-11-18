"""Тесты хранилища: схема, переходы статусов, очередь, TTL, дедуп, черновики."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from bot.db import (
    SCHEMA_VERSION,
    DuplicatePostError,
    InvalidTransitionError,
    OwnerBusyError,
    PostNotFoundError,
    StorageError,
)
from bot.models import DedupState, DraftOrigin, PostStatus
from tests.helpers import T0, run


# ----------------------------------------------------------------------
# Схема и миграции
# ----------------------------------------------------------------------
def test_schema_created_with_indexes_and_wal(db) -> None:
    connection = sqlite3.connect(db.path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    finally:
        connection.close()

    assert version == SCHEMA_VERSION
    assert str(mode).lower() == "wal"
    assert {"posts", "draft_versions", "seen_urls", "dedup_log"} <= tables
    # Индексы из требований T02: статус, url, guid, created_at + частичные.
    for name in (
        "idx_posts_status",
        "idx_posts_url",
        "idx_posts_guid",
        "idx_posts_created_at",
        "idx_posts_active_owner",
        "idx_draft_versions_post",
        "idx_seen_urls_url",
        "idx_dedup_log_created",
    ):
        assert name in indexes


def test_initialize_is_idempotent(db) -> None:
    run(db.initialize())
    run(db.initialize())
    post = run(db.create_post("https://example.com/one", title_original="Заголовок"))
    assert post.id >= 1


# ----------------------------------------------------------------------
# Создание и чтение
# ----------------------------------------------------------------------
def test_create_post_defaults_and_seen_url(db) -> None:
    post = run(
        db.create_post(
            "https://example.com/gta",
            guid="urn:guid:1",
            source="IGN",
            title_original="GTA 6 снова в новостях",
            lead="Лид.",
        )
    )

    assert post.id > 0
    assert post.status is PostStatus.PENDING
    assert post.owner_id is None
    assert post.draft_title == "" and post.draft_text == ""
    assert post.dedup_state is DedupState.NEW
    assert post.dedup_score is None
    assert post.rewrite_rounds == 0
    assert post.created_at is not None

    loaded = run(db.get_post(post.id))
    assert loaded == post
    assert run(db.get_post_by_url("https://example.com/gta")) == post
    assert run(db.get_post(999_999)) is None
    assert run(db.get_post_by_url("https://example.com/missing")) is None

    # Создание поста автоматически закрывает ступень 1 дедупликации.
    assert run(db.is_url_seen("https://example.com/gta")) is True
    seen = run(db.get_seen_url("https://example.com/gta"))
    assert seen is not None and seen.post_id == post.id


def test_duplicate_url_is_rejected(db) -> None:
    run(db.create_post("https://example.com/dup"))
    with pytest.raises(DuplicatePostError):
        run(db.create_post("https://example.com/dup"))


def test_dedup_fields_are_persisted(db) -> None:
    post = run(
        db.create_post(
            "https://example.com/gray",
            dedup_score=0.77,
            dedup_state=DedupState.GRAY_ZONE,
        )
    )
    assert post.dedup_score == pytest.approx(0.77)
    assert post.dedup_state is DedupState.GRAY_ZONE


# ----------------------------------------------------------------------
# Очередь
# ----------------------------------------------------------------------
def test_pending_queue_limit_and_order(db, make_post, at) -> None:
    posts = [
        make_post(created_at=at(timedelta(minutes=minute)))
        for minute in range(1, 6)
    ]
    assert run(db.count_pending()) == 5

    newest = run(db.list_pending())
    assert [post.id for post in newest] == [posts[4].id, posts[3].id, posts[2].id]

    fifo = run(db.list_pending(newest_first=False))
    assert [post.id for post in fifo] == [posts[0].id, posts[1].id, posts[2].id]

    limited = run(db.list_pending(limit=2, newest_first=False))
    assert [post.id for post in limited] == [posts[0].id, posts[1].id]


# ----------------------------------------------------------------------
# Переходы статусов
# ----------------------------------------------------------------------
def test_intercept_touch_and_publish(db, make_post, at) -> None:
    post = make_post()
    intercepted = run(db.mark_intercepted(post.id, 42, now=at(timedelta())))

    assert intercepted.status is PostStatus.INTERCEPTED
    assert intercepted.owner_id == 42
    assert intercepted.intercepted_at == at(timedelta())
    assert intercepted.intercept_deadline == at(timedelta(minutes=10))
    assert intercepted.last_activity_at == at(timedelta())

    touched = run(
        db.touch_post(post.id, minutes=15, now=at(timedelta(minutes=2)))
    )
    assert touched.intercept_deadline == at(timedelta(minutes=17))
    assert touched.last_activity_at == at(timedelta(minutes=2))

    published = run(db.mark_published(post.id, now=at(timedelta(minutes=3))))
    assert published.status is PostStatus.PUBLISHED
    assert published.published_at == at(timedelta(minutes=3))
    assert published.intercept_deadline is None
    assert published.owner_id == 42  # кто публиковал, остаётся в истории


def test_drop_with_reason(db, make_post, at) -> None:
    post = make_post()
    dropped = run(db.mark_dropped(post.id, reason="admin_declined", now=at(timedelta())))
    assert dropped.status is PostStatus.DROPPED
    assert dropped.drop_reason == "admin_declined"
    assert dropped.dropped_at == at(timedelta())
    assert dropped.is_finished


def test_invalid_transitions_raise(db, make_post) -> None:
    post = make_post()
    run(db.mark_published(post.id))

    with pytest.raises(InvalidTransitionError):
        run(db.mark_published(post.id))
    with pytest.raises(InvalidTransitionError):
        run(db.mark_dropped(post.id))
    with pytest.raises(InvalidTransitionError):
        run(db.mark_intercepted(post.id, 42))

    with pytest.raises(PostNotFoundError):
        run(db.mark_intercepted(999_999, 42))


def test_one_active_post_per_owner(db, make_post) -> None:
    first = make_post()
    second = make_post()
    run(db.mark_intercepted(first.id, 42))

    with pytest.raises(OwnerBusyError):
        run(db.mark_intercepted(second.id, 42))

    active = run(db.find_active_post(42))
    assert active is not None and active.id == first.id

    run(db.mark_dropped(first.id, reason="admin_declined"))
    run(db.mark_intercepted(second.id, 42))
    assert run(db.find_active_post(42)).id == second.id  # type: ignore[union-attr]


def test_reset_to_pending_clears_owner(db, make_post, at) -> None:
    post = make_post()
    run(db.mark_intercepted(post.id, 42, now=at(timedelta())))

    released = run(db.reset_to_pending(post.id, now=at(timedelta(minutes=11))))
    assert released.status is PostStatus.PENDING
    assert released.owner_id is None
    assert released.intercepted_at is None
    assert released.intercept_deadline is None
    assert released.last_activity_at == at(timedelta(minutes=11))
    assert run(db.find_active_post(42)) is None

    with pytest.raises(InvalidTransitionError):
        run(db.reset_to_pending(post.id))


# ----------------------------------------------------------------------
# TTL и тайм-ауты перехвата
# ----------------------------------------------------------------------
def test_ttl_expiry(db, make_post, at) -> None:
    old = make_post(created_at=at(-timedelta(hours=49)))
    fresh = make_post(created_at=at(-timedelta(hours=1)))

    candidates = run(db.list_expired_pending(now=at(timedelta())))
    assert [post.id for post in candidates] == [old.id]
    assert candidates[0].status is PostStatus.PENDING

    expired = run(db.expire_pending(now=at(timedelta())))
    assert [post.id for post in expired] == [old.id]
    assert expired[0].status is PostStatus.DROPPED
    assert expired[0].drop_reason == "ttl"
    assert expired[0].dropped_at == at(timedelta())

    assert run(db.get_post(old.id)).status is PostStatus.DROPPED  # type: ignore[union-attr]
    assert run(db.get_post(fresh.id)).status is PostStatus.PENDING  # type: ignore[union-attr]
    assert run(db.count_pending()) == 1


def test_intercept_timeout_and_release(db, make_post, at) -> None:
    post = make_post()
    run(db.mark_intercepted(post.id, 42, now=at(timedelta())))

    assert run(db.list_timed_out(now=at(timedelta(minutes=5)))) == []

    timed_out = run(db.list_timed_out(now=at(timedelta(minutes=10))))
    assert [item.id for item in timed_out] == [post.id]
    assert timed_out[0].status is PostStatus.INTERCEPTED

    released = run(db.release_timed_out(now=at(timedelta(minutes=11))))
    assert [item.id for item in released] == [post.id]
    assert released[0].status is PostStatus.PENDING
    assert released[0].owner_id is None
    assert released[0].intercept_deadline is None

    assert run(db.get_post(post.id)).status is PostStatus.PENDING  # type: ignore[union-attr]
    assert run(db.release_timed_out(now=at(timedelta(minutes=12)))) == []


# ----------------------------------------------------------------------
# Дедупликация: ступень 1 и лог
# ----------------------------------------------------------------------
def test_seen_urls_by_url_and_guid(db) -> None:
    assert run(db.is_url_seen("https://example.com/a")) is False

    run(db.mark_url_seen("https://example.com/a", guid="guid-a", source="VGC"))
    assert run(db.is_url_seen("https://example.com/a")) is True
    assert run(db.is_url_seen("https://example.com/other", guid="guid-a")) is True
    assert run(db.is_url_seen("https://example.com/other", guid="guid-b")) is False

    # Повторная запись не падает и не создаёт дубликат.
    run(db.mark_url_seen("https://example.com/a", guid="guid-a"))
    seen = run(db.get_seen_url("https://example.com/a"))
    assert seen is not None and seen.post_id is None
    assert seen.source == "VGC"


def test_dedup_log_tracks_gray_zone(db, make_post) -> None:
    post = make_post()
    run(
        db.add_dedup_log(
            candidate_title="GTA 6 выйдет в 2026",
            compared_title="GTA 6 выйдет в 2025",
            score=0.72,
            decision=DedupState.GRAY_ZONE,
            post_id=post.id,
            created_at=T0,
        )
    )
    run(
        db.add_dedup_log(
            candidate_title="Совсем другой заголовок",
            compared_title="Rockstar анонсировала GTA 6",
            score=0.35,
            decision=DedupState.NEW,
            created_at=T0 + timedelta(minutes=1),
        )
    )

    entries = run(db.list_dedup_log())
    assert len(entries) == 2
    assert entries[0].score == pytest.approx(0.35)  # свежие первыми
    assert entries[1].decision is DedupState.GRAY_ZONE
    assert entries[1].post_id == post.id
    assert entries[1].created_at == T0

    assert len(run(db.list_dedup_log(limit=1))) == 1


def test_dedup_profile_window(db, make_post, at) -> None:
    make_post(created_at=at(-timedelta(days=8)))   # старше окна
    pending = make_post(created_at=at(-timedelta(days=1)))
    published = make_post(created_at=at(-timedelta(days=2)))
    dropped = make_post(created_at=at(-timedelta(days=3)))

    run(db.mark_published(published.id, now=at(timedelta())))
    run(db.mark_dropped(dropped.id, reason="ttl", now=at(timedelta())))

    profile = run(db.list_for_dedup(now=at(timedelta())))
    assert {post.id for post in profile} == {pending.id, published.id}
    assert [post.created_at for post in profile] == sorted(
        (post.created_at for post in profile), reverse=True
    )


# ----------------------------------------------------------------------
# История черновиков
# ----------------------------------------------------------------------
def test_draft_versions_and_rewrite_rounds(db, make_post) -> None:
    post = make_post()

    first = run(
        db.add_draft_version(
            post.id, title="Заголовок", body="Первый текст", origin=DraftOrigin.MANUAL
        )
    )
    second = run(
        db.add_draft_version(
            post.id, title="Заголовок 2", body="Второй текст", origin=DraftOrigin.AI
        )
    )
    third = run(
        db.add_draft_version(
            post.id, title="Заголовок 3", body="Третий текст", origin=DraftOrigin.AI
        )
    )

    assert [first.version_no, second.version_no, third.version_no] == [1, 2, 3]
    assert [first.round_no, second.round_no, third.round_no] == [0, 1, 2]

    versions = run(db.get_draft_versions(post.id))
    assert len(versions) == 3
    # Объекты — снимки на момент вставки, поэтому «текущую» версию смотрим в БД.
    assert [v.is_current for v in versions] == [False, False, True]
    assert run(db.get_current_draft(post.id)).version_no == 3  # type: ignore[union-attr]
    assert run(db.count_rewrite_rounds(post.id)) == 2

    updated = run(db.get_post(post.id))
    assert updated is not None
    assert updated.draft_title == "Заголовок 3"
    assert updated.draft_text == "Третий текст"
    assert updated.rewrite_rounds == 2


def test_rollback_creates_new_current_version(db, make_post) -> None:
    post = make_post()
    run(db.add_draft_version(post.id, title="A", body="Текст A"))
    run(
        db.add_draft_version(
            post.id, title="B", body="Текст B", origin=DraftOrigin.AI
        )
    )

    rolled_back = run(db.rollback_draft(post.id, 1))
    assert rolled_back.version_no == 3
    assert rolled_back.origin is DraftOrigin.ROLLBACK
    assert rolled_back.title == "A" and rolled_back.body == "Текст A"
    assert rolled_back.is_current

    reloaded = run(db.get_post(post.id))
    assert reloaded is not None
    assert reloaded.draft_text == "Текст A"
    assert reloaded.rewrite_rounds == 1  # откат не сбрасывает счётчик кругов
    assert run(db.count_rewrite_rounds(post.id)) == 1

    with pytest.raises(StorageError):
        run(db.rollback_draft(post.id, 99))


def test_draft_version_for_missing_post(db) -> None:
    with pytest.raises(PostNotFoundError):
        run(db.add_draft_version(999_999, title="x", body="y"))


# ----------------------------------------------------------------------
# Картинки
# ----------------------------------------------------------------------
def test_set_and_clear_image(db, make_post) -> None:
    post = make_post(image_url="https://example.com/cover.jpg")

    with_image = run(
        db.set_image(post.id, image_path="data/images/1.jpg")
    )
    assert with_image.image_path == "data/images/1.jpg"
    assert with_image.image_url == "https://example.com/cover.jpg"

    cleared = run(db.set_image(post.id, image_path=None))
    assert cleared.image_path is None
    # URL источника сохраняется: по нему можно перезагрузить картинку.
    assert cleared.image_url == "https://example.com/cover.jpg"
