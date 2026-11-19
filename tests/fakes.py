"""Двойники Telegram для тестов хендлеров.

Объекты ``Message`` и ``CallbackQuery`` — настоящие, aiogram-овские: так
проверяется и поведение самих типов (``edit_text``, ``answer``), а не
только наш код. ``FakeBot`` собирает отправленные методы, поэтому тест
видит, что именно ушло бы в Telegram.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from itertools import count
from typing import Any

from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, User

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24

_ids = count(1)


class FakeBot:
    """Минимальный бот: записывает вызовы Telegram и отдаёт байты «файла»."""

    def __init__(self, payload: bytes = PNG) -> None:
        self.methods: list[Any] = []
        self.payload = payload

    async def __call__(self, method: Any, **kwargs: Any) -> bool:
        # aiogram 3.32 отправляет методы через ``bot(method)``.
        self.methods.append(method)
        return True

    async def download(self, file: Any):
        """``bot.download(photo)`` → буфер с картинкой."""
        return io.BytesIO(self.payload)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: Any = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> bool:
        """Обёртка над ``Bot.send_message`` (сервис вызывает её напрямую)."""
        self.methods.append(
            SendMessage(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        )
        return True

    async def send_photo(
        self,
        chat_id: int,
        photo: Any,
        caption: str | None = None,
        parse_mode: Any = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> bool:
        """Обёртка над ``Bot.send_photo``."""
        self.methods.append(
            SendPhoto(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        )
        return True

    def of(self, cls: type) -> list[Any]:
        return [method for method in self.methods if isinstance(method, cls)]

    @property
    def texts(self) -> list[str]:
        return [method.text for method in self.of(SendMessage)]


class FakeState:
    """Подмена ``FSMContext``: хранит состояние и данные в памяти."""

    def __init__(self, state: str | None = None, data: dict[str, Any] | None = None) -> None:
        self.state = state
        self.data = dict(data or {})
        self.cleared = False

    async def set_state(self, state: Any) -> None:
        self.state = getattr(state, "state", state)

    async def get_state(self) -> str | None:
        return self.state

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data.clear()
        self.cleared = True


def make_message(
    bot: FakeBot,
    *,
    chat_id: int = 1,
    text: str | None = None,
    photo: bool = False,
    file_size: int | None = None,
    user_id: int = 1,
) -> Message:
    """Настоящий ``Message`` с привязанным ``FakeBot``."""
    message = Message.model_construct(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat.model_construct(id=chat_id, type="private"),
        from_user=User.model_construct(id=user_id, is_bot=False, first_name="Админ"),
        text=text,
        photo=(
            [
                PhotoSize.model_construct(
                    file_id="photo-1",
                    file_unique_id="unique-1",
                    width=8,
                    height=8,
                    file_size=len(PNG) if file_size is None else file_size,
                )
            ]
            if photo
            else None
        ),
    )
    return message.as_(bot)


def make_callback(
    bot: FakeBot, data: str, message: Message, *, user_id: int = 1
) -> CallbackQuery:
    """Настоящий ``CallbackQuery`` с привязанным ``FakeBot``."""
    return CallbackQuery.model_construct(
        id=f"cb-{next(_ids)}",
        from_user=User.model_construct(id=user_id, is_bot=False, first_name="Админ"),
        chat_instance="chat-instance",
        data=data,
        message=message,
    ).as_(bot)


def callback_datas(markup) -> list[str]:
    """Плоский список ``callback_data`` клавиатуры."""
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


__all__ = [
    "PNG",
    "FakeBot",
    "FakeState",
    "callback_datas",
    "make_callback",
    "make_message",
]
