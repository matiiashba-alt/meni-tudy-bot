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
    await message.answer(
        "✅ Геолокацію отримано!\n\n"
        "📍 Твоє місцезнаходження збережено.\n\n"
        "🏁 А тепер напиши, куди тобі потрібно їхати.\n\n"
        "Наприклад: Південний бульвар 35",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(F.text)
async def text_handler(message: Message):
    text = message.text.strip()

    if text == "✏️ Ввести адресу вручну":
        await message.answer(
            "📍 Напиши адресу, звідки ти їдеш.\n\n"
            "Наприклад: Київська 1",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await message.answer(
        "✅ Адресу отримано!\n\n"
        f"🏁 Куди: {text}\n\n"
        "🔎 Наступним кроком підключимо пошук "
        "реального маршруту громадським транспортом."
    )


async def main():
    print("MENI-TUDY BOT STARTED", flush=True)

    await dp.start_polling(
        bot,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    asyncio.run(main())
