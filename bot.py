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

GTFS_URL = "https://city.dozor.tech/iv-frankivsk/gtfs/static.zip"

users = {}

stops = {}
routes = {}
trips = {}

# trip_id -> список словників зупинок у правильній послідовності
trip_sequences = {}

# stop_id -> [(trip_id, position)]
stop_to_trips = defaultdict(list)


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


# ==================================================
# ВІДСТАНЬ
# ==================================================

def distance_meters(lat1, lon1, lat2, lon2):
    radius = 6371000

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dlon / 2) ** 2
    )

    return radius * 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a),
    )


# ==================================================
# GTFS TIME
# ==================================================

def gtfs_time_to_seconds(value):
    if not value:
        return None

    try:
        parts = value.split(":")

        if len(parts) != 3:
            return None

        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])

        return (
            hours * 3600
            + minutes * 60
            + seconds
        )

    except Exception:
        return None


def format_minutes(minutes):
    minutes = max(1, round(minutes))

    if minutes < 60:
        return f"{minutes} хв"

    hours = minutes // 60
    rest = minutes % 60

    if rest == 0:
        return f"{hours} год"

    return f"{hours} год {rest} хв"


# ==================================================
# ГЕОКОДУВАННЯ
# ==================================================

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
            headers=headers,
            timeout=timeout,
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

                return {
                    "latitude": float(data[0]["lat"]),
                    "longitude": float(data[0]["lon"]),
                }

    except Exception as error:
        print(
            f"GEOCODING ERROR: {error}",
            flush=True,
        )
        return None


# ==================================================
# GTFS
# ==================================================

def read_csv_from_zip(archive, filename):
    with archive.open(filename) as file:
        wrapper = io.TextIOWrapper(
            file,
            encoding="utf-8-sig",
        )

        return list(
            csv.DictReader(wrapper)
        )


async def load_gtfs():
    global stops
    global routes
    global trips
    global trip_sequences
    global stop_to_trips

    print("GTFS downloading...", flush=True)

    headers = {
        "User-Agent": "MeniTudyTelegramBot/1.0"
    }

    timeout = aiohttp.ClientTimeout(total=60)

    try:
        async with aiohttp.ClientSession(
            headers=headers,
            timeout=timeout,
        ) as session:

            async with session.get(
                GTFS_URL
            ) as response:

                if response.status != 200:
                    print(
                        f"GTFS HTTP {response.status}",
                        flush=True,
                    )
                    return

                content = await response.read()

        archive = zipfile.ZipFile(
            io.BytesIO(content)
        )

        # --------------------------
        # STOPS
        # --------------------------

        new_stops = {}

        for row in read_csv_from_zip(
            archive,
            "stops.txt",
        ):
            try:
                stop_id = row["stop_id"]

                new_stops[stop_id] = {
                    "id": stop_id,
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

            except Exception:
                continue

        stops = new_stops

        # --------------------------
        # ROUTES
        # --------------------------

        new_routes = {}

        for row in read_csv_from_zip(
            archive,
            "routes.txt",
        ):
            route_id = row.get("route_id")

            if not route_id:
                continue

            new_routes[route_id] = {
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

        routes = new_routes

        # --------------------------
        # TRIPS
        # --------------------------

        new_trips = {}

        for row in read_csv_from_zip(
            archive,
            "trips.txt",
        ):
            trip_id = row.get("trip_id")

            if not trip_id:
                continue

            new_trips[trip_id] = {
                "route_id": row.get(
                    "route_id",
                    "",
                ),
                "headsign": row.get(
                    "trip_headsign",
                    "",
                ),
                "direction_id": row.get(
                    "direction_id",
                    "",
                ),
            }

        trips = new_trips

        # --------------------------
        # STOP TIMES
        # --------------------------

        raw_sequences = defaultdict(list)

        for row in read_csv_from_zip(
            archive,
            "stop_times.txt",
        ):
            trip_id = row.get("trip_id")
            stop_id = row.get("stop_id")

            if not trip_id or not stop_id:
                continue

            try:
                sequence = int(
                    row.get(
                        "stop_sequence",
                        "0",
                    )
                )
            except ValueError:
                continue

            arrival = gtfs_time_to_seconds(
                row.get("arrival_time")
            )

            departure = gtfs_time_to_seconds(
                row.get("departure_time")
            )

            raw_sequences[trip_id].append(
                {
                    "sequence": sequence,
                    "stop_id": stop_id,
                    "arrival": arrival,
                    "departure": departure,
                }
            )

        new_sequences = {}
        new_stop_to_trips = defaultdict(list)

        for trip_id, sequence in raw_sequences.items():

            sequence.sort(
                key=lambda x: x["sequence"]
            )

            new_sequences[trip_id] = sequence

            for position, item in enumerate(
                sequence
            ):
                new_stop_to_trips[
                    item["stop_id"]
                ].append(
                    (
                        trip_id,
                        position,
                    )
                )

        trip_sequences = new_sequences
        stop_to_trips = new_stop_to_trips

        print(
            f"GTFS READY: "
            f"{len(stops)} stops / "
            f"{len(routes)} routes / "
            f"{len(trips)} trips",
            flush=True,
        )

    except Exception as error:
        print(
            f"GTFS ERROR: {error}",
            flush=True,
        )


# ==================================================
# ЗУПИНКИ ПОРУЧ
# ==================================================

def nearby_stops(
    latitude,
    longitude,
    radius=900,
    limit=20,
):
    result = []

    for stop in stops.values():

        distance = distance_meters(
            latitude,
            longitude,
            stop["latitude"],
            stop["longitude"],
        )

        if distance <= radius:

            item = stop.copy()
            item["distance"] = distance

            result.append(item)

    result.sort(
        key=lambda x: x["distance"]
    )

    return result[:limit]


# ==================================================
# ROUTE INFO
# ==================================================

def route_info(trip_id):
    trip = trips.get(trip_id)

    if not trip:
        return None

    route = routes.get(
        trip["route_id"]
    )

    if not route:
        return None

    number = (
        route["short_name"]
        or route["long_name"]
        or trip["route_id"]
    )

    return {
        "trip_id": trip_id,
        "route_id": trip["route_id"],
        "number": number,
        "headsign": trip["headsign"],
    }


# ==================================================
# ЧАС ПОЇЗДКИ
# ==================================================

def trip_minutes(
    sequence,
    origin_position,
    destination_position,
):
    origin_item = sequence[
        origin_position
    ]

    destination_item = sequence[
        destination_position
    ]

    start_time = (
        origin_item["departure"]
        if origin_item["departure"] is not None
        else origin_item["arrival"]
    )

    end_time = (
        destination_item["arrival"]
        if destination_item["arrival"] is not None
        else destination_item["departure"]
    )

    if (
        start_time is None
        or end_time is None
    ):
        return None

    difference = end_time - start_time

    if difference <= 0:
        return None

    # Захист від явно дивних даних
    if difference > 3 * 3600:
        return None

    return difference / 60


# ==================================================
# ПРЯМІ МАРШРУТИ
# ==================================================

def find_direct_routes(
    origin_candidates,
    destination_candidates,
):
    results = []

    destination_map = {
        stop["id"]: stop
        for stop in destination_candidates
    }

    for origin in origin_candidates:

        # Не пропонуємо дуже далеку посадку
        if origin["distance"] > 650:
            continue

        for (
            trip_id,
            origin_position,
        ) in stop_to_trips.get(
            origin["id"],
            [],
        ):

            sequence = trip_sequences.get(
                trip_id,
                [],
            )

            info = route_info(
                trip_id
            )

            if not sequence or not info:
                continue

            for destination_position in range(
                origin_position + 1,
                len(sequence),
            ):

                destination_id = sequence[
                    destination_position
                ]["stop_id"]

                if destination_id not in destination_map:
                    continue

                destination = destination_map[
                    destination_id
                ]

                # Дуже далеку висадку відкидаємо
                if destination["distance"] > 650:
                    continue

                ride_minutes = trip_minutes(
                    sequence,
                    origin_position,
                    destination_position,
                )

                stop_count = (
                    destination_position
                    - origin_position
                )

                # Якщо GTFS не дав нормального часу,
                # використовуємо лише запасну оцінку.
                if ride_minutes is None:
                    ride_minutes = (
                        stop_count * 2.2
                    )

                # Середня швидкість ходьби ~80 м/хв
                walk_to_minutes = (
                    origin["distance"] / 80
                )

                walk_from_minutes = (
                    destination["distance"] / 80
                )

                total_minutes = (
                    walk_to_minutes
                    + ride_minutes
                    + walk_from_minutes
                )

                # Далека ходьба після транспорту
                # має додатковий штраф.
                penalty = 0

                if destination["distance"] > 400:
                    penalty += (
                        destination["distance"] - 400
                    ) / 30

                if origin["distance"] > 400:
                    penalty += (
                        origin["distance"] - 400
                    ) / 40

                score = (
                    total_minutes
                    + penalty
                )

                results.append(
                    {
                        "score": score,
                        "route": info,
                        "origin": origin,
                        "destination": destination,
                        "stop_count": stop_count,
                        "ride_minutes": ride_minutes,
                        "walk_to_minutes": walk_to_minutes,
                        "walk_from_minutes": walk_from_minutes,
                        "total_minutes": total_minutes,
                    }
                )

                break

    # Один найкращий варіант для кожного маршруту
    best_by_route = {}

    for item in results:

        route_id = item[
            "route"
        ]["route_id"]

        old = best_by_route.get(
            route_id
        )

        if (
            old is None
            or item["score"] < old["score"]
        ):
            best_by_route[
                route_id
            ] = item

    result = list(
        best_by_route.values()
    )

    result.sort(
        key=lambda x: x["score"]
    )

    return result


# ==================================================
# /START
# ==================================================

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


# ==================================================
# ГЕОЛОКАЦІЯ
# ==================================================

@dp.message(F.location)
async def location_handler(message: Message):
    user_id = message.from_user.id

    latitude = message.location.latitude
    longitude = message.location.longitude

    origin_stops = nearby_stops(
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


# ==================================================
# ТЕКСТ
# ==================================================

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

    # ----------------------------------------------
    # АДРЕСА СТАРТУ
    # ----------------------------------------------

    if step == "waiting_origin_address":

        await message.answer(
            "🔎 Шукаю адресу..."
        )

        result = await geocode_address(
            text
        )

        if result is None:

            await message.answer(
                "❌ Не вдалося знайти адресу."
            )

            return

        origin_stops = nearby_stops(
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

    # ----------------------------------------------
    # ПУНКТ ПРИЗНАЧЕННЯ
    # ----------------------------------------------

    if step == "waiting_destination":

        await message.answer(
            "🔎 Розраховую найкращий маршрут..."
        )

        result = await geocode_address(
            text
        )

        if result is None:

            await message.answer(
                "❌ Не вдалося знайти "
                "адресу призначення."
            )

            return

        destination_stops = nearby_stops(
            result["latitude"],
            result["longitude"],
        )

        origin_stops = users[
            user_id
        ].get(
            "origin_stops",
            [],
        )

        direct_routes = find_direct_routes(
            origin_stops,
            destination_stops,
        )

        users[user_id]["step"] = "done"

        if not direct_routes:

            await message.answer(
                "⚠️ Не знайшов зручного прямого "
                "маршруту в межах нормальної "
                "пішої відстані.\n\n"
                "Наступним кроком додамо "
                "розрахунок з пересадкою."
            )

            return

        best = direct_routes[0]

        route = best["route"]

        lines = [
            "🚌 Найкращий прямий маршрут",
            "",
            f"🏁 Куди: {text}",
            "",
            f"🚌 Маршрут №{route['number']}",
        ]

        if route["headsign"]:

            lines.append(
                f"➡️ Напрямок: {route['headsign']}"
            )

        lines.extend(
            [
                "",
                (
                    f"🚶 Пройди "
                    f"{round(best['origin']['distance'])} м "
                    f"до «{best['origin']['name']}»"
                ),
                (
                    f"   ≈ {format_minutes(best['walk_to_minutes'])}"
                ),
                "",
                (
                    f"🚌 Їдь "
                    f"{best['stop_count']} зуп."
                ),
                (
                    f"   ≈ {format_minutes(best['ride_minutes'])}"
                ),
                "",
                (
                    f"🚏 Виходь на "
                    f"«{best['destination']['name']}»"
                ),
                (
                    f"🚶 До адреси "
                    f"{round(best['destination']['distance'])} м"
                ),
                (
                    f"   ≈ {format_minutes(best['walk_from_minutes'])}"
                ),
                "",
                (
                    f"⏱ Орієнтовно в дорозі: "
                    f"{format_minutes(best['total_minutes'])}"
                ),
                "",
                "ℹ️ Час поїздки взято з GTFS-розкладу.",
                "Час очікування транспорту поки не враховано.",
            ]
        )

        # Додаємо ще максимум 2 альтернативи
        alternatives = direct_routes[1:3]

        if alternatives:

            lines.extend(
                [
                    "",
                    "Інші прямі варіанти:",
                ]
            )

            for item in alternatives:

                lines.append(
                    f"• №{item['route']['number']} — "
                    f"≈ {format_minutes(item['total_minutes'])}"
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


# ==================================================
# ЗАПУСК
# ==================================================

async def main():

    print(
        "MENI-TUDY TIME ROUTING STARTING...",
        flush=True,
    )

    await load_gtfs()

    print(
        "MENI-TUDY TIME ROUTING ONLINE",
        flush=True,
    )

    await dp.start_polling(
        bot,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    asyncio.run(main())
