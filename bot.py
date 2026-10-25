import asyncio
import csv
import io
import math
import os
import zipfile
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

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
KYIV_TZ = ZoneInfo("Europe/Kyiv")

users = {}

stops = {}
routes = {}
trips = {}
trip_sequences = {}
stop_to_trips = defaultdict(list)

calendar_services = {}
calendar_exceptions = defaultdict(dict)


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
# ЧАС
# ==================================================

def gtfs_time_to_seconds(value):
    if not value:
        return None

    try:
        hours, minutes, seconds = map(
            int,
            value.split(":"),
        )

        return (
            hours * 3600
            + minutes * 60
            + seconds
        )

    except Exception:
        return None


def seconds_to_clock(seconds):
    seconds = int(seconds)

    hours = (seconds // 3600) % 24
    minutes = (seconds % 3600) // 60

    return f"{hours:02d}:{minutes:02d}"


def format_minutes(minutes):
    minutes = max(
        1,
        round(minutes),
    )

    if minutes < 60:
        return f"{minutes} хв"

    hours = minutes // 60
    rest = minutes % 60

    if rest == 0:
        return f"{hours} год"

    return f"{hours} год {rest} хв"


def current_time_info():
    now = datetime.now(KYIV_TZ)

    seconds = (
        now.hour * 3600
        + now.minute * 60
        + now.second
    )

    return now, seconds


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
# CSV
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


# ==================================================
# GTFS
# ==================================================

async def load_gtfs():
    global stops
    global routes
    global trips
    global trip_sequences
    global stop_to_trips
    global calendar_services
    global calendar_exceptions

    print(
        "GTFS downloading...",
        flush=True,
    )

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

        filenames = set(
            archive.namelist()
        )

        # ------------------------------------------
        # STOPS
        # ------------------------------------------

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

        # ------------------------------------------
        # ROUTES
        # ------------------------------------------

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

        # ------------------------------------------
        # TRIPS
        # ------------------------------------------

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
                "service_id": row.get(
                    "service_id",
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

        # ------------------------------------------
        # CALENDAR
        # ------------------------------------------

        new_calendar_services = {}

        if "calendar.txt" in filenames:

            for row in read_csv_from_zip(
                archive,
                "calendar.txt",
            ):
                service_id = row.get(
                    "service_id"
                )

                if not service_id:
                    continue

                new_calendar_services[
                    service_id
                ] = row

            print(
                f"calendar.txt loaded: "
                f"{len(new_calendar_services)} services",
                flush=True,
            )

        else:
            print(
                "calendar.txt not found",
                flush=True,
            )

        calendar_services = (
            new_calendar_services
        )

        # ------------------------------------------
        # CALENDAR DATES
        # ------------------------------------------

        new_exceptions = defaultdict(dict)

        if "calendar_dates.txt" in filenames:

            for row in read_csv_from_zip(
                archive,
                "calendar_dates.txt",
            ):
                service_id = row.get(
                    "service_id"
                )

                date = row.get(
                    "date"
                )

                exception_type = row.get(
                    "exception_type"
                )

                if (
                    service_id
                    and date
                    and exception_type
                ):
                    new_exceptions[
                        date
                    ][
                        service_id
                    ] = exception_type

            print(
                f"calendar_dates.txt loaded: "
                f"{len(new_exceptions)} dates",
                flush=True,
            )

        else:
            print(
                "calendar_dates.txt not found",
                flush=True,
            )

        calendar_exceptions = (
            new_exceptions
        )

        # ------------------------------------------
        # STOP TIMES
        # ------------------------------------------

        raw_sequences = defaultdict(list)

        for row in read_csv_from_zip(
            archive,
            "stop_times.txt",
        ):
            trip_id = row.get(
                "trip_id"
            )

            stop_id = row.get(
                "stop_id"
            )

            if not trip_id or not stop_id:
                continue

            try:
                sequence_number = int(
                    row.get(
                        "stop_sequence",
                        "0",
                    )
                )
            except ValueError:
                continue

            arrival = gtfs_time_to_seconds(
                row.get(
                    "arrival_time"
                )
            )

            departure = gtfs_time_to_seconds(
                row.get(
                    "departure_time"
                )
            )

            raw_sequences[
                trip_id
            ].append(
                {
                    "sequence": sequence_number,
                    "stop_id": stop_id,
                    "arrival": arrival,
                    "departure": departure,
                }
            )

        new_sequences = {}
        new_stop_to_trips = defaultdict(list)

        for (
            trip_id,
            sequence,
        ) in raw_sequences.items():

            sequence.sort(
                key=lambda x: x["sequence"]
            )

            new_sequences[
                trip_id
            ] = sequence

            for (
                position,
                item,
            ) in enumerate(sequence):

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
# АКТИВНИЙ SERVICE_ID
# ==================================================

def service_is_active(
    service_id,
    date,
):
    date_string = date.strftime(
        "%Y%m%d"
    )

    # Спочатку перевіряємо виняток
    exception = (
        calendar_exceptions
        .get(date_string, {})
        .get(service_id)
    )

    if exception == "1":
        return True

    if exception == "2":
        return False

    # Потім звичайний календар
    calendar = calendar_services.get(
        service_id
    )

    if calendar:

        start_date = calendar.get(
            "start_date",
            ""
        )

        end_date = calendar.get(
            "end_date",
            ""
        )

        if (
            start_date
            and date_string < start_date
        ):
            return False

        if (
            end_date
            and date_string > end_date
        ):
            return False

        weekday_names = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]

        weekday = weekday_names[
            date.weekday()
        ]

        return (
            calendar.get(
                weekday,
                "0",
            )
            == "1"
        )

    # Якщо calendar.txt немає,
    # але є calendar_dates.txt,
    # активним є тільки явно доданий рейс.
    if calendar_exceptions:
        return False

    # Fallback для GTFS без обох календарів
    return True


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
    trip = trips.get(
        trip_id
    )

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
        "service_id": trip["service_id"],
    }


# ==================================================
# ПОШУК РЕАЛЬНОГО НАЙБЛИЖЧОГО РЕЙСУ
# ==================================================

def find_best_current_routes(
    origin_candidates,
    destination_candidates,
):
    now, current_seconds = (
        current_time_info()
    )

    destination_map = {
        stop["id"]: stop
        for stop in destination_candidates
    }

    results = []

    for origin in origin_candidates:

        # До посадки максимум 600 м
        if origin["distance"] > 600:
            continue

        walk_to_minutes = (
            origin["distance"] / 80
        )

        # Даємо 1 хв запасу
        arrival_at_stop_seconds = (
            current_seconds
            + walk_to_minutes * 60
            + 60
        )

        for (
            trip_id,
            origin_position,
        ) in stop_to_trips.get(
            origin["id"],
            [],
        ):

            trip = trips.get(
                trip_id
            )

            if not trip:
                continue

            if not service_is_active(
                trip["service_id"],
                now.date(),
            ):
                continue

            sequence = trip_sequences.get(
                trip_id,
                [],
            )

            if not sequence:
                continue

            origin_item = sequence[
                origin_position
            ]

            departure = (
                origin_item["departure"]
                if origin_item["departure"] is not None
                else origin_item["arrival"]
            )

            if departure is None:
                continue

            # Цей транспорт уже не встигнемо
            if departure < arrival_at_stop_seconds:
                continue

            # Не показуємо рейси через багато годин
            if (
                departure
                - current_seconds
                > 2 * 3600
            ):
                continue

            for destination_position in range(
                origin_position + 1,
                len(sequence),
            ):

                destination_id = sequence[
                    destination_position
                ]["stop_id"]

                if (
                    destination_id
                    not in destination_map
                ):
                    continue

                destination = (
                    destination_map[
                        destination_id
                    ]
                )

                # Після висадки максимум 600 м
                if destination["distance"] > 600:
                    continue

                destination_item = sequence[
                    destination_position
                ]

                arrival = (
                    destination_item["arrival"]
                    if destination_item["arrival"] is not None
                    else destination_item["departure"]
                )

                if arrival is None:
                    continue

                if arrival <= departure:
                    continue

                route = route_info(
                    trip_id
                )

                if not route:
                    continue

                wait_minutes = (
                    departure
                    - current_seconds
                    - walk_to_minutes * 60
                ) / 60

                wait_minutes = max(
                    0,
                    wait_minutes,
                )

                ride_minutes = (
                    arrival
                    - departure
                ) / 60

                walk_from_minutes = (
                    destination["distance"]
                    / 80
                )

                total_minutes = (
                    walk_to_minutes
                    + wait_minutes
                    + ride_minutes
                    + walk_from_minutes
                )

                stop_count = (
                    destination_position
                    - origin_position
                )

                # Невеликий штраф за довгу ходьбу
                walking_penalty = 0

                if origin["distance"] > 400:
                    walking_penalty += 5

                if destination["distance"] > 400:
                    walking_penalty += 7

                score = (
                    total_minutes
                    + walking_penalty
                )

                results.append(
                    {
                        "score": score,
                        "route": route,
                        "origin": origin,
                        "destination": destination,
                        "departure": departure,
                        "arrival": arrival,
                        "walk_to_minutes": walk_to_minutes,
                        "wait_minutes": wait_minutes,
                        "ride_minutes": ride_minutes,
                        "walk_from_minutes": walk_from_minutes,
                        "total_minutes": total_minutes,
                        "stop_count": stop_count,
                    }
                )

                break

    # Один найкращий найближчий рейс
    # для кожного маршруту
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
# START
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
        "📍 Звідки ти їдеш?",
        reply_markup=location_keyboard,
    )


# ==================================================
# LOCATION
# ==================================================

@dp.message(F.location)
async def location_handler(message: Message):
    user_id = message.from_user.id

    latitude = (
        message.location.latitude
    )

    longitude = (
        message.location.longitude
    )

    origin_stops = nearby_stops(
        latitude,
        longitude,
    )

    users[user_id] = {
        "step": "waiting_destination",
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
        "✅ Геолокацію отримано!"
        + stop_text
        + "\n\n🏁 Куди тобі потрібно їхати?",
        reply_markup=ReplyKeyboardRemove(),
    )


# ==================================================
# TEXT
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
            "📍 Напиши адресу, "
            "звідки ти їдеш.",
            reply_markup=ReplyKeyboardRemove(),
        )

        return

    if user_id not in users:

        await message.answer(
            "Натисни /start."
        )

        return

    step = users[
        user_id
    ].get("step")

    # ------------------------------------------
    # START ADDRESS
    # ------------------------------------------

    if step == "waiting_origin_address":

        await message.answer(
            "🔎 Шукаю адресу..."
        )

        result = await geocode_address(
            text
        )

        if not result:

            await message.answer(
                "❌ Не знайшов адресу."
            )

            return

        origin_stops = nearby_stops(
            result["latitude"],
            result["longitude"],
        )

        users[user_id] = {
            "step": "waiting_destination",
            "origin_stops": origin_stops,
        }

        await message.answer(
            f"✅ Звідки: {text}\n\n"
            "🏁 Тепер напиши, куди їдемо."
        )

        return

    # ------------------------------------------
    # DESTINATION
    # ------------------------------------------

    if step == "waiting_destination":

        await message.answer(
            "🔎 Перевіряю актуальні рейси "
            "та рахую час..."
        )

        destination = await geocode_address(
            text
        )

        if not destination:

            await message.answer(
                "❌ Не знайшов адресу призначення."
            )

            return

        destination_stops = nearby_stops(
            destination["latitude"],
            destination["longitude"],
        )

        origin_stops = users[
            user_id
        ].get(
            "origin_stops",
            [],
        )

        results = find_best_current_routes(
            origin_stops,
            destination_stops,
        )

        users[user_id]["step"] = "done"

        if not results:

            await message.answer(
                "⚠️ На найближчі 2 години "
                "не знайшов зручного прямого рейсу.\n\n"
                "Це може означати, що потрібна "
                "пересадка або зараз немає "
                "відповідного рейсу."
            )

            return

        best = results[0]
        route = best["route"]

        now, _ = current_time_info()

        lines = [
            "⚡ Найкращий варіант зараз",
            "",
            f"🕙 Зараз: {now.strftime('%H:%M')}",
            f"🏁 Куди: {text}",
            "",
            f"🚌 Маршрут №{route['number']}",
        ]

        if route["headsign"]:

            lines.append(
                f"➡️ Напрямок: "
                f"{route['headsign']}"
            )

        lines.extend(
            [
                "",
                (
                    f"🚶 {round(best['origin']['distance'])} м "
                    f"до «{best['origin']['name']}»"
                ),
                (
                    f"   ≈ "
                    f"{format_minutes(best['walk_to_minutes'])}"
                ),
                "",
                (
                    f"🕙 За розкладом: "
                    f"{seconds_to_clock(best['departure'])}"
                ),
                (
                    f"⌛ Після приходу на зупинку "
                    f"чекати ≈ "
                    f"{format_minutes(best['wait_minutes'])}"
                ),
                "",
                (
                    f"🚌 Їхати "
                    f"{best['stop_count']} зуп."
                ),
                (
                    f"   ≈ "
                    f"{format_minutes(best['ride_minutes'])}"
                ),
                "",
                (
                    f"🚏 Виходь: "
                    f"«{best['destination']['name']}»"
                ),
                (
                    f"🚶 Ще "
                    f"{round(best['destination']['distance'])} м"
                ),
                "",
                (
                    f"🏁 Прибуття приблизно: "
                    f"{seconds_to_clock(best['arrival'] + best['walk_from_minutes'] * 60)}"
                ),
                (
                    f"⏱ Разом від зараз: "
                    f"{format_minutes(best['total_minutes'])}"
                ),
                "",
                "ℹ️ Поки це плановий час GTFS.",
                "GPS транспорту ще не враховується.",
            ]
        )

        alternatives = results[1:3]

        if alternatives:

            lines.extend(
                [
                    "",
                    "🔄 Інші варіанти:",
                ]
            )

            for item in alternatives:

                lines.append(
                    f"• №{item['route']['number']} — "
                    f"{seconds_to_clock(item['departure'])}, "
                    f"разом ≈ "
                    f"{format_minutes(item['total_minutes'])}"
                )

        await message.answer(
            "\n".join(lines)
        )

        return

    await message.answer(
        "🔄 Для нового маршруту натисни /start."
    )


# ==================================================
# MAIN
# ==================================================

async def main():

    print(
        "MENI-TUDY LIVE SCHEDULE STARTING...",
        flush=True,
    )

    await load_gtfs()

    print(
        "MENI-TUDY LIVE SCHEDULE ONLINE",
        flush=True,
    )

    await dp.start_polling(
        bot,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    asyncio.run(main())
