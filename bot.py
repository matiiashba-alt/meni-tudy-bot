import asyncio

import csv

import io

import math

import os

import zipfile

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

# Офіційний GTFS Івано-Франківська

GTFS_URL = "https://smartcity.mvk.if.ua/transport/gtfs/static.zip"

# Зупинки з GTFS зберігатимемо тут

gtfs_stops = []

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

# --------------------------------------------------

# ВІДСТАНЬ МІЖ ДВОМА КООРДИНАТАМИ

# --------------------------------------------------

def distance_meters(lat1, lon1, lat2, lon2):

    radius = 6371000

    phi1 = math.radians(lat1)

    phi2 = math.radians(lat2)

    delta_phi = math.radians(lat2 - lat1)

    delta_lambda = math.radians(lon2 - lon1)

    a = (

        math.sin(delta_phi / 2) ** 2

        + math.cos(phi1)

        * math.cos(phi2)

        * math.sin(delta_lambda / 2) ** 2

    )

    c = 2 * math.atan2(

        math.sqrt(a),

        math.sqrt(1 - a),

    )

    return radius * c

# --------------------------------------------------

# ГЕОКОДУВАННЯ АДРЕСИ

# --------------------------------------------------

async def geocode_address(address):

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

    timeout = aiohttp.ClientTimeout(total=15)

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

# --------------------------------------------------

# ЗАВАНТАЖЕННЯ GTFS

# --------------------------------------------------

async def load_gtfs():

    global gtfs_stops

    print(

        "Downloading Ivano-Frankivsk GTFS...",

        flush=True,

    )

    timeout = aiohttp.ClientTimeout(total=30)

    headers = {

        "User-Agent": "MeniTudyTelegramBot/1.0"

    }

    try:

        async with aiohttp.ClientSession(

            timeout=timeout,

            headers=headers,

        ) as session:

            async with session.get(

                GTFS_URL

            ) as response:

                if response.status != 200:

                    print(

                        f"GTFS HTTP error: {response.status}",

                        flush=True,

                    )

                    return

                content = await response.read()

        archive = zipfile.ZipFile(

            io.BytesIO(content)

        )

        if "stops.txt" not in archive.namelist():

            print(

                "GTFS error: stops.txt not found",

                flush=True,

            )

            return

        with archive.open("stops.txt") as file:

            text_file = io.TextIOWrapper(

                file,

                encoding="utf-8-sig",

            )

            reader = csv.DictReader(text_file)

            stops = []

            for row in reader:

                try:

                    lat = float(row["stop_lat"])

                    lon = float(row["stop_lon"])

                    stops.append(

                        {

                            "id": row.get(

                                "stop_id",

                                "",

                            ),

                            "name": row.get(

                                "stop_name",

                                "Без назви",

                            ),

                            "latitude": lat,

                            "longitude": lon,

                        }

                    )

                except (

                    ValueError,

                    TypeError,

                    KeyError,

                ):

                    continue

            gtfs_stops = stops

        print(

            f"GTFS loaded: {len(gtfs_stops)} stops",

            flush=True,

        )

    except Exception as error:

        print(

            f"GTFS load error: {error}",

            flush=True,

        )

# --------------------------------------------------

# НАЙБЛИЖЧА ЗУПИНКА

# --------------------------------------------------

def find_nearest_stop(latitude, longitude):

    if not gtfs_stops:

        return None

    nearest = None

    nearest_distance = None

    for stop in gtfs_stops:

        distance = distance_meters(

            latitude,

            longitude,

            stop["latitude"],

            stop["longitude"],

        )

        if (

            nearest_distance is None

            or distance < nearest_distance

        ):

            nearest = stop

            nearest_distance = distance

    if nearest is None:

        return None

    result = nearest.copy()

    result["distance"] = nearest_distance

    return result

# --------------------------------------------------

# /START

# --------------------------------------------------

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

        "Надішли свою геолокацію "

        "або введи адресу вручну.",

        reply_markup=location_keyboard,

    )

# --------------------------------------------------

# ГЕОЛОКАЦІЯ СТАРТУ

# --------------------------------------------------

@dp.message(F.location)

async def location_handler(message: Message):

    user_id = message.from_user.id

    latitude = message.location.latitude

    longitude = message.location.longitude

    nearest_stop = find_nearest_stop(

        latitude,

        longitude,

    )

    users[user_id] = {

        "step": "waiting_destination",

        "origin_type": "location",

        "origin_latitude": latitude,

        "origin_longitude": longitude,

        "origin_stop": nearest_stop,

    }

    if nearest_stop:

        stop_text = (

            "\n\n🚏 Найближча зупинка:\n"

            f"{nearest_stop['name']} — "

            f"приблизно "

            f"{round(nearest_stop['distance'])} м"

        )

    else:

        stop_text = (

            "\n\n⚠️ Дані зупинок поки "

            "не завантажилися."

        )

    await message.answer(

        "✅ Геолокацію отримано!"

        + stop_text

        + "\n\n🏁 А тепер напиши, "

        "куди тобі потрібно їхати.",

        reply_markup=ReplyKeyboardRemove(),

    )

# --------------------------------------------------

# ТЕКСТОВІ ПОВІДОМЛЕННЯ

# --------------------------------------------------

@dp.message(F.text)

async def text_handler(message: Message):

    user_id = message.from_user.id

    text = message.text.strip()

    # Ручне введення стартової адреси

    if text == "✏️ Ввести адресу вручну":

        users[user_id] = {

            "step": "waiting_origin_address"

        }

        await message.answer(

            "📍 Напиши адресу, "

            "звідки ти їдеш.\n\n"

            "Наприклад: Київська 1",

            reply_markup=ReplyKeyboardRemove(),

        )

        return

    if user_id not in users:

        await message.answer(

            "Щоб почати пошук маршруту, "

            "натисни /start."

        )

        return

    step = users[user_id].get("step")

    # ----------------------------------------------

    # АДРЕСА СТАРТУ

    # ----------------------------------------------

    if step == "waiting_origin_address":

        await message.answer(

            "🔎 Шукаю адресу..."

        )

        result = await geocode_address(text)

        if result is None:

            await message.answer(

                "❌ Не вдалося знайти цю адресу.\n\n"

                "Спробуй написати її точніше."

            )

            return

        nearest_stop = find_nearest_stop(

            result["latitude"],

            result["longitude"],

        )

        users[user_id] = {

            "step": "waiting_destination",

            "origin_type": "address",

            "origin": text,

            "origin_latitude": result["latitude"],

            "origin_longitude": result["longitude"],

            "origin_stop": nearest_stop,

        }

        if nearest_stop:

            stop_text = (

                "\n\n🚏 Найближча зупинка:\n"

                f"{nearest_stop['name']} — "

                f"приблизно "

                f"{round(nearest_stop['distance'])} м"

            )

        else:

            stop_text = ""

        await message.answer(

            f"✅ Звідки: {text}"

            + stop_text

            + "\n\n🏁 Тепер напиши адресу, "

            "куди тобі потрібно їхати."

        )

        return

    # ----------------------------------------------

    # АДРЕСА ПРИЗНАЧЕННЯ

    # ----------------------------------------------

    if step == "waiting_destination":

        await message.answer(

            "🔎 Шукаю адресу призначення..."

        )

        result = await geocode_address(text)

        if result is None:

            await message.answer(

                "❌ Не вдалося знайти "

                "адресу призначення.\n\n"

                "Перевір назву вулиці "

                "та номер будинку."

            )

            return

        destination_stop = find_nearest_stop(

            result["latitude"],

            result["longitude"],

        )

        users[user_id]["destination"] = text

        users[user_id]["destination_latitude"] = (

            result["latitude"]

        )

        users[user_id]["destination_longitude"] = (

            result["longitude"]

        )

        users[user_id]["destination_stop"] = (

            destination_stop

        )

        users[user_id]["step"] = "done"

        origin_stop = users[user_id].get(

            "origin_stop"

        )

        if users[user_id]["origin_type"] == "location":

            origin_text = "Твоя геолокація 📍"

        else:

            origin_text = users[user_id]["origin"]

        if origin_stop:

            origin_stop_text = (

                f"🚏 Зупинка старту: "

                f"{origin_stop['name']}\n"

                f"🚶 Приблизно "

                f"{round(origin_stop['distance'])} м"

            )

        else:

            origin_stop_text = (

                "🚏 Зупинку старту "

                "не вдалося визначити."

            )

        if destination_stop:

            destination_stop_text = (

                f"🚏 Зупинка біля пункту призначення: "

                f"{destination_stop['name']}\n"

                f"🚶 Приблизно "

                f"{round(destination_stop['distance'])} м"

            )

        else:

            destination_stop_text = (

                "🚏 Зупинку біля пункту призначення "

                "не вдалося визначити."

            )

        await message.answer(

            "✅ Обидві точки знайдено!\n\n"

            f"📍 Звідки: {origin_text}\n"

            f"🏁 Куди: {text}\n\n"

            f"{origin_stop_text}\n\n"

            f"{destination_stop_text}\n\n"

            "🚌 Наступний крок — визначити, "

            "які маршрути проходять через "

            "ці зупинки."

        )

        return

    if step == "done":

        await message.answer(

            "🔄 Щоб знайти інший маршрут, "

            "натисни /start."

        )

# --------------------------------------------------

# ЗАПУСК

# --------------------------------------------------

async def main():

    print(

        "MENI-TUDY STARTING...",

        flush=True,

    )

    await load_gtfs()

    print(

        "MENI-TUDY BOT ONLINE",

        flush=True,

    )

    await dp.start_polling(

        bot,

        allowed_updates=["message"],

    )

if __name__ == "__main__":

    asyncio.run(main())
