"""Получить chat_id и user_id через getUpdates.

Зачем: узнать CHANNEL_ID (группа/канал) и ADMIN_IDS (ваш Telegram ID),
не прибегая к сторонним ботам.

Порядок действий:
1. Бот уже создан, токен вписан в .env (BOT_TOKEN=...).
2. Бот добавлен в группу/канал.
3. Отправьте в группу команду /start@имя_вашего_бота,
   а боту в личку — /start (если нужен свой user_id).
4. Запустите: .venv\\Scripts\\python.exe tools\\get_chat_id.py

Скрипт слушает getUpdates заданное время (по умолчанию 45 секунд) и печатает
все найденные чаты и пользователей. Выход: 0 — что-то найдено, 1 — нет.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


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
    parser = argparse.ArgumentParser(description="Найти chat_id и user_id через getUpdates")
    parser.add_argument("--wait", type=int, default=45, help="сколько секунд слушать (по умолчанию 45)")
    args = parser.parse_args()

    token = read_token()
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    deadline = time.monotonic() + max(args.wait, 5)
    chats: dict[int, str] = {}
    users: dict[int, str] = {}

    print(f"Слушаю обновления {args.wait} с. Напишите /start@имя_бота в группе и /start боту в личку...")
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
                msg = (
                    upd.get("message")
                    or upd.get("channel_post")
                    or upd.get("edited_message")
                    or upd.get("my_chat_member")
                )
                if not msg:
                    continue

                chat = msg.get("chat")
                if chat:
                    chat_id = chat["id"]
                    kind = chat.get("type", "?")
                    title = chat.get("title") or chat.get("username") or chat.get("first_name") or "-"
                    chats[chat_id] = f"{kind} | {title}"
                    print(f"  чат: chat_id={chat_id}  тип={kind}  название={title}")

                sender = msg.get("from")
                if sender and not sender.get("is_bot"):
                    user_id = sender["id"]
                    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
                    users[user_id] = f"@{sender['username']}" if sender.get("username") else name or "-"
                    print(f"  пользователь: user_id={user_id}  {users[user_id]}")

    if not chats and not users:
        print("Ничего не найдено. Бот добавлен в чат? Команда отправлена?")
        return 1

    print("\n--- Для .env ---")
    if chats:
        for chat_id, desc in chats.items():
            print(f"CHANNEL_ID={chat_id}    # {desc}")
    if users:
        ids = ",".join(str(uid) for uid in users)
        print(f"ADMIN_IDS={ids}    # собственник(и) нажатий/сообщений")
        for uid, desc in users.items():
            print(f"    {uid} = {desc}")

    print("\nЕсли у группы включены темы (topics), для публикации понадобится message_thread_id — скажите, если нужно.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
