"""Тесты сборки приложения (задача T11).

Здесь проверяется только то, что нельзя покрыть тестами сервисов:
диспетчер действительно получает оба роутера, а служебные команды
зарегистрированы. Сеть и Telegram не используются.

Важно: ``build_dispatcher`` можно вызвать только один раз за процесс —
роутеры модульные, и aiogram запрещает подключать их к двум диспетчерам.
Поэтому все проверки собраны в одном тесте.
"""

from __future__ import annotations

from bot.main import build_dispatcher
from tests.helpers import make_settings


def test_dispatcher_is_assembled_correctly(db) -> None:
    settings = make_settings(admin_ids=(1, 2))

    dispatcher = build_dispatcher(settings, db)

    assert dispatcher["settings"] is settings
    assert dispatcher["db"] is db
    assert {router.name for router in dispatcher.sub_routers} == {"queue", "cards"}
    assert {"message", "callback_query"} <= set(
        dispatcher.resolve_used_update_types()
    )

    callbacks = {
        handler.callback.__name__ for handler in dispatcher.message.handlers
    }
    assert {"handle_start", "handle_ping"} <= callbacks
