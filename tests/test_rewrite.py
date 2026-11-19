"""Тесты рерайта через ИИ (задача T09).

Сеть не нужна: HTTP-клиент собирается на ``httpx.MockTransport``, поэтому
проверяются и цепочка провайдеров, и разбор ответа, и сохранение черновика.
"""

from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText

from bot.db import Database
from bot.handlers.cards import handle_post_action
from bot.models import DraftOrigin
from bot.services.cards import ACTION_AI, post_callback
from bot.services.rewrite import (
    BODY_MAX,
    BODY_MIN,
    DRY_RUN_BODY,
    DRY_RUN_TITLE,
    HINT_TEXT,
    PROVIDER_DRY_RUN,
    PROVIDER_GEMINI,
    PROVIDER_GROQ,
    RewriteError,
    RewriteFormatError,
    TITLE_MAX,
    TITLE_MIN,
    build_prompt,
    configured_providers,
    fit_body,
    fit_title,
    generate,
    generate_draft,
    normalize,
    parse_response,
    request_gemini,
    request_groq,
    rewrite_post,
)
from tests.fakes import FakeBot, FakeState, make_callback, make_message
from tests.helpers import T0, make_settings, run

GOOD_TITLE = "Rockstar показала новый кадр GTA 6 и дату релиза"
GOOD_BODY = (
    "Студия Rockstar Games показала новый кадр из GTA 6. В сообщении "
    "подтверждена дата выхода игры. Подробности обещают раскрыть позже."
)


# ----------------------------------------------------------------------
# Двойники HTTP
# ----------------------------------------------------------------------
def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Клиент с подменённым транспортом: в сеть не ходит."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def model_json(title: str = GOOD_TITLE, body: str = GOOD_BODY) -> str:
    return json.dumps({"title": title, "body": body}, ensure_ascii=False)


def gemini_payload(title: str = GOOD_TITLE, body: str = GOOD_BODY) -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": model_json(title, body)}]},
                "finishReason": "STOP",
            }
        ]
    }


def groq_payload(title: str = GOOD_TITLE, body: str = GOOD_BODY) -> dict:
    return {"choices": [{"message": {"content": model_json(title, body)}}]}


@pytest.fixture()
def live_settings(tmp_path):
    """Боевые настройки с ключами (сеть подменяется клиентом)."""
    return make_settings(
        dry_run=False,
        gemini_api_key="gemini-key",
        groq_api_key="groq-key",
        images_dir=tmp_path / "images",
    )


@pytest.fixture()
def admin_settings(tmp_path):
    return make_settings(admin_ids=(1,), dry_run=True, images_dir=tmp_path / "images")


def take(db: Database, post, owner_id: int = 1):
    run(db.mark_intercepted(post.id, owner_id, now=T0))
    stored = run(db.get_post(post.id))
    assert stored is not None
    return stored


# ----------------------------------------------------------------------
# Промпт
# ----------------------------------------------------------------------
def test_prompt_contains_original_news(db, make_post) -> None:
    post = make_post(
        title_original="Оригинальный заголовок", lead="Оригинальный текст статьи."
    )

    prompt = build_prompt(post)

    assert prompt.startswith("Новость (источник истины")
    assert "Оригинальный заголовок" in prompt
    assert "Оригинальный текст статьи." in prompt
    assert "Предыдущая версия" not in prompt
    assert f"{TITLE_MIN}–{TITLE_MAX}" in prompt
    assert f"{BODY_MIN}–{BODY_MAX}" in prompt


def test_prompt_puts_original_before_draft(db, make_post) -> None:
    post = make_post(
        title_original="Оригинальный заголовок", lead="Оригинальный текст статьи."
    )
    run(
        db.add_draft_version(
            post.id, title="Старый черновик", body="Старый текст черновика."
        )
    )
    stored = run(db.get_post(post.id))
    assert stored is not None

    prompt = build_prompt(stored)

    assert "Предыдущая версия" in prompt
    assert "Старый черновик" in prompt
    assert prompt.index("Оригинальный заголовок") < prompt.index("Старый черновик")
    assert "НЕ источник фактов" in prompt


# ----------------------------------------------------------------------
# Разбор ответа
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        model_json(),
        f"```json\n{model_json()}\n```",
        f"Вот результат:\n{model_json()}\nГотово.",
        f"```\n{model_json()}\n```\n\nПояснений не нужно.",
    ],
)
def test_parse_json_variants(raw: str) -> None:
    assert parse_response(raw) == (GOOD_TITLE, GOOD_BODY)


def test_parse_plain_text_fallback() -> None:
    raw = f"{GOOD_TITLE}\n{GOOD_BODY}"
    assert parse_response(raw) == (GOOD_TITLE, GOOD_BODY)


@pytest.mark.parametrize("raw", ["", "   ", "{}", "одна строка без тела"])
def test_parse_garbage_raises(raw: str) -> None:
    with pytest.raises(RewriteFormatError):
        parse_response(raw)


def test_parse_removes_links_tags_and_mentions() -> None:
    raw = model_json(
        title="Заголовок https://example.com/gta",
        body="Текст новости #GTA6 и @channel https://example.com",
    )

    title, body = parse_response(raw)

    assert "http" not in title.lower()
    assert "http" not in body.lower()
    assert "#" not in body
    assert "@" not in body


def test_parse_strips_quotes_in_title() -> None:
    raw = model_json(title=f"«{GOOD_TITLE}»")
    assert parse_response(raw)[0] == GOOD_TITLE


def test_parse_removes_trailing_ellipsis() -> None:
    raw = model_json(
        title="GTA 6 выйдет в 2027 году...",
        body="Rockstar подтвердила дату релиза. Окно выхода станет известно позже...",
    )

    title, body = parse_response(raw)

    assert title == "GTA 6 выйдет в 2027 году"
    # Незавершённая часть не показывается: остаётся целое предложение.
    assert body == "Rockstar подтвердила дату релиза."
    assert "…" not in title + body
    assert "..." not in title + body


# ----------------------------------------------------------------------
# Длины
# ----------------------------------------------------------------------
def test_good_sample_fits_limits() -> None:
    assert TITLE_MIN <= len(GOOD_TITLE) <= TITLE_MAX
    assert BODY_MIN <= len(GOOD_BODY) <= BODY_MAX


def test_fit_title_keeps_short_text() -> None:
    assert fit_title("Короткий заголовок") == "Короткий заголовок"


def test_fit_title_cuts_by_word() -> None:
    fitted = fit_title("Очень длинный заголовок " * 10)
    assert len(fitted) <= TITLE_MAX
    assert not fitted.endswith(" ")


def test_fit_body_prefers_sentence_boundary() -> None:
    body = GOOD_BODY + " Это ещё одно предложение." * 20
    fitted = fit_body(body)
    assert len(fitted) <= BODY_MAX
    assert fitted.endswith(".")


def test_fit_body_drops_unfinished_text() -> None:
    # Ни одного законченного предложения — обрыв не показывается совсем.
    assert fit_body("я" * 1000) == ""


def test_normalize_valid_text_has_no_warnings() -> None:
    title, body, warnings = normalize(GOOD_TITLE, GOOD_BODY)
    assert (title, body) == (GOOD_TITLE, GOOD_BODY)
    assert warnings == ()


def test_normalize_warns_about_short_parts() -> None:
    title, body, warnings = normalize("Слишком короткий", "Мало текста")
    assert title == "Слишком короткий"
    assert body == "Мало текста"
    assert any("заголовок короче" in warning for warning in warnings)
    assert any("текст короче" in warning for warning in warnings)


def test_normalize_trims_overlong_answer() -> None:
    title, body, warnings = normalize(GOOD_TITLE * 3, GOOD_BODY * 5)
    assert len(title) <= TITLE_MAX
    assert len(body) <= BODY_MAX
    assert warnings == ()


# ----------------------------------------------------------------------
# Провайдеры
# ----------------------------------------------------------------------
def test_configured_providers_respect_keys(tmp_path) -> None:
    both = make_settings(gemini_api_key="g", groq_api_key="r", dry_run=False)
    assert [name for name, _ in configured_providers(both)] == [
        PROVIDER_GEMINI,
        PROVIDER_GROQ,
    ]

    only_groq = make_settings(gemini_api_key="", groq_api_key="r", dry_run=False)
    assert [name for name, _ in configured_providers(only_groq)] == [PROVIDER_GROQ]

    none = make_settings(gemini_api_key="", groq_api_key="", dry_run=True)
    assert configured_providers(none) == []


def test_gemini_request_shape(live_settings) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=gemini_payload())

    raw = run(
        request_gemini(live_settings, "промпт", client=mock_client(handler))
    )

    assert "generativelanguage.googleapis.com" in seen["url"]
    assert live_settings.gemini_model in seen["url"]
    assert seen["key"] == "gemini-key"
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "промпт"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert json.loads(raw)["title"] == GOOD_TITLE


def test_gemini_geo_error_is_explained(live_settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "status": "FAILED_PRECONDITION",
                    "message": "User location is not supported for the API use.",
                }
            },
        )

    with pytest.raises(RewriteError) as excinfo:
        run(request_gemini(live_settings, "промпт", client=mock_client(handler)))

    assert "регион" in str(excinfo.value)


def test_gemini_blocked_answer_is_reported(live_settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"promptFeedback": {"blockReason": "SAFETY"}}
        )

    with pytest.raises(RewriteError) as excinfo:
        run(request_gemini(live_settings, "промпт", client=mock_client(handler)))

    assert "SAFETY" in str(excinfo.value)


def test_groq_uses_json_mode_and_low_reasoning(tmp_path) -> None:
    settings = make_settings(
        dry_run=False, groq_api_key="groq-key", groq_model="openai/gpt-oss-120b"
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=groq_payload())

    raw = run(request_groq(settings, "промпт", client=mock_client(handler)))

    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer groq-key"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["reasoning_effort"] == "low"
    assert seen["body"]["model"] == "openai/gpt-oss-120b"
    assert json.loads(raw)["body"] == GOOD_BODY


def test_generate_prefers_gemini(live_settings) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json=gemini_payload())

    provider, _ = run(generate(live_settings, "промпт", client=mock_client(handler)))

    assert provider == PROVIDER_GEMINI
    assert hosts == ["generativelanguage.googleapis.com"]


def test_generate_falls_back_on_rate_limit(live_settings) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, json=groq_payload())

    provider, _ = run(generate(live_settings, "промпт", client=mock_client(handler)))

    assert provider == PROVIDER_GROQ
    assert hosts == ["generativelanguage.googleapis.com", "api.groq.com"]


def test_generate_falls_back_on_network_error(live_settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "generativelanguage.googleapis.com":
            raise httpx.ConnectError("нет сети", request=request)
        return httpx.Response(200, json=groq_payload())

    provider, _ = run(generate(live_settings, "промпт", client=mock_client(handler)))

    assert provider == PROVIDER_GROQ


def test_generate_reports_attempts_of_both(live_settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(RewriteError) as excinfo:
        run(generate(live_settings, "промпт", client=mock_client(handler)))

    assert len(excinfo.value.attempts) == 2
    assert "gemini" in str(excinfo.value)
    assert "groq" in str(excinfo.value)


def test_generate_without_keys_raises() -> None:
    settings = make_settings(dry_run=True, gemini_api_key="", groq_api_key="")

    with pytest.raises(RewriteError) as excinfo:
        run(generate(settings, "промпт"))

    assert "ключи" in str(excinfo.value)


def test_generate_draft_falls_back_on_unreadable_answer(live_settings) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "generativelanguage.googleapis.com":
            # Gemini ответил, но это не заголовок с телом.
            return httpx.Response(200, json=gemini_payload(title="", body=""))
        return httpx.Response(200, json=groq_payload())

    provider, title, body = run(
        generate_draft(live_settings, "промпт", client=mock_client(handler))
    )

    assert provider == PROVIDER_GROQ
    assert (title, body) == (GOOD_TITLE, GOOD_BODY)
    assert hosts == ["generativelanguage.googleapis.com", "api.groq.com"]


def test_generate_draft_without_keys_raises() -> None:
    settings = make_settings(dry_run=True, gemini_api_key="", groq_api_key="")

    with pytest.raises(RewriteError):
        run(generate_draft(settings, "промпт"))


# ----------------------------------------------------------------------
# Рерайт поста
# ----------------------------------------------------------------------
def test_dry_run_rewrite_saves_draft(db, make_post, admin_settings) -> None:
    post = take(db, make_post())

    outcome = run(rewrite_post(db, admin_settings, post, actor_id=1))

    assert outcome.result.provider == PROVIDER_DRY_RUN
    assert outcome.round_no == 1
    assert outcome.post.draft_title == DRY_RUN_TITLE
    assert outcome.post.draft_text == DRY_RUN_BODY
    assert outcome.post.rewrite_rounds == 1
    versions = run(db.get_draft_versions(post.id))
    assert [version.origin for version in versions] == [DraftOrigin.AI]


def test_live_rewrite_normalizes_and_saves(db, make_post, live_settings) -> None:
    post = take(db, make_post())
    long_body = GOOD_BODY + " Ещё одно предложение." * 20
    client = mock_client(
        lambda request: httpx.Response(200, json=gemini_payload(body=long_body))
    )

    outcome = run(rewrite_post(db, live_settings, post, actor_id=1, client=client))

    assert outcome.result.provider == PROVIDER_GEMINI
    assert outcome.result.warnings == ()
    assert len(outcome.post.draft_title) <= TITLE_MAX
    assert len(outcome.post.draft_text) <= BODY_MAX
    assert outcome.post.draft_text.endswith(".")


def test_live_rewrite_uses_groq_when_gemini_answer_is_broken(
    db, make_post, live_settings
) -> None:
    post = take(db, make_post())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(200, text="извините, не могу")
        return httpx.Response(200, json=groq_payload())

    client = mock_client(handler)

    outcome = run(rewrite_post(db, live_settings, post, actor_id=1, client=client))

    assert outcome.result.provider == PROVIDER_GROQ
    assert outcome.post.draft_title == GOOD_TITLE
    assert outcome.post.draft_text == GOOD_BODY


def test_second_round_sends_previous_draft(db, make_post, live_settings) -> None:
    post = take(db, make_post())
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json=gemini_payload())

    client = mock_client(handler)
    run(rewrite_post(db, live_settings, post, actor_id=1, client=client))
    fresh = run(db.get_post(post.id))
    assert fresh is not None
    outcome = run(rewrite_post(db, live_settings, fresh, actor_id=1, client=client))

    assert outcome.round_no == 2
    assert outcome.post.rewrite_rounds == 2
    assert len(prompts) == 2
    assert "Предыдущая версия" not in prompts[0]
    assert "Предыдущая версия" in prompts[1]
    assert GOOD_TITLE in prompts[1]


def test_hint_appears_after_three_rounds(db, make_post, admin_settings) -> None:
    post = take(db, make_post())

    outcomes = [
        run(rewrite_post(db, admin_settings, post, actor_id=1)) for _ in range(3)
    ]

    assert [outcome.round_no for outcome in outcomes] == [1, 2, 3]
    assert outcomes[0].hint is None
    assert outcomes[1].hint is None
    assert outcomes[2].hint == HINT_TEXT


def test_rewrite_rejects_foreign_admin(db, make_post, admin_settings) -> None:
    post = take(db, make_post(), owner_id=1)

    with pytest.raises(RewriteError):
        run(rewrite_post(db, admin_settings, post, actor_id=2))

    assert run(db.get_post(post.id)).draft_text == ""


def test_rewrite_rejects_pending_post(db, make_post, admin_settings) -> None:
    post = make_post()  # не перехвачен

    with pytest.raises(RewriteError):
        run(rewrite_post(db, admin_settings, post, actor_id=1))


# ----------------------------------------------------------------------
# Кнопка «Доработать с ИИ»
# ----------------------------------------------------------------------
def test_ai_button_saves_draft_and_updates_card(
    db, make_post, admin_settings
) -> None:
    post = take(db, make_post())
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_AI, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, admin_settings, FakeState()))

    stored = run(db.get_post(post.id))
    assert stored.draft_text == DRY_RUN_BODY
    edits = bot.of(EditMessageText)
    assert edits and DRY_RUN_TITLE in (edits[-1].text or "")
    assert any("Переписываю" in (answer.text or "") for answer in bot.of(AnswerCallbackQuery))


def test_ai_button_reports_provider_failure(db, make_post) -> None:
    settings = make_settings(
        admin_ids=(1,), dry_run=False, gemini_api_key="", groq_api_key=""
    )
    post = take(db, make_post())
    bot = FakeBot()
    callback = make_callback(
        bot, post_callback(ACTION_AI, post.id), make_message(bot)
    )

    run(handle_post_action(callback, bot, db, settings, FakeState()))

    assert any("Не удалось переписать" in text for text in bot.texts)
    assert run(db.get_post(post.id)).draft_text == ""
