"""Получить chat_id группы или канала через getUpdates.

Зачем: узнать CHANNEL_ID для .env. Проще всего попросить бота самому сообщить,
какие чаты он видит.

Порядок действий:
1. Бот уже создан, токен вписан в .env (BOT_TOKEN=...).
2. Бот добавлен в группу/канал.
3. Отправьте в чат команду /start@имя_вашего_бота
   (или любое сообщение, если у бота отключён privacy mode в BotFather).
4. Запустите: .venv\\Scripts\\python.exe tools\\get_chat_id.py

Скрипт опрашивает getUpdates и печатает ID найденных чатов.
Выход: 0 — ID найден, 1 — не найден за отведённое время.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 3
MAX_WAIT_SECONDS = 60


def read_token() -> str:
    env_path = ROOT / ".env"
    if not env_path.exists():
        print(f"Нет файла {env_path}. Сначала создайте .env (copy .env.example .env).")
        raise SystemExit(1)
    token = (dotenv_values(env_path).get("BOT_TOKEN") or "").strip()
    if not token or token.startswith("PASTE_"):
        print("BOT_TOKEN в .env не заполнен. Впишите токен от @BotFather.")
        raise SystemExit(1)
    return token


def main() -> int:
    token = read_token()
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    seen: dict[int, str] = {}

    print("Жду обновлений. Напишите /start@имя_бота в нужной группе...")
    with httpx.Client(timeout=15.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{api}/getUpdates", params={"offset": offset, "timeout": 10})
                data = resp.json()
            except httpx.HTTPError as exc:
                print(f"Сеть недоступна: {exc}")
                return 1

            if not data.get("ok"):
                print(f"Telegram ответил ошибкой: {data.get('description')}")
                return 1

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post") or upd.get("edited_message")
                if not msg:
                    continue
                chat = msg["chat"]
                chat_id = chat["id"]
                kind = chat.get("type", "?")
                title = chat.get("title") or chat.get("username") or chat.get("first_name") or "-"
                seen[chat_id] = f"{kind} | {title}"
                print(f"  найдено: chat_id={chat_id}  тип={kind}  название={title}")

            if seen:
                break

    if not seen:
        print("Ничего не найдено. Проверьте, что бот добавлен в чат и команда отправлена.")
        return 1

    print("\nДля .env:")
    for chat_id, desc in seen.items():
        print(f"CHANNEL_ID={chat_id}    # {desc}")
    print("\nЕсли это группа и в ней включены темы (topics), для публикации может понадобиться message_thread_id — уточните, если нужно.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
