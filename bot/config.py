"""Конфигурация бота: чтение ``.env``, разбор значений, валидация.

Правила:

* значения читаются из переменных окружения; файл ``.env`` в корне проекта
  подгружается через ``python-dotenv`` (окружение имеет приоритет над файлом);
* у всего, кроме секретов и обязательных ID, есть безопасные значения
  по умолчанию - код должен запускаться без правки конфига;
* ошибки собираются в один список и выдаются одним понятным сообщением
  (см. :class:`ConfigError`), без трейсбеков;
* при ``DRY_RUN=1`` ключи ИИ и ``CHANNEL_ID`` не обязательны: бот работает
  на заглушках. ``BOT_TOKEN`` нужен всегда - без него не подключиться к Telegram.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта - каталог, где лежат ``.env`` и ``data/`` (родитель пакета bot).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

_TRUE_VALUES = {"1", "true", "yes", "y", "on", "да"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "нет"}
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}

# Ключевые слова отбора новостей по умолчанию (п. 3 ТЗ).
# Меняются переменной NEWS_KEYWORDS — без правки кода.
DEFAULT_NEWS_KEYWORDS: tuple[str, ...] = (
    "GTA 6",
    "GTA VI",
    "Grand Theft Auto",
    "Rockstar",
)

# Признаки незаполненной заглушки из ``.env.example``.
_PLACEHOLDER_MARKERS = ("PASTE", "ЗАМЕНИ", "ЗАМЕНИТЬ", "XXXX", "<", ">")


class ConfigError(Exception):
    """Конфигурация некорректна.

    В сообщении - список проблем, по одной в строке (без префикса-маркера,
    его добавляет вызывающий код).
    """


def _looks_like_placeholder(value: str) -> bool:
    """Похоже ли значение на заглушку из ``.env.example``."""
    upper = value.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def _split_admins(raw: str, errors: list[str]) -> tuple[int, ...]:
    """Разобрать ``ADMIN_IDS`` (числа через запятую, точку с запятой или пробел)."""
    if not raw.strip():
        errors.append(
            "Не задан ADMIN_IDS - список числовых Telegram ID администраторов "
            "через запятую (пример: 123456789,987654321)."
        )
        return ()

    if _looks_like_placeholder(raw):
        errors.append(
            f"ADMIN_IDS: в .env осталась заглушка ({raw!r}). "
            "Укажите реальные числовые Telegram ID через запятую."
        )
        return ()

    admins: list[int] = []
    for chunk in re.split(r"[,;\s]+", raw.strip()):
        if not chunk:
            continue
        try:
            admin_id = int(chunk)
        except ValueError:
            errors.append(
                f"ADMIN_IDS: {chunk!r} - не числовой Telegram ID. "
                "Ожидаются числа через запятую."
            )
            continue
        if admin_id <= 0:
            errors.append(
                f"ADMIN_IDS: {admin_id} - некорректный Telegram ID (должен быть > 0)."
            )
            continue
        if admin_id not in admins:
            admins.append(admin_id)

    if not admins and not any(message.startswith("ADMIN_IDS") for message in errors):
        errors.append("ADMIN_IDS: после разбора не осталось ни одного ID.")
    return tuple(admins)


def _split_keywords(
    raw: str, default: tuple[str, ...], errors: list[str]
) -> tuple[str, ...]:
    """Разобрать ``NEWS_KEYWORDS`` (ключи через запятую или точку с запятой).

    Пустое значение — штатный случай: берутся ключи по умолчанию из
    :data:`DEFAULT_NEWS_KEYWORDS`. Повторы (без учёта регистра) убираются,
    лишние пробелы внутри ключа схлопываются.
    """
    text = raw.strip()
    if not text:
        return default

    keywords: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;]", text):
        keyword = " ".join(chunk.split())
        if not keyword:
            continue
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)

    if not keywords:
        errors.append(
            "NEWS_KEYWORDS: после разбора не осталось ни одного ключевого слова."
        )
        return default
    return tuple(keywords)


def _resolve_path(raw: str, default: str) -> Path:
    """Путь из конфига: относительный считается от корня проекта."""
    path = Path(raw.strip() or default)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


class _Reader:
    """Читает и разбирает переменные, накапливая ошибки и предупреждения."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        self.warnings: list[str] = []

    def text(self, name: str, default: str = "") -> str:
        return (os.getenv(name) or default).strip()

    def flag(self, name: str, default: bool) -> bool:
        raw = self.text(name)
        if not raw:
            return default
        lowered = raw.lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
        self.errors.append(
            f"{name}: {raw!r} - не похоже на да/нет. Используйте 1 или 0."
        )
        return default

    def integer(
        self,
        name: str,
        default: int,
        *,
        minimum: int = 0,
        maximum: int | None = None,
    ) -> int:
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{name}: {raw!r} - ожидается целое число.")
            return default
        if value < minimum or (maximum is not None and value > maximum):
            bounds = f"от {minimum} до {maximum}" if maximum is not None else f"не меньше {minimum}"
            self.errors.append(f"{name}: {value} вне допустимых значений ({bounds}).")
            return default
        return value

    def real(self, name: str, default: float, *, low: float, high: float) -> float:
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            self.errors.append(f"{name}: {raw!r} - ожидается число (например, 0.85).")
            return default
        if not low <= value <= high:
            self.errors.append(f"{name}: {value} вне диапазона {low}–{high}.")
            return default
        return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Все настройки бота одним неизменяемым объектом."""

    # Telegram
    bot_token: str
    channel_id: str
    admin_ids: tuple[int, ...]
    dry_run: bool

    # Отбор новостей (T04)
    news_keywords: tuple[str, ...]

    # ИИ (нужны только вне DRY_RUN)
    gemini_api_key: str
    gemini_model: str
    groq_api_key: str
    groq_model: str

    # Логи и файлы
    log_level: str
    logs_dir: Path
    database_path: Path
    images_dir: Path
    backup_dir: Path

    # Картинки (T06)
    image_max_mb: int
    image_keep_hours: int

    # Опрос RSS
    poll_interval_min_sec: int
    poll_interval_max_sec: int
    http_timeout_sec: int

    # Жизненный цикл поста
    post_ttl_hours: int
    intercept_timeout_minutes: int
    queue_visible_limit: int

    # Дедупликация
    dedup_duplicate_threshold: float
    dedup_gray_zone_threshold: float

    # Публикация (T10): тихий режим — без уведомления подписчиков
    publish_silent: bool = False

    warnings: tuple[str, ...] = field(default_factory=tuple)

    def is_admin(self, user_id: int | None) -> bool:
        """Является ли пользователь администратором канала."""
        return user_id is not None and user_id in self.admin_ids

    @property
    def image_max_bytes(self) -> int:
        """Лимит размера картинки в байтах (для сверки с ``Content-Length``)."""
        return self.image_max_mb * 1024 * 1024

    def ensure_directories(self) -> None:
        """Создать рабочие каталоги (данные, картинки, логи), если их нет."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)


def _clean_optional(raw: str) -> str:
    """Необязательное значение: заглушка из .env.example считается пустым."""
    if _looks_like_placeholder(raw):
        return ""
    return raw


def load_settings(env_path: Path | None = None) -> Settings:
    """Прочитать ``.env`` и вернуть настройки.

    :param env_path: путь к файлу с переменными; по умолчанию ``<корень>/.env``.
    :raises ConfigError: если обязательные значения отсутствуют или некорректны.
    """
    path = ENV_PATH if env_path is None else env_path
    if path.is_file():
        # override=False: реальные переменные окружения важнее файла.
        load_dotenv(path, override=False, encoding="utf-8")

    errors: list[str] = []
    reader = _Reader(errors)

    dry_run = reader.flag("DRY_RUN", default=True)
    if dry_run:
        reader.warnings.append(
            "DRY_RUN=1: бот работает на заглушках, публикация в канал и ИИ не выполняются."
        )

    # --- BOT_TOKEN: обязателен всегда -------------------------------------
    bot_token = reader.text("BOT_TOKEN")
    if not bot_token:
        errors.append(
            "Не задан BOT_TOKEN - токен бота от @BotFather. "
            "Скопируйте .env.example в .env и укажите токен."
        )
    elif _looks_like_placeholder(bot_token):
        errors.append(
            "BOT_TOKEN: в .env осталась заглушка. Вставьте настоящий токен от @BotFather."
        )

    # --- CHANNEL_ID: обязателен вне DRY_RUN -------------------------------
    channel_id = reader.text("CHANNEL_ID")
    if _looks_like_placeholder(channel_id):
        channel_id = ""
    if not channel_id:
        if not dry_run:
            errors.append(
                "Не задан CHANNEL_ID - ID канала для публикации "
                "(например -1001234567890 или @my_channel)."
            )
        else:
            reader.warnings.append("CHANNEL_ID не задан: публикация в канал недоступна (DRY_RUN).")
    elif channel_id.startswith("@"):
        if len(channel_id) < 2:
            errors.append("CHANNEL_ID: после @ должно идти имя канала.")
    elif not re.fullmatch(r"-?\d+", channel_id):
        errors.append(
            f"CHANNEL_ID: {channel_id!r} - ожидается числовой ID (например -1001234567890) "
            "или @username канала."
        )

    # --- Админы ------------------------------------------------------------
    admin_ids = _split_admins(reader.text("ADMIN_IDS"), errors)

    # --- Отбор новостей по ключам -----------------------------------------
    news_keywords = _split_keywords(
        reader.text("NEWS_KEYWORDS"), DEFAULT_NEWS_KEYWORDS, errors
    )

    # --- Ключи ИИ: вне DRY_RUN достаточно одного провайдера ----------------
    gemini_api_key = _clean_optional(reader.text("GEMINI_API_KEY"))
    groq_api_key = _clean_optional(reader.text("GROQ_API_KEY"))
    if not dry_run:
        if not gemini_api_key and not groq_api_key:
            errors.append(
                "Не задан ни один ключ ИИ: нужен GEMINI_API_KEY (основной) "
                "или GROQ_API_KEY (запасной)."
            )
        elif not gemini_api_key:
            reader.warnings.append(
                "GEMINI_API_KEY не задан: рерайт пойдёт только через Groq."
            )
        elif not groq_api_key:
            reader.warnings.append(
                "GROQ_API_KEY не задан: запасной провайдер отключён, "
                "рерайт работает только через Gemini."
            )

    # --- Прочие настройки с дефолтами -------------------------------------
    log_level = reader.text("LOG_LEVEL", "INFO").upper()
    if log_level not in _LOG_LEVELS:
        reader.warnings.append(
            f"LOG_LEVEL: {log_level!r} неизвестен, используется INFO."
        )
        log_level = "INFO"

    poll_interval_min_sec = reader.integer("POLL_INTERVAL_MIN_SEC", 300, minimum=30)
    poll_interval_max_sec = reader.integer("POLL_INTERVAL_MAX_SEC", 600, minimum=30)
    if poll_interval_max_sec < poll_interval_min_sec:
        errors.append(
            "POLL_INTERVAL_MAX_SEC меньше POLL_INTERVAL_MIN_SEC - "
            "проверьте интервалы опроса RSS."
        )

    dedup_gray_zone_threshold = reader.real(
        "DEDUP_GRAY_ZONE_THRESHOLD", 0.70, low=0.0, high=1.0
    )
    dedup_duplicate_threshold = reader.real(
        "DEDUP_DUPLICATE_THRESHOLD", 0.85, low=0.0, high=1.0
    )
    if dedup_duplicate_threshold <= dedup_gray_zone_threshold:
        errors.append(
            "DEDUP_DUPLICATE_THRESHOLD должен быть больше DEDUP_GRAY_ZONE_THRESHOLD."
        )

    image_max_mb = reader.integer("IMAGE_MAX_MB", 10, minimum=1)
    image_keep_hours = reader.integer("IMAGE_KEEP_HOURS", 48, minimum=1)

    if errors:
        raise ConfigError("\n".join(f"- {message}" for message in errors))

    database_path = _resolve_path(reader.text("DATABASE_PATH"), "data/bot.db")
    backup_dir = _resolve_path(
        reader.text("BACKUP_DIR"), str(database_path.parent / "backups")
    )

    return Settings(
        bot_token=bot_token,
        channel_id=channel_id,
        admin_ids=admin_ids,
        dry_run=dry_run,
        news_keywords=news_keywords,
        gemini_api_key=gemini_api_key,
        gemini_model=reader.text("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        groq_api_key=groq_api_key,
        groq_model=reader.text("GROQ_MODEL", "openai/gpt-oss-120b"),
        log_level=log_level,
        logs_dir=_resolve_path(reader.text("LOGS_DIR"), "logs"),
        database_path=database_path,
        images_dir=_resolve_path(reader.text("IMAGES_DIR"), "data/images"),
        backup_dir=backup_dir,
        image_max_mb=image_max_mb,
        image_keep_hours=image_keep_hours,
        poll_interval_min_sec=poll_interval_min_sec,
        poll_interval_max_sec=poll_interval_max_sec,
        http_timeout_sec=reader.integer("HTTP_TIMEOUT_SEC", 20, minimum=5),
        post_ttl_hours=reader.integer("POST_TTL_HOURS", 48, minimum=1),
        intercept_timeout_minutes=reader.integer(
            "INTERCEPT_TIMEOUT_MINUTES", 10, minimum=1
        ),
        queue_visible_limit=reader.integer("QUEUE_VISIBLE_LIMIT", 10, minimum=1),
        dedup_duplicate_threshold=dedup_duplicate_threshold,
        dedup_gray_zone_threshold=dedup_gray_zone_threshold,
        publish_silent=reader.flag("PUBLISH_SILENT", default=False),
        warnings=tuple(reader.warnings),
    )
