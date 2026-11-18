"""Список RSS-источников (п. 2 ТЗ).

Источники — данные, а не код: чтобы добавить сайт, достаточно дописать
элемент в :data:`SOURCES`. Запасные источники второго этапа (PC Gamer,
Kotaku, Dexerto, Insider Gaming) сюда не входят.

Поле ``key`` — короткий машинный идентификатор (для имён задач и логов),
``name`` — как сайт называется в карточке новости, ``url`` — адрес ленты.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FeedSource:
    """Один RSS/Atom-источник новостей."""

    key: str
    name: str
    url: str
    enabled: bool = True


# Порядок = порядок в ТЗ. Все ленты проверены на ноябрь 2026.
SOURCES: tuple[FeedSource, ...] = (
    FeedSource(
        key="vgc",
        name="VGC",
        url="https://videogameschronicle.com/feed/",
    ),
    FeedSource(
        key="ign",
        name="IGN",
        url="https://feeds.feedburner.com/ign/games-all",
    ),
    FeedSource(
        key="eurogamer",
        name="Eurogamer",
        url="https://eurogamer.net/feed",
    ),
    FeedSource(
        key="gamesradar",
        name="GamesRadar+",
        url="https://gamesradar.com/feeds.xml",
    ),
    FeedSource(
        key="gamespot",
        name="GameSpot",
        url="https://gamespot.com/feeds/news/",
    ),
)

# Быстрый поиск источника по ключу — на случай ручных вызовов и тестов.
SOURCES_BY_KEY: dict[str, FeedSource] = {source.key: source for source in SOURCES}


def get_source(key: str) -> FeedSource:
    """Источник по ключу.

    :raises KeyError: если ключа нет в :data:`SOURCES`.
    """
    return SOURCES_BY_KEY[key]


__all__ = [
    "SOURCES",
    "SOURCES_BY_KEY",
    "FeedSource",
    "get_source",
]
