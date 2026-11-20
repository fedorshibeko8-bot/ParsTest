"""Конвейер новости: лента → фильтр → дедупликация → пост → картинка → анонс.

Задача T11: связать готовые части T03–T07 в один обработчик, который
получает записи от опроса RSS.

Порядок шагов принципиален и взят из ТЗ:

1. **фильтр по ключевым словам** — не тратим дедупликацию на нерелевантное;
2. **дедупликация** — ``check_news`` вызывается **до** ``mark_url_seen``
   (это делает парсер), чтобы ступень 1 «URL/GUID» работала;
3. **создание поста** в ``pending`` с оценкой и состоянием дедупликации —
   серая зона попадёт в карточку с пометкой;
4. **картинка** — скачивается после создания поста; ошибка не отменяет
   новость, карточка и публикация работают без фото;
5. **анонс** — уведомление админам без контента и кнопкой перехвата.

Анонсы отправляются **пакетом за один проход опроса**, а не по одному:
``announce_posts`` сам оставляет до ``QUEUE_VISIBLE_LIMIT`` самых свежих
постов и шлёт их по старшинству. Иначе первый запуск (в лентах сразу
сотня записей) завалил бы админов сотней личных сообщений.

Первый проход после установки ограничен той же десяткой: остальная
история лент запоминается как просмотренная и в очередь не попадает —
бот работает с новостями, появившимися после запуска.

Конвейер ничего не публикует: в канал пост уходит только по кнопке админа.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Sequence

from aiogram import Bot

from bot.config import Settings
from bot.db import Database, DuplicatePostError
from bot.filters.dedup import check_news
from bot.filters.keywords import is_relevant
from bot.models import Post
from bot.parsers.rss import (
    SOURCES,
    FeedItem,
    PollOutcome,
    jitter_delay,
    poll_all_sources,
)
from bot.services.images import try_download_image
from bot.services.queue import announce_posts

logger = logging.getLogger(__name__)


class ImportBudget:
    """Сколько постов ещё можно создать в первом проходе после установки.

    Счётчик общий на проход: пять лент опрашиваются параллельно, но
    подписчик вызывается по очереди, поэтому проверка и списание
    не разъезжаются.
    """

    __slots__ = ("total", "left")

    def __init__(self, limit: int) -> None:
        self.total = max(0, int(limit))
        self.left = self.total

    @property
    def exhausted(self) -> bool:
        """Свободных мест больше нет."""
        return self.left <= 0

    def spend(self) -> None:
        """Занять одно место (вызывать только после успешного создания поста)."""
        if self.left > 0:
            self.left -= 1


async def process_feed_item(
    db: Database,
    settings: Settings,
    item: FeedItem,
    *,
    now: datetime | None = None,
    budget: ImportBudget | None = None,
) -> Post | None:
    """Провести одну запись через фильтр, дедупликацию и картинку.

    Возвращает созданный пост (``pending``) или ``None``, если запись
    не по теме, оказалась дублем или не влезла в лимит первого прохода.

    Уведомления здесь не отправляются: их делает :func:`announce_created`
    пакетом, чтобы соблюсти лимит видимой очереди.

    Ошибки скачивания картинки не пробрасываются: пост уже создан и
    должен остаться в очереди даже при сбое сети.
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

    if budget is not None and budget.exhausted:
        # Первый проход: берём только самые свежие записи, остальное — история.
        logger.debug("Первая загрузка ленты: пропущено — %s", item.title)
        return None

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

    if budget is not None:
        budget.spend()

    image = await try_download_image(item.image_url, settings)
    if image is not None:
        post = await db.set_image(
            post.id, image_path=str(image.path), image_url=item.image_url
        )
    else:
        logger.info("Пост #%s без картинки: %s", post.id, item.title)

    return post


async def announce_created(
    bot: Bot,
    db: Database,
    settings: Settings,
    posts: Sequence[Post],
) -> None:
    """Разослать уведомления о пачке новых постов (до видимого лимита).

    ``announce_posts`` оставляет ``QUEUE_VISIBLE_LIMIT`` самых свежих и
    отправляет их в порядке FIFO. Ошибка рассылки не откатывает посты:
    они уже в БД и останутся в очереди.
    """
    if not posts:
        return
    try:
        await announce_posts(bot, db, settings, posts)
    except Exception:
        logger.exception(
            "Не удалось разослать уведомления о %d новых постах", len(posts)
        )


async def handle_feed_items(
    bot: Bot,
    db: Database,
    settings: Settings,
    items: Sequence[FeedItem],
    *,
    now: datetime | None = None,
    budget: ImportBudget | None = None,
) -> list[Post]:
    """Обработать пачку записей и один раз анонсировать созданные посты."""
    created: list[Post] = []
    for item in items:
        post = await process_feed_item(db, settings, item, now=now, budget=budget)
        if post is not None:
            created.append(post)
    await announce_created(bot, db, settings, created)
    return created


async def handle_feed_item(
    bot: Bot,
    db: Database,
    settings: Settings,
    item: FeedItem,
    *,
    now: datetime | None = None,
    budget: ImportBudget | None = None,
) -> Post | None:
    """Обработать одну запись (обёртка над :func:`handle_feed_items`)."""
    created = await handle_feed_items(
        bot, db, settings, [item], now=now, budget=budget
    )
    return created[0] if created else None


# ----------------------------------------------------------------------
# Фоновый цикл опроса и анонсов
# ----------------------------------------------------------------------
async def _sleep_or_stop(timeout_sec: float, stop_event: asyncio.Event) -> bool:
    """Подождать паузу или досрочную остановку. ``True`` — пора выходить."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, timeout_sec))
    except asyncio.TimeoutError:
        return False
    return True


async def poll_news_loop(
    bot: Bot,
    db: Database,
    settings: Settings,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Опрашивать все ленты и анонсировать новости пачкой до остановки.

    Один проход = опрос всех «свободных» источников (параллельно) + одна
    рассылка уведомлений. Пауза между проходами — ``POLL_INTERVAL_MIN_SEC``…
    ``POLL_INTERVAL_MAX_SEC`` со случайным сдвигом.

    ``Retry-After`` (429) ставит на паузу **только провинившийся источник**:
    остальные продолжают работать, а общая пауза не растягивается на час
    из-за одной ленты. Сетевые сбои не останавливают цикл.
    """
    stop = stop_event if stop_event is not None else asyncio.Event()
    # key источника → момент (loop.time), раньше которого его не опрашиваем.
    snoozed_until: dict[str, float] = {}
    loop = asyncio.get_running_loop()

    enabled = [source for source in SOURCES if source.enabled]
    if not enabled:
        logger.warning("Нет включённых RSS-источников — опрос не запущен.")
        return
    # Первый разбор каждой ленты — это её история, а не новости. Берём
    # не больше видимой очереди, поделив её между источниками, чтобы один
    # сайт не занял все места своими старыми гайдами.
    per_source = max(1, int(settings.queue_visible_limit) // max(1, len(enabled)))
    first_imports: dict[str, ImportBudget] = {
        source.key: ImportBudget(per_source) for source in enabled
    }

    while not stop.is_set():
        moment = loop.time()
        due = [
            source
            for source in enabled
            if snoozed_until.get(source.key, 0.0) <= moment
        ]
        if not due:
            # Все источники попросили подождать — спим до ближайшего.
            wait = min(snoozed_until.values()) - moment
            if await _sleep_or_stop(wait, stop):
                break
            continue

        created: list[Post] = []
        keys_by_name = {source.name: source.key for source in due}

        async def on_item(item: FeedItem) -> None:
            key = keys_by_name.get(item.source)
            budget = first_imports.get(key) if key else None
            post = await process_feed_item(db, settings, item, budget=budget)
            if post is not None:
                created.append(post)

        try:
            outcomes: list[PollOutcome] = await poll_all_sources(
                db, settings, sources=due, on_item=on_item
            )
        except Exception:
            # Ошибка БД или бага в конвейере не должна убивать фоновый цикл.
            logger.exception("Проход опроса RSS завершился ошибкой")
            outcomes = []

        for outcome in outcomes:
            key = keys_by_name.get(outcome.source, outcome.source)
            if outcome.retry_after:
                snoozed_until[key] = loop.time() + outcome.retry_after
                logger.warning(
                    "Источник %s попросил паузу %.0f с",
                    outcome.source,
                    outcome.retry_after,
                )
            if outcome.entries:
                # Лента разобрана — её история больше не «первая».
                first_imports.pop(key, None)

        await announce_created(bot, db, settings, created)

        delay = jitter_delay(settings)
        logger.debug("Следующий проход опроса через %.0f с", delay)
        if await _sleep_or_stop(delay, stop):
            break

    logger.info("Опрос RSS остановлен.")


__all__ = [
    "ImportBudget",
    "announce_created",
    "handle_feed_item",
    "handle_feed_items",
    "poll_news_loop",
    "process_feed_item",
]
