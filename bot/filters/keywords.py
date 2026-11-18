"""Отбор новостей по ключевым словам (задача T04).

Правила из п. 3 ТЗ:

* проверяются заголовок и лид (лид уже очищен от HTML и обрезан до ~200
  символов в :mod:`bot.parsers.rss`);
* список ключей конфигурируемый — :attr:`bot.config.Settings.news_keywords`
  (переменная ``NEWS_KEYWORDS``), по умолчанию «GTA 6», «GTA VI»,
  «Grand Theft Auto», «Rockstar»;
* регистр не важен;
* допускаются варианты написания: «GTA-6», «GTA6», «GTA–6», «(GTA VI)»;
* никакого LLM — только строковый поиск.

Модуль не знает ни о сети, ни о БД: на вход подаются строки, на выходе —
совпадение и список найденных ключей (последнее пригодится в логах T07).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

# Разделители, которые могут стоять между словами ключа:
# «GTA 6», «GTA-6», «GTA_6», «GTA–6», «GTA.6» — всё один ключ.
_SEPARATOR_CHARS = r"[\s\-–—_.]"

# Границы слова: ключ не должен «прилипать» к соседним буквам или цифрам,
# иначе «GTA 6» найдётся в «GTA 60», а «Rockstar» — в «RockstarGames».
_LEFT_BOUNDARY = r"(?<![0-9a-zа-яё])"
_RIGHT_BOUNDARY = r"(?![0-9a-zа-яё])"

# Разбиение ключа на слова — по тем же разделителям.
_KEYWORD_SPLIT_RE = re.compile(_SEPARATOR_CHARS + "+")


def normalize(text: str) -> str:
    """Привести текст к виду для поиска: нижний регистр, единые пробелы.

    «ё» приравнивается к «е», типографские тире — к дефису: это снимает
    самые частые расхождения между источниками. Пунктуация не вырезается,
    её обрабатывают границы слова в шаблоне ключа.
    """
    if not text:
        return ""
    lowered = text.casefold().replace("ё", "е")
    lowered = re.sub(r"[\u2010-\u2015\u2212]", "-", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def compile_keyword(keyword: str) -> re.Pattern[str]:
    """Собрать регулярное выражение для одного ключевого слова.

    Слова ключа соединяются через «любой разделитель или ничего»
    (``[\\s\\-–—_.]*``), поэтому «GTA 6» находит и «GTA6», и «GTA-6».
    Границы слова не дают ключу срабатывать внутри других слов и чисел.

    :raises ValueError: если в ключе нет ни одного слова.
    """
    words = [word for word in _KEYWORD_SPLIT_RE.split(normalize(keyword)) if word]
    if not words:
        raise ValueError("Ключевое слово пустое")
    # Между словами разрешаем любое число разделителей либо ничего.
    body = f"{_SEPARATOR_CHARS}*".join(re.escape(word) for word in words)
    return re.compile(f"{_LEFT_BOUNDARY}{body}{_RIGHT_BOUNDARY}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class KeywordMatcher:
    """Набор ключей и готовых шаблонов для поиска по тексту.

    Создавать через :meth:`from_keywords` (или :func:`build_matcher`):
    он убирает повторы и «мусорные» значения. Экземпляр неизменяемый,
    поэтому его безопасно кэшировать и переиспользовать между опросами.
    """

    keywords: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]

    @classmethod
    def from_keywords(cls, keywords: Iterable[str]) -> "KeywordMatcher":
        """Построить сопоставитель: обрезать, убрать пустые и повторы."""
        cleaned: list[str] = []
        seen: set[str] = set()
        for keyword in keywords:
            text = " ".join(str(keyword).split())
            if not text:
                continue
            key = normalize(text)
            if not key or key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        return cls(
            keywords=tuple(cleaned),
            patterns=tuple(compile_keyword(keyword) for keyword in cleaned),
        )

    def find(self, text: str) -> tuple[str, ...]:
        """Найденные в тексте ключи в исходном написании (порядок списка)."""
        prepared = normalize(text)
        if not prepared:
            return ()
        return tuple(
            keyword
            for keyword, pattern in zip(self.keywords, self.patterns)
            if pattern.search(prepared)
        )

    def matches(self, text: str) -> bool:
        """Есть ли в тексте хотя бы один ключ."""
        return bool(self.find(text))

    def find_in(self, *parts: str) -> tuple[str, ...]:
        """Найденные ключи по нескольким полям сразу (заголовок + лид)."""
        return self.find(" ".join(part for part in parts if part))

    def is_relevant(self, *parts: str) -> bool:
        """Подходит ли новость по теме (заголовок и/или лид)."""
        return bool(self.find_in(*parts))


@lru_cache(maxsize=16)
def build_matcher(keywords: tuple[str, ...]) -> KeywordMatcher:
    """Сопоставитель для набора ключей (кэш по неизменяемому кортежу).

    Настройки меняются только при перезапуске бота, поэтому кэш из 16
    вариантов покрывает все реальные сценарии и не растёт.
    """
    return KeywordMatcher.from_keywords(keywords)


def find_keywords(
    title: str, lead: str, keywords: Iterable[str]
) -> tuple[str, ...]:
    """Найти ключи в заголовке и лиде новости (обёртка для T05/T07)."""
    return build_matcher(tuple(keywords)).find_in(title, lead)


def is_relevant(title: str, lead: str, keywords: Iterable[str]) -> bool:
    """Подходит ли новость по теме: заголовок или лид содержат ключ."""
    return bool(find_keywords(title, lead, keywords))


__all__ = [
    "KeywordMatcher",
    "build_matcher",
    "compile_keyword",
    "find_keywords",
    "is_relevant",
    "normalize",
]
