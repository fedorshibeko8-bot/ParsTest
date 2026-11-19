"""Рерайт новости через Gemini Flash-Lite с фоллбэком на Groq (задача T09).

Правила из ТЗ (п. 9):

* первый вызов — полный текст статьи → выжимка;
* второй и далее — полный текст статьи **и** текущий черновик, но источник
  истины всегда оригинал (защита от дрейфа и галлюцинаций);
* ИИ только переводит и пересказывает: никакой фильтрации, дедупликации
  и проверки фактов здесь нет;
* заголовок 40–70 символов, тело 120–350, итого до 420.

Провайдеры вызываются по цепочке: первый настроенный, затем резервный.
Любая ошибка (сеть, 429, отказ модели, нечитаемый ответ) переводит
на следующего; если не ответил никто — :class:`RewriteError` с причинами.

В ``DRY_RUN`` сеть не используется: возвращается предсказуемый текст,
чтобы карточку и публикацию можно было проверить без ключей.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Final, TypeVar

import httpx

from bot.config import Settings
from bot.db import Database
from bot.models import DraftOrigin, Post
from bot.services.format import (
    BODY_MAX,
    BODY_MIN,
    TITLE_MAX,
    TITLE_MIN,
    TOTAL_MAX,
    clean_text,
    cut_sentences,
    cut_words,
    one_line,
    remove_trailing_ellipsis,
)

logger = logging.getLogger(__name__)

#: Как вызывается провайдер: настройки, промпт и общий HTTP-клиент.
ProviderRequest = Callable[..., Awaitable[str]]

T = TypeVar("T")

# ----------------------------------------------------------------------
# Провайдеры и лимиты
# ----------------------------------------------------------------------
GEMINI_ENDPOINT: Final[str] = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GROQ_ENDPOINT: Final[str] = "https://api.groq.com/openai/v1/chat/completions"

PROVIDER_GEMINI: Final[str] = "gemini"
PROVIDER_GROQ: Final[str] = "groq"
PROVIDER_DRY_RUN: Final[str] = "dry_run"

LLM_TIMEOUT_SEC: Final[float] = 45.0
MAX_OUTPUT_TOKENS: Final[int] = 1024

HINT_AFTER_ROUNDS: Final[int] = 3
HINT_TEXT: Final[str] = (
    "Текст уже сильно отработан — дальше лучше править вручную."
)

DRY_RUN_TITLE: Final[str] = "Драй-ран: новость переписана без ключей ИИ"
DRY_RUN_BODY: Final[str] = (
    "Это тестовый рерайт в режиме DRY_RUN. Настоящий текст появится, когда "
    "в окружении будут ключи Gemini или Groq. Публикация в канал отключена, "
    "поэтому пост можно спокойно открыть и отредактировать."
)

SYSTEM_PROMPT: Final[str] = (
    "Ты редактор русскоязычного Telegram-канала об играх. Ты переводишь "
    "англоязычные игровые новости и пересказываешь их своими словами. "
    "Ты никогда не добавляешь факты, которых нет в исходном тексте, не "
    "придумываешь числа, даты, цитаты и названия. Ты не пишешь ссылки, "
    "хэштеги, имена каналов и подписи. Не обрывай предложения многоточием. "
    "Ответ — строго JSON без пояснений."
)


# ----------------------------------------------------------------------
# Ошибки и результаты
# ----------------------------------------------------------------------
class RewriteError(RuntimeError):
    """Рерайт не удался; ``attempts`` — причины по каждому провайдеру."""

    def __init__(self, message: str, *, attempts: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.attempts = attempts


class RewriteFormatError(RewriteError):
    """Провайдер ответил, но текст не удалось разобрать."""


@dataclass(frozen=True, slots=True)
class RewriteResult:
    """Готовый черновик и служебная информация о нём."""

    title: str
    body: str
    provider: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RewriteOutcome:
    """Результат рерайта вместе с обновлённым постом."""

    post: Post
    result: RewriteResult
    round_no: int

    @property
    def hint(self) -> str | None:
        """Подсказка «пора править вручную» после 3–4 кругов."""
        return HINT_TEXT if self.round_no >= HINT_AFTER_ROUNDS else None


# ----------------------------------------------------------------------
# Промпт
# ----------------------------------------------------------------------
def build_prompt(post: Post) -> str:
    """Собрать запрос: оригинал всегда на первом месте.

    Черновик добавляется отдельным блоком с пометкой «не источник фактов»,
    чтобы повторный круг улучшал формулировки, а не уводил от статьи.
    """
    lines = [
        "Новость (источник истины, факты только отсюда):",
        f"Заголовок: {post.title_original.strip()}",
        f"Текст: {post.lead.strip()}",
        "",
    ]
    if post.draft_text.strip():
        lines += [
            "Предыдущая версия (можно улучшить формулировки, но НЕ источник фактов):",
            f"Заголовок: {post.draft_title.strip()}",
            f"Текст: {post.draft_text.strip()}",
            "",
        ]
    lines += [
        "Задача: переведи новость на русский и перескажи своими словами.",
        "Требования:",
        f"- заголовок: {TITLE_MIN}–{TITLE_MAX} символов, без кавычек и точки в конце;",
        f"- тело: {BODY_MIN}–{BODY_MAX} символов, 2–3 предложения;",
        "- не добавляй факты, которых нет в новости;",
        "- сохраняй числа, даты и названия так, как они даны в новости;",
        "- не вставляй ссылки, хэштеги, имена каналов и подписи;",
        "- без вводных фраз вроде «в статье сообщается»;",
        "- не ставь многоточие в конце: предложения должны быть законченными;",
        'Ответ — только JSON: {"title": "...", "body": "..."}',
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Разбор и нормализация ответа
# ----------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```(?:json)?|```\s*$", re.MULTILINE)


def _load_json(raw: str) -> dict | None:
    """Разобрать JSON, в том числе внутри ```-блока или с текстом вокруг."""
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def parse_response(raw: str) -> tuple[str, str]:
    """Достать заголовок и тело из ответа модели.

    Основной путь — JSON. Если модель ответила обычным текстом, берём
    первую строку как заголовок, остальное — как тело.
    """
    data = _load_json(raw)
    if data is not None:
        title = remove_trailing_ellipsis(one_line(clean_text(data.get("title"))))
        body = remove_trailing_ellipsis(clean_text(data.get("body")))
        if title and body:
            return title, body
        if "title" in data or "body" in data:
            raise RewriteFormatError("В ответе ИИ нет заголовка или текста.")

    lines = [line.strip() for line in _FENCE_RE.sub("", raw).splitlines() if line.strip()]
    if len(lines) >= 2:
        title = remove_trailing_ellipsis(one_line(clean_text(lines[0])))
        body = remove_trailing_ellipsis(clean_text(" ".join(lines[1:])))
        if title and body:
            return title, body

    raise RewriteFormatError("Не удалось разобрать ответ ИИ.")


def fit_title(title: str) -> str:
    """Уложить заголовок в 70 символов по границе слова, без многоточия."""
    return cut_words(one_line(title), TITLE_MAX)


def fit_body(body: str) -> str:
    """Уложить тело в 350 символов только по концу целого предложения.

    Если ни одно предложение не влезло, возвращается пустая строка: обрывать
    фразу и ставить многоточие запрещено.
    """
    return cut_sentences(one_line(body), BODY_MAX)


def length_warnings(title: str, body: str) -> tuple[str, ...]:
    """Что не влезло в требования ТЗ (укорачивание делает :func:`normalize`)."""
    warnings: list[str] = []
    if len(title) < TITLE_MIN:
        warnings.append(f"заголовок короче {TITLE_MIN} символов ({len(title)})")
    if len(body) < BODY_MIN:
        warnings.append(f"текст короче {BODY_MIN} символов ({len(body)})")
    if len(title) + len(body) > TOTAL_MAX:
        warnings.append("итого больше 420 символов")
    return tuple(warnings)


def normalize(title: str, body: str) -> tuple[str, str, tuple[str, ...]]:
    """Обрезать слишком длинное и вернуть предупреждения о коротком.

    Удлинять текст нельзя: это означало бы придумать факты. Поэтому
    слишком короткий ответ сохраняется как есть, а админу показывается
    замечание — он может нажать «Доработать с ИИ» ещё раз или поправить вручную.
    """
    fitted_title = fit_title(remove_trailing_ellipsis(title))
    fitted_body = fit_body(remove_trailing_ellipsis(body))
    return fitted_title, fitted_body, length_warnings(fitted_title, fitted_body)


# ----------------------------------------------------------------------
# HTTP-провайдеры
# ----------------------------------------------------------------------
def _short(value: object, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _error_detail(response: httpx.Response) -> str:
    """Достать человекочитаемую причину из тела ошибки, если она есть."""
    try:
        payload = response.json()
    except ValueError:
        return _short(response.text)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("status")
            if message:
                return _short(message)
        if isinstance(error, str):
            return _short(error)
    return _short(response.text)


def _check(response: httpx.Response, provider: str) -> None:
    """Превратить HTTP-ошибку в понятное сообщение для логов и админа."""
    if response.status_code < 400:
        return
    detail = _error_detail(response)
    if response.status_code == 429:
        raise RewriteError(f"{provider}: лимит запросов (429). {detail}".strip())
    if response.status_code in (401, 403):
        raise RewriteError(f"{provider}: ключ не принят ({response.status_code}).")
    if "location is not supported" in detail.lower():
        raise RewriteError(
            f"{provider}: регион не поддерживается — нужен зарубежный IP."
        )
    raise RewriteError(f"{provider}: HTTP {response.status_code}. {detail}".strip())


async def request_gemini(
    settings: Settings, prompt: str, *, client: httpx.AsyncClient
) -> str:
    """Один вызов Gemini API; возвращает сырой текст ответа."""
    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
        },
    }
    response = await client.post(
        url, json=payload, headers={"x-goog-api-key": settings.gemini_api_key}
    )
    _check(response, PROVIDER_GEMINI)

    try:
        data = response.json()
    except ValueError as exc:
        raise RewriteFormatError("gemini: ответ не является JSON.") from exc

    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise RewriteError(f"gemini: пустой ответ (blockReason={reason}).")
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(str(part.get("text") or "") for part in parts).strip()
    if not text:
        finish = candidate.get("finishReason")
        raise RewriteError(f"gemini: модель не вернула текст (finishReason={finish}).")
    return text


async def request_groq(
    settings: Settings, prompt: str, *, client: httpx.AsyncClient
) -> str:
    """Один вызов Groq (OpenAI-совместимый API); возвращает сырой текст."""
    payload: dict = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }
    if "gpt-oss" in settings.groq_model:
        # Рассуждающей модели хватает минимального бюджета: задача — пересказ.
        payload["reasoning_effort"] = "low"

    response = await client.post(
        GROQ_ENDPOINT,
        json=payload,
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
    )
    _check(response, PROVIDER_GROQ)

    try:
        data = response.json()
    except ValueError as exc:
        raise RewriteFormatError("groq: ответ не является JSON.") from exc

    choices = data.get("choices") or []
    if not choices:
        raise RewriteError("groq: пустой ответ.")
    text = str((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RewriteError("groq: модель не вернула текст.")
    return text


def configured_providers(
    settings: Settings,
) -> list[tuple[str, ProviderRequest]]:
    """Цепочка провайдеров: только те, для которых есть ключ."""
    providers: list[tuple[str, ProviderRequest]] = []
    if settings.gemini_api_key:
        providers.append((PROVIDER_GEMINI, request_gemini))
    if settings.groq_api_key:
        providers.append((PROVIDER_GROQ, request_groq))
    return providers


async def _ask_providers(
    settings: Settings,
    prompt: str,
    *,
    client: httpx.AsyncClient | None,
    parse: Callable[[str], T],
) -> tuple[str, T]:
    """Обойти цепочку провайдеров, применяя ``parse`` к ответу.

    Нечитаемый ответ — такая же неудача, как 429 или таймаут: он тоже
    переводит на следующего провайдера, иначе плохой ответ Gemini
    отменял бы исправный Groq.
    """
    providers = configured_providers(settings)
    if not providers:
        raise RewriteError(
            "Не заданы ключи ИИ: добавьте GEMINI_API_KEY или GROQ_API_KEY."
        )

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=LLM_TIMEOUT_SEC)

    attempts: list[str] = []
    try:
        for name, request in providers:
            try:
                raw = await request(settings, prompt, client=client)
                value = parse(raw)
            except httpx.HTTPError as exc:
                attempts.append(f"{name}: сеть ({type(exc).__name__})")
            except RewriteError as exc:
                attempts.append(str(exc))
            else:
                logger.info("Рерайт выполнен провайдером %s", name)
                return name, value
            logger.warning("Провайдер %s не ответил: %s", name, attempts[-1])
    finally:
        if owns_client:
            await client.aclose()

    raise RewriteError(
        "ИИ недоступен: " + "; ".join(attempts), attempts=tuple(attempts)
    )


async def generate(
    settings: Settings,
    prompt: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Спросить ИИ по цепочке; вернуть ``(провайдер, сырой ответ)``."""
    name, raw = await _ask_providers(
        settings, prompt, client=client, parse=lambda value: value
    )
    return name, raw


async def generate_draft(
    settings: Settings,
    prompt: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str, str]:
    """Спросить ИИ и сразу разобрать: ``(провайдер, заголовок, тело)``."""
    name, parsed = await _ask_providers(
        settings, prompt, client=client, parse=parse_response
    )
    title, body = parsed
    return name, title, body


# ----------------------------------------------------------------------
# Основная операция
# ----------------------------------------------------------------------
def dry_run_text(post: Post) -> tuple[str, str]:
    """Предсказуемый ответ без сети (``DRY_RUN=1``)."""
    return DRY_RUN_TITLE, DRY_RUN_BODY


async def rewrite_post(
    db: Database,
    settings: Settings,
    post: Post,
    *,
    actor_id: int,
    client: httpx.AsyncClient | None = None,
) -> RewriteOutcome:
    """Переписать пост и сохранить новую версию черновика.

    :raises RewriteError: пост уже не у этого админа или ИИ не ответил.
    """
    fresh = await db.get_post(post.id)
    if fresh is None or not fresh.is_intercepted or fresh.owner_id != actor_id:
        raise RewriteError("Пост уже не ваш — рерайт отменён.")

    if settings.dry_run:
        title, body = dry_run_text(fresh)
        provider = PROVIDER_DRY_RUN
        warnings: tuple[str, ...] = ()
    else:
        prompt = build_prompt(fresh)
        provider, title, body = await generate_draft(settings, prompt, client=client)
        title, body, warnings = normalize(title, body)

    round_no = fresh.rewrite_rounds + 1
    await db.add_draft_version(
        fresh.id,
        title=title,
        body=body,
        origin=DraftOrigin.AI,
        round_no=round_no,
    )
    updated = await db.get_post(fresh.id)
    if updated is None:  # pragma: no cover - пост не удаляется
        raise RewriteError("Пост исчез во время рерайта.")

    result = RewriteResult(
        title=title, body=body, provider=provider, warnings=warnings
    )
    logger.info(
        "Пост #%s: круг %d, провайдер %s, %d/%d символов",
        fresh.id,
        round_no,
        provider,
        len(title),
        len(body),
    )
    return RewriteOutcome(post=updated, result=result, round_no=round_no)


__all__ = [
    "BODY_MAX",
    "BODY_MIN",
    "DRY_RUN_BODY",
    "DRY_RUN_TITLE",
    "HINT_AFTER_ROUNDS",
    "HINT_TEXT",
    "PROVIDER_DRY_RUN",
    "PROVIDER_GEMINI",
    "PROVIDER_GROQ",
    "RewriteError",
    "RewriteFormatError",
    "RewriteOutcome",
    "RewriteResult",
    "TITLE_MAX",
    "TITLE_MIN",
    "TOTAL_MAX",
    "build_prompt",
    "configured_providers",
    "dry_run_text",
    "fit_body",
    "fit_title",
    "generate",
    "generate_draft",
    "length_warnings",
    "normalize",
    "parse_response",
    "request_gemini",
    "request_groq",
    "rewrite_post",
]
