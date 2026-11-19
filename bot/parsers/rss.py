"""RSS/Atom-парсер и планировщик опроса источников (задача T03).

Модуль отвечает только за «достать новости»:

* загрузка лент через ``httpx.AsyncClient`` (свой User-Agent, таймаут из
  ``settings.http_timeout_sec``, следование редиректам);
* разбор через ``feedparser`` в **чистой функции** :func:`parse_feed`
  (байты ленты → список :class:`FeedItem`) — её можно тестировать офлайн;
* нормализация записи: URL, GUID, источник, заголовок, лид (~200 символов
  после очистки HTML), дата публикации, ссылка на картинку;
* защита от повторов через таблицу ``seen_urls``
  (:meth:`bot.db.Database.is_url_seen` / :meth:`~bot.db.Database.mark_url_seen`);
* цикл опроса с jitter'ом (:func:`poll_forever`) и отменой по
  ``asyncio.Event``.

Чего здесь **нет** и быть не должно: фильтрации по ключевым словам (T04),
дедупликации по заголовкам (T05), создания постов, скачивания картинок (T06)
и отправки в Telegram. Подписчик на найденные записи передаётся параметром
``on_item`` — им пользуются T05/T07.

Ошибки внешних источников (сеть, таймаут, 403/429, битый XML) логируются
и не прерывают опрос остальных лент.
"""

from __future__ import annotations

import asyncio
import html
import inspect
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlparse

import feedparser
import httpx

from bot.config import Settings
from bot.db import Database
from bot.models import utcnow
from bot.parsers.sources import SOURCES, FeedSource
from bot.services.format import cut_sentences

logger = logging.getLogger(__name__)

# Представляемся честно: сайтам проще связаться с нами, чем блокировать бота.
USER_AGENT = (
    "ParsTestBot/1.0 (+https://github.com/fedorshibeko8-bot/ParsTest; "
    "Telegram news bot; RSS reader)"
)

# Сколько символов лида оставляем после очистки HTML (п. 3 ТЗ — «первые ~200»).
LEAD_MAX_CHARS = 200

# Защита от «злых» значений Retry-After: дольше часа ждать не готовы.
MAX_RETRY_AFTER_SEC = 3600.0

# Расширения картинок — для лент, где тип enclosure не указан.
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?…%])")


class FeedError(Exception):
    """Общая ошибка получения или разбора ленты."""


class FeedFetchError(FeedError):
    """Ленту не удалось скачать: сеть, таймаут или HTTP-статус."""


class FeedHTTPError(FeedFetchError):
    """Источник ответил ошибкой HTTP (403, 429 и т.п.).

    :ivar status_code: код ответа.
    :ivar retry_after: сколько секунд просил подождать источник (``Retry-After``).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class FeedParseError(FeedError):
    """Лента не разбирается: битый XML или пустой документ."""


@dataclass(frozen=True, slots=True)
class FeedItem:
    """Нормализованная запись ленты — вход для T04/T05/T06."""

    url: str
    source: str
    title: str
    guid: str | None = None
    lead: str = ""
    published_at: datetime | None = None
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """Итог одного опроса одного источника.

    ``error`` заполняется вместо исключения: цикл опроса не должен падать
    из-за одного недоступного сайта. ``retry_after`` — просьба источника
    подождать (429), её учитывает :func:`poll_forever`.
    """

    source: str
    entries: int
    new_items: tuple[FeedItem, ...] = ()
    error: str | None = None
    retry_after: float | None = None

    @property
    def is_error(self) -> bool:
        """Был ли опрос неудачным."""
        return self.error is not None


ItemCallback = Callable[[FeedItem], Awaitable[Any] | Any]


# ----------------------------------------------------------------------
# Нормализация текста
# ----------------------------------------------------------------------
def clean_text(raw: str) -> str:
    """Очистить HTML до плоского текста с одиночными пробелами.

    Убирает теги, раскрывает HTML-сущности (``&amp;``, ``&nbsp;``) и
    схлопывает любые пробелы/переводы строк. Используется для заголовка
    и лида: в БД и Telegram они должны попасть текстом, а не разметкой.
    """
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = _TAG_RE.sub(" ", text)
    # Сущности могли «раскрыться» в теги (&lt;p&gt;) — чистим повторно.
    text = _TAG_RE.sub(" ", html.unescape(text))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    # Тег перед запятой не должен оставлять пробел: «GTA 6 , включая» → «GTA 6, включая».
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)


def make_lead(raw: str, *, max_chars: int = LEAD_MAX_CHARS) -> str:
    """Лид новости: чистый текст, обрезанный по концу целого предложения.

    Многоточие не ставится. Если ни одно предложение не укладывается в лимит,
    возвращается полный текст — обрывать фразу запрещено.
    """
    text = clean_text(raw)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return cut_sentences(text, max_chars) or text


def _is_image_url(url: str, mime_type: str | None = None) -> bool:
    """Похож ли адрес на картинку (по MIME-типу или расширению)."""
    if mime_type and mime_type.lower().startswith("image/"):
        return True
    return urlparse(url).path.lower().endswith(_IMAGE_EXTENSIONS)


def extract_image_url(entry: Mapping[str, Any]) -> str | None:
    """Картинка записи из RSS: enclosure → ``media:content`` → ``media:thumbnail``.

    Только извлечение URL: скачивание и валидация файла — задача T06.
    """
    for enclosure in entry.get("enclosures") or []:
        url = (enclosure.get("href") or enclosure.get("url") or "").strip()
        if url and _is_image_url(url, enclosure.get("type")):
            return url

    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = (media.get("url") or "").strip()
            if url and _is_image_url(url, media.get("type")):
                return url

    return None


def _entry_published(entry: Mapping[str, Any]) -> datetime | None:
    """Дата публикации записи в UTC (``published_parsed`` → ``updated_parsed``)."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*tuple(value)[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_url(entry: Mapping[str, Any], guid: str | None) -> str:
    """Ссылка на статью; если её нет, годится GUID-URL."""
    url = (entry.get("link") or "").strip()
    if url:
        return url
    if guid and guid.startswith(("http://", "https://")):
        return guid
    return ""


def _entry_to_item(
    entry: Mapping[str, Any], source: FeedSource
) -> FeedItem | None:
    """Одна запись feedparser → :class:`FeedItem` (``None`` — если нечего хранить)."""
    guid = (entry.get("id") or entry.get("guid") or "").strip() or None
    url = _entry_url(entry, guid)
    if not url:
        return None

    summary = entry.get("summary") or ""
    if not summary:
        content = entry.get("content") or []
        if content:
            summary = content[0].get("value") or ""

    return FeedItem(
        url=url,
        source=source.name,
        title=clean_text(entry.get("title") or ""),
        guid=guid,
        lead=make_lead(summary),
        published_at=_entry_published(entry),
        image_url=extract_image_url(entry),
    )


# ----------------------------------------------------------------------
# Разбор ленты (чистая функция — офлайн-тесты)
# ----------------------------------------------------------------------
def parse_feed(raw: bytes | str, source: FeedSource) -> list[FeedItem]:
    """Разобрать байты ленты в список нормализованных записей.

    Функция ничего не знает о сети, БД и настройках, поэтому её легко
    проверять на сохранённых XML из ``tests/fixtures``.

    :param raw: содержимое ответа (RSS 2.0 или Atom), как пришло по HTTP.
    :param source: источник — из него берётся поле ``source`` записи.
    :raises FeedParseError: документ не разобран и записей в нём нет.
    """
    parsed = feedparser.parse(raw)
    entries = list(parsed.entries or [])

    items: list[FeedItem] = []
    for entry in entries:
        item = _entry_to_item(entry, source)
        if item is None:
            logger.debug("Запись ленты %s без ссылки — пропущена", source.name)
            continue
        items.append(item)

    if parsed.get("bozo"):
        if not items:
            raise FeedParseError(
                f"{source.name}: лента не разобрана ({parsed.get('bozo_exception')})"
            )
        # Частичные ошибки (незакрытый тег, лишняя сущность) не мешают читать записи.
        logger.warning(
            "Лента %s разобрана с замечанием: %s",
            source.name,
            parsed.get("bozo_exception"),
        )
    return items


# ----------------------------------------------------------------------
# Сеть
# ----------------------------------------------------------------------
def build_client(settings: Settings) -> httpx.AsyncClient:
    """HTTP-клиент для лент: свой User-Agent, таймаут, редиректы."""
    return httpx.AsyncClient(
        timeout=float(settings.http_timeout_sec),
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                "*/*;q=0.8"
            ),
        },
    )


def parse_retry_after(raw: str | None, *, now: datetime | None = None) -> float | None:
    """Разобрать заголовок ``Retry-After`` в секунды.

    Поддерживаются оба формата из RFC 9110: число секунд и HTTP-дата.
    Отрицательные значения приводятся к нулю, слишком большие — к
    :data:`MAX_RETRY_AFTER_SEC`; нераспознанное значение даёт ``None``.
    """
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None

    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = (moment - (now or utcnow())).total_seconds()

    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SEC)


async def fetch_feed(
    client: httpx.AsyncClient, source: FeedSource, settings: Settings
) -> bytes:
    """Скачать ленту. Сетевые ошибки и статусы ≥400 превращаются в исключения.

    :raises FeedHTTPError: ответ 4xx/5xx (у 429 сохраняется ``retry_after``).
    :raises FeedFetchError: таймаут, обрыв соединения, пустой ответ.
    """
    try:
        response = await client.get(
            source.url,
            follow_redirects=True,
            timeout=float(settings.http_timeout_sec),
        )
    except httpx.TimeoutException as exc:
        raise FeedFetchError(f"{source.name}: таймаут запроса ленты") from exc
    except httpx.HTTPError as exc:
        raise FeedFetchError(
            f"{source.name}: сетевая ошибка ({type(exc).__name__})"
        ) from exc

    status = response.status_code
    if status >= 400:
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        if status == 429:
            message = f"{source.name}: источник ограничил частоту (HTTP 429)"
        elif status in (401, 403):
            message = f"{source.name}: доступ к ленте запрещён (HTTP {status})"
        else:
            message = f"{source.name}: HTTP {status}"
        raise FeedHTTPError(message, status_code=status, retry_after=retry_after)

    if not response.content:
        raise FeedFetchError(f"{source.name}: пустой ответ вместо ленты")
    return response.content


# ----------------------------------------------------------------------
# Опрос одного источника
# ----------------------------------------------------------------------
async def _notify(on_item: ItemCallback | None, item: FeedItem) -> bool:
    """Отдать запись подписчику. ``False`` — подписчик упал (запись повторим)."""
    if on_item is None:
        return True
    try:
        result = on_item(item)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception(
            "Обработчик новости упал на %s — запись не помечена виденной", item.url
        )
        return False
    return True


async def _keep_new_items(
    db: Database,
    items: Sequence[FeedItem],
    on_item: ItemCallback | None,
) -> tuple[FeedItem, ...]:
    """Оставить только невиденные записи и запомнить их в ``seen_urls``."""
    new_items: list[FeedItem] = []
    for item in items:
        if await db.is_url_seen(item.url, item.guid):
            continue
        if not await _notify(on_item, item):
            continue
        # INSERT OR IGNORE: пост, созданный подписчиком, уже мог записать URL.
        await db.mark_url_seen(item.url, guid=item.guid, source=item.source)
        new_items.append(item)
    return tuple(new_items)


async def poll_source(
    db: Database,
    settings: Settings,
    source: FeedSource,
    *,
    client: httpx.AsyncClient | None = None,
    on_item: ItemCallback | None = None,
) -> PollOutcome:
    """Опросить один источник: скачать, разобрать, записать новинки.

    Ошибки не пробрасываются — они возвращаются в :class:`PollOutcome`,
    чтобы цикл продолжал работать с остальными источниками.

    :param client: готовый клиент (в тестах — с ``httpx.MockTransport``);
        если не задан, создаётся и закрывается внутри.
    :param on_item: подписчик на каждую новую запись (T05/T07).
    """
    own_client = client is None
    if client is None:
        client = build_client(settings)

    try:
        try:
            raw = await fetch_feed(client, source, settings)
        except FeedHTTPError as exc:
            logger.warning("Лента %s недоступна: %s", source.name, exc)
            return PollOutcome(
                source=source.name,
                entries=0,
                error=str(exc),
                retry_after=exc.retry_after,
            )
        except FeedFetchError as exc:
            logger.warning("Лента %s недоступна: %s", source.name, exc)
            return PollOutcome(source=source.name, entries=0, error=str(exc))

        try:
            items = parse_feed(raw, source)
        except FeedParseError as exc:
            logger.error("Лента %s не разобрана: %s", source.name, exc)
            return PollOutcome(source=source.name, entries=0, error=str(exc))

        new_items = await _keep_new_items(db, items, on_item)
        if new_items:
            logger.info(
                "Источник %s: записей %d, новых %d",
                source.name,
                len(items),
                len(new_items),
            )
        else:
            logger.debug("Источник %s: новых записей нет (всего %d)", source.name, len(items))
        return PollOutcome(
            source=source.name, entries=len(items), new_items=new_items
        )
    finally:
        if own_client:
            await client.aclose()


async def poll_all_sources(
    db: Database,
    settings: Settings,
    *,
    sources: Sequence[FeedSource] | None = None,
    client: httpx.AsyncClient | None = None,
    on_item: ItemCallback | None = None,
) -> list[PollOutcome]:
    """Один проход по всем источникам (параллельно) — ручной запуск и тесты."""
    source_list = tuple(
        source for source in (SOURCES if sources is None else sources) if source.enabled
    )
    if not source_list:
        return []

    own_client = client is None
    if client is None:
        client = build_client(settings)

    try:
        return list(
            await asyncio.gather(
                *(
                    poll_source(db, settings, source, client=client, on_item=on_item)
                    for source in source_list
                )
            )
        )
    finally:
        if own_client:
            await client.aclose()


# ----------------------------------------------------------------------
# Планировщик
# ----------------------------------------------------------------------
def jitter_delay(settings: Settings, rng: random.Random | None = None) -> float:
    """Случайная пауза для источника в границах из настроек (п. 2 ТЗ).

    ``random.uniform`` даёт равномерное распределение: сайты не получают
    запросы строго по расписанию, а бот не выглядит сканером.
    """
    low = float(settings.poll_interval_min_sec)
    high = float(settings.poll_interval_max_sec)
    if low > high:
        low, high = high, low
    generator = rng if rng is not None else random
    return float(generator.uniform(low, high))


async def _sleep_or_stop(timeout_sec: float, stop_event: asyncio.Event) -> bool:
    """Пауза до таймаута или до остановки. ``True`` — пришла остановка."""
    if timeout_sec <= 0:
        return stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return False
    return True


async def _source_loop(
    source: FeedSource,
    db: Database,
    settings: Settings,
    client: httpx.AsyncClient,
    on_item: ItemCallback | None,
    stop_event: asyncio.Event,
    rng: random.Random | None,
) -> None:
    """Опрос одного источника до остановки: опрос → jitter-пауза → опрос."""
    while not stop_event.is_set():
        retry_after: float | None = None
        try:
            outcome = await poll_source(
                db, settings, source, client=client, on_item=on_item
            )
            retry_after = outcome.retry_after
        except Exception:
            # Непредвиденное (ошибка БД, баг в подписчике) не должно убивать цикл.
            logger.exception("Непредвиденная ошибка опроса источника %s", source.name)

        delay = jitter_delay(settings, rng)
        if retry_after is not None:
            delay = max(delay, retry_after)
        logger.debug("Источник %s: следующая попытка через %.0f с", source.name, delay)
        if await _sleep_or_stop(delay, stop_event):
            return
    logger.debug("Источник %s: цикл остановлен", source.name)


async def poll_forever(
    db: Database,
    settings: Settings,
    stop_event: asyncio.Event | None = None,
    on_item: ItemCallback | None = None,
    *,
    sources: Sequence[FeedSource] | None = None,
    client: httpx.AsyncClient | None = None,
    rng: random.Random | None = None,
) -> None:
    """Фоновый опрос всех источников с jitter'ом до остановки.

    У каждого источника своя задача и своя случайная пауза
    ``random.uniform(poll_interval_min_sec, poll_interval_max_sec)``.
    Первый опрос выполняется сразу (свежие новости после рестарта), затем —
    пауза. ``Retry-After`` от источника (429) увеличивает паузу.

    :param stop_event: установка события завершает цикл; ``None`` — цикл
        живёт до отмены задачи.
    :param on_item: подписчик на найденные записи (T05/T07), может быть
        корутиной. Его ошибки логируются и не останавливают опрос.
    :param client: внешний ``httpx.AsyncClient`` (тесты); иначе создаётся свой.
    :param rng: генератор случайных чисел (для воспроизводимых тестов).
    """
    stop = stop_event if stop_event is not None else asyncio.Event()
    source_list = tuple(
        source for source in (SOURCES if sources is None else sources) if source.enabled
    )
    if not source_list:
        logger.warning("Нет включённых RSS-источников — опрос не запущен.")
        return

    own_client = client is None
    if client is None:
        client = build_client(settings)

    logger.info(
        "Опрос RSS запущен: %s (интервал %d-%d с)",
        ", ".join(source.name for source in source_list),
        settings.poll_interval_min_sec,
        settings.poll_interval_max_sec,
    )
    tasks = [
        asyncio.create_task(
            _source_loop(source, db, settings, client, on_item, stop, rng),
            name=f"rss-{source.key}",
        )
        for source in source_list
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if own_client:
            await client.aclose()
        logger.info("Опрос RSS остановлен.")


__all__ = [
    "FeedError",
    "FeedFetchError",
    "FeedHTTPError",
    "FeedItem",
    "FeedParseError",
    "LEAD_MAX_CHARS",
    "MAX_RETRY_AFTER_SEC",
    "PollOutcome",
    "USER_AGENT",
    "build_client",
    "clean_text",
    "extract_image_url",
    "fetch_feed",
    "jitter_delay",
    "make_lead",
    "parse_feed",
    "parse_retry_after",
    "poll_all_sources",
    "poll_forever",
    "poll_source",
]
