# CHANGELOG

Все заметные изменения проекта. Формат — по мотивам [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Задачи и их статусы — в `TASKS.md`. Требования — в `ТЗ.md`.

**Правило для агентов:** после завершения задачи добавить сюда запись в раздел `[Unreleased]` (или в раздел с датой), указав ID задачи, что сделано, какие файлы появились и кто это сделал.

---

## [Unreleased]

### Добавлено
- `TASKS.md` — реестр задач T01–T12 со статусами, зависимостями и правилами обновления.
- `CHANGELOG.md` — этот журнал.
- `docs/AGENTS.md` — правила работы ИИ-агентов над проектом.
- **T01: скелет проекта и конфигурация.** Пакет `bot/` (`__init__.py`, `main.py`, `config.py`, `utils/logging.py`) и пустые заготовки `bot/parsers/`, `bot/filters/`, `bot/services/`, `bot/handlers/`; конфиг с валидацией и дефолтами, логирование в консоль и `logs/bot.log` (UTF-8, ротация в полночь, хранение 30 дней), точка входа с aiogram 3.32.0 и long polling, команды `/start` и `/ping` только для админов, корректный выход без трейсбека при отсутствии токена, `requirements.txt` с фиксированными версиями и `.env.example` с заглушками и русскими комментариями. Файлы: `bot/main.py`, `bot/config.py`, `bot/utils/logging.py`, `bot/__init__.py`, `bot/utils/__init__.py`, `bot/parsers/__init__.py`, `bot/filters/__init__.py`, `bot/services/__init__.py`, `bot/handlers/__init__.py`, `requirements.txt`, `.env.example`. Автор: агент-исполнитель T01 (DeepSeek/OpenCode).

### Добавлено
- **T02: хранилище SQLite и модели данных.** `bot/models.py` (статусы `pending`/`intercepted`/`published`/`dropped`, `DedupState`, `DraftOrigin`, dataclass'ы `Post`, `DraftVersion`, `SeenUrl`, `DedupLogEntry`, помощники `to_iso`/`from_iso` в UTC), `bot/db.py` (`Database` — async-фасад над `sqlite3` через `asyncio.to_thread`: схема v1 и миграции по `PRAGMA user_version`, WAL, `foreign_keys=ON`, busy timeout; таблицы `posts`, `draft_versions`, `seen_urls`, `dedup_log`; индексы по `status`/`url`/`guid`/`created_at`; частичный уникальный индекс — один активный пост на админа; переходы статусов с `InvalidTransitionError`, перехват с `OwnerBusyError`, TTL и тайм-ауты, откат черновиков, лог серых зон), `bot/services/backup.py` (`VACUUM INTO`, хранение 7 снимков, суточный цикл с остановкой по `asyncio.Event`), `tests/` (`test_models.py`, `test_db.py`, `test_backup.py`, `conftest.py`, `helpers.py`), `requirements-dev.txt`, `pytest.ini`. Файлы: `bot/models.py`, `bot/db.py`, `bot/services/backup.py`, `tests/*`. Авторы: агент T02 (DeepSeek/OpenCode) и координатор (правка теста черновиков, тесты бэкапа). **31 тест проходит.**
- **Telegram-доступы настроены (частично T12).** Токен `@ParsgtaBot_bot` проверен через `getMe`; бот добавлен администратором в канал «FOWGSL» (`@fowgsl`, `-1004371911262`) с правами публикации. `CHANNEL_ID` и `ADMIN_IDS` (`7846135495`, `1389758957`, `1825876129`) записаны в локальный `.env`, ACL файла ограничен пользователем. Все три админа активировали бота. Проверен запуск: long polling, «Run polling for bot @ParsgtaBot_bot», ошибок нет. Ключи Gemini/Groq пока не предоставлены — режим `DRY_RUN=1`. Автор: агент-координатор (OpenCode).
- **Утилита получения chat_id.** `tools/get_chat_id.py` опрашивает `getUpdates` и печатает ID групп/каналов, где бот видит сообщения — чтобы заполнить `CHANNEL_ID` без сторонних ботов. Автор: агент-координатор (OpenCode).

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
