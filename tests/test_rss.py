"""Тесты RSS-парсера и планировщика опроса (T03).

Полностью офлайновые: разбор идёт по сохранённым XML из ``tests/fixtures``,
сеть подменяется ``httpx.MockTransport``. Реальные ленты и ``.env``
не читаются.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path
from typing import Callable

import httpx
import pytest

from bot.parsers.rss import (
    LEAD_MAX_CHARS,
    MAX_RETRY_AFTER_SEC,
    USER_AGENT,
    FeedFetchError,
    FeedHTTPError,
    FeedItem,
    FeedParseError,
    PollOutcome,
    build_client,
    clean_text,
    extract_image_url,
    fetch_feed,
    jitter_delay,
    make_lead,
    parse_feed,
    parse_retry_after,
    poll_forever,
    poll_source,
)
from bot.parsers.sources import SOURCES
from tests.helpers import T0, make_settings, run

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VGC = SOURCES[0]
IGN = SOURCES[1]
GAMESPOT = SOURCES[4]


def fixture(name: str) -> bytes:
    """Сохранённая лента из ``tests/fixtures``."""
    return (FIXTURES / name).read_bytes()


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """AsyncClient с подменённым транспортом: в сеть не ходит."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def body_client(
    body: bytes, *, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    """Клиент, который на любой запрос отвечает одинаково."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers)

    return mock_client(handler)


# ----------------------------------------------------------------------
# Разбор лент
# ----------------------------------------------------------------------
def test_parse_rss2_entries_are_normalized() -> None:
    items = parse_feed(fixture("rss_vgc.xml"), VGC)

    assert len(items) == 2
    first = items[0]
    assert first.url == "https://videogameschronicle.com/news/gta-6-trailer-records/"
    assert first.guid == "vgc-2026-11-18-001"
    assert first.source == "VGC"
    assert first.title.startswith("GTA 6 trailer breaks records")
    assert "GTA 6 trailer reached 100 million views" in first.lead
    assert "<" not in first.lead
    assert first.published_at is not None
    assert first.published_at.isoformat() == "2026-11-18T09:30:00+00:00"
    assert first.image_url == (
        "https://cdn.videogameschronicle.com/files/2026/11/gta6-trailer.jpg"
    )


def test_parse_rss2_uses_media_content_image() -> None:
    items = parse_feed(fixture("rss_vgc.xml"), VGC)

    second = items[1]
    assert second.image_url == (
        "https://cdn.videogameschronicle.com/files/2026/11/silent-hill.jpg"
    )
    assert second.published_at is not None
    assert second.published_at.hour == 8


def test_parse_atom_link_id_and_media_thumbnail() -> None:
    items = parse_feed(fixture("atom_ign.xml"), IGN)

    assert len(items) == 2
    first = items[0]
    assert first.url == "https://www.ign.com/articles/gta-6-what-we-know"
    # В Atom GUID — это <id>, а не ссылка.
    assert first.guid == "tag:ign.com,2026:news/12345"
    assert first.source == "IGN"
    assert first.image_url == "https://assets.ign.com/thumbs/gta6-16x9.jpg"
    assert first.published_at is not None
    assert first.published_at.isoformat() == "2026-11-18T09:45:00+00:00"
    # Лид без HTML-тегов и лишних пробелов перед запятой.
    assert first.lead.startswith("A rundown of every confirmed detail about GTA 6,")

    assert items[1].image_url is None


def test_parse_broken_feed_raises() -> None:
    with pytest.raises(FeedParseError):
        parse_feed(fixture("broken_gamespot.xml"), GAMESPOT)


def test_parse_valid_empty_feed_returns_empty_list() -> None:
    raw = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<rss version="2.0"><channel><title>Empty</title></channel></rss>'
    )
    assert parse_feed(raw, VGC) == []


# ----------------------------------------------------------------------
# Нормализация текста и картинки
# ----------------------------------------------------------------------
def test_clean_text_strips_html_and_entities() -> None:
    raw = "<p>Rockstar&nbsp;Games</p>\n<strong>GTA &amp; VI</strong>"
    assert clean_text(raw) == "Rockstar Games GTA & VI"
    assert clean_text("") == ""
    assert clean_text("<p> </p>") == ""


def test_make_lead_shortens_long_text() -> None:
    raw = "<p>" + "Слово " * 100 + "</p>"
    lead = make_lead(raw)

    assert len(lead) <= LEAD_MAX_CHARS + 1
    assert lead.endswith("…")
    assert "<" not in lead
    assert "  " not in lead
    # Короткий текст не трогаем и многоточие не добавляем.
    assert make_lead("<p>Короткий <b>текст</b></p>") == "Короткий текст"


def test_make_lead_custom_limit() -> None:
    assert make_lead("один два три четыре", max_chars=8) == "один два…"
    assert make_lead("один два три четыре", max_chars=9) == "один два…"


def test_extract_image_prefers_enclosure() -> None:
    entry = {
        "enclosures": [{"href": "https://example.com/a.jpg", "type": "image/jpeg"}],
        "media_content": [{"url": "https://example.com/b.jpg", "medium": "image"}],
        "media_thumbnail": [{"url": "https://example.com/c.jpg"}],
    }
    assert extract_image_url(entry) == "https://example.com/a.jpg"


def test_extract_image_skips_non_image_enclosure() -> None:
    entry = {
        "enclosures": [{"href": "https://example.com/podcast.mp3", "type": "audio/mpeg"}],
        "media_content": [{"url": "https://example.com/frame.jpg", "medium": "image"}],
    }
    assert extract_image_url(entry) == "https://example.com/frame.jpg"
    assert extract_image_url({"enclosures": [{"href": "https://example.com/a.mp3"}]}) is None


# ----------------------------------------------------------------------
# HTTP: статусы, таймауты, редиректы
# ----------------------------------------------------------------------
def test_fetch_feed_follows_redirects() -> None:
    settings = make_settings()
    requested: list[str] = []
    body = fixture("rss_vgc.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == VGC.url:
            return httpx.Response(
                301, headers={"Location": "https://videogameschronicle.com/feed/new/"}
            )
        return httpx.Response(200, content=body)

    async def scenario() -> bytes:
        async with mock_client(handler) as client:
            return await fetch_feed(client, VGC, settings)

    assert run(scenario()) == body
    assert requested == [VGC.url, "https://videogameschronicle.com/feed/new/"]


def test_fetch_feed_timeout_raises_fetch_error() -> None:
    settings = make_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("нет связи")

    async def scenario() -> None:
        async with mock_client(handler) as client:
            await fetch_feed(client, VGC, settings)

    with pytest.raises(FeedFetchError):
        run(scenario())


def test_fetch_feed_429_has_retry_after() -> None:
    settings = make_settings()

    async def scenario() -> None:
        async with body_client(b"", status=429, headers={"Retry-After": "120"}) as client:
            await fetch_feed(client, IGN, settings)

    with pytest.raises(FeedHTTPError) as excinfo:
        run(scenario())
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == 120.0


def test_build_client_headers_and_timeout() -> None:
    settings = make_settings(http_timeout_sec=7)
    client = build_client(settings)
    try:
        assert client.headers["User-Agent"] == USER_AGENT
        assert client.follow_redirects is True
        assert client.timeout == httpx.Timeout(7.0)
    finally:
        run(client.aclose())


# ----------------------------------------------------------------------
# Опрос одного источника и дедупликация seen_urls
# ----------------------------------------------------------------------
def test_poll_source_marks_seen_and_skips_repeats(db) -> None:
    settings = make_settings()
    received: list[str] = []

    async def on_item(item: FeedItem) -> None:
        received.append(item.url)

    async def scenario() -> tuple[PollOutcome, PollOutcome]:
        async with body_client(fixture("rss_vgc.xml")) as client:
            first = await poll_source(db, settings, VGC, client=client, on_item=on_item)
            second = await poll_source(db, settings, VGC, client=client, on_item=on_item)
        return first, second

    first, second = run(scenario())

    assert not first.is_error
    assert first.entries == 2
    assert len(first.new_items) == 2
    assert received == [item.url for item in first.new_items]

    assert second.entries == 2
    assert second.new_items == ()
    assert len(received) == 2  # подписчик не получил повторов

    seen = run(db.get_seen_url(first.new_items[0].url))
    assert seen is not None
    assert seen.guid == "vgc-2026-11-18-001"
    assert seen.source == "VGC"

    connection = sqlite3.connect(db.path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM seen_urls").fetchone()[0]
    finally:
        connection.close()
    assert count == 2  # повторно не пишем


def test_poll_source_429_returns_retry_after_and_no_items(db) -> None:
    settings = make_settings()

    async def scenario() -> PollOutcome:
        async with body_client(
            b"", status=429, headers={"Retry-After": "120"}
        ) as client:
            return await poll_source(db, settings, IGN, client=client)

    outcome = run(scenario())

    assert outcome.is_error
    assert "429" in (outcome.error or "")
    assert outcome.retry_after == 120.0
    assert outcome.new_items == ()


def test_poll_source_403_is_logged_not_raised(db) -> None:
    settings = make_settings()

    async def scenario() -> PollOutcome:
        async with body_client(b"", status=403) as client:
            return await poll_source(db, settings, VGC, client=client)

    outcome = run(scenario())

    assert outcome.is_error
    assert "403" in (outcome.error or "")
    assert outcome.retry_after is None


def test_poll_source_broken_xml_does_not_raise(db) -> None:
    settings = make_settings()

    async def scenario() -> PollOutcome:
        async with body_client(fixture("broken_gamespot.xml")) as client:
            return await poll_source(db, settings, GAMESPOT, client=client)

    outcome = run(scenario())

    assert outcome.is_error
    assert outcome.entries == 0
    assert outcome.new_items == ()


def test_poll_source_timeout_does_not_raise(db) -> None:
    settings = make_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("источник молчит")

    async def scenario() -> PollOutcome:
        async with mock_client(handler) as client:
            return await poll_source(db, settings, VGC, client=client)

    outcome = run(scenario())

    assert outcome.is_error
    assert "таймаут" in (outcome.error or "").lower()
    assert outcome.new_items == ()


# ----------------------------------------------------------------------
# Retry-After и jitter
# ----------------------------------------------------------------------
def test_parse_retry_after_seconds_and_http_date() -> None:
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("не дата") is None
    assert parse_retry_after("Wed, 18 Nov 2026 12:01:30 GMT", now=T0) == 90.0
    # Слишком большие и уже прошедшие даты ограничиваются разумно.
    assert parse_retry_after("99999999") == MAX_RETRY_AFTER_SEC
    assert parse_retry_after("Wed, 18 Nov 2026 11:00:00 GMT", now=T0) == 0.0


def test_jitter_delay_within_settings_bounds() -> None:
    settings = make_settings(poll_interval_min_sec=300, poll_interval_max_sec=600)
    rng = random.Random(2026)
    values = [jitter_delay(settings, rng) for _ in range(500)]

    assert all(300.0 <= value <= 600.0 for value in values)
    assert len(set(values)) > 1  # это действительно случайная величина
    assert min(values) < 450.0 < max(values)


def test_jitter_delay_with_equal_bounds_is_constant() -> None:
    settings = make_settings(poll_interval_min_sec=120, poll_interval_max_sec=120)
    assert jitter_delay(settings) == 120.0


# ----------------------------------------------------------------------
# Цикл опроса
# ----------------------------------------------------------------------
def test_poll_forever_stops_when_event_already_set(db) -> None:
    settings = make_settings()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("опрос не должен начинаться после остановки")

    stop = asyncio.Event()
    stop.set()

    async def scenario() -> None:
        async with mock_client(handler) as client:
            await asyncio.wait_for(
                poll_forever(db, settings, stop, sources=[VGC], client=client),
                timeout=5,
            )

    run(scenario())
    assert stop.is_set()


def test_poll_forever_polls_and_stops_from_callback(db) -> None:
    settings = make_settings(poll_interval_min_sec=0, poll_interval_max_sec=0)
    stop = asyncio.Event()
    received: list[FeedItem] = []

    async def on_item(item: FeedItem) -> None:
        received.append(item)
        stop.set()

    async def scenario() -> None:
        async with body_client(fixture("rss_vgc.xml")) as client:
            await asyncio.wait_for(
                poll_forever(
                    db, settings, stop, on_item, sources=[VGC], client=client
                ),
                timeout=10,
            )

    run(scenario())

    assert stop.is_set()
    assert received, "подписчик должен получить хотя бы одну новость"
    assert received[0].source == "VGC"
