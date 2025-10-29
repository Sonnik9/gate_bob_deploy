import asyncio
import json
import os
import aiohttp

from a_config import TG_BOT_TOKEN

FILE = "ids.json"
BASE_URL = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


def load_ids():
    if os.path.exists(FILE):
        with open(FILE, "r", encoding="utf-8") as f:
            return set(tuple(x) for x in json.load(f))
    return set()


def save_ids(ids):
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump([list(x) for x in ids], f, ensure_ascii=False, indent=2)


async def get_updates(session):
    url = f"{BASE_URL}/getUpdates"
    async with session.get(url) as resp:
        data = await resp.json()
        if not data.get("ok"):
            print(f"Ошибка Telegram API: {data}")
            return
        return data.get("result", [])


async def main():
    ids = load_ids()

    async with aiohttp.ClientSession() as session:
        updates = await get_updates(session)

        if not updates:
            print("Нет новых апдейтов. Напиши что-нибудь боту и запусти снова.")
        else:
            for upd in updates:
                msg = upd.get("message")
                if msg:
                    user = msg.get("from", {})
                    chat = msg.get("chat", {})
                    username = f"@{user.get('username')}" if user.get("username") else user.get("first_name")
                    ids.add((username, user.get("id"), chat.get("id")))

            save_ids(ids)

            print("Все найденные чаты:")
            for username, user_id, chat_id in ids:
                print(f"user: {username} | user_id: {user_id} | chat_id: {chat_id}")


if __name__ == "__main__":
    asyncio.run(main())