import asyncio
import os

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

# Тимчасово зберігаємо дані користувачів
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
        "latitude": message.location.latitude,
        "longitude": message.location.longitude,
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

    # Введення початкової адреси вручну
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

    # Якщо користувач ще не запускав бота
    if user_id not in users:
        await message.answer(
            "Щоб почати пошук маршруту, натисни /start."
        )
        return

    step = users[user_id].get("step")

    # Отримали адресу старту
    if step == "waiting_origin_address":
        users[user_id]["origin_type"] = "address"
        users[user_id]["origin"] = text
        users[user_id]["step"] = "waiting_destination"

        await message.answer(
            f"✅ Звідки: {text}\n\n"
            "🏁 Тепер напиши адресу, куди тобі потрібно їхати."
        )
        return

    # Отримали пункт призначення
    if step == "waiting_destination":
        users[user_id]["destination"] = text
        users[user_id]["step"] = "done"

        if users[user_id]["origin_type"] == "location":
            origin = "Твоя поточна геолокація 📍"
        else:
            origin = users[user_id]["origin"]

        await message.answer(
            "✅ Маршрут отримано!\n\n"
            f"📍 Звідки: {origin}\n"
            f"🏁 Куди: {text}\n\n"
            "🔎 Тепер можемо шукати громадський транспорт "
            "між цими двома точками."
        )
        return

    if step == "done":
        await message.answer(
            "🔄 Щоб знайти інший маршрут, натисни /start."
        )


async def main():
    print("MENI-TUDY BOT STARTED", flush=True)

    await dp.start_polling(
        bot,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    asyncio.run(main())
