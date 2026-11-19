"""Требования к тексту поста и его очистка (общие для рерайта и публикации).

Здесь живут лимиты из ТЗ и всё, что превращает ответ ИИ или текст админа
в то, что можно отправить в канал: снятие ссылок, хэштегов и упоминаний,
схлопывание пробелов, проверка длин. Модуль намеренно не знает ни про
Telegram, ни про БД — его можно проверять отдельно, а использовать и в
``bot.services.rewrite`` (T09), и в ``bot.services.publisher`` (T10).
"""

from __future__ import annotations

import re
from typing import Final

#: Заголовок: 40–70 символов (п. 9 ТЗ).
TITLE_MIN: Final[int] = 40
TITLE_MAX: Final[int] = 70

#: Тело: 120–350 символов.
BODY_MIN: Final[int] = 120
BODY_MAX: Final[int] = 350

#: Весь пост: 160–420 символов без учёта HTML-разметки.
TOTAL_MIN: Final[int] = 160
TOTAL_MAX: Final[int] = 420

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_TAG_RE = re.compile(r"(?<!\w)#\w+")
_MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{4,}")


def one_line(value: object) -> str:
    """Схлопнуть пробелы и снять кавычки по краям."""
    text = " ".join(str(value or "").split())
    return text.strip(" \"'«»“”„")


def clean_text(value: object) -> str:
    """Убрать запрещённое в публикации: ссылки, хэштеги, упоминания.

    ТЗ требует, чтобы в канале не было ссылок на источник, хэштегов и
    подписи канала. ИИ обычно и так их не пишет, но ручная правка или
    ответ модели могут их добавить — чистим перед проверкой длин.
    """
    text = " ".join(str(value or "").split())
    text = _URL_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    return " ".join(text.split()).strip(" -—–,;")


def post_lengths(title: str, body: str) -> tuple[int, int, int]:
    """Длины ``(заголовок, тело, итого)`` в символах без HTML."""
    title_len = len(title)
    body_len = len(body)
    return title_len, body_len, title_len + body_len


def length_problems(title: str, body: str) -> tuple[str, ...]:
    """Что в посте не соответствует ТЗ; пустой кортеж — всё в порядке.

    Публикация вызывает эту проверку, чтобы в канал не ушёл пост,
    который не проходит требования по длине: удлинять или сокращать
    «на лету» нельзя без потери смысла, поэтому решение за админом.
    """
    title_len, body_len, total = post_lengths(title, body)
    problems: list[str] = []
    if title_len < TITLE_MIN:
        problems.append(f"заголовок короче {TITLE_MIN} символов ({title_len})")
    elif title_len > TITLE_MAX:
        problems.append(f"заголовок длиннее {TITLE_MAX} символов ({title_len})")
    if body_len < BODY_MIN:
        problems.append(f"текст короче {BODY_MIN} символов ({body_len})")
    elif body_len > BODY_MAX:
        problems.append(f"текст длиннее {BODY_MAX} символов ({body_len})")
    if total < TOTAL_MIN:
        problems.append(f"итого короче {TOTAL_MIN} символов ({total})")
    elif total > TOTAL_MAX:
        problems.append(f"итого длиннее {TOTAL_MAX} символов ({total})")
    return tuple(problems)


__all__ = [
    "BODY_MAX",
    "BODY_MIN",
    "TITLE_MAX",
    "TITLE_MIN",
    "TOTAL_MAX",
    "TOTAL_MIN",
    "clean_text",
    "length_problems",
    "one_line",
    "post_lengths",
]
