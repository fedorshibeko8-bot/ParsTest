"""Конвейер новости: лента → фильтр → дедупликация → пост → картинка → анонс.

Задача T11: связать готовые части T03–T07 в один обработчик ``on_item``,
который получает :class:`~bot.parsers.rss.FeedItem` от цикла опроса RSS.

Порядок шагов принципиален и взят из ТЗ:

1. **фильтр по ключевым словам** — не тратим дедупликацию на нерелевантное;
2. **дедупликация** — ``check_news`` вызывается **до** ``mark_url_seen``
   (это делает сам парсер), чтобы ступень 1 «URL/GUID» работала;
3. **создание поста** в ``pending`` с оценкой и состоянием дедупликации —
   серая зона попадёт в карточку с пометкой;
4. **картинка** — скачивается после создания поста; ошибка не отменяет
   новость, карточка и публикация работают без фото;
5. **анонс** — уведомление админам без контента и кнопкой перехвата.

Конвейер ничего не публикует: в канал пост уходит только по кнопке админа.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot

from bot.config import Settings
from bot.db import Database, DuplicatePostError
from bot.filters.dedup import check_news
from bot.filters.keywords import is_relevant
from bot.models import Post
from bot.parsers.rss import FeedItem
from bot.services.images import try_download_image
from bot.services.queue import announce_posts

logger = logging.getLogger(__name__)


async def handle_feed_item(
    bot: Bot,
    db: Database,
    settings: Settings,
    item: FeedItem,
    *,
    now: datetime | None = None,
) -> Post | None:
    """Обработать одну новость из ленты; ``None`` — она не попала в очередь.

    Ошибки скачивания картинки и отправки уведомлений не пробрасываются:
    пост уже создан, и он должен остаться в очереди даже при сбое сети.
    """
    if not is_relevant(item.title, item.lead, settings.news_keywords):
        logger.debug("Не по теме: %s", item.title)
        return None

    verdict = await check_news(
        db,
        settings,
        title=item.title,
        lead=item.lead,
        url=item.url,
        guid=item.guid,
        now=now,
    )
    if verdict.is_duplicate:
        logger.info("Дубль пропущен: %s", item.title)
        return None
    if verdict.is_gray_zone:
        logger.info(
            "Серая зона %.0f%%, покажу админу: %s",
            (verdict.score or 0.0) * 100,
            item.title,
        )

    try:
        post = await db.create_post(
            item.url,
            guid=item.guid,
            source=item.source,
            title_original=item.title,
            lead=item.lead,
            dedup_score=verdict.score,
            dedup_state=verdict.state,
            created_at=now,
        )
    except DuplicatePostError:
        # URL уже есть в БД: параллельный опрос успел раньше.
        logger.info("Пост уже создан: %s", item.url)
        return None

    image = await try_download_image(item.image_url, settings)
    if image is not None:
        post = await db.set_image(
            post.id, image_path=str(image.path), image_url=item.image_url
        )
    else:
        logger.info("Пост #%s без картинки: %s", post.id, item.title)

    try:
        await announce_posts(bot, db, settings, [post])
    except Exception:
        # Пост уже в БД — админ увидит его в следующем проходе очереди.
        logger.exception("Не удалось анонсировать пост #%s", post.id)

    return post


__all__ = ["handle_feed_item"]
