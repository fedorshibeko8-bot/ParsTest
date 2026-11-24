"""Тесты настроек, не связанных с отбором новостей.

Главная проверка — правила для ключей ИИ: вне `DRY_RUN` достаточно
одного провайдера. Пока бот работает только с Gemini, отсутствие
`GROQ_API_KEY` не должно мешать запуску.
"""

from __future__ import annotations

import pytest

from bot.config import ConfigError, load_settings

CHANNEL = "-1001234567890"


def _read(monkeypatch: pytest.MonkeyPatch, tmp_path, **env: str):
    """Прочитать Settings из окружения, не трогая локальный .env."""
    for name in (
        "DRY_RUN",
        "CHANNEL_ID",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "DATABASE_PATH",
        "IMAGES_DIR",
        "LOGS_DIR",
        "BACKUP_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("DRY_RUN", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings(env_path=tmp_path / "missing.env")


def test_gemini_only_is_enough_for_live_mode(monkeypatch, tmp_path) -> None:
    settings = _read(
        monkeypatch,
        tmp_path,
        DRY_RUN="0",
        CHANNEL_ID=CHANNEL,
        GEMINI_API_KEY="gemini-key",
    )

    assert settings.gemini_api_key == "gemini-key"
    assert settings.groq_api_key == ""
    assert any("только через Gemini" in warning for warning in settings.warnings)


def test_groq_only_is_enough_for_live_mode(monkeypatch, tmp_path) -> None:
    settings = _read(
        monkeypatch,
        tmp_path,
        DRY_RUN="0",
        CHANNEL_ID=CHANNEL,
        GROQ_API_KEY="groq-key",
    )

    assert settings.groq_api_key == "groq-key"
    assert any("только через Groq" in warning for warning in settings.warnings)


def test_no_ai_keys_is_config_error(monkeypatch, tmp_path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        _read(monkeypatch, tmp_path, DRY_RUN="0", CHANNEL_ID=CHANNEL)

    assert "ключ ИИ" in str(excinfo.value)


def test_placeholder_key_counts_as_missing(monkeypatch, tmp_path) -> None:
    with pytest.raises(ConfigError):
        _read(
            monkeypatch,
            tmp_path,
            DRY_RUN="0",
            CHANNEL_ID=CHANNEL,
            GEMINI_API_KEY="PASTE_GEMINI_API_KEY",
        )


def test_dry_run_needs_no_keys(monkeypatch, tmp_path) -> None:
    settings = _read(monkeypatch, tmp_path)

    assert settings.gemini_api_key == ""
    assert settings.groq_api_key == ""


def test_data_paths_are_configurable(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "persistent"
    settings = _read(
        monkeypatch,
        tmp_path,
        DATABASE_PATH=str(data_dir / "bot.db"),
        IMAGES_DIR=str(data_dir / "images"),
        LOGS_DIR=str(data_dir / "logs"),
        BACKUP_DIR=str(data_dir / "backups"),
    )

    assert settings.database_path == data_dir / "bot.db"
    assert settings.images_dir == data_dir / "images"
    assert settings.logs_dir == data_dir / "logs"
    assert settings.backup_dir == data_dir / "backups"
    settings.ensure_directories()
    assert all(
        path.is_dir()
        for path in (
            settings.database_path.parent,
            settings.images_dir,
            settings.logs_dir,
            settings.backup_dir,
        )
    )


def test_backup_dir_defaults_next_to_database(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "persistent"
    settings = _read(monkeypatch, tmp_path, DATABASE_PATH=str(data_dir / "bot.db"))

    assert settings.backup_dir == data_dir / "backups"
