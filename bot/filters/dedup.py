"""Дедупликация новостей (задача T05).

Три ступени из п. 4 ТЗ, от дешёвой к дорогой:

1. **URL/GUID** — точное совпадение с таблицей ``seen_urls``
   (:meth:`bot.db.Database.is_url_seen`); найденное считается дублем.
2. **Заголовки** — :class:`difflib.SequenceMatcher` по нормализованным
   заголовкам (нижний регистр, «ё»→«е», без пунктуации и лишних пробелов).
3. **История** — сравнение с постами ``pending`` и ``published`` за
   последние 7 дней (:meth:`bot.db.Database.list_for_dedup`).

Решение по порогам из :class:`bot.config.Settings`:

* ``score >= dedup_duplicate_threshold`` (по умолчанию 0.85) — дубль,
  молча пропускаем;
* ``score >= dedup_gray_zone_threshold`` (0.70) — «серая зона»: новость
  показывается админу с пометкой, сравнение пишется в ``dedup_log``;
* ниже — новая новость.

LLM и эмбеддинги здесь не используются: это второй этап (см. ТЗ).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Sequence

from bot.config import Settings
from bot.db import Database
from bot.models import DedupState, Post

# После нормализации в заголовке остаются только буквы (латиница/кириллица)
# и цифры; всё остальное превращается в пробел.
_NOT_WORD_RE = re.compile(r"[^0-9a-zа-я]+")
_DASHES_RE = re.compile(r"[\u2010-\u2015\u2212]")
_SPACES_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Нормализовать заголовок для сравнения.

    «GTA 6: Trailer Breaks All Records! (VGC)» → «gta 6 trailer breaks all
    records vgc». Регистр, «ё»→«е», тире и пунктуация не влияют на
    сравнение, поэтому один и тот же заголовок с разных сайтов совпадает.
    """
    if not title:
        return ""
    lowered = title.casefold().replace("ё", "е")
    lowered = _DASHES_RE.sub(" ", lowered)
    lowered = _NOT_WORD_RE.sub(" ", lowered)
    return _SPACES_RE.sub(" ", lowered).strip()


def similarity(left: str, right: str) -> float:
    """Схожесть двух заголовков от 0.0 до 1.0.

    Сравниваются нормализованные строки; ``autojunk=False`` — иначе
    ``SequenceMatcher`` начинает игнорировать частые символы в длинных
    заголовках и занижает оценку.
    """
    first = normalize_title(left)
    second = normalize_title(right)
    if not first or not second:
        return 0.0
    return SequenceMatcher(None, first, second, autojunk=False).ratio()


def classify(
    score: float,
    *,
    duplicate_threshold: float,
    gray_zone_threshold: float,
) -> DedupState:
    """Решение по оценке схожести и порогам из настроек."""
    if score >= duplicate_threshold:
        return DedupState.DUPLICATE
    if score >= gray_zone_threshold:
        return DedupState.GRAY_ZONE
    return DedupState.NEW


@dataclass(frozen=True, slots=True)
class DedupVerdict:
    """Итог проверки новости на дубль.

    ``score`` и ``matched_post`` заполняются только тогда, когда новость
    признана дублем или попала в серую зону: для новой новости ссылаться
    не на что.
    """

    state: DedupState
    score: float | None = None
    matched_post: Post | None = None

    @property
    def is_duplicate(self) -> bool:
        """Точный дубль — новость пропускается молча."""
        return self.state is DedupState.DUPLICATE

    @property
    def is_gray_zone(self) -> bool:
        """Похоже на существующую новость — решает админ."""
        return self.state is DedupState.GRAY_ZONE

    @property
    def is_new(self) -> bool:
        """Новой новости нет в истории."""
        return self.state is DedupState.NEW


def compare_title(
    candidate: str,
    posts: Sequence[Post],
    *,
    duplicate_threshold: float,
    gray_zone_threshold: float,
) -> DedupVerdict:
    """Сравнить заголовок кандидата с историей и выбрать лучшее совпадение.

    Берётся самый похожий пост: его оценка и определяет решение. Это
    чистая функция — без БД, сети и времени; удобно тестировать.
    """
    best_score = 0.0
    best_post: Post | None = None
    for post in posts:
        score = similarity(candidate, post.title_original)
        if best_post is None or score > best_score:
            best_score = score
            best_post = post

    if best_post is None:
        return DedupVerdict(DedupState.NEW)

    state = classify(
        best_score,
        duplicate_threshold=duplicate_threshold,
        gray_zone_threshold=gray_zone_threshold,
    )
    if state is DedupState.NEW:
        return DedupVerdict(DedupState.NEW)
    return DedupVerdict(state, score=best_score, matched_post=best_post)


async def check_news(
    db: Database,
    settings: Settings,
    *,
    title: str,
    lead: str = "",
    url: str = "",
    guid: str | None = None,
    now: datetime | None = None,
) -> DedupVerdict:
    """Проверить новость по всем трём ступеням (точка входа для T07).

    Порядок важен: при вызове из парсера URL ещё **не** помечен виденным
    (``on_item`` вызывается до ``mark_url_seen``), поэтому ступень 1 честно
    ловит повторный заход на ту же ссылку.

    :param title: оригинальный заголовок новости.
    :param lead: лид — используется, если заголовка нет.
    :param url: ссылка на статью.
    :param guid: GUID записи ленты, если есть.
    :param now: момент проверки (в тестах — ``T0``), по умолчанию текущий.
    """
    if url and await db.is_url_seen(url, guid):
        return DedupVerdict(DedupState.DUPLICATE)

    candidate = title if normalize_title(title) else lead
    if not normalize_title(candidate):
        # Совсем пустой заголовок сравнивать не с чем — считаем новой.
        return DedupVerdict(DedupState.NEW)

    posts = await db.list_for_dedup(now=now)
    verdict = compare_title(
        candidate,
        posts,
        duplicate_threshold=settings.dedup_duplicate_threshold,
        gray_zone_threshold=settings.dedup_gray_zone_threshold,
    )

    if verdict.is_gray_zone and verdict.matched_post is not None:
        # Серые зоны копим в лог: по нему потом подберём пороги точнее.
        await db.add_dedup_log(
            candidate_title=title or lead,
            compared_title=verdict.matched_post.title_original,
            score=verdict.score or 0.0,
            decision=DedupState.GRAY_ZONE,
            compared_post_id=verdict.matched_post.id,
            created_at=now,
        )
    return verdict


__all__ = [
    "DedupVerdict",
    "check_news",
    "classify",
    "compare_title",
    "normalize_title",
    "similarity",
]
