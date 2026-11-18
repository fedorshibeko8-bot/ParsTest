"""Тесты картинок новостей (задача T06).

Сеть подменяется ``httpx.MockTransport``: реальные сайты не трогаются.
Проверяются определение формата по байтам, лимит размера, отказ от HTML,
атомарная запись и очистка файлов через 48 ч после публикации/снятия.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
import pytest

from bot.config import ConfigError, load_settings
from bot.services.images import (
    ALLOWED_IMAGE_TYPES,
    ImageDownloadError,
    ImageTooLargeError,
    ImageTypeError,
    cleanup_post_images,
    detect_image_type,
    download_image,
    extension_for,
    remove_image_file,
    try_download_image,
)
from tests.helpers import T0, make_settings, offset, run

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 24
WEBP = b"RIFF" + (20).to_bytes(4, "little") + b"WEBP" + b"\x00" * 16
AVIF = b"\x00\x00\x00\x20ftypavif" + b"\x00" * 16
HTML = b"<!doctype html><html><body>Not found</body></html>"


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Клиент с подменённым транспортом: в сеть не ходит."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def image_response(body: bytes, content_type: str | None = "image/png") -> httpx.Response:
    headers = {"Content-Type": content_type} if content_type else {}
    return httpx.Response(200, content=body, headers=headers)


# ----------------------------------------------------------------------
# Определение типа по содержимому
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (WEBP, "image/webp"),
        (AVIF, "image/avif"),
    ],
)
def test_detect_image_type(data: bytes, expected: str) -> None:
    assert detect_image_type(data) == expected


@pytest.mark.parametrize("data", [b"", HTML, b"%PDF-1.7", b"\x00" * 16])
def test_detect_image_type_rejects_non_images(data: bytes) -> None:
    assert detect_image_type(data) is None


def test_every_allowed_type_has_extension() -> None:
    for mime_type in ALLOWED_IMAGE_TYPES:
        assert extension_for(mime_type).startswith(".")


def test_extension_for_unknown_type_raises() -> None:
    with pytest.raises(ImageTypeError):
        extension_for("image/svg+xml")


# ----------------------------------------------------------------------
# Скачивание
# ----------------------------------------------------------------------
def test_download_png_by_magic_bytes_without_content_type(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")
    url = "https://cdn.example.com/picture"  # без расширения

    async def scenario():
        client = mock_client(lambda request: image_response(PNG, content_type=None))
        try:
            return await download_image(url, settings, client=client)
        finally:
            await client.aclose()

    result = run(scenario())

    assert result.mime_type == "image/png"
    assert result.size_bytes == len(PNG)
    assert result.path.suffix == ".png"
    assert result.path.parent == settings.images_dir
    assert result.path.is_file()
    assert result.path.read_bytes() == PNG
    # Временный файл после записи не остаётся.
    assert list(settings.images_dir.glob("*.part")) == []


def test_content_type_is_ignored_if_bytes_say_otherwise(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    async def scenario():
        client = mock_client(
            lambda request: image_response(PNG, content_type="image/jpeg")
        )
        try:
            return await download_image(
                "https://cdn.example.com/p.png", settings, client=client
            )
        finally:
            await client.aclose()

    result = run(scenario())
    assert result.mime_type == "image/png"
    assert result.path.suffix == ".png"


def test_html_declared_as_image_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    async def scenario():
        client = mock_client(
            lambda request: image_response(HTML, content_type="image/jpeg")
        )
        try:
            await download_image("https://cdn.example.com/x.jpg", settings, client=client)
        finally:
            await client.aclose()

    with pytest.raises(ImageTypeError):
        run(scenario())
    assert not settings.images_dir.exists() or list(settings.images_dir.iterdir()) == []


def test_html_content_type_is_rejected_before_saving(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    async def scenario():
        client = mock_client(
            lambda request: image_response(HTML, content_type="text/html; charset=utf-8")
        )
        try:
            await download_image("https://example.com/page", settings, client=client)
        finally:
            await client.aclose()

    with pytest.raises(ImageTypeError):
        run(scenario())


def test_content_length_over_limit_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=PNG,
            headers={"Content-Type": "image/png", "Content-Length": str(10_000)},
        )

    async def scenario():
        client = mock_client(handler)
        try:
            await download_image(
                "https://cdn.example.com/big.png", settings, client=client, max_bytes=1000
            )
        finally:
            await client.aclose()

    with pytest.raises(ImageTooLargeError) as excinfo:
        run(scenario())
    assert excinfo.value.limit_bytes == 1000
    assert excinfo.value.size_bytes == 10_000
    assert not settings.images_dir.exists() or list(settings.images_dir.iterdir()) == []


def test_stream_over_limit_without_content_length_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    def handler(request: httpx.Request) -> httpx.Response:
        # Без Content-Length: лимит проверяется уже в потоке.
        return httpx.Response(
            200,
            stream=httpx.ByteStream(PNG),
            headers={"Content-Type": "image/png"},
        )

    async def scenario():
        client = mock_client(handler)
        try:
            await download_image(
                "https://cdn.example.com/stream.png",
                settings,
                client=client,
                max_bytes=len(PNG) - 1,
            )
        finally:
            await client.aclose()

    with pytest.raises(ImageTooLargeError):
        run(scenario())
    assert not settings.images_dir.exists() or list(settings.images_dir.iterdir()) == []


def test_size_equal_to_limit_is_allowed(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    async def scenario():
        client = mock_client(lambda request: image_response(PNG))
        try:
            return await download_image(
                "https://cdn.example.com/ok.png",
                settings,
                client=client,
                max_bytes=len(PNG),
            )
        finally:
            await client.aclose()

    assert run(scenario()).size_bytes == len(PNG)


def test_http_error_becomes_download_error(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    async def scenario():
        client = mock_client(
            lambda request: httpx.Response(404, content=b"missing")
        )
        try:
            await download_image("https://cdn.example.com/404.png", settings, client=client)
        finally:
            await client.aclose()

    with pytest.raises(ImageDownloadError):
        run(scenario())


def test_network_error_becomes_download_error(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сеть недоступна", request=request)

    async def scenario():
        client = mock_client(handler)
        try:
            await download_image("https://cdn.example.com/x.png", settings, client=client)
        finally:
            await client.aclose()

    with pytest.raises(ImageDownloadError):
        run(scenario())


@pytest.mark.parametrize("url", ["", "ftp://example.com/x.png", "not-a-url"])
def test_invalid_url_is_rejected(url: str, tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")
    with pytest.raises(ImageDownloadError):
        run(download_image(url, settings))


def test_try_download_image_returns_none_on_failure(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    async def scenario():
        client = mock_client(lambda request: httpx.Response(500))
        try:
            return await try_download_image(
                "https://cdn.example.com/broken.png", settings, client=client
            )
        finally:
            await client.aclose()

    assert run(scenario()) is None


def test_try_download_image_returns_none_without_url(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")
    assert run(try_download_image(None, settings)) is None
    assert run(try_download_image("", settings)) is None


def test_try_download_image_success(tmp_path: Path) -> None:
    settings = make_settings(images_dir=tmp_path / "images")

    async def scenario():
        client = mock_client(lambda request: image_response(JPEG, "image/jpeg"))
        try:
            return await try_download_image(
                "https://cdn.example.com/ok.jpg", settings, client=client
            )
        finally:
            await client.aclose()

    result = run(scenario())
    assert result is not None and result.mime_type == "image/jpeg"


# ----------------------------------------------------------------------
# Удаление файлов
# ----------------------------------------------------------------------
def _make_finished_post(
    db,
    make_post,
    tmp_path: Path,
    *,
    status: str,
    created_at=T0,
    image_name: str = "photo.png",
    content: bytes = PNG,
):
    """Пост с картинкой на диске в нужном статусе."""
    images_dir = tmp_path / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    path = images_dir / image_name
    path.write_bytes(content)

    post = make_post(image_path=str(path), image_url="https://cdn.example.com/a.png")
    if status == "published":
        run(db.mark_published(post.id, now=created_at))
    elif status == "dropped":
        run(db.mark_dropped(post.id, reason="тест", now=created_at))
    return post, path


@pytest.mark.parametrize("status", ["published", "dropped"])
def test_cleanup_removes_file_after_keep_hours(
    db, make_post, tmp_path: Path, status: str
) -> None:
    settings = make_settings(images_dir=tmp_path / "images")
    post, path = _make_finished_post(db, make_post, tmp_path, status=status)

    result = run(
        cleanup_post_images(db, settings, now=T0 + offset(hours=49), keep_hours=48)
    )

    assert result.checked == 1
    assert result.removed == 1
    assert result.cleared == 1
    assert not path.exists()

    stored = run(db.get_post(post.id))
    assert stored is not None
    assert stored.image_path is None
    # Ссылка на источник картинки остаётся: она нужна в карточке/логах.
    assert stored.image_url == "https://cdn.example.com/a.png"


@pytest.mark.parametrize("status", ["published", "dropped"])
def test_cleanup_keeps_recent_files(
    db, make_post, tmp_path: Path, status: str
) -> None:
    settings = make_settings(images_dir=tmp_path / "images")
    post, path = _make_finished_post(db, make_post, tmp_path, status=status)

    result = run(
        cleanup_post_images(db, settings, now=T0 + offset(hours=47), keep_hours=48)
    )

    assert result.checked == 0
    assert path.exists()
    stored = run(db.get_post(post.id))
    assert stored is not None and stored.image_path == str(path)


def test_cleanup_ignores_pending_and_intercepted(
    db, make_post, tmp_path: Path
) -> None:
    settings = make_settings(images_dir=tmp_path / "images")
    pending, pending_path = _make_finished_post(
        db, make_post, tmp_path, status="pending", image_name="pending.png"
    )
    intercepted, intercepted_path = _make_finished_post(
        db, make_post, tmp_path, status="pending", image_name="intercepted.png"
    )
    run(db.mark_intercepted(intercepted.id, owner_id=1, now=T0))

    result = run(
        cleanup_post_images(db, settings, now=T0 + offset(days=10), keep_hours=48)
    )

    assert result.checked == 0
    assert pending_path.exists() and intercepted_path.exists()


def test_cleanup_clears_path_when_file_already_missing(
    db, make_post, tmp_path: Path
) -> None:
    settings = make_settings(images_dir=tmp_path / "images")
    post, path = _make_finished_post(db, make_post, tmp_path, status="published")
    path.unlink()

    result = run(
        cleanup_post_images(db, settings, now=T0 + offset(hours=49), keep_hours=48)
    )

    assert result.checked == 1
    assert result.removed == 0
    assert result.cleared == 1
    stored = run(db.get_post(post.id))
    assert stored is not None and stored.image_path is None


def test_cleanup_does_not_touch_files_outside_images_dir(
    db, make_post, tmp_path: Path
) -> None:
    settings = make_settings(images_dir=tmp_path / "images")
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)

    post = make_post(image_path=str(outside))
    run(db.mark_published(post.id, now=T0))

    result = run(
        cleanup_post_images(db, settings, now=T0 + offset(hours=49), keep_hours=48)
    )

    assert result.removed == 0
    assert result.cleared == 1
    assert outside.exists()  # файл вне каталога картинок не тронут
    stored = run(db.get_post(post.id))
    assert stored is not None and stored.image_path is None


def test_remove_image_file_handles_empty_and_missing(tmp_path: Path) -> None:
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    assert remove_image_file(None, images_dir) is False
    assert remove_image_file("", images_dir) is False
    assert remove_image_file(images_dir / "missing.png", images_dir) is False


def test_remove_image_file_deletes_inside_dir(tmp_path: Path) -> None:
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    path = images_dir / "x.png"
    path.write_bytes(PNG)

    assert remove_image_file(path, images_dir) is True
    assert not path.exists()


# ----------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------
def _read_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str):
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("DRY_RUN", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings(env_path=tmp_path / "missing.env")


def test_image_settings_have_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IMAGE_MAX_MB", raising=False)
    monkeypatch.delenv("IMAGE_KEEP_HOURS", raising=False)
    settings = _read_settings(monkeypatch, tmp_path)
    assert settings.image_max_mb == 10
    assert settings.image_keep_hours == 48
    assert settings.image_max_bytes == 10 * 1024 * 1024


def test_image_settings_are_read_from_env(monkeypatch, tmp_path: Path) -> None:
    settings = _read_settings(
        monkeypatch, tmp_path, IMAGE_MAX_MB="5", IMAGE_KEEP_HOURS="24"
    )
    assert settings.image_max_mb == 5
    assert settings.image_keep_hours == 24


@pytest.mark.parametrize(
    "env",
    [{"IMAGE_MAX_MB": "0"}, {"IMAGE_KEEP_HOURS": "0"}, {"IMAGE_MAX_MB": "abc"}],
)
def test_invalid_image_settings_are_config_errors(
    monkeypatch, tmp_path: Path, env: dict[str, str]
) -> None:
    with pytest.raises(ConfigError):
        _read_settings(monkeypatch, tmp_path, **env)
