"""Тесты моделей и статусов (без БД)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from bot.models import (
    DedupState,
    DraftOrigin,
    PostStatus,
    from_iso,
    to_iso,
    utcnow,
)


def test_status_values_match_contract() -> None:
    assert {status.value for status in PostStatus} == {
        "pending",
        "intercepted",
        "published",
        "dropped",
    }
    assert {state.value for state in DedupState} == {"new", "duplicate", "gray_zone"}
    assert {origin.value for origin in DraftOrigin} == {
        "original",
        "ai",
        "manual",
        "rollback",
    }


def test_iso_roundtrip_keeps_utc() -> None:
    moment = datetime(2026, 11, 18, 12, 30, 15, tzinfo=timezone.utc)
    restored = from_iso(to_iso(moment))
    assert restored == moment
    assert restored is not None and restored.tzinfo is not None


def test_iso_naive_treated_as_utc() -> None:
    restored = from_iso(to_iso(datetime(2026, 11, 18, 12, 0)))
    assert restored == datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def test_iso_none_and_empty() -> None:
    assert to_iso(None) is None
    assert from_iso(None) is None
    assert from_iso("") is None


def test_utcnow_has_timezone() -> None:
    assert utcnow().tzinfo is not None


def test_post_predicates(make_post) -> None:
    post = make_post()
    assert post.is_pending and not post.is_intercepted
    assert not post.is_finished
    assert not post.has_draft
    assert not post.is_expired(48, now=post.created_at)

    expired = post.is_expired(48, now=post.created_at + timedelta(hours=48))
    assert expired  # ровно на границе TTL пост уже просрочен

    with_draft = replace(post, draft_title="Заголовок", draft_text="Текст")
    assert with_draft.has_draft

    finished = replace(post, status=PostStatus.PUBLISHED)
    assert finished.is_finished and not finished.is_expired(48)


def test_intercept_timeout_predicate(make_post) -> None:
    post = make_post()
    intercepted = replace(
        post,
        status=PostStatus.INTERCEPTED,
        owner_id=42,
        intercept_deadline=post.created_at + timedelta(minutes=10),
    )
    assert intercepted.is_intercepted
    assert not intercepted.is_intercept_timed_out(now=post.created_at)
    assert intercepted.is_intercept_timed_out(
        now=post.created_at + timedelta(minutes=10)
    )
    assert not replace(post, status=PostStatus.PENDING).is_intercept_timed_out()
