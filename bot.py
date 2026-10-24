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

dp = Dispatcher()

location_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="📍 Надіслати мою геолокацію",
                request_location=True,
            )
        ],
        [
            KeyboardButton(text="✏️ Ввести адресу вручну")
        ],
    ],
    resize_keyboard=True,
)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Привіт! Я — «Мені туди».\n\n"
        "Допоможу знайти зручний громадський транспорт "
        "в Івано-Франківську. 🚌\n\n"
        "📍 Звідки ти їдеш?\n\n"
        "Надішли свою геолокацію або введи адресу вручну.",
        reply_markup=location_keyboard,
    )


@dp.message(F.text == "✏️ Ввести адресу вручну")
async def enter_address(message: Message):
    await message.answer(
        "📍 Напиши адресу, звідки ти їдеш.\n\n"
        "Наприклад: Київська 1",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(F.location)
async def receive_location(message: Message):
    latitude = message.location.latitude
    longitude = message.location.longitude

    await message.answer(
        "✅ Геолокацію отримано!\n\n"
        "📍 Твоє місцезнаходження збережено.\n\n"
        "🏁 А тепер напиши, куди тобі потрібно їхати.",
        reply_markup=ReplyKeyboardRemove(),
    )

    print(f"Location: {latitude}, {longitude}")


async def main():
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
