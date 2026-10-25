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
trip_sequences = {}
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

    dp_lat = math.radians(lat2 - lat1)
    dp_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(dp_lat / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dp_lon / 2) ** 2
    )

    return radius * (
        2
        * math.atan2(
            math.sqrt(a),
            math.sqrt(1 - a),
        )
    )


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
            f"Geocoding error: {error}",
            flush=True,
        )
        return None


# ==================================================
# ЧИТАННЯ GTFS
# ==================================================

def read_csv_from_zip(archive, filename):
    with archive.open(filename) as file:
        wrapper = io.TextIOWrapper(
            file,
            encoding="utf-8-sig",
        )
        return list(csv.DictReader(wrapper))


async def load_gtfs():
    global stops
    global routes
    global trips
    global trip_sequences
    global stop_to_trips

    print("GTFS: downloading...", flush=True)

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

        # ЗУПИНКИ

        stop_rows = read_csv_from_zip(
            archive,
            "stops.txt",
        )

        new_stops = {}

        for row in stop_rows:
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

        # МАРШРУТИ

        route_rows = read_csv_from_zip(
            archive,
            "routes.txt",
        )

        new_routes = {}

        for row in route_rows:
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

        # РЕЙСИ

        trip_rows = read_csv_from_zip(
            archive,
            "trips.txt",
        )

        new_trips = {}

        for row in trip_rows:
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

        # ПОСЛІДОВНІСТЬ ЗУПИНОК

        stop_time_rows = read_csv_from_zip(
            archive,
            "stop_times.txt",
        )

        raw_sequences = defaultdict(list)

        for row in stop_time_rows:
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

            raw_sequences[trip_id].append(
                (
                    sequence,
                    stop_id,
                )
            )

        new_sequences = {}
        new_stop_to_trips = defaultdict(list)

        for trip_id, sequence in raw_sequences.items():

            sequence.sort(
                key=lambda x: x[0]
            )

            stop_ids = [
                stop_id
                for _, stop_id in sequence
            ]

            new_sequences[trip_id] = stop_ids

            for position, stop_id in enumerate(
                stop_ids
            ):
                new_stop_to_trips[stop_id].append(
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
    radius=1200,
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
# ІНФОРМАЦІЯ ПРО МАРШРУТ
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
        "name": route["long_name"],
        "headsign": trip["headsign"],
    }


# ==================================================
# ПРЯМИЙ МАРШРУТ
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

        connections = stop_to_trips.get(
            origin["id"],
            [],
        )

        for trip_id, origin_position in connections:

            sequence = trip_sequences.get(
                trip_id,
                [],
            )

            if not sequence:
                continue

            for destination_position in range(
                origin_position + 1,
                len(sequence),
            ):

                destination_id = sequence[
                    destination_position
                ]

                if destination_id not in destination_map:
                    continue

                destination = destination_map[
                    destination_id
                ]

                info = route_info(trip_id)

                if not info:
                    continue

                stop_count = (
                    destination_position
                    - origin_position
                )

                score = (
                    origin["distance"] * 1.5
                    + destination["distance"] * 1.5
                    + stop_count * 40
                )

                results.append(
                    {
                        "score": score,
                        "route": info,
                        "origin": origin,
                        "destination": destination,
                        "stop_count": stop_count,
                    }
                )

                break

    best = {}

    for item in results:

        key = (
            item["route"]["route_id"],
            item["origin"]["id"],
            item["destination"]["id"],
        )

        old = best.get(key)

        if (
            old is None
            or item["score"] < old["score"]
        ):
            best[key] = item

    result = list(best.values())

    result.sort(
        key=lambda x: x["score"]
    )

    return result


# ==================================================
# ПОШУК ОДНІЄЇ ПЕРЕСАДКИ
# ==================================================

def find_transfer_routes(
    origin_candidates,
    destination_candidates,
):
    results = []

    destination_ids = {
        stop["id"]: stop
        for stop in destination_candidates
    }

    for origin in origin_candidates:

        first_connections = stop_to_trips.get(
            origin["id"],
            [],
        )

        for first_trip_id, first_start_pos in first_connections:

            first_sequence = trip_sequences.get(
                first_trip_id,
                [],
            )

            first_info = route_info(
                first_trip_id
            )

            if not first_sequence or not first_info:
                continue

            # Не розглядаємо сотні зупинок.
            # Достатньо наступних 35.
            first_end = min(
                len(first_sequence),
                first_start_pos + 36,
            )

            for transfer_pos in range(
                first_start_pos + 1,
                first_end,
            ):

                transfer_stop_id = first_sequence[
                    transfer_pos
                ]

                transfer_stop = stops.get(
                    transfer_stop_id
                )

                if not transfer_stop:
                    continue

                second_connections = stop_to_trips.get(
                    transfer_stop_id,
                    [],
                )

                for (
                    second_trip_id,
                    second_start_pos,
                ) in second_connections:

                    second_info = route_info(
                        second_trip_id
                    )

                    if not second_info:
                        continue

                    # Пересадка на той самий маршрут
                    # нам не потрібна.
                    if (
                        second_info["route_id"]
                        == first_info["route_id"]
                    ):
                        continue

                    second_sequence = trip_sequences.get(
                        second_trip_id,
                        [],
                    )

                    if not second_sequence:
                        continue

                    for destination_pos in range(
                        second_start_pos + 1,
                        len(second_sequence),
                    ):

                        destination_id = second_sequence[
                            destination_pos
                        ]

                        if destination_id not in destination_ids:
                            continue

                        destination = destination_ids[
                            destination_id
                        ]

                        first_stop_count = (
                            transfer_pos
                            - first_start_pos
                        )

                        second_stop_count = (
                            destination_pos
                            - second_start_pos
                        )

                        total_stops = (
                            first_stop_count
                            + second_stop_count
                        )

                        score = (
                            origin["distance"] * 1.5
                            + destination["distance"] * 1.5
                            + total_stops * 40
                            + 600
                        )

                        results.append(
                            {
                                "score": score,
                                "first_route": first_info,
                                "second_route": second_info,
                                "origin": origin,
                                "transfer": transfer_stop,
                                "destination": destination,
                                "first_stop_count": first_stop_count,
                                "second_stop_count": second_stop_count,
                            }
                        )

                        break

    # Прибираємо дублікати комбінацій маршрутів
    best = {}

    for item in results:

        key = (
            item["first_route"]["route_id"],
            item["second_route"]["route_id"],
            item["origin"]["id"],
            item["transfer"]["id"],
            item["destination"]["id"],
        )

        old = best.get(key)

        if (
            old is None
            or item["score"] < old["score"]
        ):
            best[key] = item

    result = list(best.values())

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
                "❌ Не вдалося знайти адресу.\n\n"
                "Спробуй написати її точніше."
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
            "🔎 Шукаю найзручніший маршрут..."
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

        if not origin_stops:

            await message.answer(
                "❌ Не знайшов зупинок "
                "біля точки старту."
            )

            return

        if not destination_stops:

            await message.answer(
                "❌ Не знайшов зупинок "
                "біля пункту призначення."
            )

            return

        # Спочатку шукаємо прямі маршрути
        direct_routes = find_direct_routes(
            origin_stops,
            destination_stops,
        )

        users[user_id]["step"] = "done"

        if direct_routes:

            best_routes = direct_routes[:3]

            lines = [
                "✅ Знайшов прямий маршрут!",
                "",
                f"🏁 Куди: {text}",
            ]

            for index, item in enumerate(
                best_routes,
                start=1,
            ):

                route = item["route"]

                lines.extend(
                    [
                        "",
                        f"━━ Варіант {index} ━━",
                        f"🚌 Маршрут №{route['number']}",
                        (
                            f"➡️ Напрямок: {route['headsign']}"
                            if route["headsign"]
                            else "➡️ Напрямок: за маршрутом"
                        ),
                        (
                            f"🚶 Пройди "
                            f"{round(item['origin']['distance'])} м "
                            f"до «{item['origin']['name']}»"
                        ),
                        (
                            f"🚌 Їдь приблизно "
                            f"{item['stop_count']} зуп."
                        ),
                        (
                            f"🚏 Виходь на "
                            f"«{item['destination']['name']}»"
                        ),
                        (
                            f"🚶 До адреси залишиться "
                            f"{round(item['destination']['distance'])} м"
                        ),
                    ]
                )

            await message.answer(
                "\n".join(lines)
            )

            return

        # Якщо прямого немає — одна пересадка

        await message.answer(
            "🔄 Прямого маршруту не знайшов. "
            "Шукаю варіант з однією пересадкою..."
        )

        transfer_routes = find_transfer_routes(
            origin_stops,
            destination_stops,
        )

        if not transfer_routes:

            await message.answer(
                "⚠️ Не вдалося знайти зручний "
                "маршрут навіть з однією пересадкою.\n\n"
                "Спробуй іншу адресу або "
                "трохи пізніше."
            )

            return

        best = transfer_routes[0]

        first = best["first_route"]
        second = best["second_route"]

        lines = [
            "🚌 Знайшов маршрут з пересадкою!",
            "",
            f"🏁 Куди: {text}",
            "",
            "1️⃣ Перший транспорт",
            f"🚌 Маршрут №{first['number']}",
        ]

        if first["headsign"]:
            lines.append(
                f"➡️ Напрямок: {first['headsign']}"
            )

        lines.extend(
            [
                (
                    f"🚶 Пройди "
                    f"{round(best['origin']['distance'])} м "
                    f"до «{best['origin']['name']}»"
                ),
                (
                    f"🚌 Їдь приблизно "
                    f"{best['first_stop_count']} зуп."
                ),
                "",
                "🔄 Пересадка",
                (
                    f"🚏 На зупинці "
                    f"«{best['transfer']['name']}»"
                ),
                "",
                "2️⃣ Другий транспорт",
                f"🚌 Маршрут №{second['number']}",
            ]
        )

        if second["headsign"]:
            lines.append(
                f"➡️ Напрямок: {second['headsign']}"
            )

        lines.extend(
            [
                (
                    f"🚌 Їдь приблизно "
                    f"{best['second_stop_count']} зуп."
                ),
                (
                    f"🚏 Виходь на "
                    f"«{best['destination']['name']}»"
                ),
                (
                    f"🚶 До адреси залишиться "
                    f"{round(best['destination']['distance'])} м"
                ),
                "",
                "ℹ️ Маршрут розраховано "
                "за офіційною схемою GTFS.",
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


# ==================================================
# ЗАПУСК
# ==================================================

async def main():

    print(
        "MENI-TUDY SMART ROUTING STARTING...",
        flush=True,
    )

    await load_gtfs()

    print(
        "MENI-TUDY SMART ROUTING ONLINE",
        flush=True,
    )

    await dp.start_polling(
        bot,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    asyncio.run(main())
