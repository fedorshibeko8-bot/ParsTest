"""Тесты дедупликации (задача T05).

Проверяем чистую логику (нормализация, схожесть, пороги, выбор лучшего
совпадения) и три ступени на временной БД: URL/GUID, заголовки, история
pending/published за 7 дней, запись серых зон в ``dedup_log``.
"""

from __future__ import annotations

from datetime import timedelta

from bot.filters.dedup import (
    DedupVerdict,
    check_news,
    classify,
    compare_title,
    normalize_title,
    similarity,
)
from bot.models import DedupState
from tests.helpers import T0, make_settings, run


# ----------------------------------------------------------------------
# Нормализация и схожесть
# ----------------------------------------------------------------------
def test_normalize_title_removes_case_punctuation_and_extra_spaces() -> None:
    assert (
        normalize_title("GTA 6: Trailer Breaks All Records! (VGC)")
        == "gta 6 trailer breaks all records vgc"
    )


def test_normalize_title_unifies_dashes_and_yo() -> None:
    assert normalize_title("GTA—6: Ёлка") == "gta 6 елка"


def test_normalize_title_empty() -> None:
    assert normalize_title("   ") == ""


def test_similarity_identical_after_normalization() -> None:
    assert similarity("GTA 6 trailer breaks records!", "gta-6 trailer breaks records") == 1.0


def test_similarity_empty_is_zero() -> None:
    assert similarity("", "GTA 6") == 0.0
    assert similarity("GTA 6", "") == 0.0


def test_similarity_unrelated_is_low() -> None:
    assert similarity("GTA 6 trailer", "Nintendo Switch sales report") < 0.5


# ----------------------------------------------------------------------
# Пороги
# ----------------------------------------------------------------------
def test_classify_boundaries() -> None:
    kwargs = {"duplicate_threshold": 0.85, "gray_zone_threshold": 0.70}
    assert classify(0.85, **kwargs) is DedupState.DUPLICATE
    assert classify(0.99, **kwargs) is DedupState.DUPLICATE
    assert classify(0.70, **kwargs) is DedupState.GRAY_ZONE
    assert classify(0.84, **kwargs) is DedupState.GRAY_ZONE
    assert classify(0.69, **kwargs) is DedupState.NEW


# ----------------------------------------------------------------------
# Сравнение с историей (чистая функция)
# ----------------------------------------------------------------------
def test_compare_title_without_history_is_new() -> None:
    verdict = compare_title(
        "GTA 6", [], duplicate_threshold=0.85, gray_zone_threshold=0.70
    )
    assert verdict == DedupVerdict(DedupState.NEW)
    assert verdict.is_new


def test_compare_title_picks_best_match(make_post) -> None:
    low = make_post(title_original="GTA 6 release date announced")
    high = make_post(title_original="GTA 6 release date announced by Rockstar")

    verdict = compare_title(
        "GTA 6 release date announced by Rockstar",
        [low, high],
        duplicate_threshold=0.85,
        gray_zone_threshold=0.70,
    )

    assert verdict.state is DedupState.DUPLICATE
    assert verdict.matched_post is not None
    assert verdict.matched_post.id == high.id
    assert verdict.score == 1.0


def test_compare_title_gray_zone(make_post) -> None:
    post = make_post(title_original="GTA 6 release date announced")
    verdict = compare_title(
        "GTA 6 release date announced by Rockstar",
        [post],
        duplicate_threshold=0.85,
        gray_zone_threshold=0.70,
    )
    assert verdict.state is DedupState.GRAY_ZONE
    assert verdict.score is not None and 0.70 <= verdict.score < 0.85


# ----------------------------------------------------------------------
# Ступень 1: URL/GUID
# ----------------------------------------------------------------------
def test_check_news_duplicate_by_url(db) -> None:
    url = "https://example.com/news/seen"
    run(db.mark_url_seen(url, guid="guid-seen", source="VGC"))

    verdict = run(check_news(db, make_settings(), title="Совсем другая новость", url=url))

    assert verdict.is_duplicate
    assert verdict.score is None


def test_check_news_duplicate_by_guid(db) -> None:
    url = "https://example.com/news/other"
    run(db.mark_url_seen("https://example.com/news/first", guid="guid-1", source="VGC"))

    verdict = run(
        check_news(db, make_settings(), title="Новость", url=url, guid="guid-1")
    )

    assert verdict.is_duplicate


def test_check_news_new_url_and_title(db) -> None:
    verdict = run(
        check_news(
            db,
            make_settings(),
            title="GTA 6 trailer breaks records",
            url="https://example.com/news/new",
        )
    )
    assert verdict.is_new


# ----------------------------------------------------------------------
# Ступень 2–3: заголовки и история
# ----------------------------------------------------------------------
def test_check_news_duplicate_by_title_is_silent(db, make_post) -> None:
    make_post(title_original="GTA 6 trailer breaks records")

    verdict = run(
        check_news(
            db,
            make_settings(),
            title="GTA 6: Trailer Breaks Records!",
            url="https://example.com/news/dup",
        )
    )

    assert verdict.is_duplicate
    assert run(db.list_dedup_log(limit=10)) == []


def test_check_news_gray_zone_is_logged(db, make_post) -> None:
    existing = make_post(title_original="GTA 6 release date announced")

    verdict = run(
        check_news(
            db,
            make_settings(),
            title="GTA 6 release date announced by Rockstar",
            url="https://example.com/news/gray",
        )
    )

    assert verdict.is_gray_zone
    assert verdict.matched_post is not None and verdict.matched_post.id == existing.id

    entries = run(db.list_dedup_log(limit=10))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.decision is DedupState.GRAY_ZONE
    assert entry.compared_post_id == existing.id
    assert 0.70 <= entry.score < 0.85


def test_published_posts_participate_in_dedup(db, make_post) -> None:
    post = make_post(title_original="Rockstar announces GTA 6 delay")
    run(db.mark_published(post.id))

    verdict = run(
        check_news(
            db,
            make_settings(),
            title="Rockstar announces GTA 6 delay",
            url="https://example.com/news/late",
        )
    )
    assert verdict.is_duplicate


def test_dropped_posts_do_not_participate_in_dedup(db, make_post) -> None:
    post = make_post(title_original="Rockstar announces GTA 6 delay")
    run(db.mark_dropped(post.id, reason="тест"))

    verdict = run(
        check_news(
            db,
            make_settings(),
            title="Rockstar announces GTA 6 delay",
            url="https://example.com/news/again",
        )
    )
    assert verdict.is_new


def test_old_posts_outside_history_window_are_ignored(db) -> None:
    run(
        db.create_post(
            "https://example.com/news/old",
            title_original="GTA 6 trailer breaks records",
            created_at=T0 - timedelta(days=8),
        )
    )

    verdict = run(
        check_news(
            db,
            make_settings(),
            title="GTA 6 trailer breaks records",
            url="https://example.com/news/fresh",
            now=T0,
        )
    )
    assert verdict.is_new


def test_lead_is_used_when_title_is_empty(db, make_post) -> None:
    make_post(title_original="GTA 6 trailer breaks records")

    verdict = run(
        check_news(
            db,
            make_settings(),
            title="",
            lead="GTA 6 trailer breaks records",
            url="https://example.com/news/lead",
        )
    )
    assert verdict.is_duplicate


def test_thresholds_come_from_settings(db, make_post) -> None:
    make_post(title_original="GTA 6 release date announced")

    strict = make_settings(dedup_duplicate_threshold=0.99, dedup_gray_zone_threshold=0.98)
    verdict = run(
        check_news(
            db,
            strict,
            title="GTA 6 release date announced by Rockstar",
            url="https://example.com/news/strict",
        )
    )
    # По строгим порогам похожий заголовок — новая новость, а не серая зона.
    assert verdict.is_new


def test_verdict_flags() -> None:
    assert DedupVerdict(DedupState.NEW).is_new
    assert DedupVerdict(DedupState.DUPLICATE).is_duplicate
    assert DedupVerdict(DedupState.GRAY_ZONE).is_gray_zone
    assert not DedupVerdict(DedupState.NEW).is_duplicate
