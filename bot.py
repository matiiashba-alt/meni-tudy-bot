import asyncio
import csv
import io
import math
import os
import zipfile
from collections import defaultdict

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

GTFS_URL = "https://city.dozor.tech/iv-frankivsk/gtfs/static.zip"

gtfs_stops = []
gtfs_routes = {}
gtfs_trips = {}
trip_stops = defaultdict(list)


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
# ВІДСТАНЬ
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
# ГЕОКОДУВАННЯ
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
                }

    except Exception as error:
        print(
            f"Geocoding error: {error}",
            flush=True,
        )
        return None


# --------------------------------------------------
# ЧИТАННЯ CSV ІЗ GTFS
# --------------------------------------------------

def read_gtfs_csv(archive, filename):
    with archive.open(filename) as file:
        text_file = io.TextIOWrapper(
            file,
            encoding="utf-8-sig",
        )

        return list(
            csv.DictReader(text_file)
        )


# --------------------------------------------------
# ЗАВАНТАЖЕННЯ GTFS
# --------------------------------------------------

async def load_gtfs():
    global gtfs_stops
    global gtfs_routes
    global gtfs_trips
    global trip_stops

    print(
        "Downloading Ivano-Frankivsk GTFS...",
        flush=True,
    )

    headers = {
        "User-Agent": "MeniTudyTelegramBot/1.0"
    }

    timeout = aiohttp.ClientTimeout(total=60)

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

        required_files = [
            "stops.txt",
            "routes.txt",
            "trips.txt",
            "stop_times.txt",
        ]

        for filename in required_files:
            if filename not in archive.namelist():
                print(
                    f"GTFS missing: {filename}",
                    flush=True,
                )
                return

        # --------------------------
        # ЗУПИНКИ
        # --------------------------

        stops_rows = read_gtfs_csv(
            archive,
            "stops.txt",
        )

        stops = []

        for row in stops_rows:
            try:
                stops.append(
                    {
                        "id": row["stop_id"],
                        "name": row.get(
                            "stop_name",
                            "Без назви",
                        ),
                        "latitude": float(
                            row["stop_lat"]
                        ),
                        "longitude": float(
                            row["stop_lon"]
                        ),
                    }
                )
            except (
                ValueError,
                TypeError,
                KeyError,
            ):
                continue

        gtfs_stops = stops

        # --------------------------
        # МАРШРУТИ
        # --------------------------

        routes_rows = read_gtfs_csv(
            archive,
            "routes.txt",
        )

        routes = {}

        for row in routes_rows:
            route_id = row.get("route_id")

            if not route_id:
                continue

            routes[route_id] = {
                "short_name": row.get(
                    "route_short_name",
                    "",
                ),
                "long_name": row.get(
                    "route_long_name",
                    "",
                ),
                "type": row.get(
                    "route_type",
                    "",
                ),
            }

        gtfs_routes = routes

        # --------------------------
        # РЕЙСИ
        # --------------------------

        trips_rows = read_gtfs_csv(
            archive,
            "trips.txt",
        )

        trips = {}

        for row in trips_rows:
            trip_id = row.get("trip_id")
            route_id = row.get("route_id")

            if not trip_id or not route_id:
                continue

            trips[trip_id] = {
                "route_id": route_id,
                "headsign": row.get(
                    "trip_headsign",
                    "",
                ),
                "direction_id": row.get(
                    "direction_id",
                    "",
                ),
            }

        gtfs_trips = trips

        # --------------------------
        # ПОСЛІДОВНІСТЬ ЗУПИНОК
        # --------------------------

        stop_times_rows = read_gtfs_csv(
            archive,
            "stop_times.txt",
        )

        stops_by_trip = defaultdict(list)

        for row in stop_times_rows:
            trip_id = row.get("trip_id")
            stop_id = row.get("stop_id")

            if not trip_id or not stop_id:
                continue

            try:
                sequence = int(
                    row.get(
                        "stop_sequence",
                        0,
                    )
                )
            except ValueError:
                continue

            stops_by_trip[trip_id].append(
                (
                    sequence,
                    stop_id,
                )
            )

        for trip_id in stops_by_trip:
            stops_by_trip[trip_id].sort(
                key=lambda item: item[0]
            )

        trip_stops = stops_by_trip

        print(
            f"GTFS loaded: "
            f"{len(gtfs_stops)} stops, "
            f"{len(gtfs_routes)} routes, "
            f"{len(gtfs_trips)} trips",
            flush=True,
        )

    except Exception as error:
        print(
            f"GTFS load error: {error}",
            flush=True,
        )


# --------------------------------------------------
# НАЙБЛИЖЧІ ЗУПИНКИ
# --------------------------------------------------

def find_nearest_stops(
    latitude,
    longitude,
    limit=5,
    max_distance=700,
):
    results = []

    for stop in gtfs_stops:

        distance = distance_meters(
            latitude,
            longitude,
            stop["latitude"],
            stop["longitude"],
        )

        if distance <= max_distance:
            item = stop.copy()
            item["distance"] = distance
            results.append(item)

    results.sort(
        key=lambda item: item["distance"]
    )

    return results[:limit]


# --------------------------------------------------
# ПОШУК ПРЯМОГО МАРШРУТУ
# --------------------------------------------------

def find_direct_routes(
    origin_stops,
    destination_stops,
):
    if not origin_stops or not destination_stops:
        return []

    origin_ids = {
        stop["id"]: stop
        for stop in origin_stops
    }

    destination_ids = {
        stop["id"]: stop
        for stop in destination_stops
    }

    found = {}

    for trip_id, sequence in trip_stops.items():

        trip = gtfs_trips.get(trip_id)

        if not trip:
            continue

        origin_match = None
        destination_match = None

        for index, (_, stop_id) in enumerate(sequence):

            if (
                origin_match is None
                and stop_id in origin_ids
            ):
                origin_match = (
                    index,
                    stop_id,
                )

            if (
                origin_match is not None
                and stop_id in destination_ids
                and index > origin_match[0]
            ):
                destination_match = (
                    index,
                    stop_id,
                )
                break

        if (
            origin_match is None
            or destination_match is None
        ):
            continue

        route_id = trip["route_id"]
        route = gtfs_routes.get(route_id)

        if not route:
            continue

        origin_stop = origin_ids[
            origin_match[1]
        ]

        destination_stop = destination_ids[
            destination_match[1]
        ]

        stop_count = (
            destination_match[0]
            - origin_match[0]
        )

        key = (
            route_id,
            origin_stop["id"],
            destination_stop["id"],
        )

        candidate = {
            "route_id": route_id,
            "route_number": (
                route["short_name"]
                or route["long_name"]
                or route_id
            ),
            "route_name": route["long_name"],
            "headsign": trip["headsign"],
            "origin_stop": origin_stop,
            "destination_stop": destination_stop,
            "stop_count": stop_count,
        }

        if key not in found:
            found[key] = candidate

    results = list(found.values())

    results.sort(
        key=lambda item: (
            item["origin_stop"]["distance"]
            + item["destination_stop"]["distance"],
            item["stop_count"],
        )
    )

    return results


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
# ГЕОЛОКАЦІЯ
# --------------------------------------------------

@dp.message(F.location)
async def location_handler(message: Message):
    user_id = message.from_user.id

    latitude = message.location.latitude
    longitude = message.location.longitude

    origin_stops = find_nearest_stops(
        latitude,
        longitude,
    )

    users[user_id] = {
        "step": "waiting_destination",
        "origin_type": "location",
        "origin_latitude": latitude,
        "origin_longitude": longitude,
        "origin_stops": origin_stops,
    }

    if origin_stops:
        nearest = origin_stops[0]

        stop_text = (
            "\n\n🚏 Найближча зупинка:\n"
            f"«{nearest['name']}»\n"
            f"🚶 {round(nearest['distance'])} м"
        )
    else:
        stop_text = (
            "\n\n⚠️ Не знайшов зупинок поруч."
        )

    await message.answer(
        "✅ Геолокацію отримано!"
        + stop_text
        + "\n\n🏁 А тепер напиши, "
        "куди тобі потрібно їхати.",
        reply_markup=ReplyKeyboardRemove(),
    )


# --------------------------------------------------
# ТЕКСТ
# --------------------------------------------------

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
            "Щоб почати пошук маршруту, "
            "натисни /start."
        )

        return

    step = users[user_id].get("step")

    # --------------------------------------------------
    # АДРЕСА СТАРТУ
    # --------------------------------------------------

    if step == "waiting_origin_address":

        await message.answer(
            "🔎 Шукаю адресу..."
        )

        result = await geocode_address(text)

        if result is None:

            await message.answer(
                "❌ Не вдалося знайти цю адресу."
            )

            return

        origin_stops = find_nearest_stops(
            result["latitude"],
            result["longitude"],
        )

        users[user_id] = {
            "step": "waiting_destination",
            "origin_type": "address",
            "origin": text,
            "origin_latitude": result["latitude"],
            "origin_longitude": result["longitude"],
            "origin_stops": origin_stops,
        }

        if origin_stops:
            nearest = origin_stops[0]

            stop_text = (
                "\n\n🚏 Найближча зупинка:\n"
                f"«{nearest['name']}» — "
                f"{round(nearest['distance'])} м"
            )
        else:
            stop_text = (
                "\n\n⚠️ Не знайшов зупинок поруч."
            )

        await message.answer(
            f"✅ Звідки: {text}"
            + stop_text
            + "\n\n🏁 Тепер напиши адресу, "
            "куди тобі потрібно їхати."
        )

        return

    # --------------------------------------------------
    # ПРИЗНАЧЕННЯ
    # --------------------------------------------------

    if step == "waiting_destination":

        await message.answer(
            "🔎 Шукаю адресу та транспорт..."
        )

        result = await geocode_address(text)

        if result is None:

            await message.answer(
                "❌ Не вдалося знайти "
                "адресу призначення."
            )

            return

        destination_stops = find_nearest_stops(
            result["latitude"],
            result["longitude"],
        )

        origin_stops = users[user_id].get(
            "origin_stops",
            [],
        )

        routes = find_direct_routes(
            origin_stops,
            destination_stops,
        )

        users[user_id]["step"] = "done"

        if users[user_id]["origin_type"] == "location":
            origin_text = "Твоя геолокація 📍"
        else:
            origin_text = users[user_id]["origin"]

        if not routes:

            origin_nearest = (
                origin_stops[0]
                if origin_stops
                else None
            )

            destination_nearest = (
                destination_stops[0]
                if destination_stops
                else None
            )

            details = ""

            if origin_nearest:
                details += (
                    "\n\n🚏 Біля тебе:\n"
                    f"«{origin_nearest['name']}» — "
                    f"{round(origin_nearest['distance'])} м"
                )

            if destination_nearest:
                details += (
                    "\n\n🚏 Біля пункту призначення:\n"
                    f"«{destination_nearest['name']}» — "
                    f"{round(destination_nearest['distance'])} м"
                )

            await message.answer(
                "⚠️ Прямого маршруту між "
                "найближчими зупинками не знайшов."
                + details
                + "\n\n🔄 Наступним етапом "
                "додамо маршрути з пересадкою."
            )

            return

        # Показуємо максимум 3 найкращі варіанти
        best_routes = routes[:3]

        lines = [
            "🚌 Знайшов прямі маршрути!",
            "",
            f"📍 Звідки: {origin_text}",
            f"🏁 Куди: {text}",
        ]

        for number, route in enumerate(
            best_routes,
            start=1,
        ):
            lines.extend(
                [
                    "",
                    f"━━ Варіант {number} ━━",
                    f"🚌 Маршрут №{route['route_number']}",
                    (
                        f"➡️ Напрямок: {route['headsign']}"
                        if route["headsign"]
                        else "➡️ Напрямок: за маршрутом"
                    ),
                    (
                        f"🚶 {round(route['origin_stop']['distance'])} м "
                        f"до «{route['origin_stop']['name']}»"
                    ),
                    (
                        f"🚏 Виходь: "
                        f"«{route['destination_stop']['name']}»"
                    ),
                    (
                        f"🛑 Приблизно "
                        f"{route['stop_count']} зуп."
                    ),
                    (
                        f"🚶 Потім "
                        f"{round(route['destination_stop']['distance'])} м "
                        f"до адреси."
                    ),
                ]
            )

        lines.extend(
            [
                "",
                "ℹ️ Поки це маршрут за офіційною "
                "схемою GTFS.",
                "Наступним кроком підключимо GPS "
                "транспорту та час прибуття.",
            ]
        )

        await message.answer(
            "\n".join(lines)
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
        "MENI-TUDY ROUTING STARTING...",
        flush=True,
    )

    await load_gtfs()

    print(
        "MENI-TUDY ROUTING ONLINE",
        flush=True,
    )

    await dp.start_polling(
        bot,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    asyncio.run(main())
