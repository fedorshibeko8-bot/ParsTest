# CHANGELOG

Все заметные изменения проекта. Формат — по мотивам [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Задачи и их статусы — в `TASKS.md`. Требования — в `ТЗ.md`.

**Правило для агентов:** после завершения задачи добавить сюда запись в раздел `[Unreleased]` (или в раздел с датой), указав ID задачи, что сделано, какие файлы появились и кто это сделал.

---

## [Unreleased]

### Добавлено
- **T05: дедупликация (3 ступени).** `bot/filters/dedup.py` — `normalize_title` (нижний регистр, «ё»→«е», тире и пунктуация → пробел), `similarity` на `difflib.SequenceMatcher(autojunk=False)`, `classify` по порогам `Settings` (≥0.85 дубль, 0.70–0.85 серая зона, ниже — новая), `DedupVerdict` с флагами состояния, чистая `compare_title` (выбирает самое похожее из истории) и `check_news`: ступень 1 — URL/GUID через `seen_urls`; ступени 2–3 — заголовки против `pending`+`published` за 7 дней (`Database.list_for_dedup`). Точные дубли пропускаются молча, серые зоны пишутся в `dedup_log` (оценка, `compared_post_id`) и вернутся в карточку пометкой (T08). Тесты: `tests/test_dedup.py` (21 тест). Файлы: `bot/filters/dedup.py`, `tests/test_dedup.py`, `TASKS.md`, `CHANGELOG.md`. Автор: агент T05 (DeepSeek/OpenCode). **109 тестов проходит.**

### Решения
- **T05: порядок вызова важен.** `check_news` рассчитан на вызов из `on_item` **до** `mark_url_seen` (так устроен парсер): ступень 1 честно ловит повторные URL. Если будущий код сначала пометит URL, ступень 1 будет срабатывать всегда — не менять порядок без необходимости.
- **T05: в истории участвуют только `pending` и `published`.** `dropped` и посты старше 7 дней игнорируются — иначе отклонённые новости навсегда блокировали бы повторную публикацию.

### Передача дальше (T06/T07)
- `check_news(db, settings, title=..., lead=..., url=..., guid=..., now=...)` возвращает `DedupVerdict`; для нового поста `db.create_post(..., dedup_score=verdict.score, dedup_state=verdict.state)`.
- Для серой зоны `verdict.matched_post` — пост, на который похожа новость; в карточке T08 показать «⚠️ похоже на уже опубликованное».
- Фильтр по ключам (`bot.filters.keywords.is_relevant`) вызывать до дедупликации, чтобы не тратить сравнения на нерелевантные новости.

### Добавлено
- **T04: фильтр по ключевым словам.** `bot/filters/keywords.py` — нормализация текста (регистр, «ё»→«е», тире), шаблоны ключей с гибкими разделителями и границами слова (`GTA 6` находит `GTA-6`/`GTA6`/`GTA–6`, но не `GTA 60`, не `GTA Online` и не `RockstarGames`), `KeywordMatcher` с `find`/`matches`/`find_in`/`is_relevant`, кэш сопоставителей, обёртки `find_keywords`/`is_relevant`. В `bot/config.py` — `DEFAULT_NEWS_KEYWORDS` и разбор `NEWS_KEYWORDS` (запятая или точка с запятой, дедуп без учёта регистра, пустое значение → дефолты), новое поле `Settings.news_keywords`; `.env.example` дополнен переменной. Тесты: `tests/test_keywords.py` (33 теста: варианты написания, ложные срабатывания, поиск по лиду, пользовательские ключи, конфиг). Файлы: `bot/filters/keywords.py`, `bot/config.py`, `.env.example`, `tests/test_keywords.py`, `tests/helpers.py`. Автор: агент T04 (DeepSeek/OpenCode). **88 тестов проходит.**

### Решения
- **T04: ключи — из `Settings`, а не из кода.** Список задаётся переменной `NEWS_KEYWORDS`, по умолчанию «GTA 6», «GTA VI», «Grand Theft Auto», «Rockstar». Варианты написания и границы слова закрывает шаблон, поэтому дублировать «GTA-6»/«GTA6» в списке не нужно.
- **T04: «мягкие» границы слова.** Ключ не срабатывает внутри другого слова или числа (`GTA 60`, `RockstarGames`), но срабатывает рядом с пунктуацией и притяжательным `'s` (`Rockstar's`).

### Передача дальше (T05)
- Фильтр готов: `is_relevant(title, lead, settings.news_keywords)` или `build_matcher(settings.news_keywords)`. Ничего не кэшируйте вручную — `build_matcher` уже кэширован.
- `feed_item.title` и `feed_item.lead` уже очищены от HTML; повторно чистить не нужно.
- Дедупликация (T05) должна вызываться **после** фильтра: сначала отсекаем нерелевантное, потом сравниваем заголовки с pending/published за 7 дней.

### Добавлено
- `TASKS.md` — реестр задач T01–T12 со статусами, зависимостями и правилами обновления.
- `CHANGELOG.md` — этот журнал.
- `docs/AGENTS.md` — правила работы ИИ-агентов над проектом.
- **T01: скелет проекта и конфигурация.** Пакет `bot/` (`__init__.py`, `main.py`, `config.py`, `utils/logging.py`) и пустые заготовки `bot/parsers/`, `bot/filters/`, `bot/services/`, `bot/handlers/`; конфиг с валидацией и дефолтами, логирование в консоль и `logs/bot.log` (UTF-8, ротация в полночь, хранение 30 дней), точка входа с aiogram 3.32.0 и long polling, команды `/start` и `/ping` только для админов, корректный выход без трейсбека при отсутствии токена, `requirements.txt` с фиксированными версиями и `.env.example` с заглушками и русскими комментариями. Файлы: `bot/main.py`, `bot/config.py`, `bot/utils/logging.py`, `bot/__init__.py`, `bot/utils/__init__.py`, `bot/parsers/__init__.py`, `bot/filters/__init__.py`, `bot/services/__init__.py`, `bot/handlers/__init__.py`, `requirements.txt`, `.env.example`. Автор: агент-исполнитель T01 (DeepSeek/OpenCode).

### Добавлено
- **T02: хранилище SQLite и модели данных.** `bot/models.py` (статусы `pending`/`intercepted`/`published`/`dropped`, `DedupState`, `DraftOrigin`, dataclass'ы `Post`, `DraftVersion`, `SeenUrl`, `DedupLogEntry`, помощники `to_iso`/`from_iso` в UTC), `bot/db.py` (`Database` — async-фасад над `sqlite3` через `asyncio.to_thread`: схема v1 и миграции по `PRAGMA user_version`, WAL, `foreign_keys=ON`, busy timeout; таблицы `posts`, `draft_versions`, `seen_urls`, `dedup_log`; индексы по `status`/`url`/`guid`/`created_at`; частичный уникальный индекс — один активный пост на админа; переходы статусов с `InvalidTransitionError`, перехват с `OwnerBusyError`, TTL и тайм-ауты, откат черновиков, лог серых зон), `bot/services/backup.py` (`VACUUM INTO`, хранение 7 снимков, суточный цикл с остановкой по `asyncio.Event`), `tests/` (`test_models.py`, `test_db.py`, `test_backup.py`, `conftest.py`, `helpers.py`), `requirements-dev.txt`, `pytest.ini`. Файлы: `bot/models.py`, `bot/db.py`, `bot/services/backup.py`, `tests/*`. Авторы: агент T02 (DeepSeek/OpenCode) и координатор (правка теста черновиков, тесты бэкапа). **31 тест проходит.**
- **Telegram-доступы настроены (частично T12).** Токен `@ParsgtaBot_bot` проверен через `getMe`; бот добавлен администратором в канал «FOWGSL» (`@fowgsl`, `-1004371911262`) с правами публикации. `CHANNEL_ID` и `ADMIN_IDS` (`7846135495`, `1389758957`, `1825876129`) записаны в локальный `.env`, ACL файла ограничен пользователем. Все три админа активировали бота. Проверен запуск: long polling, «Run polling for bot @ParsgtaBot_bot», ошибок нет. Ключи Gemini/Groq пока не предоставлены — режим `DRY_RUN=1`. Автор: агент-координатор (OpenCode).
- **Утилита получения chat_id.** `tools/get_chat_id.py` опрашивает `getUpdates` и печатает ID групп/каналов, где бот видит сообщения — чтобы заполнить `CHANNEL_ID` без сторонних ботов. Автор: агент-координатор (OpenCode).

### Добавлено
- **T03: RSS-парсер и планировщик опроса.** `bot/parsers/sources.py` — 5 источников из п. 2 ТЗ как данные (`FeedSource`, `SOURCES`: VGC, IGN, Eurogamer, GamesRadar+, GameSpot), без размазывания URL по коду. `bot/parsers/rss.py` — загрузка через `httpx.AsyncClient` (свой User-Agent, таймаут из `HTTP_TIMEOUT_SEC`, следование редиректам), чистая функция `parse_feed` (байты ленты → `FeedItem`, RSS 2.0 и Atom), нормализация заголовка и лида (~200 символов после очистки HTML), дата публикации в UTC, картинка из `enclosure` → `media:content` → `media:thumbnail` (только URL, скачивание — T06), дедуп через `seen_urls` (`is_url_seen`/`mark_url_seen`), `jitter_delay` = `random.uniform(POLL_INTERVAL_MIN_SEC, POLL_INTERVAL_MAX_SEC)`, `poll_forever` — отдельная задача на источник, отмена по `asyncio.Event`, подписчик `on_item` для T05/T07, `poll_all_sources` для ручного прогона. 403/429, таймауты и битый XML логируются и не роняют цикл; `Retry-After` учитывается (с ограничением 1 ч). Тесты: `tests/test_rss.py` (24 офлайн-теста на `httpx.MockTransport`), фикстуры `tests/fixtures/rss_vgc.xml`, `tests/fixtures/atom_ign.xml`, `tests/fixtures/broken_gamespot.xml`, помощник `make_settings` в `tests/helpers.py`. Файлы: `bot/parsers/sources.py`, `bot/parsers/rss.py`, `bot/parsers/__init__.py`, `tests/test_rss.py`, `tests/fixtures/*.xml`, `tests/helpers.py`. Автор: агент T03 (DeepSeek/OpenCode). **55 тестов проходит.**

### Решения
- **Канал — только для новостей.** В «FOWGSL» попадают исключительно опубликованные посты. Все служебные сообщения (уведомления, «взял админ», превью, ошибки) идут только в личку админам. `CHANNEL_ID` использует только `publisher` (T10).
- **Тихий режим — опция, а не режим по умолчанию.** Новости публикуются с обычным уведомлением подписчиков; `disable_notification` включается флагом `PUBLISH_SILENT=1`.
- **Telegram-доступы:** бот `@ParsgtaBot_bot`, канал `-1004371911262` («FOWGSL»), админы `7846135495` (@frozfs), `1389758957` (@septtf), `1825876129` (@fringehat). @PeterElnikov по решению владельца доступа не имеет.
- **T02: объекты моделей — снимки.** `DraftVersion.is_current` отражает состояние на момент чтения; тесты проверяют актуальность повторным запросом, а не сравнением старых объектов.
- **Деплой только на домашний ПК (Windows).** Пункт 12 ТЗ (systemd, Bothost, VPS) заменяется на `setup.bat` + `run.bat` и гайд `ЗАПУСК.md`.
- **Ключей пока нет:** весь код пишется с заглушками и режимом `DRY_RUN=1`. Реальные значения — только в локальном `.env`, который в git не попадает.
- **Журнал — обязательный артефакт:** каждый агент читает `TASKS.md` перед работой и обновляет `TASKS.md` + `CHANGELOG.md` после.
- **T01: `DRY_RUN` по умолчанию `1`.** Если переменная не задана, бот считается тестовым: безопаснее, чем случайно опубликовать в канал.
- **T01: `BOT_TOKEN` обязателен всегда,** в том числе в `DRY_RUN`: без него aiogram не подключится к Telegram. `CHANNEL_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY` обязательны только при `DRY_RUN=0`.
- **T01: заглушки из `.env.example` (`PASTE_...`) распознаются.** Для необязательных значений в `DRY_RUN` они превращаются в пустую строку (сервисы ИИ/публикации увидят «не настроено»), для `BOT_TOKEN` и для обязательных значений вне `DRY_RUN` — понятная ошибка конфигурации.
- **T01: коды возврата процесса** — `0` успех, `1` ошибка работы, `2` ошибка конфигурации, `3` Telegram отклонил токен. Пригодится `run.bat` и Планировщику задач Windows.
- **T01: имена переменных зафиксированы** — см. `.env.example` (`POLL_INTERVAL_MIN_SEC`/`POLL_INTERVAL_MAX_SEC`, `HTTP_TIMEOUT_SEC`, `POST_TTL_HOURS`, `INTERCEPT_TIMEOUT_MINUTES`, `QUEUE_VISIBLE_LIMIT`, `DEDUP_DUPLICATE_THRESHOLD`, `DEDUP_GRAY_ZONE_THRESHOLD`, `LOGS_DIR`). Следующие задачи (T03, T05, T07) должны читать значения только из `Settings`, а не из `os.getenv` напрямую.
- **T01: версии зависимостей зафиксированы:** aiogram 3.32.0, python-dotenv 1.2.4, feedparser 6.0.14, httpx 0.28.1. SDK Gemini/Groq не добавлялись — только REST через `httpx` (T09).

### Передача дальше (T04)
- Новости приходят как `bot.parsers.rss.FeedItem` (`url`, `source`, `title`, `guid`, `lead`, `published_at`, `image_url`). Лид уже очищен от HTML и обрезан до ~200 символов — фильтру остаётся нормализовать регистр и искать ключи в `title` + `lead`.
- Подписка на находки: `poll_forever(db, settings, stop_event, on_item)`, `on_item` может быть корутиной (вызывается до записи в `seen_urls`; ошибка подписчика не теряет запись — она повторится).
- Парсер сам пишет URL/GUID в `seen_urls`: в T04 этого делать не нужно и пост создавать не нужно (это T07).
- Никакой сети в тестах: берите `parse_feed` + фикстуры из `tests/fixtures`, настройки — через `make_settings` из `tests/helpers.py`. Новые ключи и пороги добавляйте в `Settings`/`.env.example`, не через `os.getenv`.

### Передача дальше (T03)
- Парсер строится на `Database` (`bot/db.py`): `create_post(...)`, `is_url_seen(url, guid)`, `mark_url_seen(...)`, `list_pending`, `list_for_dedup`. Не писать SQL в парсере.
- Все параметры (интервалы, таймаут HTTP, пороги) — из `bot.config.Settings`; новых `os.getenv` не добавлять.
- `bot/parsers/rss.py` должен уметь работать без сети в тестах: вынести разбор ленты в чистую функцию и подавать в неё сохранённые XML-фикстуры из `tests/fixtures/`.
- Jitter реализовать как `random.uniform(min_sec, max_sec)`; опрос — `asyncio`-цикл с отменой по `asyncio.Event`, без блокирующих вызовов.
- Статусы и TTL/Tайм-ауты уже в БД — в T03 их не дублировать.

### Передача дальше (T02)
- Хранилище создавать поверх `bot.config.Settings`: путь к БД — `settings.database_path`, каталоги уже создаёт `settings.ensure_directories()`. Не читать `os.getenv` в обход конфига.
- В `Settings` уже есть `post_ttl_hours` (48), `intercept_timeout_minutes` (10), `queue_visible_limit` (10), пороги дедупликации — использовать их, а не константы в коде.
- Точка входа намеренно тонкая: `bot/main.py` пока содержит только `/start` и `/ping`. Хендлеры T07–T08 подключать через `bot/handlers/`, не разрастаясь в `main.py`.
- При старте без `.env` ожидаемый код возврата — `2` (ошибка конфигурации), это нормальный сценарий проверки, а не баг.

---

## Шаблон записи

```
## [ГГГГ-ММ-ДД]

### Добавлено
- T0X: краткое описание. Файлы: `path/one.py`, `path/two.py`. Автор: <агент/модель>.

### Изменено
- ...

### Исправлено
- ...
```
