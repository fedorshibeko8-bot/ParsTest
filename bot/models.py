"""Модели данных и статусы постов.

Только ``dataclass``-ы и перечисления — никакой ORM. Слой хранения
(:mod:`bot.db`) читает строки SQLite и превращает их в эти объекты.

Временные метки в БД хранятся строками ISO 8601 в UTC (``...+00:00``).
Помощники :func:`to_iso` / :func:`from_iso` держат формат единым: строки
в UTC с одинаковой точностью можно сравнивать лексикографически, поэтому
выборки по TTL не требуют разбора дат в SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping

# Точность меток времени. Секунд достаточно для очереди и тайм-аутов,
# а формат остаётся читаемым при ручном просмотре БД.
_ISO_TIMESPEC = "seconds"


def utcnow() -> datetime:
    """Текущее время в UTC с явной таймзоной."""
    return datetime.now(tz=timezone.utc)


def to_iso(moment: datetime | None) -> str | None:
    """``datetime`` → строка для SQLite (UTC, ISO 8601). ``None`` остаётся ``None``."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        # Наивное время считаем UTC: в проекте других таймзон нет.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec=_ISO_TIMESPEC)


def from_iso(raw: str | None) -> datetime | None:
    """Строка из SQLite → ``datetime`` в UTC."""
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PostStatus(StrEnum):
    """Жизненный цикл поста (п. 5 ТЗ)."""

    PENDING = "pending"
    INTERCEPTED = "intercepted"
    PUBLISHED = "published"
    DROPPED = "dropped"


class DedupState(StrEnum):
    """Результат дедупликации для конкретного поста (п. 4 ТЗ)."""

    NEW = "new"
    DUPLICATE = "duplicate"
    GRAY_ZONE = "gray_zone"


class DraftOrigin(StrEnum):
    """Откуда взялась версия черновика."""

    ORIGINAL = "original"  # исходный текст статьи / первичная выжимка
    AI = "ai"              # рерайт через ИИ
    MANUAL = "manual"      # правка админом
    ROLLBACK = "rollback"  # откат к одной из прошлых версий


@dataclass(frozen=True, slots=True)
class Post:
    """Новость и её черновик в очереди."""

    id: int
    url: str
    guid: str | None
    source: str
    title_original: str
    lead: str
    image_path: str | None
    image_url: str | None
    status: PostStatus
    owner_id: int | None
    created_at: datetime
    intercepted_at: datetime | None
    published_at: datetime | None
    dropped_at: datetime | None
    draft_title: str
    draft_text: str
    dedup_score: float | None
    dedup_state: DedupState
    rewrite_rounds: int
    intercept_deadline: datetime | None
    last_activity_at: datetime | None
    drop_reason: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Post":
        """Собрать объект из строки SQLite (``sqlite3.Row``)."""
        return cls(
            id=int(row["id"]),
            url=row["url"],
            guid=row["guid"],
            source=row["source"],
            title_original=row["title_original"],
            lead=row["lead"],
            image_path=row["image_path"],
            image_url=row["image_url"],
            status=PostStatus(row["status"]),
            owner_id=row["owner_id"],
            created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
            intercepted_at=from_iso(row["intercepted_at"]),
            published_at=from_iso(row["published_at"]),
            dropped_at=from_iso(row["dropped_at"]),
            draft_title=row["draft_title"],
            draft_text=row["draft_text"],
            dedup_score=row["dedup_score"],
            dedup_state=DedupState(row["dedup_state"]),
            rewrite_rounds=int(row["rewrite_rounds"]),
            intercept_deadline=from_iso(row["intercept_deadline"]),
            last_activity_at=from_iso(row["last_activity_at"]),
            drop_reason=row["drop_reason"],
        )

    # --- Удобные предикаты ------------------------------------------------
    @property
    def is_pending(self) -> bool:
        return self.status is PostStatus.PENDING

    @property
    def is_intercepted(self) -> bool:
        return self.status is PostStatus.INTERCEPTED

    @property
    def is_finished(self) -> bool:
        """Опубликован или снят — дальше по жизненному циклу не идёт."""
        return self.status in (PostStatus.PUBLISHED, PostStatus.DROPPED)

    @property
    def has_draft(self) -> bool:
        return bool(self.draft_title.strip() or self.draft_text.strip())

    def is_expired(self, ttl_hours: int, *, now: datetime | None = None) -> bool:
        """Истёк ли TTL ожидания в очереди (по умолчанию 48 ч)."""
        return self.is_pending and self.created_at + timedelta(hours=ttl_hours) <= (
            now or utcnow()
        )

    def is_intercept_timed_out(self, *, now: datetime | None = None) -> bool:
        """Просрочен ли тайм-аут бездействия владельца."""
        return (
            self.is_intercepted
            and self.intercept_deadline is not None
            and self.intercept_deadline <= (now or utcnow())
        )


@dataclass(frozen=True, slots=True)
class DraftVersion:
    """Одна версия черновика: история для отката и подсчёта кругов рерайта."""

    id: int
    post_id: int
    version_no: int
    round_no: int
    origin: DraftOrigin
    title: str
    body: str
    created_at: datetime
    is_current: bool

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "DraftVersion":
        return cls(
            id=int(row["id"]),
            post_id=int(row["post_id"]),
            version_no=int(row["version_no"]),
            round_no=int(row["round_no"]),
            origin=DraftOrigin(row["origin"]),
            title=row["title"],
            body=row["body"],
            created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
            is_current=bool(row["is_current"]),
        )


@dataclass(frozen=True, slots=True)
class SeenUrl:
    """Ссылка/GUID, которые бот уже видел (ступень 1 дедупликации)."""

    id: int
    url: str
    guid: str | None
    source: str
    first_seen_at: datetime
    post_id: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "SeenUrl":
        return cls(
            id=int(row["id"]),
            url=row["url"],
            guid=row["guid"],
            source=row["source"],
            first_seen_at=from_iso(row["first_seen_at"]),  # type: ignore[arg-type]
            post_id=row["post_id"],
        )


@dataclass(frozen=True, slots=True)
class DedupLogEntry:
    """Запись о сравнении заголовков — в первую очередь «серая зона» 0.70–0.85.

    Копится, чтобы потом можно было подобрать пороги по реальным данным.
    """

    id: int
    post_id: int | None
    compared_post_id: int | None
    candidate_title: str
    compared_title: str
    score: float
    decision: DedupState
    created_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "DedupLogEntry":
        return cls(
            id=int(row["id"]),
            post_id=row["post_id"],
            compared_post_id=row["compared_post_id"],
            candidate_title=row["candidate_title"],
            compared_title=row["compared_title"],
            score=float(row["score"]),
            decision=DedupState(row["decision"]),
            created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
        )


# Явный список полей полезен тестам и отладке при сверке со схемой БД.
POST_FIELDS: tuple[str, ...] = tuple(field.name for field in fields(Post))

__all__ = [
    "DedupLogEntry",
    "DedupState",
    "DraftOrigin",
    "DraftVersion",
    "POST_FIELDS",
    "Post",
    "PostStatus",
    "SeenUrl",
    "from_iso",
    "to_iso",
    "utcnow",
]
