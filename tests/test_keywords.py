"""Тесты отбора новостей по ключевым словам (задача T04).

Проверяем только чистую логику: варианты написания ключей, границы слов,
поиск по заголовку и лиду, разбор ``NEWS_KEYWORDS`` из конфигурации.
Сеть и БД не нужны.
"""

from __future__ import annotations

import pytest

from bot.config import DEFAULT_NEWS_KEYWORDS, ConfigError, load_settings
from bot.filters.keywords import (
    KeywordMatcher,
    build_matcher,
    compile_keyword,
    find_keywords,
    is_relevant,
    normalize,
)

DEFAULT = DEFAULT_NEWS_KEYWORDS


# ----------------------------------------------------------------------
# Нормализация текста
# ----------------------------------------------------------------------
def test_normalize_lowercases_and_collapses_spaces() -> None:
    assert normalize("  GTA   VI\tNews  ") == "gta vi news"


def test_normalize_unifies_dashes() -> None:
    assert normalize("GTA—6") == normalize("GTA–6") == normalize("GTA-6")


def test_normalize_yo_to_e() -> None:
    assert normalize("Ёлка") == "елка"


def test_normalize_empty() -> None:
    assert normalize("") == ""


# ----------------------------------------------------------------------
# Варианты написания ключей
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "title",
    [
        "GTA 6 trailer breaks records",
        "gta 6 is delayed",
        "GTA-6 release date",
        "GTA6 news roundup",
        "GTA–6 screenshot leaked",
        "GTA_6: what we know",
        "(GTA 6) — first look",
    ],
)
def test_gta6_variants_match(title: str) -> None:
    assert is_relevant(title, "", DEFAULT) is True


@pytest.mark.parametrize(
    "title",
    [
        "GTA VI announced",
        "GTAVI logo spotted",
        "GTA-VI speculation",
    ],
)
def test_gta_vi_variants_match(title: str) -> None:
    assert is_relevant(title, "", DEFAULT) is True


def test_full_name_matches() -> None:
    assert is_relevant("Grand Theft Auto VI: новые подробности", "", DEFAULT)


def test_rockstar_possessive_matches() -> None:
    # Граница слова не должна ломаться на притяжательном 's.
    assert is_relevant("Rockstar's next game", "", DEFAULT)


# ----------------------------------------------------------------------
# Ложные срабатывания
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "title",
    [
        "GTA Online update adds new cars",  # это не шестая часть
        "GTA V remaster for consoles",      # пятая часть
        "GTA 60 fps mode tested",           # «60» не должно ловить «6»
        "RockstarGames acquired a studio",  # ключ внутри другого слова
    ],
)
def test_false_positives_are_ignored(title: str) -> None:
    assert is_relevant(title, "", DEFAULT) is False


def test_empty_title_and_lead() -> None:
    assert is_relevant("", "", DEFAULT) is False


def test_unrelated_news() -> None:
    assert is_relevant("Студия Rocksteady анонсировала Batman", "Подробности внутри", DEFAULT) is False


# ----------------------------------------------------------------------
# Поиск по лиду и несколько полей
# ----------------------------------------------------------------------
def test_lead_is_checked_when_title_is_neutral() -> None:
    assert is_relevant(
        "Студия сделала заявление",
        "Подробности обещают раскрыть на презентации, посвящённой GTA 6.",
        DEFAULT,
    )


def test_find_keywords_reports_matches() -> None:
    found = find_keywords("Rockstar shows GTA 6", "Grand Theft Auto VI coming", DEFAULT)
    assert set(found) == {"GTA 6", "Grand Theft Auto", "Rockstar"}


def test_find_returns_original_spelling() -> None:
    assert find_keywords("gta 6", "", DEFAULT) == ("GTA 6",)


# ----------------------------------------------------------------------
# Пользовательские ключи и построение сопоставителя
# ----------------------------------------------------------------------
def test_custom_keywords_replace_defaults() -> None:
    assert is_relevant("The Legend of Zelda returns", "", ("Zelda",)) is True
    assert is_relevant("GTA 6 returns", "", ("Zelda",)) is False


def test_matcher_deduplicates_keywords() -> None:
    matcher = build_matcher(("GTA 6", "gta 6", " GTA   6 "))
    assert matcher.keywords == ("GTA 6",)


def test_matcher_skips_blank_keywords() -> None:
    matcher = KeywordMatcher.from_keywords(("", "   ", "Rockstar"))
    assert matcher.keywords == ("Rockstar",)


def test_compile_keyword_rejects_empty() -> None:
    with pytest.raises(ValueError):
        compile_keyword("   ")


def test_build_matcher_is_cached() -> None:
    assert build_matcher(DEFAULT) is build_matcher(DEFAULT)


# ----------------------------------------------------------------------
# Конфигурация NEWS_KEYWORDS
# ----------------------------------------------------------------------
def _read_settings(monkeypatch: pytest.MonkeyPatch, tmp_path, **env: str):
    """Прочитать Settings из окружения, не трогая локальный .env."""
    monkeypatch.setenv("BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("DRY_RUN", "1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings(env_path=tmp_path / "missing.env")


def test_keywords_default_when_not_configured(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NEWS_KEYWORDS", raising=False)
    settings = _read_settings(monkeypatch, tmp_path)
    assert settings.news_keywords == DEFAULT_NEWS_KEYWORDS


def test_keywords_are_parsed_and_deduplicated(monkeypatch, tmp_path) -> None:
    settings = _read_settings(
        monkeypatch,
        tmp_path,
        NEWS_KEYWORDS="  Zelda ; gta 6,GTA 6 ,  Mario  ",
    )
    assert settings.news_keywords == ("Zelda", "gta 6", "Mario")


def test_keywords_with_only_separators_is_config_error(monkeypatch, tmp_path) -> None:
    with pytest.raises(ConfigError):
        _read_settings(monkeypatch, tmp_path, NEWS_KEYWORDS=",,;;  ,")
