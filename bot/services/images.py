"""Скачивание и удаление картинок новостей (задача T06).

Картинка нужна только для поста, поэтому скачиваем её в момент обнаружения
новости (T07), а не при публикации: к моменту показа карточки файл уже на
диске, и публикация не зависит от доступности чужого сайта.

Что делает модуль:

* берёт URL картинки из :class:`bot.parsers.rss.FeedItem` (извлечение —
  ``extract_image_url`` в T03, здесь только загрузка);
* качает потоком через ``httpx``, не читая в память больше лимита
  (``IMAGE_MAX_MB``, по умолчанию 10 МБ);
* проверяет реальный тип по «магическим» байтам, а не только по
  ``Content-Type``: HTML-страница ошибки не должна сохраниться как картинка;
* пишет файл атомарно в ``settings.images_dir`` под уникальным именем
  (``uuid4`` + расширение) — два поста с одинаковой картинкой не мешают
  друг другу при удалении;
* удаляет файлы через ``IMAGE_KEEP_HOURS`` (48 ч) после ``published`` или
  ``dropped`` и очищает ``image_path`` в БД.

Чего здесь нет: выбора «лучшей» картинки из статьи (og:image) — это фоллбэк
второго этапа; ресайза и сжатия — Telegram сам готовит превью.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

import httpx

from bot.config import Settings
from bot.db import Database
from bot.models import Post
from bot.parsers.rss import USER_AGENT

logger = logging.getLogger(__name__)

# Разрешённые форматы: Telegram принимает их как фото без доработок.
ALLOWED_IMAGE_TYPES: Final[dict[str, str]] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}

# Ошибки скачивания, которые не должны ронять обработку новости.
class ImageError(Exception):
    """Картинку не удалось получить или сохранить."""


class ImageDownloadError(ImageError):
    """Сеть, таймаут или HTTP-ошибка при скачивании картинки."""


class ImageTypeError(ImageError):
    """Содержимое не является поддерживаемой картинкой."""


class ImageTooLargeError(ImageError):
    """Картинка больше лимита ``IMAGE_MAX_MB``."""

    def __init__(self, message: str, *, limit_bytes: int, size_bytes: int) -> None:
        super().__init__(message)
        self.limit_bytes = limit_bytes
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    """Скачанный файл картинки."""

    path: Path
    mime_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ImageCleanupResult:
    """Итог прохода по завершённым постам."""

    checked: int = 0
    removed: int = 0
    cleared: int = 0
    failed: int = 0


# ----------------------------------------------------------------------
# Определение типа по содержимому
# ----------------------------------------------------------------------
def detect_image_type(data: bytes) -> str | None:
    """Определить MIME-тип картинки по первым байтам.

    Возвращает ``None`` для всего, что не входит в
    :data:`ALLOWED_IMAGE_TYPES` (в том числе SVG и HTML).
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


def extension_for(mime_type: str) -> str:
    """Расширение файла для MIME-типа картинки."""
    try:
        return ALLOWED_IMAGE_TYPES[mime_type]
    except KeyError as exc:
        raise ImageTypeError(f"Неподдерживаемый тип картинки: {mime_type}") from exc


# ----------------------------------------------------------------------
# Скачивание
# ----------------------------------------------------------------------
def build_client(settings: Settings) -> httpx.AsyncClient:
    """HTTP-клиент для картинок: свой User-Agent, редиректы, таймаут."""
    return httpx.AsyncClient(
        timeout=settings.http_timeout_sec,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _content_length(response: httpx.Response) -> int | None:
    """Размер из ``Content-Length``; мусор и отрицательные значения — ``None``."""
    raw = response.headers.get("content-length")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _looks_like_html(content_type: str) -> bool:
    """Явно не картинка: страница ошибки, заглушка, JSON и т.п."""
    if not content_type:
        return False
    if content_type.startswith("image/"):
        return False
    return content_type in {
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "text/plain",
    }


async def download_image(
    url: str,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    max_bytes: int | None = None,
) -> DownloadedImage:
    """Скачать картинку и сохранить её в ``settings.images_dir``.

    :param url: прямой адрес картинки (из RSS-enclosure или ``media:content``).
    :param settings: настройки — лимит размера, таймаут, каталог картинок.
    :param client: готовый клиент (в тестах — с ``httpx.MockTransport``);
        если не задан, создаётся и закрывается внутри.
    :param max_bytes: переопределить лимит (в тестах; по умолчанию из настроек).
    :raises ImageError: любая проблема — сеть, статус, тип, размер, запись.
    """
    if not url:
        raise ImageDownloadError("Пустой URL картинки.")
    if not url.lower().startswith(("http://", "https://")):
        raise ImageDownloadError(f"Неподдерживаемая схема URL: {url!r}")

    limit = settings.image_max_bytes if max_bytes is None else int(max_bytes)
    own_client = client is None
    if client is None:
        client = build_client(settings)

    try:
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()

                declared = _content_length(response)
                if declared is not None and declared > limit:
                    raise ImageTooLargeError(
                        f"Картинка {url} больше лимита: {declared} > {limit} байт.",
                        limit_bytes=limit,
                        size_bytes=declared,
                    )

                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if _looks_like_html(content_type):
                    raise ImageTypeError(
                        f"По адресу {url} не картинка: Content-Type {content_type!r}."
                    )

                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > limit:
                        raise ImageTooLargeError(
                            f"Картинка {url} превысила лимит {limit} байт при загрузке.",
                            limit_bytes=limit,
                            size_bytes=len(payload),
                        )
        except ImageError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ImageDownloadError(
                f"Источник картинки ответил {exc.response.status_code}: {url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ImageDownloadError(f"Не удалось скачать картинку {url}: {exc}") from exc
    finally:
        if own_client and client is not None:
            await client.aclose()

    data = bytes(payload)
    mime_type = detect_image_type(data)
    if mime_type is None:
        raise ImageTypeError(
            f"По адресу {url} не картинка поддерживаемого формата "
            f"(Content-Type {content_type or 'не указан'!r})."
        )

    path = await asyncio.to_thread(
        _write_image_atomic, data, settings.images_dir, extension_for(mime_type)
    )
    logger.info("Картинка сохранена: %s (%d байт, %s)", path, len(data), mime_type)
    return DownloadedImage(path=path, mime_type=mime_type, size_bytes=len(data))


def _write_image_atomic(data: bytes, images_dir: Path, extension: str) -> Path:
    """Записать файл через временное имя и ``os.replace`` (без «половины»)."""
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / f"{uuid4().hex}{extension}"
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return target


async def try_download_image(
    url: str | None,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> DownloadedImage | None:
    """Мягкий вариант :func:`download_image`: ошибка → ``None`` и запись в лог.

    Используется в конвейере новостей (T07): новость без картинки всё равно
    должна попасть в очередь, а причина отказа — в лог.
    """
    if not url:
        return None
    try:
        return await download_image(url, settings, client=client)
    except ImageError as exc:
        logger.warning("Картинка не скачана (%s): %s", url, exc)
        return None


async def save_image_bytes(data: bytes, settings: Settings) -> DownloadedImage:
    """Сохранить присланные админом байты картинки (T08).

    Тип и размер проверяются так же, как при скачивании с сайта: Telegram
    уже сжимает фото, но лимит и формат подтверждаем сами.
    """
    if not data:
        raise ImageTypeError("Пустой файл картинки.")
    limit = settings.image_max_bytes
    if len(data) > limit:
        raise ImageTooLargeError(
            f"Картинка больше лимита: {len(data)} > {limit} байт.",
            limit_bytes=limit,
            size_bytes=len(data),
        )
    mime_type = detect_image_type(data)
    if mime_type is None:
        raise ImageTypeError("Файл не является картинкой JPEG/PNG/GIF/WebP/AVIF.")
    path = await asyncio.to_thread(
        _write_image_atomic, data, settings.images_dir, extension_for(mime_type)
    )
    logger.info("Картинка от админа сохранена: %s (%d байт, %s)", path, len(data), mime_type)
    return DownloadedImage(path=path, mime_type=mime_type, size_bytes=len(data))


# ----------------------------------------------------------------------
# Удаление старых файлов
# ----------------------------------------------------------------------
def remove_image_file(path: str | Path | None, images_dir: Path) -> bool:
    """Удалить файл картинки, только если он лежит внутри ``images_dir``.

    Защита от «пути из БД»: даже если в ``image_path`` окажется что-то вне
    каталога картинок, файл снаружи не будет удалён. Отсутствующий файл —
    не ошибка (он мог быть удалён вручную).
    """
    if not path:
        return False
    base = images_dir.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    if resolved == base or not resolved.is_relative_to(base):
        logger.warning("Пропущено удаление файла вне каталога картинок: %s", path)
        return False
    try:
        resolved.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Не удалось удалить файл картинки %s: %s", resolved, exc)
        return False
    return True


async def cleanup_post_images(
    db: Database,
    settings: Settings,
    *,
    now=None,
    keep_hours: int | None = None,
) -> ImageCleanupResult:
    """Удалить файлы картинок завершённых постов и очистить ``image_path``.

    Посты старше ``IMAGE_KEEP_HOURS`` (48 ч) после ``published``/``dropped``
    перестают занимать диск. Если файла уже нет, путь в БД всё равно
    очищается, чтобы не проверять этот пост на каждом проходе. Ошибка
    удаления оставляет путь: следующий проход попробует снова.
    """
    hours = settings.image_keep_hours if keep_hours is None else int(keep_hours)
    posts = await db.list_for_image_cleanup(now=now, keep_hours=hours)

    checked = removed = cleared = failed = 0
    for post in posts:
        checked += 1
        if await asyncio.to_thread(remove_image_file, post.image_path, settings.images_dir):
            removed += 1
        elif _file_still_exists(post, settings.images_dir):
            # Не смогли удалить (например, файл занят) — оставим на следующий раз.
            failed += 1
            continue
        await db.set_image(post.id, image_path=None)
        cleared += 1

    if checked:
        logger.info(
            "Очистка картинок: проверено %d, удалено файлов %d, очищено записей %d, ошибок %d",
            checked,
            removed,
            cleared,
            failed,
        )
    return ImageCleanupResult(
        checked=checked, removed=removed, cleared=cleared, failed=failed
    )


def _file_still_exists(post: Post, images_dir: Path) -> bool:
    """Существует ли файл поста внутри каталога картинок (после неудачного unlink)."""
    if not post.image_path:
        return False
    base = images_dir.resolve()
    candidate = Path(post.image_path)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved.is_relative_to(base) and resolved.is_file()


async def cleanup_loop(
    db: Database,
    settings: Settings,
    *,
    interval_hours: int = 6,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Фоновая очистка картинок (подключается в T11 вместе с другими циклами)."""
    interval_sec = max(1, int(interval_hours)) * 3600
    while True:
        try:
            await cleanup_post_images(db, settings)
        except Exception:
            logger.exception("Ошибка фоновой очистки картинок")
        if stop_event is None:
            await asyncio.sleep(interval_sec)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            continue
        logger.info("Цикл очистки картинок остановлен.")
        return


__all__ = [
    "ALLOWED_IMAGE_TYPES",
    "DownloadedImage",
    "ImageCleanupResult",
    "ImageDownloadError",
    "ImageError",
    "ImageTooLargeError",
    "ImageTypeError",
    "build_client",
    "cleanup_loop",
    "cleanup_post_images",
    "detect_image_type",
    "download_image",
    "extension_for",
    "remove_image_file",
    "save_image_bytes",
    "try_download_image",
]
