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
# CONFIG
# ==================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()

GTFS_URL = "https://city.dozor.tech/iv-frankivsk/gtfs/static.zip"

DEVICES_URL = (
    "https://city.dozor.tech/ua/iv-frankivsk/devices.json"
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# GPS старіший за цей час не вважаємо "живим"
MAX_GPS_AGE_SECONDS = 5 * 60

# Максимальна відстань пішки до/від зупинки
MAX_WALK_METERS = 850

users = {}

stops = {}
routes = {}
trips = {}

trip_sequences = defaultdict(list)
stop_to_trips = defaultdict(list)


# ==================================================
# KEYBOARDS
# ==================================================

start_keyboard = ReplyKeyboardMarkup(
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


new_route_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="🧭 Новий маршрут",
            )
        ],
        [
            KeyboardButton(
                text="📍 Оновити мою геолокацію",
                request_location=True,
            )
        ],
    ],
    resize_keyboard=True,
)


# ==================================================
# BASIC HELPERS
# ==================================================

def distance_meters(
    lat1,
    lon1,
    lat2,
    lon2,
):
    radius = 6371000

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dlat = math.radians(
        lat2 - lat1
    )

    dlon = math.radians(
        lon2 - lon1
    )

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


def walking_minutes(distance):
    # ~80 м/хв
    return max(
        1,
        round(distance / 80),
    )


def current_time():
    return datetime.now(
        KYIV_TZ
    )


def parse_gps_time(value):
    if not value:
        return None

    try:
        dt = datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S",
        )

        return dt.replace(
            tzinfo=KYIV_TZ
        )

    except Exception:
        return None


# ==================================================
# HTTP
# ==================================================

def default_headers():
    return {
        "User-Agent": "MeniTudyTelegramBot/2.0",
        "Accept": "*/*",
    }


# ==================================================
# GEOCODING
# ==================================================

async def geocode_address(address):
    url = (
        "https://nominatim.openstreetmap.org/search"
    )

    params = {
        "q": (
            f"{address}, "
            "Івано-Франківськ, Україна"
        ),
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "ua",
    }

    timeout = aiohttp.ClientTimeout(
        total=15
    )

    try:
        async with aiohttp.ClientSession(
            headers=default_headers(),
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
                    "latitude": float(
                        data[0]["lat"]
                    ),
                    "longitude": float(
                        data[0]["lon"]
                    ),
                    "display_name": data[0].get(
                        "display_name",
                        address,
                    ),
                }

    except Exception as error:
        print(
            f"GEOCODING ERROR: {error}",
            flush=True,
        )

        return None


# ==================================================
# GTFS CSV
# ==================================================

def read_csv(
    archive,
    filename,
):
    with archive.open(
        filename
    ) as raw:

        text = io.TextIOWrapper(
            raw,
            encoding="utf-8-sig",
        )

        for row in csv.DictReader(
            text
        ):
            yield row


# ==================================================
# LOAD GTFS
# ==================================================

async def load_gtfs():
    global stops
    global routes
    global trips
    global trip_sequences
    global stop_to_trips

    print(
        "GTFS DOWNLOAD START",
        flush=True,
    )

    timeout = aiohttp.ClientTimeout(
        total=60
    )

    try:
        async with aiohttp.ClientSession(
            headers=default_headers(),
            timeout=timeout,
        ) as session:

            async with session.get(
                GTFS_URL
            ) as response:

                if response.status != 200:
                    print(
                        "GTFS HTTP ERROR:",
                        response.status,
                        flush=True,
                    )

                    return False

                content = await response.read()

        archive = zipfile.ZipFile(
            io.BytesIO(content)
        )

        # ------------------------------------------
        # STOPS
        # ------------------------------------------

        new_stops = {}

        for row in read_csv(
            archive,
            "stops.txt",
        ):
            try:
                stop_id = row[
                    "stop_id"
                ]

                new_stops[
                    stop_id
                ] = (
                    row.get(
                        "stop_name",
                        "Без назви",
                    ),
                    float(
                        row["stop_lat"]
                    ),
                    float(
                        row["stop_lon"]
                    ),
                )

            except Exception:
                continue

        stops = new_stops

        # ------------------------------------------
        # ROUTES
        # ------------------------------------------

        new_routes = {}

        for row in read_csv(
            archive,
            "routes.txt",
        ):
            route_id = row.get(
                "route_id"
            )

            if not route_id:
                continue

            new_routes[
                route_id
            ] = {
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

        for row in read_csv(
            archive,
            "trips.txt",
        ):
            trip_id = row.get(
                "trip_id"
            )

            if not trip_id:
                continue

            new_trips[
                trip_id
            ] = {
                "route_id": row.get(
                    "route_id",
                    "",
                ),
                "headsign": row.get(
                    "trip_headsign",
                    "",
                ),
            }

        trips = new_trips

        # ------------------------------------------
        # STOP TIMES
        # streaming — без великого проміжного list
        # ------------------------------------------

        new_sequences = defaultdict(
            list
        )

        with archive.open(
            "stop_times.txt"
        ) as raw:

            text = io.TextIOWrapper(
                raw,
                encoding="utf-8-sig",
            )

            reader = csv.DictReader(
                text
            )

            for row in reader:

                trip_id = row.get(
                    "trip_id"
                )

                stop_id = row.get(
                    "stop_id"
                )

                if (
                    not trip_id
                    or not stop_id
                ):
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

                new_sequences[
                    trip_id
                ].append(
                    (
                        sequence,
                        stop_id,
                    )
                )

        # ------------------------------------------
        # INDEX
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
            "GTFS READY:",
            f"{len(stops)} stops,",
            f"{len(routes)} routes,",
            f"{len(trips)} trips",
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
# LIVE VEHICLES
# ==================================================

async def get_live_vehicles():
    timeout = aiohttp.ClientTimeout(
        total=15
    )

    try:
        async with aiohttp.ClientSession(
            headers=default_headers(),
            timeout=timeout,
        ) as session:

            async with session.get(
                DEVICES_URL
            ) as response:

                if response.status != 200:
                    return []

                raw = await response.read()

        data = json.loads(
            raw.decode(
                "utf-8-sig"
            )
        )

        if not isinstance(
            data,
            list,
        ):
            return []

        now = current_time()

        vehicles = []

        for item in data:

            if not isinstance(
                item,
                dict,
            ):
                continue

            try:
                latitude = float(
                    item.get(
                        "latitude"
                    )
                )

                longitude = float(
                    item.get(
                        "longitude"
                    )
                )

            except Exception:
                continue

            gps_time = parse_gps_time(
                item.get(
                    "time"
                )
            )

            if not gps_time:
                continue

            age = (
                now - gps_time
            ).total_seconds()

            # Майбутній timestamp або дуже старий GPS
            if (
                age < -60
                or age > MAX_GPS_AGE_SECONDS
            ):
                continue

            vehicles.append(
                {
                    "imei": str(
                        item.get(
                            "imei",
                            "",
                        )
                    ),
                    "gov_number": str(
                        item.get(
                            "gov_number",
                            "",
                        )
                    ),
                    "route_id": str(
                        item.get(
                            "route_id",
                            "",
                        )
                    ),
                    "route_short_name": str(
                        item.get(
                            "route_short_name",
                            "",
                        )
                    ).strip(),
                    "route_long_name": str(
                        item.get(
                            "route_long_name",
                            "",
                        )
                    ).strip(),
                    "route_type": item.get(
                        "route_type"
                    ),
                    "time": gps_time,
                    "age": age,
                    "latitude": latitude,
                    "longitude": longitude,
                    "speed": item.get(
                        "speed",
                        0,
                    ),
                    "satellites": item.get(
                        "satellites",
                    ),
                }
            )

        return vehicles

    except Exception as error:
        print(
            f"GPS ERROR: {repr(error)}",
            flush=True,
        )

        return []


# ==================================================
# NEARBY STOPS
# ==================================================

def nearby_stops(
    latitude,
    longitude,
    radius=1000,
    limit=25,
):
    result = []

    for (
        stop_id,
        stop_data,
    ) in stops.items():

        (
            name,
            stop_lat,
            stop_lon,
        ) = stop_data

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
                    "latitude": stop_lat,
                    "longitude": stop_lon,
                    "distance": distance,
                }
            )

    result.sort(
        key=lambda x: x[
            "distance"
        ]
    )

    return result[:limit]


# ==================================================
# ROUTE INFO
# ==================================================

def get_route_info(
    trip_id
):
    trip = trips.get(
        trip_id
    )

    if not trip:
        return None

    route_id = trip[
        "route_id"
    ]

    route = routes.get(
        route_id
    )

    if not route:
        return None

    number = (
        route.get(
            "short_name"
        )
        or route.get(
            "long_name"
        )
        or route_id
    )

    return {
        "route_id": route_id,
        "number": str(
            number
        ).strip(),
        "long_name": route.get(
            "long_name",
            "",
        ),
        "headsign": trip.get(
            "headsign",
            "",
        ),
    }


# ==================================================
# DIRECT ROUTE SEARCH
# ==================================================

def find_direct_routes(
    origin_candidates,
    destination_candidates,
):
    destination_map = {
        stop["id"]: stop
        for stop in destination_candidates
        if stop["distance"]
        <= MAX_WALK_METERS
    }

    candidates = []

    for origin in origin_candidates:

        if (
            origin["distance"]
            > MAX_WALK_METERS
        ):
            continue

        connections = (
            stop_to_trips.get(
                origin["id"],
                [],
            )
        )

        for (
            trip_id,
            origin_position,
        ) in connections:

            sequence = (
                trip_sequences.get(
                    trip_id
                )
            )

            if not sequence:
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

                info = get_route_info(
                    trip_id
                )

                if not info:
                    break

                stop_count = (
                    destination_position
                    - origin_position
                )

                total_walk = (
                    origin["distance"]
                    + destination[
                        "distance"
                    ]
                )

                # Сильніше караємо довгу ходьбу.
                score = (
                    total_walk
                    + stop_count * 18
                )

                if (
                    origin["distance"]
                    > 500
                ):
                    score += 400

                if (
                    destination[
                        "distance"
                    ]
                    > 500
                ):
                    score += 500

                candidates.append(
                    {
                        "score": score,
                        "route": info,
                        "trip_id": trip_id,
                        "origin": origin,
                        "destination": (
                            destination
                        ),
                        "origin_position": (
                            origin_position
                        ),
                        "destination_position": (
                            destination_position
                        ),
                        "stop_count": (
                            stop_count
                        ),
                    }
                )

                break

    # Один найкращий варіант
    # на кожен route_id.
    best_by_route = {}

    for item in candidates:

        route_id = item[
            "route"
        ]["route_id"]

        previous = (
            best_by_route.get(
                route_id
            )
        )

        if (
            previous is None
            or item["score"]
            < previous["score"]
        ):
            best_by_route[
                route_id
            ] = item

    result = list(
        best_by_route.values()
    )

    result.sort(
        key=lambda x: x[
            "score"
        ]
    )

    return result


# ==================================================
# MATCH GPS VEHICLES TO ROUTE
# ==================================================

def live_vehicles_for_route(
    route,
    vehicles,
):
    route_id = str(
        route.get(
            "route_id",
            ""
        )
    ).strip()

    number = str(
        route.get(
            "number",
            ""
        )
    ).strip()

    matches = []

    for vehicle in vehicles:

        vehicle_route_id = str(
            vehicle.get(
                "route_id",
                ""
            )
        ).strip()

        vehicle_number = str(
            vehicle.get(
                "route_short_name",
                ""
            )
        ).strip()

        # Найнадійніше — route_id.
        # short_name лишаємо fallback.
        if (
            vehicle_route_id
            == route_id
            or (
                number
                and vehicle_number
                == number
            )
        ):
            matches.append(
                vehicle
            )

    return matches


# ==================================================
# GPS ANALYSIS FOR BOARDING STOP
# ==================================================

def analyze_live_route(
    candidate,
    vehicles,
):
    matches = live_vehicles_for_route(
        candidate["route"],
        vehicles,
    )

    if not matches:
        return {
            "count": 0,
            "nearest": None,
        }

    stop = candidate[
        "origin"
    ]

    ranked = []

    for vehicle in matches:

        distance = distance_meters(
            vehicle[
                "latitude"
            ],
            vehicle[
                "longitude"
            ],
            stop[
                "latitude"
            ],
            stop[
                "longitude"
            ],
        )

        ranked.append(
            (
                distance,
                vehicle,
            )
        )

    ranked.sort(
        key=lambda x: x[0]
    )

    nearest_distance, nearest = (
        ranked[0]
    )

    return {
        "count": len(matches),
        "nearest": nearest,
        "distance_to_stop": (
            nearest_distance
        ),
    }


# ==================================================
# FORMAT GPS
# ==================================================

def format_gps_age(
    seconds
):
    seconds = max(
        0,
        int(seconds),
    )

    if seconds < 60:
        return (
            f"{seconds} с тому"
        )

    return (
        f"{seconds // 60} хв тому"
    )


def format_vehicle_distance(
    meters
):
    if meters < 1000:
        return (
            f"{round(meters)} м "
            "від зупинки"
        )

    return (
        f"{meters / 1000:.1f} км "
        "від зупинки"
    )


# ==================================================
# BUILD ROUTE RESPONSE
# ==================================================

async def build_route_result(
    destination_text,
    origin_stops,
    destination_stops,
):
    direct_routes = (
        find_direct_routes(
            origin_stops,
            destination_stops,
        )
    )

    if not direct_routes:
        return None

    # Отримуємо GPS один раз
    # для всіх маршрутів.
    vehicles = (
        await get_live_vehicles()
    )

    enriched = []

    for candidate in (
        direct_routes[:10]
    ):

        live = analyze_live_route(
            candidate,
            vehicles,
        )

        candidate[
            "live"
        ] = live

        # Якщо є живий транспорт —
        # він отримує бонус.
        live_bonus = 0

        if live["count"] > 0:
            live_bonus = -350

            distance = live.get(
                "distance_to_stop",
                999999,
            )

            if distance < 1500:
                live_bonus -= 250

            elif distance < 3000:
                live_bonus -= 120

        candidate[
            "final_score"
        ] = (
            candidate["score"]
            + live_bonus
        )

        enriched.append(
            candidate
        )

    enriched.sort(
        key=lambda x: x[
            "final_score"
        ]
    )

    return {
        "destination": (
            destination_text
        ),
        "vehicles_total": len(
            vehicles
        ),
        "routes": enriched,
    }


# ==================================================
# START
# ==================================================

@dp.message(CommandStart())
async def start_handler(
    message: Message
):
    users[
        message.from_user.id
    ] = {
        "step": "waiting_origin"
    }

    await message.answer(
        "🚌 МЕНІ ТУДИ\n"
        "━━━━━━━━━━━━━━\n"
        "Твій транспортний помічник "
        "в Івано-Франківську ⚡\n\n"
        "Напиши, куди тобі потрібно, "
        "а я допоможу розібратися з дорогою:\n\n"
        "🚏 знайду зручну зупинку\n"
        "🚌 підберу прямий маршрут\n"
        "🛰 перевірю транспорт наживо\n"
        "📍 покажу, де найближча машина\n"
        "🏁 підкажу, де виходити\n\n"
        "Для початку — звідки вирушаємо? 👇",
        reply_markup=start_keyboard,
    )


# ==================================================
# LOCATION
# ==================================================

@dp.message(F.location)
async def location_handler(
    message: Message
):
    user_id = (
        message.from_user.id
    )

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
        "step": (
            "waiting_destination"
        ),
        "latitude": latitude,
        "longitude": longitude,
        "origin_stops": (
            origin_stops
        ),
    }

    if origin_stops:

        nearest = origin_stops[0]

        await message.answer(
            "📍 Є! Точку старту визначив\n"
            "━━━━━━━━━━━━━━\n"
            f"🚏 Найближча зупинка:\n"
            f"«{nearest['name']}»\n\n"
            f"🚶 До неї приблизно "
            f"{round(nearest['distance'])} м "
            f"({walking_minutes(nearest['distance'])} хв)\n\n"
            "Тепер найцікавіше 😎\n"
            "🏁 Куди тобі потрібно?",
            reply_markup=ReplyKeyboardRemove(),
        )

    else:

        await message.answer(
            "📍 Точку старту отримав ✓\n\n"
            "Поруч не знайшов зупинки "
            "в межах приблизно 1 км.\n\n"
            "🏁 Напиши пункт призначення — "
            "все одно спробую знайти варіант.",
            reply_markup=ReplyKeyboardRemove(),
        )


# ==================================================
# DEBUG GPS
# ==================================================

@dp.message(F.text == "/gpsdebug")
async def gps_debug_handler(
    message: Message
):
    vehicles = (
        await get_live_vehicles()
    )

    now = current_time()

    route_counts = defaultdict(
        int
    )

    for vehicle in vehicles:
        route_counts[
            vehicle[
                "route_short_name"
            ]
        ] += 1

    top_routes = sorted(
        route_counts.items(),
        key=lambda x: (
            -x[1],
            x[0],
        ),
    )

    lines = [
        "🛰 GPS DEBUG",
        "",
        f"🕙 {now.strftime('%H:%M:%S')}",
        f"✅ Свіжих машин: {len(vehicles)}",
        "",
        "Маршрути:",
    ]

    for (
        number,
        count,
    ) in top_routes[:30]:

        lines.append(
            f"№{number or '?'} — "
            f"{count} машин"
        )

    await message.answer(
        "\n".join(lines)
    )


# ==================================================
# DEBUG
# ==================================================

@dp.message(F.text == "/debug")
async def debug_handler(
    message: Message
):
    vehicles = (
        await get_live_vehicles()
    )

    await message.answer(
        "🔧 DEBUG\n\n"
        f"🚏 Зупинок: {len(stops)}\n"
        f"🚌 Маршрутів GTFS: {len(routes)}\n"
        f"🚍 Trips: {len(trips)}\n"
        f"🛰 Свіжих GPS-машин: {len(vehicles)}"
    )


# ==================================================
# TEXT
# ==================================================

@dp.message(F.text)
async def text_handler(
    message: Message
):
    user_id = (
        message.from_user.id
    )

    text = (
        message.text.strip()
    )

    # ----------------------------------------------
    # NEW ROUTE
    # ----------------------------------------------

    if text == "🧭 Новий маршрут":

        users[
            user_id
        ] = {
            "step": "waiting_origin"
        }

        await message.answer(
            "🧭 Будуємо новий маршрут\n\n"
            "Звідки вирушаємо? 👇",
            reply_markup=start_keyboard,
        )

        return

    # ----------------------------------------------
    # MANUAL ORIGIN
    # ----------------------------------------------

    if text == "🔎 Ввести адресу":

        users[
            user_id
        ] = {
            "step": (
                "waiting_origin_address"
            )
        }

        await message.answer(
            "🔎 Напиши адресу, "
            "звідки вирушаєш.\n\n"
            "Наприклад:\n"
            "Південний бульвар 35",
            reply_markup=ReplyKeyboardRemove(),
        )

        return

    if user_id not in users:

        await message.answer(
            "👋 Почнемо спочатку.\n"
            "Натисни /start"
        )

        return

    step = users[
        user_id
    ].get(
        "step"
    )

    # ----------------------------------------------
    # ORIGIN ADDRESS
    # ----------------------------------------------

    if (
        step
        == "waiting_origin_address"
    ):

        await message.answer(
            "🔎 Шукаю точку старту..."
        )

        result = (
            await geocode_address(
                text
            )
        )

        if not result:

            await message.answer(
                "🤔 Не зміг знайти цю адресу.\n\n"
                "Спробуй написати назву вулиці "
                "та номер будинку ще раз."
            )

            return

        origin_stops = nearby_stops(
            result[
                "latitude"
            ],
            result[
                "longitude"
            ],
        )

        users[
            user_id
        ] = {
            "step": (
                "waiting_destination"
            ),
            "latitude": result[
                "latitude"
            ],
            "longitude": result[
                "longitude"
            ],
            "origin_stops": (
                origin_stops
            ),
        }

        if origin_stops:

            nearest = (
                origin_stops[0]
            )

            await message.answer(
                "📍 Знайшов ✓\n"
                "━━━━━━━━━━━━━━\n"
                f"🚏 Найближча зупинка:\n"
                f"«{nearest['name']}»\n\n"
                f"🚶 {round(nearest['distance'])} м "
                f"≈ {walking_minutes(nearest['distance'])} хв\n\n"
                "🏁 Тепер напиши, куди їдемо."
            )

        else:

            await message.answer(
                "📍 Адресу знайшов ✓\n\n"
                "🏁 Тепер напиши, куди їдемо."
            )

        return

    # ----------------------------------------------
    # DESTINATION
    # ----------------------------------------------

    if (
        step
        == "waiting_destination"
    ):

        await message.answer(
            "🧭 Будую маршрут...\n"
            "🛰 Одночасно перевіряю транспорт наживо."
        )

        destination = (
            await geocode_address(
                text
            )
        )

        if not destination:

            await message.answer(
                "🤔 Не знайшов пункт призначення.\n\n"
                "Спробуй написати адресу точніше, "
                "наприклад: Височана 18."
            )

            return

        destination_stops = (
            nearby_stops(
                destination[
                    "latitude"
                ],
                destination[
                    "longitude"
                ],
            )
        )

        origin_stops = users[
            user_id
        ].get(
            "origin_stops",
            [],
        )

        result = (
            await build_route_result(
                text,
                origin_stops,
                destination_stops,
            )
        )

        if not result:

            await message.answer(
                "🤔 Прямого маршруту поруч "
                "не знайшов.\n\n"
                "Це не означає, що дістатися "
                "неможливо — найімовірніше, "
                "потрібна пересадка.\n\n"
                "🔄 Пошук маршрутів з пересадкою "
                "буде наступним кроком.",
                reply_markup=new_route_keyboard,
            )

            users[
                user_id
            ]["step"] = "done"

            return

        options = result[
            "routes"
        ]

        best = options[0]

        route = best[
            "route"
        ]

        live = best[
            "live"
        ]

        lines = [
            "⚡ МАРШРУТ ЗНАЙДЕНО",
            "━━━━━━━━━━━━━━",
            f"🏁 Куди: {text}",
            "",
            f"🚌 Маршрут №{route['number']}",
        ]

        if route.get(
            "headsign"
        ):

            lines.append(
                f"➡️ Напрямок: "
                f"{route['headsign']}"
            )

        lines.extend(
            [
                "",
                "1️⃣ ДО ЗУПИНКИ",
                (
                    f"🚶 {round(best['origin']['distance'])} м "
                    f"до «{best['origin']['name']}»"
                ),
                (
                    f"⏱ приблизно "
                    f"{walking_minutes(best['origin']['distance'])} хв пішки"
                ),
                "",
            ]
        )

        # ------------------------------------------
        # LIVE GPS
        # ------------------------------------------

        if (
            live
            and live[
                "count"
            ] > 0
        ):

            vehicle = live[
                "nearest"
            ]

            lines.extend(
                [
                    "2️⃣ ТРАНСПОРТ НАЖИВО 🛰",
                    (
                        f"🟢 На маршруті зараз бачу "
                        f"{live['count']} "
                        f"{'машину' if live['count'] == 1 else 'машини'}"
                    ),
                    (
                        f"📍 Найближча GPS-машина: "
                        f"{format_vehicle_distance(live['distance_to_stop'])}"
                    ),
                ]
            )

            if vehicle.get(
                "gov_number"
            ):

                lines.append(
                    f"🚍 Борт/номер: "
                    f"{vehicle['gov_number']}"
                )

            try:
                speed = float(
                    vehicle.get(
                        "speed",
                        0,
                    )
                )

                lines.append(
                    f"💨 Швидкість: "
                    f"{round(speed)} км/год"
                )

            except Exception:
                pass

            lines.append(
                f"🕒 GPS оновлено "
                f"{format_gps_age(vehicle['age'])}"
            )

            lines.extend(
                [
                    "",
                    "ℹ️ Відстань до автобуса зараз "
                    "рахується за GPS по прямій.",
                    "Точний час прибуття на зупинку "
                    "ще не показую, щоб не вводити тебе в оману.",
                    "",
                ]
            )

        else:

            lines.extend(
                [
                    "2️⃣ ТРАНСПОРТ НАЖИВО 🛰",
                    (
                        f"🟡 Свіжої GPS-позиції "
                        f"маршруту №{route['number']} зараз не бачу."
                    ),
                    (
                        "Маршрут все одно існує в транспортній схемі."
                    ),
                    "",
                ]
            )

        # ------------------------------------------
        # RIDE
        # ------------------------------------------

        lines.extend(
            [
                "3️⃣ ПОЇЗДКА",
                (
                    f"🚌 Їдь приблизно "
                    f"{best['stop_count']} зуп."
                ),
                "",
                "4️⃣ ДЕ ВИХОДИТИ",
                (
                    f"🚏 «{best['destination']['name']}»"
                ),
                (
                    f"🚶 Потім ще "
                    f"{round(best['destination']['distance'])} м "
                    f"≈ {walking_minutes(best['destination']['distance'])} хв"
                ),
            ]
        )

        # ------------------------------------------
        # ALTERNATIVES
        # ------------------------------------------

        alternatives = (
            options[1:3]
        )

        if alternatives:

            lines.extend(
                [
                    "",
                    "━━━━━━━━━━━━━━",
                    "🔄 ЩЕ ВАРІАНТИ",
                ]
            )

            for alternative in alternatives:

                alt_route = (
                    alternative[
                        "route"
                    ]
                )

                alt_live = (
                    alternative[
                        "live"
                    ]
                )

                if (
                    alt_live
                    and alt_live[
                        "count"
                    ] > 0
                ):
                    live_mark = (
                        f"🟢 {alt_live['count']} GPS"
                    )
                else:
                    live_mark = "⚪ без GPS"

                lines.append(
                    f"• №{alt_route['number']} — "
                    f"{round(alternative['origin']['distance'])} м "
                    f"до посадки, {live_mark}"
                )

        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━",
                "🛰 Дані транспорту перевірені наживо.",
            ]
        )

        await message.answer(
            "\n".join(
                lines
            ),
            reply_markup=new_route_keyboard,
        )

        users[
            user_id
        ]["step"] = "done"

        return

    await message.answer(
        "🧭 Готовий прокласти ще один маршрут.",
        reply_markup=new_route_keyboard,
    )


# ==================================================
# MAIN
# ==================================================

async def main():
    print(
        "MENI-TUDY LIVE GPS START",
        flush=True,
    )

    loaded = (
        await load_gtfs()
    )

    if not loaded:
        print(
            "WARNING: GTFS NOT LOADED",
            flush=True,
        )

    print(
        "BOT POLLING START",
        flush=True,
    )

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message"
        ],
    )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
