import asyncio
import os

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()

users = {}


location_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="📍 Надіслати мою геолокацію",
                request_location=True,
            )
        ],
        [
            KeyboardButton(
                text="✏️ Ввести адресу вручну"
            )
        ],
    ],
    resize_keyboard=True,
)


async def geocode_address(address: str):
    """
    Перетворює адресу в Івано-Франківську на координати.
    """

    url = "https://nominatim.openstreetmap.org/search"

    params = {
        "q": f"{address}, Івано-Франківськ, Україна",
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "ua",
    }

    headers = {
        "User-Agent": "MeniTudyTelegramBot/1.0"
    }

    timeout = aiohttp.ClientTimeout(total=10)

    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        ) as session:
            async with session.get(
                url,
                params=params,
            ) as response:

                if response.status != 200:
                    return None

                data = await response.json()

                if not data:
                    return None

                result = data[0]

                return {
                    "latitude": float(result["lat"]),
                    "longitude": float(result["lon"]),
                    "display_name": result.get(
                        "display_name",
                        address,
                    ),
                }

    except Exception as error:
        print(
            f"Geocoding error: {error}",
            flush=True,
        )
        return None


@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id

    users[user_id] = {
        "step": "waiting_origin"
    }

    await message.answer(
        "👋 Привіт! Я — «Мені туди».\n\n"
        "Допоможу знайти зручний громадський транспорт "
        "в Івано-Франківську. 🚌\n\n"
        "📍 Звідки ти їдеш?\n\n"
        "Надішли свою геолокацію або введи адресу вручну.",
        reply_markup=location_keyboard,
    )


@dp.message(F.location)
async def location_handler(message: Message):
    user_id = message.from_user.id

    users[user_id] = {
        "step": "waiting_destination",
        "origin_type": "location",
        "origin_latitude": message.location.latitude,
        "origin_longitude": message.location.longitude,
    }

    await message.answer(
        "✅ Геолокацію отримано!\n\n"
        "📍 Твоє місцезнаходження збережено.\n\n"
        "🏁 А тепер напиши, куди тобі потрібно їхати.",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(F.text)
async def text_handler(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text == "✏️ Ввести адресу вручну":
        users[user_id] = {
            "step": "waiting_origin_address"
        }

        await message.answer(
            "📍 Напиши адресу, звідки ти їдеш.\n\n"
            "Наприклад: Київська 1",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if user_id not in users:
        await message.answer(
            "Щоб почати пошук маршруту, натисни /start."
        )
        return

    step = users[user_id].get("step")

    # Адреса початкової точки
    if step == "waiting_origin_address":

        await message.answer(
            "🔎 Шукаю цю адресу..."
        )

        result = await geocode_address(text)

        if result is None:
            await message.answer(
                "❌ Не вдалося знайти цю адресу.\n\n"
                "Спробуй написати її точніше.\n"
                "Наприклад: Київська 1"
            )
            return

        users[user_id] = {
            "step": "waiting_destination",
            "origin_type": "address",
            "origin": text,
            "origin_latitude": result["latitude"],
            "origin_longitude": result["longitude"],
        }

        await message.answer(
            f"✅ Звідки: {text}\n\n"
            "📍 Адресу знайдено.\n\n"
            "🏁 Тепер напиши адресу, куди тобі потрібно їхати."
        )
        return

    # Адреса призначення
    if step == "waiting_destination":

        await message.answer(
            "🔎 Шукаю адресу призначення..."
        )

        result = await geocode_address(text)

        if result is None:
            await message.answer(
                "❌ Не вдалося знайти адресу призначення.\n\n"
                "Перевір назву вулиці та номер будинку "
                "і спробуй ще раз."
            )
            return

        users[user_id]["destination"] = text
        users[user_id]["destination_latitude"] = result["latitude"]
        users[user_id]["destination_longitude"] = result["longitude"]
        users[user_id]["step"] = "done"

        if users[user_id]["origin_type"] == "location":
            origin = "Твоя поточна геолокація 📍"
        else:
            origin = users[user_id]["origin"]

        await message.answer(
            "✅ Обидві точки знайдено!\n\n"
            f"📍 Звідки: {origin}\n"
            f"🏁 Куди: {text}\n\n"
            "🗺 Координати точки призначення:\n"
            f"{result['latitude']:.6f}, "
            f"{result['longitude']:.6f}\n\n"
            "Наступний крок — знайти найближчі "
            "зупинки громадського транспорту. 🚌"
        )
        return

    if step == "done":
        await message.answer(
            "🔄 Щоб знайти інший маршрут, натисни /start."
        )


async def main():
    print(
        "MENI-TUDY BOT WITH GEOCODING STARTED",
        flush=True,
    )

    await dp.start_polling(
        bot,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    asyncio.run(main())
