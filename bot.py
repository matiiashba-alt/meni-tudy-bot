import asyncio
import csv
import io
import json
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

# ==================================================
# НАЛАШТУВАННЯ
# ==================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()

GTFS_URL = "https://city.dozor.tech/iv-frankivsk/gtfs/static.zip"

GPS_URLS = [
    (
        "vehicle_position",
        "https://city.dozor.tech/iv-frankivsk/gtfs/vehicle_position",
    ),
    (
        "devices.json",
        "https://city.dozor.tech/ua/iv-frankivsk/devices.json",
    ),
]

KYIV_TZ = ZoneInfo("Europe/Kyiv")

users = {}

stops = {}
routes = {}
trips = {}

trip_sequences = defaultdict(list)
stop_to_trips = defaultdict(list)

calendar_services = {}
calendar_exceptions = defaultdict(dict)


# ==================================================
# КЛАВІАТУРА
# ==================================================

location_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="📍 Я тут",
                request_location=True,
            )
        ],
        [
            KeyboardButton(
                text="🔎 Ввести адресу",
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
        h, m, s = map(
            int,
            value.split(":"),
        )

        return h * 3600 + m * 60 + s

    except Exception:
        return None


def seconds_to_clock(seconds):
    seconds = int(seconds)

    hours = (seconds // 3600) % 24
    minutes = (seconds % 3600) // 60

    return f"{hours:02d}:{minutes:02d}"


def format_minutes(value):
    value = max(
        1,
        round(value),
    )

    if value < 60:
        return f"{value} хв"

    hours = value // 60
    minutes = value % 60

    if minutes == 0:
        return f"{hours} год"

    return f"{hours} год {minutes} хв"


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

def read_small_csv(archive, filename):
    with archive.open(filename) as raw:

        text = io.TextIOWrapper(
            raw,
            encoding="utf-8-sig",
        )

        for row in csv.DictReader(text):
            yield row


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
        "GTFS DOWNLOAD START",
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
                        f"GTFS HTTP ERROR {response.status}",
                        flush=True,
                    )

                    return False

                content = await response.read()

        print(
            f"GTFS ZIP: {len(content)} bytes",
            flush=True,
        )

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

        for row in read_small_csv(
            archive,
            "stops.txt",
        ):

            try:
                stop_id = row["stop_id"]

                new_stops[stop_id] = (
                    row.get(
                        "stop_name",
                        "Без назви",
                    ),
                    float(row["stop_lat"]),
                    float(row["stop_lon"]),
                )

            except Exception:
                continue

        stops = new_stops

        print(
            f"STOPS: {len(stops)}",
            flush=True,
        )

        # ------------------------------------------
        # ROUTES
        # ------------------------------------------

        new_routes = {}

        for row in read_small_csv(
            archive,
            "routes.txt",
        ):

            route_id = row.get(
                "route_id"
            )

            if not route_id:
                continue

            new_routes[route_id] = (
                row.get(
                    "route_short_name",
                    "",
                ),
                row.get(
                    "route_long_name",
                    "",
                ),
            )

        routes = new_routes

        print(
            f"ROUTES: {len(routes)}",
            flush=True,
        )

        # ------------------------------------------
        # TRIPS
        # ------------------------------------------

        new_trips = {}

        for row in read_small_csv(
            archive,
            "trips.txt",
        ):

            trip_id = row.get(
                "trip_id"
            )

            if not trip_id:
                continue

            new_trips[trip_id] = (
                row.get(
                    "route_id",
                    "",
                ),
                row.get(
                    "service_id",
                    "",
                ),
                row.get(
                    "trip_headsign",
                    "",
                ),
            )

        trips = new_trips

        print(
            f"TRIPS: {len(trips)}",
            flush=True,
        )

        # ------------------------------------------
        # CALENDAR
        # ------------------------------------------

        new_calendar = {}

        if "calendar.txt" in filenames:

            for row in read_small_csv(
                archive,
                "calendar.txt",
            ):

                service_id = row.get(
                    "service_id"
                )

                if service_id:

                    new_calendar[
                        service_id
                    ] = row

        calendar_services = new_calendar

        # ------------------------------------------
        # CALENDAR DATES
        # ------------------------------------------

        new_exceptions = defaultdict(dict)

        if "calendar_dates.txt" in filenames:

            for row in read_small_csv(
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
                    ][service_id] = exception_type

        calendar_exceptions = new_exceptions

        # ------------------------------------------
        # STOP TIMES
        # Потокове читання для економії RAM
        # ------------------------------------------

        new_sequences = defaultdict(list)

        print(
            "READING STOP_TIMES...",
            flush=True,
        )

        row_count = 0

        with archive.open(
            "stop_times.txt"
        ) as raw:

            text = io.TextIOWrapper(
                raw,
                encoding="utf-8-sig",
            )

            reader = csv.DictReader(text)

            for row in reader:

                trip_id = row.get(
                    "trip_id"
                )

                stop_id = row.get(
                    "stop_id"
                )

                if not trip_id or not stop_id:
                    continue

                try:
                    sequence = int(
                        row.get(
                            "stop_sequence",
                            "0",
                        )
                    )

                except Exception:
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

                new_sequences[
                    trip_id
                ].append(
                    (
                        sequence,
                        stop_id,
                        arrival,
                        departure,
                    )
                )

                row_count += 1

        print(
            f"STOP_TIMES: {row_count}",
            flush=True,
        )

        # ------------------------------------------
        # ІНДЕКС ЗУПИНОК
        # ------------------------------------------

        new_stop_to_trips = defaultdict(
            list
        )

        for (
            trip_id,
            sequence,
        ) in new_sequences.items():

            sequence.sort(
                key=lambda x: x[0]
            )

            for (
                position,
                item,
            ) in enumerate(sequence):

                stop_id = item[1]

                new_stop_to_trips[
                    stop_id
                ].append(
                    (
                        trip_id,
                        position,
                    )
                )

        trip_sequences = new_sequences
        stop_to_trips = new_stop_to_trips

        archive.close()

        del content

        print(
            "GTFS READY",
            flush=True,
        )

        return True

    except Exception as error:

        print(
            f"GTFS ERROR: {repr(error)}",
            flush=True,
        )

        return False


# ==================================================
# КАЛЕНДАР
# ==================================================

def service_is_active(
    service_id,
    date,
):
    date_string = date.strftime(
        "%Y%m%d"
    )

    exception = (
        calendar_exceptions
        .get(date_string, {})
        .get(service_id)
    )

    if exception == "1":
        return True

    if exception == "2":
        return False

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

        weekdays = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]

        weekday = weekdays[
            date.weekday()
        ]

        return (
            calendar.get(
                weekday,
                "0",
            )
            == "1"
        )

    return True


# ==================================================
# ЗУПИНКИ
# ==================================================

def nearby_stops(
    latitude,
    longitude,
    radius=900,
    limit=20,
):
    result = []

    for (
        stop_id,
        stop_data,
    ) in stops.items():

        name, stop_lat, stop_lon = (
            stop_data
        )

        distance = distance_meters(
            latitude,
            longitude,
            stop_lat,
            stop_lon,
        )

        if distance <= radius:

            result.append(
                {
                    "id": stop_id,
                    "name": name,
                    "distance": distance,
                }
            )

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

    route_id, service_id, headsign = trip

    route = routes.get(
        route_id
    )

    if not route:
        return None

    short_name, long_name = route

    number = (
        short_name
        or long_name
        or route_id
    )

    return {
        "route_id": route_id,
        "service_id": service_id,
        "number": number,
        "headsign": headsign,
    }


# ==================================================
# ПОШУК МАРШРУТУ
# ==================================================

def find_best_current_routes(
    origin_candidates,
    destination_candidates,
):
    now, current_seconds = (
        current_time_info()
    )

    destination_map = {
        item["id"]: item
        for item in destination_candidates
    }

    results = []

    for origin in origin_candidates:

        if origin["distance"] > 600:
            continue

        walk_to_minutes = (
            origin["distance"] / 80
        )

        reach_stop_seconds = (
            current_seconds
            + walk_to_minutes * 60
            + 60
        )

        connections = stop_to_trips.get(
            origin["id"],
            [],
        )

        for (
            trip_id,
            origin_position,
        ) in connections:

            trip = trips.get(
                trip_id
            )

            if not trip:
                continue

            route_id, service_id, _ = trip

            if not service_is_active(
                service_id,
                now.date(),
            ):
                continue

            sequence = trip_sequences.get(
                trip_id
            )

            if not sequence:
                continue

            origin_item = sequence[
                origin_position
            ]

            departure = (
                origin_item[3]
                if origin_item[3] is not None
                else origin_item[2]
            )

            if departure is None:
                continue

            if departure < reach_stop_seconds:
                continue

            if (
                departure
                - current_seconds
                > 7200
            ):
                continue

            for destination_position in range(
                origin_position + 1,
                len(sequence),
            ):

                destination_id = (
                    sequence[
                        destination_position
                    ][1]
                )

                destination = (
                    destination_map.get(
                        destination_id
                    )
                )

                if not destination:
                    continue

                if destination["distance"] > 600:
                    continue

                destination_item = (
                    sequence[
                        destination_position
                    ]
                )

                arrival = (
                    destination_item[2]
                    if destination_item[2] is not None
                    else destination_item[3]
                )

                if (
                    arrival is None
                    or arrival <= departure
                ):
                    continue

                info = route_info(
                    trip_id
                )

                if not info:
                    continue

                wait_minutes = max(
                    0,
                    (
                        departure
                        - current_seconds
                        - walk_to_minutes * 60
                    )
                    / 60,
                )

                ride_minutes = (
                    arrival - departure
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

                walking_penalty = 0

                if origin["distance"] > 400:
                    walking_penalty += 5

                if destination["distance"] > 400:
                    walking_penalty += 7

                results.append(
                    {
                        "score": (
                            total_minutes
                            + walking_penalty
                        ),
                        "route": info,
                        "origin": origin,
                        "destination": destination,
                        "departure": departure,
                        "arrival": arrival,
                        "walk_to_minutes": walk_to_minutes,
                        "wait_minutes": wait_minutes,
                        "ride_minutes": ride_minutes,
                        "walk_from_minutes": walk_from_minutes,
                        "total_minutes": total_minutes,
                        "stop_count": (
                            destination_position
                            - origin_position
                        ),
                    }
                )

                break

    best = {}

    for item in results:

        route_id = item[
            "route"
        ]["route_id"]

        old = best.get(
            route_id
        )

        if (
            old is None
            or item["score"] < old["score"]
        ):
            best[route_id] = item

    result = list(
        best.values()
    )

    result.sort(
        key=lambda x: x["score"]
    )

    return result


# ==================================================
# GPS DEBUG
# ==================================================

@dp.message(F.text == "/gpsdebug")
async def gps_debug_handler(message: Message):

    await message.answer(
        "🛰 Перевіряю живі GPS-дані транспорту...\n"
        "Зачекай кілька секунд."
    )

    lines = [
        "🛰 GPS DEBUG",
        "",
    ]

    headers = {
        "User-Agent": "MeniTudyTelegramBot/1.0",
        "Accept": "*/*",
    }

    timeout = aiohttp.ClientTimeout(
        total=20
    )

    try:

        async with aiohttp.ClientSession(
            headers=headers,
            timeout=timeout,
        ) as session:

            for name, url in GPS_URLS:

                lines.append(
                    f"━━ {name} ━━"
                )

                try:

                    async with session.get(
                        url
                    ) as response:

                        raw = await response.read()

                        content_type = (
                            response.headers.get(
                                "Content-Type",
                                "невідомо",
                            )
                        )

                        lines.append(
                            f"HTTP: {response.status}"
                        )

                        lines.append(
                            f"Content-Type: {content_type}"
                        )

                        lines.append(
                            f"Розмір: {len(raw)} байт"
                        )

                        if response.status != 200:

                            lines.append(
                                "❌ Сервер не повернув HTTP 200"
                            )

                            lines.append("")

                            continue

                        # --------------------------
                        # ПРОБУЄМО UTF-8 + JSON
                        # --------------------------

                        try:

                            decoded = raw.decode(
                                "utf-8-sig"
                            )

                            data = json.loads(
                                decoded
                            )

                            lines.append(
                                "Формат: ✅ JSON"
                            )

                            # ----------------------
                            # JSON LIST
                            # ----------------------

                            if isinstance(
                                data,
                                list,
                            ):

                                lines.append(
                                    f"Об'єктів: {len(data)}"
                                )

                                if data:

                                    first = data[0]

                                    lines.append(
                                        f"Тип першого: "
                                        f"{type(first).__name__}"
                                    )

                                    if isinstance(
                                        first,
                                        dict,
                                    ):

                                        keys = list(
                                            first.keys()
                                        )

                                        lines.append(
                                            "Поля:"
                                        )

                                        lines.append(
                                            ", ".join(
                                                keys[:25]
                                            )
                                        )

                                        preview = {}

                                        for key in keys[:15]:

                                            value = first.get(
                                                key
                                            )

                                            if isinstance(
                                                value,
                                                (
                                                    str,
                                                    int,
                                                    float,
                                                    bool,
                                                    type(None),
                                                ),
                                            ):

                                                preview[
                                                    key
                                                ] = value

                                        lines.append(
                                            "Перший об'єкт:"
                                        )

                                        lines.append(
                                            str(
                                                preview
                                            )[:1200]
                                        )

                            # ----------------------
                            # JSON DICT
                            # ----------------------

                            elif isinstance(
                                data,
                                dict,
                            ):

                                keys = list(
                                    data.keys()
                                )

                                lines.append(
                                    "Тип: JSON object"
                                )

                                lines.append(
                                    "Верхні поля:"
                                )

                                lines.append(
                                    ", ".join(
                                        keys[:25]
                                    )
                                )

                                found_child = False

                                for key in keys[:20]:

                                    value = data.get(
                                        key
                                    )

                                    if isinstance(
                                        value,
                                        list,
                                    ):

                                        found_child = True

                                        lines.append(
                                            f"{key}: "
                                            f"{len(value)} об'єктів"
                                        )

                                        if (
                                            value
                                            and isinstance(
                                                value[0],
                                                dict,
                                            )
                                        ):

                                            child_keys = list(
                                                value[0].keys()
                                            )

                                            lines.append(
                                                "Поля першого:"
                                            )

                                            lines.append(
                                                ", ".join(
                                                    child_keys[:25]
                                                )
                                            )

                                            preview = {}

                                            for child_key in child_keys[:15]:

                                                child_value = (
                                                    value[0].get(
                                                        child_key
                                                    )
                                                )

                                                if isinstance(
                                                    child_value,
                                                    (
                                                        str,
                                                        int,
                                                        float,
                                                        bool,
                                                        type(None),
                                                    ),
                                                ):

                                                    preview[
                                                        child_key
                                                    ] = child_value

                                            lines.append(
                                                "Приклад:"
                                            )

                                            lines.append(
                                                str(
                                                    preview
                                                )[:1200]
                                            )

                                        break

                                if not found_child:

                                    preview = {}

                                    for key in keys[:15]:

                                        value = data.get(
                                            key
                                        )

                                        if isinstance(
                                            value,
                                            (
                                                str,
                                                int,
                                                float,
                                                bool,
                                                type(None),
                                            ),
                                        ):

                                            preview[
                                                key
                                            ] = value

                                    if preview:

                                        lines.append(
                                            "Приклад:"
                                        )

                                        lines.append(
                                            str(
                                                preview
                                            )[:1200]
                                        )

                        except Exception as json_error:

                            lines.append(
                                "Формат: ❓ не JSON"
                            )

                            lines.append(
                                f"JSON error: "
                                f"{type(json_error).__name__}"
                            )

                            lines.append(
                                "Перші 32 байти (HEX):"
                            )

                            lines.append(
                                raw[:32].hex()
                            )

                except Exception as error:

                    lines.append(
                        f"❌ Помилка запиту: "
                        f"{type(error).__name__}: {error}"
                    )

                lines.append("")

    except Exception as error:

        lines.append(
            f"❌ Загальна помилка: "
            f"{type(error).__name__}: {error}"
        )

    result = "\n".join(
        lines
    )

    # Telegram має ліміт повідомлення.
    if len(result) > 3900:
        result = result[:3900] + "\n\n…обрізано"

    await message.answer(
        result
    )


# ==================================================
# GTFS DEBUG
# ==================================================

@dp.message(F.text == "/debug")
async def debug_handler(message: Message):

    now, _ = current_time_info()

    service_ids = {
        trip[1]
        for trip in trips.values()
        if trip[1]
    }

    active_services = {
        service_id
        for service_id in service_ids
        if service_is_active(
            service_id,
            now.date(),
        )
    }

    active_trips = sum(
        1
        for trip in trips.values()
        if service_is_active(
            trip[1],
            now.date(),
        )
    )

    date_string = now.strftime(
        "%Y%m%d"
    )

    await message.answer(
        "🔧 GTFS DEBUG\n\n"
        f"🕙 {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"🚏 Зупинок: {len(stops)}\n"
        f"🚌 Маршрутів: {len(routes)}\n"
        f"🚍 Рейсів: {len(trips)}\n"
        f"🧭 Trip sequences: {len(trip_sequences)}\n\n"
        f"📅 service_id: {len(service_ids)}\n"
        f"📅 calendar.txt: {len(calendar_services)}\n"
        f"📅 calendar_dates: {len(calendar_exceptions)}\n\n"
        f"✅ Активних service_id: {len(active_services)}\n"
        f"✅ Активних рейсів: {active_trips}\n"
        f"🗓 Винятків сьогодні: "
        f"{len(calendar_exceptions.get(date_string, {}))}"
    )


# ==================================================
# DEBUG ROUTE
# ==================================================

@dp.message(F.text == "/debugroute")
async def debug_route_handler(message: Message):

    user_id = message.from_user.id

    user = users.get(
        user_id
    )

    if not user:

        await message.answer(
            "Спочатку натисни /start "
            "і надішли геолокацію."
        )

        return

    origin_stops = user.get(
        "origin_stops"
    )

    if not origin_stops:

        await message.answer(
            "📍 Спочатку надішли геолокацію."
        )

        return

    now, current_seconds = (
        current_time_info()
    )

    lines = [
        "🔬 DEBUG ROUTE",
        "",
        f"🕙 Зараз: {now.strftime('%H:%M:%S')}",
        "",
    ]

    for origin in origin_stops[:5]:

        lines.append(
            f"🚏 {origin['name']} — "
            f"{round(origin['distance'])} м"
        )

        connections = stop_to_trips.get(
            origin["id"],
            [],
        )

        departures = []

        for (
            trip_id,
            position,
        ) in connections:

            trip = trips.get(
                trip_id
            )

            if not trip:
                continue

            route_id, service_id, _ = trip

            if not service_is_active(
                service_id,
                now.date(),
            ):
                continue

            sequence = trip_sequences.get(
                trip_id
            )

            if (
                not sequence
                or position >= len(sequence)
            ):
                continue

            item = sequence[
                position
            ]

            departure = (
                item[3]
                if item[3] is not None
                else item[2]
            )

            if departure is None:
                continue

            route = route_info(
                trip_id
            )

            if not route:
                continue

            if (
                current_seconds - 10800
                <= departure
                <= current_seconds + 14400
            ):

                departures.append(
                    (
                        departure,
                        route["number"],
                    )
                )

        departures.sort(
            key=lambda x: x[0]
        )

        unique = []
        seen = set()

        for departure, number in departures:

            key = (
                number,
                departure,
            )

            if key in seen:
                continue

            seen.add(key)

            unique.append(
                (
                    departure,
                    number,
                )
            )

        if not unique:

            lines.append(
                "   ❌ Немає часів ± кілька годин"
            )

        else:

            for (
                departure,
                number,
            ) in unique[:12]:

                if departure < current_seconds:
                    mark = "❌"
                else:
                    mark = "✅"

                lines.append(
                    f"   №{number} — "
                    f"{seconds_to_clock(departure)} "
                    f"{mark}"
                )

        lines.append("")

    lines.extend(
        [
            "❌ = час уже минув",
            "✅ = час у GTFS ще попереду",
        ]
    )

    await message.answer(
        "\n".join(lines)
    )


# ==================================================
# START
# ==================================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    users[
        message.from_user.id
    ] = {
        "step": "waiting_origin"
    }

    await message.answer(
        "🚌 МЕНІ ТУДИ\n"
        "Івано-Франківськ\n\n"
        "Куди б ти не збирався — "
        "спробую знайти найзручніший шлях "
        "громадським транспортом ⚡\n\n"
        "📍 знайду найближчу зупинку\n"
        "🚌 підберу потрібний маршрут\n"
        "🚏 підкажу, де виходити\n"
        "🛰 готуємо підключення транспорту наживо\n\n"
        "Спочатку визначимо точку старту 👇",
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
        "latitude": latitude,
        "longitude": longitude,
    }

    if origin_stops:

        nearest = origin_stops[0]

        await message.answer(
            "📍 Точку старту знайшов ✓\n\n"
            f"Найближча зупинка:\n"
            f"🚏 «{nearest['name']}»\n"
            f"🚶 приблизно {round(nearest['distance'])} м від тебе\n\n"
            "Тепер найцікавіше 😎\n"
            "🏁 Куди тобі потрібно?",
            reply_markup=ReplyKeyboardRemove(),
        )

    else:

        await message.answer(
            "📍 Геолокацію отримав, але поруч "
            "не знайшов зупинки в базі.\n\n"
            "🏁 Все одно напиши, куди тобі потрібно.",
            reply_markup=ReplyKeyboardRemove(),
        )


# ==================================================
# TEXT
# ==================================================

@dp.message(F.text)
async def text_handler(message: Message):

    user_id = message.from_user.id
    text = message.text.strip()

    if text == "🔎 Ввести адресу":

        users[user_id] = {
            "step": "waiting_origin_address"
        }

        await message.answer(
            "🔎 Добре. Напиши адресу, "
            "звідки ти вирушаєш.\n\n"
            "Наприклад: Південний бульвар 35",
            reply_markup=ReplyKeyboardRemove(),
        )

        return

    if user_id not in users:

        await message.answer(
            "Натисни /start — і почнемо 🚀"
        )

        return

    step = users[
        user_id
    ].get("step")

    # ----------------------------------------------
    # АДРЕСА СТАРТУ
    # ----------------------------------------------

    if step == "waiting_origin_address":

        await message.answer(
            "🔎 Шукаю цю адресу..."
        )

        result = await geocode_address(
            text
        )

        if not result:

            await message.answer(
                "❌ Не зміг знайти цю адресу.\n\n"
                "Спробуй написати вулицю та номер будинку."
            )

            return

        origin_stops = nearby_stops(
            result["latitude"],
            result["longitude"],
        )

        users[user_id] = {
            "step": "waiting_destination",
            "origin_stops": origin_stops,
            "latitude": result["latitude"],
            "longitude": result["longitude"],
        }

        if origin_stops:

            nearest = origin_stops[0]

            await message.answer(
                "📍 Точку старту знайшов ✓\n\n"
                f"🚏 Найближча зупинка: "
                f"«{nearest['name']}»\n"
                f"🚶 {round(nearest['distance'])} м\n\n"
                "🏁 А тепер напиши, куди їдемо."
            )

        else:

            await message.answer(
                "📍 Адресу знайшов ✓\n\n"
                "🏁 Тепер напиши, куди їдемо."
            )

        return

    # ----------------------------------------------
    # ПРИЗНАЧЕННЯ
    # ----------------------------------------------

    if step == "waiting_destination":

        await message.answer(
            "🧭 Шукаю найкращий шлях...\n"
            "Перевіряю зупинки та маршрути."
        )

        destination = await geocode_address(
            text
        )

        if not destination:

            await message.answer(
                "❌ Не зміг знайти пункт призначення.\n\n"
                "Спробуй написати адресу трохи точніше."
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
                "⚠️ За статичним GTFS-розкладом "
                "зараз не бачу зручного прямого рейсу.\n\n"
                "Ми вже знаємо, що цей розклад "
                "не відображає весь транспорт у реальному часі.\n\n"
                "🛰 Наступний етап — GPS DozoR."
            )

            return

        best = results[0]
        route = best["route"]

        now, _ = current_time_info()

        lines = [
            "⚡ ЗНАЙШОВ ВАРІАНТ",
            "",
            f"🏁 {text}",
            f"🕙 Зараз {now.strftime('%H:%M')}",
            "",
            f"🚌 Маршрут №{route['number']}",
        ]

        if route["headsign"]:

            lines.append(
                f"➡️ {route['headsign']}"
            )

        lines.extend(
            [
                "",
                (
                    f"🚶 До зупинки "
                    f"«{best['origin']['name']}» — "
                    f"{round(best['origin']['distance'])} м"
                ),
                (
                    f"   ≈ "
                    f"{format_minutes(best['walk_to_minutes'])}"
                ),
                "",
                (
                    f"🕙 За GTFS: "
                    f"{seconds_to_clock(best['departure'])}"
                ),
                (
                    f"⌛ Очікування: "
                    f"≈ {format_minutes(best['wait_minutes'])}"
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
                    f"🚏 Виходити: "
                    f"«{best['destination']['name']}»"
                ),
                (
                    f"🚶 Звідти ще "
                    f"{round(best['destination']['distance'])} м"
                ),
                "",
                (
                    f"⏱ Орієнтовно разом: "
                    f"{format_minutes(best['total_minutes'])}"
                ),
                "",
                "ℹ️ Поки що це дані статичного GTFS.",
                "🛰 GPS транспорту підключаємо окремо.",
            ]
        )

        alternatives = results[1:3]

        if alternatives:

            lines.extend(
                [
                    "",
                    "🔄 Ще варіанти:",
                ]
            )

            for item in alternatives:

                lines.append(
                    f"• №{item['route']['number']} — "
                    f"{seconds_to_clock(item['departure'])}, "
                    f"≈ {format_minutes(item['total_minutes'])}"
                )

        await message.answer(
            "\n".join(lines)
        )

        return

    await message.answer(
        "🧭 Хочеш прокласти новий маршрут?\n"
        "Натисни /start"
    )


# ==================================================
# MAIN
# ==================================================

async def main():

    print(
        "MENI-TUDY GPS DEBUG START",
        flush=True,
    )

    await load_gtfs()

    print(
        "MENI-TUDY POLLING START",
        flush=True,
    )

    await dp.start_polling(
        bot,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    asyncio.run(main())
