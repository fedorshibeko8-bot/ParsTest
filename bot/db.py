"""Слой доступа к SQLite: соединение, схема, миграции и запросы.

Принципы
--------

* **Файл БД** задаётся путём (``settings.database_path``); каталоги создаёт
  ``settings.ensure_directories()``, но ``initialize()`` тоже подстрахуется.
* **WAL** и ``foreign_keys=ON`` включаются на каждом соединении/при инициализации,
  ``busy_timeout`` защищает от «database is locked» при редких параллельных записях.
* **Миграции** — по ``PRAGMA user_version``: список :data:`MIGRATIONS` применяется
  по порядку, каждый шаг увеличивает версию. Повторный запуск ничего не делает.
* **Асинхронность**: публичный API — ``async``, блокирующий ``sqlite3`` уходит
  в ``asyncio.to_thread``. Соединение открывается на операцию (не на процесс):
  при десятках запросов в час это дешевле, чем следить за потоками и блокировками.
* **Время** хранится строками ISO 8601 UTC (см. :mod:`bot.models`), поэтому
  сравнения по TTL/тайм-аутам работают лексикографически.
* **Инвариант «один активный пост на админа»** держит частичный уникальный
  индекс — сервисный слой T07 может на него опереться.

Публичный API — :class:`Database`; бэкап — :func:`backup_database` /
:func:`backup_database_async` (обёртка для планировщика — ``bot.services.backup``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from bot.models import (
    DedupLogEntry,
    DedupState,
    DraftOrigin,
    DraftVersion,
    Post,
    PostStatus,
    SeenUrl,
    to_iso,
    utcnow,
)

logger = logging.getLogger(__name__)

# Версия схемы = максимальный номер миграции.
SCHEMA_VERSION = 2

DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_QUEUE_VISIBLE_LIMIT = 10
DEFAULT_POST_TTL_HOURS = 48
DEFAULT_INTERCEPT_TIMEOUT_MINUTES = 10
DEFAULT_DEDUP_HISTORY_DAYS = 7


class StorageError(Exception):
    """Общая ошибка слоя хранения."""


class PostNotFoundError(StorageError):
    """Пост с указанным id не найден."""


class DuplicatePostError(StorageError):
    """Пост с таким URL уже есть в БД."""


class InvalidTransitionError(StorageError):
    """Недопустимый переход статуса (например, из ``published``)."""


class OwnerBusyError(StorageError):
    """У админа уже есть активный (перехваченный) пост."""


class SchemaVersionError(StorageError):
    """Файл БД создан более новой версией бота, чем запущенная."""


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS posts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT    NOT NULL,
    guid              TEXT,
    source            TEXT    NOT NULL DEFAULT '',
    title_original    TEXT    NOT NULL DEFAULT '',
    lead              TEXT    NOT NULL DEFAULT '',
    image_path        TEXT,
    image_url         TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'intercepted', 'published', 'dropped')),
    owner_id          INTEGER,
    created_at        TEXT    NOT NULL,
    intercepted_at    TEXT,
    published_at      TEXT,
    dropped_at        TEXT,
    draft_title       TEXT    NOT NULL DEFAULT '',
    draft_text        TEXT    NOT NULL DEFAULT '',
    dedup_score       REAL,
    dedup_state       TEXT    NOT NULL DEFAULT 'new'
                      CHECK (dedup_state IN ('new', 'duplicate', 'gray_zone')),
    rewrite_rounds    INTEGER NOT NULL DEFAULT 0,
    intercept_deadline TEXT,
    last_activity_at  TEXT,
    drop_reason       TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_url ON posts(url);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_status_created ON posts(status, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_guid ON posts(guid);
CREATE INDEX IF NOT EXISTS idx_posts_owner ON posts(owner_id, status);

-- Один админ не может держать два перехваченных поста одновременно (п. 6-7 ТЗ).
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_active_owner
    ON posts(owner_id) WHERE status = 'intercepted' AND owner_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS draft_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    version_no  INTEGER NOT NULL,
    round_no    INTEGER NOT NULL DEFAULT 0,
    origin      TEXT    NOT NULL DEFAULT 'manual'
                CHECK (origin IN ('original', 'ai', 'manual', 'rollback')),
    title       TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    is_current  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (post_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_draft_versions_post ON draft_versions(post_id, version_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_draft_versions_current
    ON draft_versions(post_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS seen_urls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT    NOT NULL,
    guid          TEXT,
    source        TEXT    NOT NULL DEFAULT '',
    first_seen_at TEXT    NOT NULL,
    post_id       INTEGER REFERENCES posts(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_seen_urls_url ON seen_urls(url);
CREATE INDEX IF NOT EXISTS idx_seen_urls_guid ON seen_urls(guid);
CREATE INDEX IF NOT EXISTS idx_seen_urls_seen_at ON seen_urls(first_seen_at);

CREATE TABLE IF NOT EXISTS dedup_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           INTEGER REFERENCES posts(id) ON DELETE SET NULL,
    compared_post_id  INTEGER REFERENCES posts(id) ON DELETE SET NULL,
    candidate_title   TEXT    NOT NULL DEFAULT '',
    compared_title    TEXT    NOT NULL DEFAULT '',
    score             REAL    NOT NULL,
    decision          TEXT    NOT NULL
                      CHECK (decision IN ('new', 'duplicate', 'gray_zone')),
    created_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dedup_log_created ON dedup_log(created_at);
CREATE INDEX IF NOT EXISTS idx_dedup_log_score ON dedup_log(score);
"""

# По одному элементу на будущую версию схемы: (номер, SQL).
# v2: дата публикации оригинальной статьи — показывается только в служебном
# уведомлении, в текст поста канала не попадает.
_SCHEMA_V2 = """
ALTER TABLE posts ADD COLUMN source_published_at TEXT;
"""

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _SCHEMA_V1), (2, _SCHEMA_V2))

_POST_COLUMNS = (
    "id, url, guid, source, title_original, lead, image_path, image_url, "
    "status, owner_id, created_at, intercepted_at, published_at, dropped_at, "
    "draft_title, draft_text, dedup_score, dedup_state, rewrite_rounds, "
    "intercept_deadline, last_activity_at, drop_reason, source_published_at"
)


def _as_datetime(now: datetime | None) -> datetime:
    """Время операции: переданное (для тестов) или текущее UTC."""
    return now or utcnow()


class Database:
    """Асинхронный фасад над SQLite.

    Экземпляр лёгкий: держит только путь и настройки сроков, соединение
    открывается на каждую операцию.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        queue_visible_limit: int = DEFAULT_QUEUE_VISIBLE_LIMIT,
        post_ttl_hours: int = DEFAULT_POST_TTL_HOURS,
        intercept_timeout_minutes: int = DEFAULT_INTERCEPT_TIMEOUT_MINUTES,
        dedup_history_days: int = DEFAULT_DEDUP_HISTORY_DAYS,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.queue_visible_limit = int(queue_visible_limit)
        self.post_ttl_hours = int(post_ttl_hours)
        self.intercept_timeout_minutes = int(intercept_timeout_minutes)
        self.dedup_history_days = int(dedup_history_days)

    @classmethod
    def from_settings(cls, settings: Any) -> "Database":
        """Собрать хранилище по ``bot.config.Settings`` (без чтения env)."""
        return cls(
            settings.database_path,
            queue_visible_limit=settings.queue_visible_limit,
            post_ttl_hours=settings.post_ttl_hours,
            intercept_timeout_minutes=settings.intercept_timeout_minutes,
        )

    # ------------------------------------------------------------------
    # Соединение и миграции
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Открыть соединение с обязательными PRAGMA и закрыть после использования."""
        connection = sqlite3.connect(
            str(self.path), timeout=self.busy_timeout_ms / 1000.0
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
            connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        """Создать каталог, применить миграции, включить WAL. Идемпотентно."""
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                logger.warning(
                    "SQLite не переключился в WAL (режим %r): %s", mode, self.path
                )
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"База {self.path} имеет версию схемы {current}, "
                    f"а бот умеет максимум {SCHEMA_VERSION}. Обновите бота."
                )
            for number, script in MIGRATIONS:
                if number <= current:
                    continue
                logger.info("Миграция БД: версия %d → %d", current, number)
                connection.executescript(script)
                connection.execute(f"PRAGMA user_version = {number:d}")
                connection.commit()
                current = number

    async def close(self) -> None:
        """Заглушка для симметричного жизненного цикла: соединений не держим."""
        return None

    # ------------------------------------------------------------------
    # Посты
    # ------------------------------------------------------------------
    async def create_post(
        self,
        url: str,
        *,
        guid: str | None = None,
        source: str = "",
        title_original: str = "",
        lead: str = "",
        image_path: str | None = None,
        image_url: str | None = None,
        dedup_score: float | None = None,
        dedup_state: DedupState | str = DedupState.NEW,
        source_published_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> Post:
        """Создать пост в статусе ``pending`` и запомнить URL как виденный.

        :param source_published_at: дата публикации оригинальной статьи;
            хранится для служебных сообщений и в пост канала не входит.
        :raises DuplicatePostError: если пост с таким URL уже существует.
        """
        return await asyncio.to_thread(
            self._create_post_sync,
            url,
            guid,
            source,
            title_original,
            lead,
            image_path,
            image_url,
            dedup_score,
            DedupState(dedup_state).value,
            source_published_at,
            created_at,
        )

    def _create_post_sync(
        self,
        url: str,
        guid: str | None,
        source: str,
        title_original: str,
        lead: str,
        image_path: str | None,
        image_url: str | None,
        dedup_score: float | None,
        dedup_state: str,
        source_published_at: datetime | None,
        created_at: datetime | None,
    ) -> Post:
        moment = _as_datetime(created_at)
        stamp = to_iso(moment)
        with self._connect() as connection, connection:
            try:
                cursor = connection.execute(
                    f"INSERT INTO posts (url, guid, source, title_original, lead, "
                    f"image_path, image_url, status, created_at, dedup_score, "
                    f"dedup_state, source_published_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        url,
                        guid,
                        source,
                        title_original,
                        lead,
                        image_path,
                        image_url,
                        PostStatus.PENDING.value,
                        stamp,
                        dedup_score,
                        dedup_state,
                        to_iso(source_published_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "posts.url" in str(exc):
                    raise DuplicatePostError(f"Пост с URL уже есть: {url}") from exc
                raise
            post_id = int(cursor.lastrowid)
            # Ступень 1 дедупликации: запоминаем ссылку вместе с постом.
            connection.execute(
                "INSERT OR IGNORE INTO seen_urls "
                "(url, guid, source, first_seen_at, post_id) VALUES (?, ?, ?, ?, ?)",
                (url, guid, source, stamp, post_id),
            )
            row = self._fetch_post_row(connection, post_id)
        return Post.from_row(row)

    async def get_post(self, post_id: int) -> Post | None:
        """Пост по id или ``None``."""
        return await asyncio.to_thread(self._get_post_sync, post_id)

    def _get_post_sync(self, post_id: int) -> Post | None:
        with self._connect() as connection:
            row = self._fetch_post_row(connection, post_id)
        return Post.from_row(row) if row is not None else None

    async def get_post_by_url(self, url: str) -> Post | None:
        """Пост по точному URL (для повторной обработки/отладки)."""
        return await asyncio.to_thread(self._get_post_by_url_sync, url)

    def _get_post_by_url_sync(self, url: str) -> Post | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts WHERE url = ?", (url,)
            ).fetchone()
        return Post.from_row(row) if row is not None else None

    async def list_pending(
        self, limit: int | None = None, *, newest_first: bool = True
    ) -> list[Post]:
        """Очередь pending: по умолчанию самые свежие, лимит — ``queue_visible_limit``.

        :param limit: сколько вернуть; ``None`` — взять лимит из конфига.
        :param newest_first: ``False`` даёт FIFO (самый старый первым) для уведомлений.
        """
        return await asyncio.to_thread(
            self._list_status_sync,
            PostStatus.PENDING,
            limit,
            newest_first,
        )

    async def count_pending(self) -> int:
        """Сколько новостей ждёт в очереди."""
        return await asyncio.to_thread(self._count_pending_sync)

    def _count_pending_sync(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM posts WHERE status = ?",
                (PostStatus.PENDING.value,),
            ).fetchone()
        return int(row[0])

    async def mark_intercepted(
        self, post_id: int, owner_id: int, *, now: datetime | None = None
    ) -> Post:
        """Взять пост админом: ``pending → intercepted`` + дедлайн тайм-аута.

        :raises PostNotFoundError: поста нет.
        :raises InvalidTransitionError: пост уже не ``pending``.
        :raises OwnerBusyError: у админа уже есть активный пост.
        """
        return await asyncio.to_thread(self._mark_intercepted_sync, post_id, owner_id, now)

    def _mark_intercepted_sync(
        self, post_id: int, owner_id: int, now: datetime | None
    ) -> Post:
        moment = _as_datetime(now)
        deadline = moment + timedelta(minutes=self.intercept_timeout_minutes)
        with self._connect() as connection, connection:
            row = self._require_post_row(connection, post_id)
            status = PostStatus(row["status"])
            if status is not PostStatus.PENDING:
                raise InvalidTransitionError(
                    f"Пост {post_id}: нельзя перехватить из статуса {status.value}."
                )
            try:
                connection.execute(
                    "UPDATE posts SET status = ?, owner_id = ?, intercepted_at = ?, "
                    "intercept_deadline = ?, last_activity_at = ?, dropped_at = NULL, "
                    "drop_reason = NULL WHERE id = ?",
                    (
                        PostStatus.INTERCEPTED.value,
                        owner_id,
                        to_iso(moment),
                        to_iso(deadline),
                        to_iso(moment),
                        post_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if (
                    "idx_posts_active_owner" in str(exc)
                    or "posts.owner_id" in str(exc)
                ):
                    raise OwnerBusyError(
                        f"У админа {owner_id} уже есть активный пост."
                    ) from exc
                raise
            row = self._fetch_post_row(connection, post_id)
        return Post.from_row(row)

    async def mark_published(
        self, post_id: int, *, now: datetime | None = None
    ) -> Post:
        """Опубликован. Допустимо из ``pending`` или ``intercepted``.

        :raises InvalidTransitionError: пост уже завершён.
        """
        return await asyncio.to_thread(self._mark_published_sync, post_id, now)

    def _mark_published_sync(self, post_id: int, now: datetime | None) -> Post:
        moment = _as_datetime(now)
        with self._connect() as connection, connection:
            row = self._require_post_row(connection, post_id)
            status = PostStatus(row["status"])
            if status not in (PostStatus.PENDING, PostStatus.INTERCEPTED):
                raise InvalidTransitionError(
                    f"Пост {post_id}: нельзя опубликовать из статуса {status.value}."
                )
            connection.execute(
                "UPDATE posts SET status = ?, published_at = ?, "
                "intercept_deadline = NULL, last_activity_at = ? WHERE id = ?",
                (
                    PostStatus.PUBLISHED.value,
                    to_iso(moment),
                    to_iso(moment),
                    post_id,
                ),
            )
            row = self._fetch_post_row(connection, post_id)
        return Post.from_row(row)

    async def mark_dropped(
        self, post_id: int, *, reason: str | None = None, now: datetime | None = None
    ) -> Post:
        """Снять пост (отказ админа, TTL, тайм-аут).

        :raises InvalidTransitionError: пост уже завершён.
        """
        return await asyncio.to_thread(self._mark_dropped_sync, post_id, reason, now)

    def _mark_dropped_sync(
        self, post_id: int, reason: str | None, now: datetime | None
    ) -> Post:
        moment = _as_datetime(now)
        with self._connect() as connection, connection:
            row = self._require_post_row(connection, post_id)
            status = PostStatus(row["status"])
            if status not in (PostStatus.PENDING, PostStatus.INTERCEPTED):
                raise InvalidTransitionError(
                    f"Пост {post_id}: нельзя снять из статуса {status.value}."
                )
            connection.execute(
                "UPDATE posts SET status = ?, dropped_at = ?, drop_reason = ?, "
                "intercept_deadline = NULL, last_activity_at = ? WHERE id = ?",
                (
                    PostStatus.DROPPED.value,
                    to_iso(moment),
                    reason,
                    to_iso(moment),
                    post_id,
                ),
            )
            row = self._fetch_post_row(connection, post_id)
        return Post.from_row(row)

    async def reset_to_pending(self, post_id: int, *, now: datetime | None = None) -> Post:
        """Вернуть перехваченный пост в очередь (тайм-аут / отказ владельца).

        Владелец и дедлайн очищаются; TTL считается от ``created_at``.

        :raises InvalidTransitionError: пост не ``intercepted``.
        """
        return await asyncio.to_thread(self._reset_to_pending_sync, post_id, now)

    def _reset_to_pending_sync(self, post_id: int, now: datetime | None) -> Post:
        moment = _as_datetime(now)
        with self._connect() as connection, connection:
            row = self._require_post_row(connection, post_id)
            status = PostStatus(row["status"])
            if status is not PostStatus.INTERCEPTED:
                raise InvalidTransitionError(
                    f"Пост {post_id}: вернуть в очередь можно только из intercepted, "
                    f"а он в {status.value}."
                )
            connection.execute(
                "UPDATE posts SET status = ?, owner_id = NULL, intercepted_at = NULL, "
                "intercept_deadline = NULL, last_activity_at = ? WHERE id = ?",
                (PostStatus.PENDING.value, to_iso(moment), post_id),
            )
            row = self._fetch_post_row(connection, post_id)
        return Post.from_row(row)

    async def touch_post(
        self, post_id: int, *, minutes: int | None = None, now: datetime | None = None
    ) -> Post:
        """Продлить тайм-аут бездействия (любое действие владельца, п. 7 ТЗ).

        :param minutes: на сколько продлить; по умолчанию ``intercept_timeout_minutes``.
        """
        return await asyncio.to_thread(self._touch_post_sync, post_id, minutes, now)

    def _touch_post_sync(
        self, post_id: int, minutes: int | None, now: datetime | None
    ) -> Post:
        moment = _as_datetime(now)
        extend = self.intercept_timeout_minutes if minutes is None else int(minutes)
        deadline = moment + timedelta(minutes=extend)
        with self._connect() as connection, connection:
            row = self._require_post_row(connection, post_id)
            status = PostStatus(row["status"])
            if status is not PostStatus.INTERCEPTED:
                raise InvalidTransitionError(
                    f"Пост {post_id}: продлевать тайм-аут можно только у intercepted, "
                    f"а он в {status.value}."
                )
            connection.execute(
                "UPDATE posts SET last_activity_at = ?, intercept_deadline = ? "
                "WHERE id = ?",
                (to_iso(moment), to_iso(deadline), post_id),
            )
            row = self._fetch_post_row(connection, post_id)
        return Post.from_row(row)

    async def find_active_post(self, owner_id: int) -> Post | None:
        """Активный (перехваченный) пост админа — не больше одного."""
        return await asyncio.to_thread(self._find_active_post_sync, owner_id)

    def _find_active_post_sync(self, owner_id: int) -> Post | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts "
                "WHERE owner_id = ? AND status = ? LIMIT 1",
                (owner_id, PostStatus.INTERCEPTED.value),
            ).fetchone()
        return Post.from_row(row) if row is not None else None

    async def list_expired_pending(
        self, *, now: datetime | None = None, ttl_hours: int | None = None
    ) -> list[Post]:
        """Pending-посты с истёкшим TTL (кандидаты на ``dropped``)."""
        return await asyncio.to_thread(
            self._list_expired_pending_sync, now, ttl_hours
        )

    def _list_expired_pending_sync(
        self, now: datetime | None, ttl_hours: int | None
    ) -> list[Post]:
        moment = _as_datetime(now)
        ttl = self.post_ttl_hours if ttl_hours is None else int(ttl_hours)
        cutoff = to_iso(moment - timedelta(hours=ttl))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts "
                "WHERE status = ? AND created_at <= ? ORDER BY created_at ASC, id ASC",
                (PostStatus.PENDING.value, cutoff),
            ).fetchall()
        return [Post.from_row(row) for row in rows]

    async def expire_pending(
        self, *, now: datetime | None = None, ttl_hours: int | None = None
    ) -> list[Post]:
        """Пометить все просроченные pending как ``dropped`` и вернуть их."""
        return await asyncio.to_thread(self._expire_pending_sync, now, ttl_hours)

    def _expire_pending_sync(
        self, now: datetime | None, ttl_hours: int | None
    ) -> list[Post]:
        moment = _as_datetime(now)
        ttl = self.post_ttl_hours if ttl_hours is None else int(ttl_hours)
        cutoff = to_iso(moment - timedelta(hours=ttl))
        stamp = to_iso(moment)
        with self._connect() as connection, connection:
            rows = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts "
                "WHERE status = ? AND created_at <= ? ORDER BY created_at ASC, id ASC",
                (PostStatus.PENDING.value, cutoff),
            ).fetchall()
            post_ids = [int(row["id"]) for row in rows]
            if post_ids:
                placeholders = ", ".join("?" for _ in post_ids)
                connection.execute(
                    "UPDATE posts SET status = ?, dropped_at = ?, drop_reason = ?, "
                    f"intercept_deadline = NULL WHERE id IN ({placeholders})",
                    (PostStatus.DROPPED.value, stamp, "ttl", *post_ids),
                )
                rows = connection.execute(
                    f"SELECT {_POST_COLUMNS} FROM posts WHERE id IN ({placeholders}) "
                    "ORDER BY created_at ASC, id ASC",
                    tuple(post_ids),
                ).fetchall()
        return [Post.from_row(row) for row in rows]

    async def list_timed_out(
        self, *, now: datetime | None = None
    ) -> list[Post]:
        """Перехваченные посты, у которых истёк тайм-аут бездействия."""
        return await asyncio.to_thread(self._list_timed_out_sync, now)

    def _list_timed_out_sync(self, now: datetime | None) -> list[Post]:
        moment = _as_datetime(now)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts "
                "WHERE status = ? AND intercept_deadline IS NOT NULL "
                "AND intercept_deadline <= ? ORDER BY intercept_deadline ASC, id ASC",
                (PostStatus.INTERCEPTED.value, to_iso(moment)),
            ).fetchall()
        return [Post.from_row(row) for row in rows]

    async def release_timed_out(self, *, now: datetime | None = None) -> list[Post]:
        """Вернуть в очередь все посты с истёкшим тайм-аутом и вернуть их."""
        return await asyncio.to_thread(self._release_timed_out_sync, now)

    def _release_timed_out_sync(self, now: datetime | None) -> list[Post]:
        moment = _as_datetime(now)
        stamp = to_iso(moment)
        with self._connect() as connection, connection:
            rows = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts "
                "WHERE status = ? AND intercept_deadline IS NOT NULL "
                "AND intercept_deadline <= ? ORDER BY intercept_deadline ASC, id ASC",
                (PostStatus.INTERCEPTED.value, stamp),
            ).fetchall()
            post_ids = [int(row["id"]) for row in rows]
            if post_ids:
                placeholders = ", ".join("?" for _ in post_ids)
                connection.execute(
                    "UPDATE posts SET status = ?, owner_id = NULL, "
                    "intercepted_at = NULL, intercept_deadline = NULL, "
                    f"last_activity_at = ? WHERE id IN ({placeholders})",
                    (PostStatus.PENDING.value, stamp, *post_ids),
                )
                rows = connection.execute(
                    f"SELECT {_POST_COLUMNS} FROM posts WHERE id IN ({placeholders}) "
                    "ORDER BY created_at ASC, id ASC",
                    tuple(post_ids),
                ).fetchall()
        return [Post.from_row(row) for row in rows]

    async def set_image(
        self,
        post_id: int,
        *,
        image_path: str | None,
        image_url: str | None = None,
    ) -> Post:
        """Заменить или убрать картинку поста (ручная правка, п. 8 ТЗ)."""
        return await asyncio.to_thread(
            self._set_image_sync, post_id, image_path, image_url
        )

    def _set_image_sync(
        self, post_id: int, image_path: str | None, image_url: str | None
    ) -> Post:
        with self._connect() as connection, connection:
            self._require_post_row(connection, post_id)
            connection.execute(
                "UPDATE posts SET image_path = ?, image_url = COALESCE(?, image_url) "
                "WHERE id = ?",
                (image_path, image_url, post_id),
            )
            row = self._fetch_post_row(connection, post_id)
        return Post.from_row(row)

    async def list_for_dedup(
        self, *, days: int | None = None, now: datetime | None = None
    ) -> list[Post]:
        """Профиль для ступени 3: pending + published за последние N дней."""
        return await asyncio.to_thread(self._list_for_dedup_sync, days, now)

    def _list_for_dedup_sync(self, days: int | None, now: datetime | None) -> list[Post]:
        moment = _as_datetime(now)
        window = self.dedup_history_days if days is None else int(days)
        cutoff = to_iso(moment - timedelta(days=window))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts "
                "WHERE status IN (?, ?) AND created_at >= ? "
                "ORDER BY created_at DESC, id DESC",
                (
                    PostStatus.PENDING.value,
                    PostStatus.PUBLISHED.value,
                    cutoff,
                ),
            ).fetchall()
        return [Post.from_row(row) for row in rows]

    async def list_for_image_cleanup(
        self, *, now: datetime | None = None, keep_hours: int = 48
    ) -> list[Post]:
        """Посты, у которых пора удалить файл картинки (T06).

        Берутся только завершённые посты (``published``/``dropped``) с
        заполненным ``image_path``, у которых момент завершения (или создания,
        если метки нет) старше ``keep_hours``. Сами строки не меняются:
        решение об удалении файла и очистке пути принимает сервис картинок.
        """
        return await asyncio.to_thread(
            self._list_for_image_cleanup_sync, now, keep_hours
        )

    def _list_for_image_cleanup_sync(
        self, now: datetime | None, keep_hours: int
    ) -> list[Post]:
        moment = _as_datetime(now)
        cutoff = to_iso(moment - timedelta(hours=max(1, int(keep_hours))))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts "
                "WHERE image_path IS NOT NULL AND status IN (?, ?) "
                "AND COALESCE(published_at, dropped_at, created_at) <= ? "
                "ORDER BY COALESCE(published_at, dropped_at, created_at) ASC, id ASC",
                (
                    PostStatus.PUBLISHED.value,
                    PostStatus.DROPPED.value,
                    cutoff,
                ),
            ).fetchall()
        return [Post.from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Дедупликация: ступень 1 и лог серых зон
    # ------------------------------------------------------------------
    async def is_url_seen(self, url: str, guid: str | None = None) -> bool:
        """Ступень 1: URL или GUID уже встречались."""
        return await asyncio.to_thread(self._is_url_seen_sync, url, guid)

    def _is_url_seen_sync(self, url: str, guid: str | None) -> bool:
        with self._connect() as connection:
            if guid:
                row = connection.execute(
                    "SELECT 1 FROM seen_urls WHERE url = ? OR guid = ? LIMIT 1",
                    (url, guid),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT 1 FROM seen_urls WHERE url = ? LIMIT 1", (url,)
                ).fetchone()
        return row is not None

    async def mark_url_seen(
        self,
        url: str,
        *,
        guid: str | None = None,
        source: str = "",
        post_id: int | None = None,
        seen_at: datetime | None = None,
    ) -> None:
        """Запомнить URL/GUID, не создавая пост (например, отфильтрованную новость)."""
        await asyncio.to_thread(
            self._mark_url_seen_sync, url, guid, source, post_id, seen_at
        )

    def _mark_url_seen_sync(
        self,
        url: str,
        guid: str | None,
        source: str,
        post_id: int | None,
        seen_at: datetime | None,
    ) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO seen_urls "
                "(url, guid, source, first_seen_at, post_id) VALUES (?, ?, ?, ?, ?)",
                (url, guid, source, to_iso(_as_datetime(seen_at)), post_id),
            )
            if post_id is not None:
                connection.execute(
                    "UPDATE seen_urls SET post_id = COALESCE(post_id, ?) WHERE url = ?",
                    (post_id, url),
                )

    async def get_seen_url(self, url: str) -> SeenUrl | None:
        """Запись ``seen_urls`` по URL (для отладки и ручных разборов)."""
        return await asyncio.to_thread(self._get_seen_url_sync, url)

    def _get_seen_url_sync(self, url: str) -> SeenUrl | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM seen_urls WHERE url = ?", (url,)
            ).fetchone()
        return SeenUrl.from_row(row) if row is not None else None

    async def add_dedup_log(
        self,
        *,
        candidate_title: str,
        compared_title: str,
        score: float,
        decision: DedupState | str,
        post_id: int | None = None,
        compared_post_id: int | None = None,
        created_at: datetime | None = None,
    ) -> int:
        """Записать сравнение заголовков (серые зоны 0.70–0.85 в первую очередь)."""
        return await asyncio.to_thread(
            self._add_dedup_log_sync,
            candidate_title,
            compared_title,
            score,
            DedupState(decision).value,
            post_id,
            compared_post_id,
            created_at,
        )

    def _add_dedup_log_sync(
        self,
        candidate_title: str,
        compared_title: str,
        score: float,
        decision: str,
        post_id: int | None,
        compared_post_id: int | None,
        created_at: datetime | None,
    ) -> int:
        with self._connect() as connection, connection:
            cursor = connection.execute(
                "INSERT INTO dedup_log (post_id, compared_post_id, candidate_title, "
                "compared_title, score, decision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    post_id,
                    compared_post_id,
                    candidate_title,
                    compared_title,
                    float(score),
                    decision,
                    to_iso(_as_datetime(created_at)),
                ),
            )
        return int(cursor.lastrowid)

    async def list_dedup_log(self, limit: int = 50) -> list[DedupLogEntry]:
        """Последние записи лога дедупликации (свежие первыми)."""
        return await asyncio.to_thread(self._list_dedup_log_sync, limit)

    def _list_dedup_log_sync(self, limit: int) -> list[DedupLogEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dedup_log ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [DedupLogEntry.from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # История черновиков
    # ------------------------------------------------------------------
    async def add_draft_version(
        self,
        post_id: int,
        *,
        title: str,
        body: str,
        origin: DraftOrigin | str = DraftOrigin.MANUAL,
        round_no: int | None = None,
        created_at: datetime | None = None,
    ) -> DraftVersion:
        """Добавить версию черновика и сделать её текущей.

        Заодно обновляются ``posts.draft_title/draft_text`` и счётчик кругов
        рерайта (``rewrite_rounds``) — карточке не нужен лишний запрос.

        :param round_no: номер круга рерайта; по умолчанию для ``ai`` — следующий
            по счёту, для остальных — 0.
        :raises PostNotFoundError: поста нет.
        """
        return await asyncio.to_thread(
            self._add_draft_version_sync,
            post_id,
            title,
            body,
            DraftOrigin(origin).value,
            round_no,
            created_at,
        )

    def _add_draft_version_sync(
        self,
        post_id: int,
        title: str,
        body: str,
        origin: str,
        round_no: int | None,
        created_at: datetime | None,
    ) -> DraftVersion:
        moment = _as_datetime(created_at)
        with self._connect() as connection, connection:
            post_row = self._require_post_row(connection, post_id)
            current_rounds = int(post_row["rewrite_rounds"])
            if round_no is None:
                round_no = current_rounds + 1 if origin == DraftOrigin.AI.value else 0
            max_version = connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) FROM draft_versions WHERE post_id = ?",
                (post_id,),
            ).fetchone()[0]
            version_no = int(max_version) + 1
            connection.execute(
                "UPDATE draft_versions SET is_current = 0 "
                "WHERE post_id = ? AND is_current = 1",
                (post_id,),
            )
            cursor = connection.execute(
                "INSERT INTO draft_versions (post_id, version_no, round_no, origin, "
                "title, body, created_at, is_current) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    post_id,
                    version_no,
                    int(round_no),
                    origin,
                    title,
                    body,
                    to_iso(moment),
                ),
            )
            new_rounds = max(current_rounds, int(round_no)) if origin == (
                DraftOrigin.AI.value
            ) else current_rounds
            connection.execute(
                "UPDATE posts SET draft_title = ?, draft_text = ?, rewrite_rounds = ?, "
                "last_activity_at = ? WHERE id = ?",
                (title, body, new_rounds, to_iso(moment), post_id),
            )
            row = connection.execute(
                "SELECT * FROM draft_versions WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return DraftVersion.from_row(row)

    async def get_draft_versions(self, post_id: int) -> list[DraftVersion]:
        """Все версии черновика по возрастанию номера (для отката)."""
        return await asyncio.to_thread(self._get_draft_versions_sync, post_id)

    def _get_draft_versions_sync(self, post_id: int) -> list[DraftVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM draft_versions WHERE post_id = ? "
                "ORDER BY version_no ASC",
                (post_id,),
            ).fetchall()
        return [DraftVersion.from_row(row) for row in rows]

    async def get_draft_version(
        self, post_id: int, version_no: int
    ) -> DraftVersion | None:
        """Конкретная версия черновика."""
        return await asyncio.to_thread(
            self._get_draft_version_sync, post_id, version_no
        )

    def _get_draft_version_sync(
        self, post_id: int, version_no: int
    ) -> DraftVersion | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM draft_versions WHERE post_id = ? AND version_no = ?",
                (post_id, version_no),
            ).fetchone()
        return DraftVersion.from_row(row) if row is not None else None

    async def get_current_draft(self, post_id: int) -> DraftVersion | None:
        """Текущая версия черновика (``is_current = 1``)."""
        return await asyncio.to_thread(self._get_current_draft_sync, post_id)

    def _get_current_draft_sync(self, post_id: int) -> DraftVersion | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM draft_versions WHERE post_id = ? AND is_current = 1",
                (post_id,),
            ).fetchone()
        return DraftVersion.from_row(row) if row is not None else None

    async def rollback_draft(
        self, post_id: int, version_no: int, *, created_at: datetime | None = None
    ) -> DraftVersion:
        """Откатить черновик к версии ``version_no``.

        История не переписывается: текст копируется в новую текущую версию
        с происхождением ``rollback``, поэтому откат тоже можно откатить.
        """
        source = await self.get_draft_version(post_id, version_no)
        if source is None:
            raise StorageError(
                f"Пост {post_id}: версия черновика {version_no} не найдена."
            )
        return await self.add_draft_version(
            post_id,
            title=source.title,
            body=source.body,
            origin=DraftOrigin.ROLLBACK,
            round_no=0,
            created_at=created_at,
        )

    async def count_rewrite_rounds(self, post_id: int) -> int:
        """Сколько кругов рерайта ИИ прошёл черновик (для подсказки после 3–4)."""
        return await asyncio.to_thread(self._count_rewrite_rounds_sync, post_id)

    def _count_rewrite_rounds_sync(self, post_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(DISTINCT round_no) FROM draft_versions "
                "WHERE post_id = ? AND origin = ? AND round_no > 0",
                (post_id, DraftOrigin.AI.value),
            ).fetchone()
            if row is not None and int(row[0]) > 0:
                return int(row[0])
            fallback = connection.execute(
                "SELECT rewrite_rounds FROM posts WHERE id = ?", (post_id,)
            ).fetchone()
        return int(fallback[0]) if fallback is not None else 0

    # ------------------------------------------------------------------
    # Бэкап
    # ------------------------------------------------------------------
    async def backup(
        self,
        backup_dir: str | Path | None = None,
        *,
        keep: int = 7,
        now: datetime | None = None,
    ) -> Path:
        """Суточный снимок БД через ``VACUUM INTO`` (без остановки бота)."""
        return await backup_database_async(
            self.path, backup_dir, keep=keep, now=now
        )

    # ------------------------------------------------------------------
    # Внутренние помощники
    # ------------------------------------------------------------------
    @staticmethod
    def _fetch_post_row(
        connection: sqlite3.Connection, post_id: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_POST_COLUMNS} FROM posts WHERE id = ?", (post_id,)
        ).fetchone()

    def _require_post_row(
        self, connection: sqlite3.Connection, post_id: int
    ) -> sqlite3.Row:
        row = self._fetch_post_row(connection, post_id)
        if row is None:
            raise PostNotFoundError(f"Пост {post_id} не найден.")
        return row

    def _list_status_sync(
        self, status: PostStatus, limit: int | None, newest_first: bool
    ) -> list[Post]:
        order = "DESC" if newest_first else "ASC"
        limit_value = self.queue_visible_limit if limit is None else int(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_POST_COLUMNS} FROM posts WHERE status = ? "
                f"ORDER BY created_at {order}, id {order} LIMIT ?",
                (status.value, limit_value),
            ).fetchall()
        return [Post.from_row(row) for row in rows]


# ----------------------------------------------------------------------
# Бэкап БД
# ----------------------------------------------------------------------
def _prune_backups(directory: Path, prefix: str, keep: int) -> list[Path]:
    """Удалить старые снимки, оставив ``keep`` самых свежих."""
    if keep <= 0:
        return []
    files = sorted(directory.glob(f"{prefix}-*.db"), key=lambda item: item.name)
    removed: list[Path] = []
    for old in files[:-keep]:
        try:
            old.unlink()
            removed.append(old)
        except OSError as exc:  # не критично: следующий запуск попробует снова
            logger.warning("Не удалось удалить старый бэкап %s: %s", old, exc)
    return removed


def backup_database(
    db_path: str | Path,
    backup_dir: str | Path | None = None,
    *,
    prefix: str = "bot",
    keep: int = 7,
    now: datetime | None = None,
) -> Path:
    """Сделать согласованный снимок БД в ``backup_dir`` (по умолчанию ``data/backups``).

    Используется ``VACUUM INTO``: получается компактная копия без остановки
    бота и без чтения незакоммиченных данных. Старые снимки (сверх ``keep``)
    удаляются.

    :returns: путь к созданному файлу.
    :raises FileNotFoundError: БД ещё не создана.
    """
    source = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"База данных не найдена: {source}")

    target_dir = Path(backup_dir) if backup_dir is not None else source.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = _as_datetime(now).strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{prefix}-{stamp}.db"
    suffix = 1
    while target.exists():
        target = target_dir / f"{prefix}-{stamp}-{suffix}.db"
        suffix += 1

    connection = sqlite3.connect(str(source), timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000.0)
    try:
        connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS:d}")
        connection.execute("VACUUM INTO ?", (str(target),))
    except Exception:
        # Не оставляем недописанный файл, иначе он попадёт в ротацию.
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    finally:
        connection.close()

    _prune_backups(target_dir, prefix, keep)
    logger.info("Бэкап БД создан: %s", target)
    return target


async def backup_database_async(
    db_path: str | Path,
    backup_dir: str | Path | None = None,
    *,
    prefix: str = "bot",
    keep: int = 7,
    now: datetime | None = None,
) -> Path:
    """Асинхронная обёртка :func:`backup_database` для планировщика."""
    return await asyncio.to_thread(
        backup_database,
        db_path,
        backup_dir,
        prefix=prefix,
        keep=keep,
        now=now,
    )


__all__ = [
    "Database",
    "DuplicatePostError",
    "InvalidTransitionError",
    "MIGRATIONS",
    "OwnerBusyError",
    "PostNotFoundError",
    "SCHEMA_VERSION",
    "SchemaVersionError",
    "StorageError",
    "backup_database",
    "backup_database_async",
]
