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
    for name in ("DRY_RUN", "CHANNEL_ID", "GEMINI_API_KEY", "GROQ_API_KEY"):
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
