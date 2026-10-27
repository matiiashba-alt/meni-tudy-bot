import asyncio
import csv
import io
import json
import math
import os
import socket
import zipfile
from urllib.parse import urlparse
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# ==================================================
# CONFIG
# ==================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Тільки цей Telegram ID має доступ до адмін-панелі
ADMIN_ID = 1962638884
DATABASE_URL = os.getenv("DATABASE_URL")
db_pool = None
db_last_error = None
admin_broadcast_drafts = {}

GTFS_URL = "https://city.dozor.tech/iv-frankivsk/gtfs/static.zip"
DEVICES_URL = "https://city.dozor.tech/ua/iv-frankivsk/devices.json"

KYIV_TZ = ZoneInfo("Europe/Kyiv")

MAX_GPS_AGE_SECONDS = 5 * 60
MAX_WALK_METERS = 900
MAX_ROUTE_MATCH_METERS = 550

DEFAULT_CITY_SPEED_KMH = 18.0

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

route_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="🛰 Де зараз транспорт?"
            ),
            KeyboardButton(
                text="🚌 Всі варіанти"
            ),
        ],
        [
            KeyboardButton(
                text="🧭 Новий маршрут"
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

admin_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="📊 Статистика"
            ),
            KeyboardButton(
                text="👥 Користувачі"
            ),
        ],
        [
            KeyboardButton(
                text="📢 Розсилка"
            ),
            KeyboardButton(
                text="🔧 Дебаг"
            ),
        ],
        [KeyboardButton(text="📝 Форми та опитування")],
        [
            KeyboardButton(
                text="❌ Закрити адмін-панель"
            )
        ],
    ],
    resize_keyboard=True,
)


# ==================================================
# BASIC HELPERS
# ==================================================

def now():
    return datetime.now(KYIV_TZ)


def is_admin(user_id):
    return user_id == ADMIN_ID


async def init_database():
    global db_pool, db_last_error
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL NOT SET", flush=True)
        return False
    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=2,
            command_timeout=10,
            statement_cache_size=0,
            ssl="require",
        )
        async with db_pool.acquire() as conn:
            await conn.execute("SELECT 1")
        db_last_error = None
        print("DATABASE CONNECTED", flush=True)
        return True
    except Exception as exc:
        db_pool = None
        db_last_error = f"{type(exc).__name__}: {exc}"
        print(f"DATABASE ERROR: {db_last_error}", flush=True)
        return False


async def track_user(message: Message):
    if not db_pool or not message.from_user:
        return
    user = message.from_user
    try:
        await db_pool.execute(
            """
            INSERT INTO bot_users (
                telegram_id, username, first_name, last_name,
                language_code, first_seen, last_seen, is_blocked
            )
            VALUES ($1, $2, $3, $4, $5, now(), now(), false)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                language_code = EXCLUDED.language_code,
                last_seen = now(),
                is_blocked = false
            """,
            user.id, user.username, user.first_name,
            user.last_name, user.language_code,
        )
    except Exception as exc:
        print(f"TRACK USER ERROR: {exc}", flush=True)


async def log_event(telegram_id, event_type):
    if not db_pool:
        return
    try:
        await db_pool.execute(
            "INSERT INTO bot_events (telegram_id, event_type) VALUES ($1, $2)",
            telegram_id, event_type,
        )
    except Exception as exc:
        print(f"LOG EVENT ERROR: {exc}", flush=True)


async def get_admin_stats():
    if not db_pool:
        return None
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    (SELECT count(*) FROM bot_users) AS total_users,
                    (SELECT count(*) FROM bot_users WHERE last_seen >= date_trunc('day', now())) AS active_today,
                    (SELECT count(*) FROM bot_users WHERE first_seen >= date_trunc('day', now())) AS new_today,
                    (SELECT count(*) FROM bot_users WHERE first_seen >= now() - interval '7 days') AS new_7,
                    (SELECT count(*) FROM bot_users WHERE first_seen >= now() - interval '30 days') AS new_30,
                    (SELECT count(*) FROM bot_events WHERE event_type = 'route_built') AS routes_total,
                    (SELECT count(*) FROM bot_events WHERE event_type = 'route_built' AND created_at >= date_trunc('day', now())) AS routes_today,
                    (SELECT count(*) FROM bot_events WHERE event_type = 'live_gps') AS gps_total,
                    (SELECT count(*) FROM bot_events WHERE event_type = 'all_routes') AS all_routes_total
            """)
            return dict(row)
    except Exception as exc:
        print(f"STATS ERROR: {exc}", flush=True)
        return None


async def get_recent_users(limit=10):
    if not db_pool:
        return []
    try:
        return await db_pool.fetch(
            """
            SELECT telegram_id, username, first_name, last_name,
                   first_seen, last_seen, is_blocked
            FROM bot_users
            ORDER BY last_seen DESC
            LIMIT $1
            """,
            limit,
        )
    except Exception as exc:
        print(f"USERS ERROR: {exc}", flush=True)
        return []


async def get_broadcast_user_ids():
    if not db_pool:
        return []
    try:
        rows = await db_pool.fetch(
            "SELECT telegram_id FROM bot_users WHERE is_blocked = false ORDER BY telegram_id"
        )
        return [row["telegram_id"] for row in rows]
    except Exception as exc:
        print(f"BROADCAST USERS ERROR: {exc}", flush=True)
        return []


async def mark_user_blocked(telegram_id):
    if not db_pool:
        return
    try:
        await db_pool.execute(
            "UPDATE bot_users SET is_blocked = true WHERE telegram_id = $1",
            telegram_id,
        )
    except Exception as exc:
        print(f"BLOCK USER ERROR: {exc}", flush=True)


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


def walking_minutes(distance):
    return max(1, round(distance / 80))


def format_distance(distance):
    if distance < 1000:
        return f"{round(distance)} м"

    return f"{distance / 1000:.1f} км"


def format_age(seconds):
    seconds = max(0, int(seconds))

    if seconds < 60:
        return f"{seconds} с тому"

    return f"{seconds // 60} хв тому"


def plural_stops(number):
    number = abs(int(number))

    if number % 10 == 1 and number % 100 != 11:
        return "зупинка"

    if (
        number % 10 in (2, 3, 4)
        and number % 100 not in (12, 13, 14)
    ):
        return "зупинки"

    return "зупинок"


def parse_gps_time(value):
    if not value:
        return None

    try:
        dt = datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S",
        )

        return dt.replace(tzinfo=KYIV_TZ)

    except Exception:
        return None


def headers():
    return {
        "User-Agent": "MeniTudyTelegramBot/4.0",
        "Accept": "*/*",
    }


# ==================================================
# GEOCODING
# ==================================================

async def geocode_address(address):
    url = "https://nominatim.openstreetmap.org/search"

    params = {
        "q": f"{address}, Івано-Франківськ, Україна",
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "ua",
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(
            headers=headers(),
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
                    "display_name": data[0].get(
                        "display_name",
                        address,
                    ),
                }

    except Exception as error:
        print(
            f"GEOCODING ERROR: {repr(error)}",
            flush=True,
        )
        return None


# ==================================================
# CSV
# ==================================================

def read_csv(archive, filename):
    with archive.open(filename) as raw:

        text = io.TextIOWrapper(
            raw,
            encoding="utf-8-sig",
        )

        for row in csv.DictReader(text):
            yield row


# ==================================================
# GTFS LOAD
# ==================================================

async def load_gtfs():
    global stops
    global routes
    global trips
    global trip_sequences
    global stop_to_trips

    print("GTFS DOWNLOAD START", flush=True)

    timeout = aiohttp.ClientTimeout(total=60)

    try:
        async with aiohttp.ClientSession(
            headers=headers(),
            timeout=timeout,
        ) as session:

            async with session.get(GTFS_URL) as response:

                if response.status != 200:
                    print(
                        f"GTFS HTTP ERROR: {response.status}",
                        flush=True,
                    )
                    return False

                content = await response.read()

        archive = zipfile.ZipFile(
            io.BytesIO(content)
        )

        new_stops = {}

        for row in read_csv(
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

        new_routes = {}

        for row in read_csv(
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

        new_trips = {}

        for row in read_csv(
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

        new_sequences = defaultdict(list)

        with archive.open(
            "stop_times.txt"
        ) as raw:

            text = io.TextIOWrapper(
                raw,
                encoding="utf-8-sig",
            )

            reader = csv.DictReader(text)

            for row in reader:

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

        new_stop_to_trips = defaultdict(list)

        for trip_id, sequence in new_sequences.items():

            sequence.sort(
                key=lambda x: x[0]
            )

            for position, item in enumerate(sequence):

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
            len(stops),
            "stops,",
            len(routes),
            "routes,",
            len(trips),
            "trips",
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
# LIVE GPS
# ==================================================

async def get_live_vehicles():
    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(
            headers=headers(),
            timeout=timeout,
        ) as session:

            async with session.get(
                DEVICES_URL
            ) as response:

                if response.status != 200:
                    return []

                raw = await response.read()

        data = json.loads(
            raw.decode("utf-8-sig")
        )

        if not isinstance(data, list):
            return []

        current = now()

        result = []

        for item in data:

            if not isinstance(item, dict):
                continue

            try:
                latitude = float(
                    item.get("latitude")
                )

                longitude = float(
                    item.get("longitude")
                )

            except Exception:
                continue

            gps_time = parse_gps_time(
                item.get("time")
            )

            if not gps_time:
                continue

            age = (
                current - gps_time
            ).total_seconds()

            if (
                age < -60
                or age > MAX_GPS_AGE_SECONDS
            ):
                continue

            try:
                speed = float(
                    item.get(
                        "speed",
                        0,
                    )
                    or 0
                )

            except Exception:
                speed = 0

            result.append(
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
                    ).strip(),
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
                    "latitude": latitude,
                    "longitude": longitude,
                    "time": gps_time,
                    "age": age,
                    "speed": speed,
                }
            )

        return result

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
    radius=1100,
    limit=28,
):
    result = []

    for stop_id, stop_data in stops.items():

        name, stop_lat, stop_lon = stop_data

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
        key=lambda x: x["distance"]
    )

    return result[:limit]


# ==================================================
# ROUTE INFO
# ==================================================

def get_route_info(trip_id):
    trip = trips.get(trip_id)

    if not trip:
        return None

    route_id = trip["route_id"]

    route = routes.get(route_id)

    if not route:
        return None

    number = (
        route.get("short_name")
        or route.get("long_name")
        or route_id
    )

    return {
        "route_id": route_id,
        "number": str(number).strip(),
        "long_name": route.get(
            "long_name",
            "",
        ),
        "headsign": trip.get(
            "headsign",
            "",
        ),
        "direction_id": trip.get(
            "direction_id",
            "",
        ),
    }


# ==================================================
# DIRECT ROUTES
# ==================================================

def find_direct_routes(
    origin_candidates,
    destination_candidates,
):
    destination_map = {
        stop["id"]: stop
        for stop in destination_candidates
        if stop["distance"] <= MAX_WALK_METERS
    }

    candidates = []

    for origin in origin_candidates:

        if origin["distance"] > MAX_WALK_METERS:
            continue

        connections = stop_to_trips.get(
            origin["id"],
            [],
        )

        for trip_id, origin_position in connections:

            sequence = trip_sequences.get(
                trip_id
            )

            if not sequence:
                continue

            for destination_position in range(
                origin_position + 1,
                len(sequence),
            ):

                destination_id = sequence[
                    destination_position
                ][1]

                destination = destination_map.get(
                    destination_id
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

                candidates.append(
                    {
                        "route": info,
                        "trip_id": trip_id,
                        "origin": origin,
                        "destination": destination,
                        "origin_position": origin_position,
                        "destination_position": destination_position,
                        "stop_count": stop_count,
                    }
                )

                break

    best = {}

    for item in candidates:

        key = (
            item["route"]["route_id"],
            item["route"]["direction_id"],
            item["origin"]["id"],
            item["destination"]["id"],
        )

        old = best.get(key)

        base_score = (
            item["origin"]["distance"]
            + item["destination"]["distance"] * 1.8
            + item["stop_count"] * 20
        )

        if (
            old is None
            or base_score < old["_base_score"]
        ):
            item["_base_score"] = base_score
            best[key] = item

    return list(best.values())


# ==================================================
# VEHICLE MATCHING
# ==================================================

def vehicle_matches_route(
    vehicle,
    route,
):
    vehicle_route_id = str(
        vehicle.get(
            "route_id",
            "",
        )
    ).strip()

    route_id = str(
        route.get(
            "route_id",
            "",
        )
    ).strip()

    if (
        vehicle_route_id
        and route_id
        and vehicle_route_id == route_id
    ):
        return True

    vehicle_number = str(
        vehicle.get(
            "route_short_name",
            "",
        )
    ).strip()

    route_number = str(
        route.get(
            "number",
            "",
        )
    ).strip()

    return (
        bool(vehicle_number)
        and bool(route_number)
        and vehicle_number == route_number
    )


def get_sequence_stop_data(trip_id):
    sequence = trip_sequences.get(
        trip_id,
        [],
    )

    result = []

    for index, item in enumerate(sequence):

        stop_id = item[1]

        stop = stops.get(stop_id)

        if not stop:
            continue

        name, latitude, longitude = stop

        result.append(
            {
                "index": index,
                "stop_id": stop_id,
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    return result


def nearest_sequence_position(
    vehicle,
    sequence_data,
):
    best = None

    for item in sequence_data:

        distance = distance_meters(
            vehicle["latitude"],
            vehicle["longitude"],
            item["latitude"],
            item["longitude"],
        )

        if (
            best is None
            or distance < best["distance"]
        ):
            best = {
                "index": item["index"],
                "stop_id": item["stop_id"],
                "name": item["name"],
                "distance": distance,
            }

    return best


def distance_along_stops(
    trip_id,
    start_position,
    end_position,
):
    sequence = trip_sequences.get(
        trip_id,
        [],
    )

    if start_position >= end_position:
        return 0

    total = 0

    for index in range(
        start_position,
        end_position,
    ):
        if index + 1 >= len(sequence):
            break

        stop_a = stops.get(
            sequence[index][1]
        )

        stop_b = stops.get(
            sequence[index + 1][1]
        )

        if not stop_a or not stop_b:
            continue

        total += distance_meters(
            stop_a[1],
            stop_a[2],
            stop_b[1],
            stop_b[2],
        )

    return total


# ==================================================
# ETA
# ==================================================

def estimate_eta_minutes(
    route_distance,
    speed,
    stops_before,
):
    if speed >= 8:

        effective_speed = max(
            15,
            min(
                25,
                speed * 0.75,
            ),
        )

    else:

        effective_speed = (
            DEFAULT_CITY_SPEED_KMH
        )

    meters_per_minute = (
        effective_speed
        * 1000
        / 60
    )

    drive_minutes = (
        route_distance
        / meters_per_minute
    )

    stop_delay = min(
        4,
        stops_before * 0.35,
    )

    estimated = (
        drive_minutes * 1.12
        + stop_delay
        + 0.5
    )

    estimated = max(
        1.0,
        estimated,
    )

    low = max(
        1,
        math.floor(
            estimated * 0.82
        ),
    )

    high = max(
        low + 1,
        math.ceil(
            estimated * 1.22
        ),
    )

    return low, high, estimated


# ==================================================
# LIVE ROUTE ANALYSIS
# ==================================================

def analyze_live_route(
    candidate,
    vehicles,
):
    route = candidate["route"]
    trip_id = candidate["trip_id"]

    origin_position = candidate[
        "origin_position"
    ]

    sequence_data = (
        get_sequence_stop_data(
            trip_id
        )
    )

    matching = [
        vehicle
        for vehicle in vehicles
        if vehicle_matches_route(
            vehicle,
            route,
        )
    ]

    approaching = []

    for vehicle in matching:

        nearest = (
            nearest_sequence_position(
                vehicle,
                sequence_data,
            )
        )

        if not nearest:
            continue

        if (
            nearest["distance"]
            > MAX_ROUTE_MATCH_METERS
        ):
            continue

        vehicle_position = nearest[
            "index"
        ]

        if vehicle_position > origin_position:
            continue

        stops_before = (
            origin_position
            - vehicle_position
        )

        route_distance = (
            distance_along_stops(
                trip_id,
                vehicle_position,
                origin_position,
            )
        )

        route_distance += nearest[
            "distance"
        ]

        low, high, middle = (
            estimate_eta_minutes(
                route_distance,
                vehicle["speed"],
                stops_before,
            )
        )

        approaching.append(
            {
                "vehicle": vehicle,
                "nearest_route_stop": nearest,
                "vehicle_position": vehicle_position,
                "stops_before": stops_before,
                "route_distance": route_distance,
                "eta_low": low,
                "eta_high": high,
                "eta_mid": middle,
            }
        )

    approaching.sort(
        key=lambda x: (
            x["eta_mid"],
            x["route_distance"],
        )
    )

    return {
        "route_vehicle_count": len(
            matching
        ),
        "approaching_count": len(
            approaching
        ),
        "best_vehicle": (
            approaching[0]
            if approaching
            else None
        ),
    }


# ==================================================
# WALK PENALTY
# ==================================================

def destination_walk_penalty(meters):
    if meters <= 300:
        return 0

    if meters <= 500:
        return (
            (meters - 300)
            / 100
        )

    if meters <= 700:
        return (
            2
            + (meters - 500)
            / 40
        )

    return (
        7
        + (meters - 700)
        / 20
    )


# ==================================================
# BUILD ROUTES
# ==================================================

async def build_route_result(
    destination_text,
    origin_stops,
    destination_stops,
):
    candidates = find_direct_routes(
        origin_stops,
        destination_stops,
    )

    if not candidates:
        return None

    vehicles = await get_live_vehicles()

    enriched = []

    candidates.sort(
        key=lambda x: x["_base_score"]
    )

    for candidate in candidates[:20]:

        live = analyze_live_route(
            candidate,
            vehicles,
        )

        walk_to = walking_minutes(
            candidate[
                "origin"
            ]["distance"]
        )

        walk_after = walking_minutes(
            candidate[
                "destination"
            ]["distance"]
        )

        ride_minutes = max(
            3,
            candidate[
                "stop_count"
            ] * 1.45,
        )

        best_vehicle = live.get(
            "best_vehicle"
        )

        if best_vehicle:

            wait_minutes = (
                best_vehicle[
                    "eta_mid"
                ]
            )

            gps_penalty = 0

        elif live[
            "route_vehicle_count"
        ] > 0:

            wait_minutes = 12
            gps_penalty = 7

        else:

            wait_minutes = 14
            gps_penalty = 10

        total_minutes = (
            walk_to
            + wait_minutes
            + ride_minutes
            + walk_after
        )

        walk_penalty = (
            destination_walk_penalty(
                candidate[
                    "destination"
                ]["distance"]
            )
        )

        origin_penalty = 0

        if (
            candidate[
                "origin"
            ]["distance"]
            > 500
        ):
            origin_penalty += 5

        if (
            candidate[
                "origin"
            ]["distance"]
            > 700
        ):
            origin_penalty += 6

        final_score = (
            total_minutes
            + walk_penalty
            + origin_penalty
            + gps_penalty
        )

        candidate["live"] = live
        candidate["walk_to_minutes"] = walk_to
        candidate["walk_after_minutes"] = walk_after
        candidate["ride_minutes"] = ride_minutes
        candidate["wait_minutes"] = wait_minutes
        candidate["total_minutes"] = total_minutes
        candidate["final_score"] = final_score

        enriched.append(candidate)

    best_by_number = {}

    for item in enriched:

        number = item[
            "route"
        ]["number"]

        old = best_by_number.get(
            number
        )

        if (
            old is None
            or item["final_score"]
            < old["final_score"]
        ):
            best_by_number[number] = item

    result = list(
        best_by_number.values()
    )

    result.sort(
        key=lambda x: x["final_score"]
    )

    return {
        "destination": destination_text,
        "routes": result,
        "updated_at": now(),
    }


# ==================================================
# WHEN TO LEAVE
# ==================================================

def leave_advice(
    eta_low,
    eta_high,
    walk_minutes,
):
    buffer_minutes = (
        1
        if walk_minutes <= 2
        else 2
    )

    leave_in = (
        eta_low
        - walk_minutes
        - buffer_minutes
    )

    if leave_in <= 0:

        return (
            "⚡ ВИХОДЬ ЗАРАЗ\n"
            "Автобус уже достатньо близько."
        )

    if leave_in == 1:

        return (
            "⚡ Збирайся виходити\n"
            "Орієнтовно через 1 хв вже варто йти."
        )

    return (
        f"⚡ Можеш виходити приблизно "
        f"через {leave_in} хв\n"
        f"🚶 До зупинки ≈ {walk_minutes} хв."
    )


# ==================================================
# FORMAT MAIN ROUTE
# ==================================================

def format_route_result(
    destination_text,
    options,
    updated_at,
):
    best = options[0]

    route = best["route"]
    live = best["live"]

    lines = [
        "⚡ НАЙКРАЩИЙ ВАРІАНТ",
        "━━━━━━━━━━━━━━",
        f"🏁 {destination_text}",
        "",
        f"🚌 Маршрут №{route['number']}",
    ]

    if route.get("headsign"):

        lines.append(
            f"➡️ {route['headsign']}"
        )

    lines.extend(
        [
            "",
            "🚶 ДО ЗУПИНКИ",
            (
                f"«{best['origin']['name']}» — "
                f"{round(best['origin']['distance'])} м"
            ),
            (
                f"≈ {best['walk_to_minutes']} хв пішки"
            ),
            "",
            "🛰 ТРАНСПОРТ НАЖИВО",
        ]
    )

    vehicle_info = live.get(
        "best_vehicle"
    )

    if vehicle_info:

        vehicle = vehicle_info[
            "vehicle"
        ]

        stops_before = vehicle_info[
            "stops_before"
        ]

        lines.append(
            f"🟢 Машина №"
            f"{vehicle['gov_number'] or '—'} "
            "ймовірно рухається до твоєї зупинки."
        )

        lines.append(
            f"📍 Зараз біля "
            f"«{vehicle_info['nearest_route_stop']['name']}»"
        )

        if stops_before == 0:

            lines.append(
                "🚏 Вона вже в районі "
                "твоєї зупинки."
            )

        else:

            lines.append(
                f"🚏 До тебе ще приблизно "
                f"{stops_before} "
                f"{plural_stops(stops_before)}"
            )

        lines.append(
            f"📏 По маршруту ≈ "
            f"{format_distance(vehicle_info['route_distance'])}"
        )

        lines.append(
            f"⏱ Очікування: "
            f"≈ {vehicle_info['eta_low']}–"
            f"{vehicle_info['eta_high']} хв"
        )

        lines.append(
            f"🕒 GPS: "
            f"{format_age(vehicle['age'])}"
        )

        lines.append("")

        lines.append(
            leave_advice(
                vehicle_info[
                    "eta_low"
                ],
                vehicle_info[
                    "eta_high"
                ],
                best[
                    "walk_to_minutes"
                ],
            )
        )

    elif live[
        "route_vehicle_count"
    ] > 0:

        lines.extend(
            [
                (
                    f"🟡 На маршруті №{route['number']} "
                    "GPS працює."
                ),
                (
                    "Але зараз не бачу машини, "
                    "яку можу достатньо надійно "
                    "визначити як таку, що наближається."
                ),
            ]
        )

    else:

        lines.append(
            f"⚪ Свіжої GPS-позиції "
            f"маршруту №{route['number']} "
            "зараз не бачу."
        )

    lines.extend(
        [
            "",
            "🚌 ПОЇЗДКА",
            (
                f"Їхати приблизно "
                f"{best['stop_count']} "
                f"{plural_stops(best['stop_count'])}"
            ),
            "",
            "🚏 ДЕ ВИХОДИТИ",
            f"«{best['destination']['name']}»",
            (
                f"🚶 До адреси ще "
                f"{round(best['destination']['distance'])} м "
                f"≈ {best['walk_after_minutes']} хв"
            ),
        ]
    )

    if vehicle_info:

        approximate_total = round(
            best["total_minutes"]
        )

        lines.extend(
            [
                "",
                (
                    f"⏱ Увесь шлях орієнтовно: "
                    f"~{approximate_total} хв"
                ),
            ]
        )

    alternatives = options[1:3]

    if alternatives:

        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━",
                "🔄 ЩЕ ВАРІАНТИ",
            ]
        )

        for alternative in alternatives:

            alt_route = alternative[
                "route"
            ]

            alt_live = alternative[
                "live"
            ]

            alt_vehicle = alt_live.get(
                "best_vehicle"
            )

            if alt_vehicle:

                status = (
                    f"🟢 {alt_vehicle['eta_low']}–"
                    f"{alt_vehicle['eta_high']} хв"
                )

            elif (
                alt_live[
                    "route_vehicle_count"
                ] > 0
            ):

                status = "🟡 GPS є"

            else:

                status = "⚪ без GPS"

            lines.append(
                f"• №{alt_route['number']} — "
                f"{round(alternative['origin']['distance'])} м "
                f"до посадки / "
                f"{round(alternative['destination']['distance'])} м "
                f"після виходу / "
                f"{status}"
            )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━",
            (
                f"🕒 Оновлено: "
                f"{updated_at.strftime('%H:%M:%S')}"
            ),
            "",
            "🛰 «Де зараз транспорт?» — "
            "оновити GPS.\n"
            "🚌 «Всі варіанти» — "
            "подивитися всі знайдені прямі маршрути.",
        ]
    )

    return "\n".join(lines)


# ==================================================
# ALL ROUTES
# ==================================================

def format_all_routes(
    destination_text,
    options,
):
    lines = [
        "🚌 ВСІ ВАРІАНТИ",
        "━━━━━━━━━━━━━━",
        f"🏁 Куди: {destination_text}",
        "",
    ]

    for index, option in enumerate(
        options,
        start=1,
    ):
        route = option["route"]
        live = option["live"]

        if index == 1:
            marker = "⭐"
        else:
            marker = f"{index}️⃣" if index <= 9 else "•"

        lines.extend(
            [
                (
                    f"{marker} МАРШРУТ №"
                    f"{route['number']}"
                ),
                (
                    f"🚶 До посадки: "
                    f"{round(option['origin']['distance'])} м"
                ),
                (
                    f"🚏 «{option['origin']['name']}»"
                ),
                (
                    f"🚌 Їхати: "
                    f"{option['stop_count']} "
                    f"{plural_stops(option['stop_count'])}"
                ),
                (
                    f"🏁 Виходити: "
                    f"«{option['destination']['name']}»"
                ),
                (
                    f"🚶 Після виходу: "
                    f"{round(option['destination']['distance'])} м"
                ),
            ]
        )

        vehicle_info = live.get(
            "best_vehicle"
        )

        if vehicle_info:

            vehicle = vehicle_info[
                "vehicle"
            ]

            lines.extend(
                [
                    (
                        f"🟢 Транспорт наближається: "
                        f"≈ {vehicle_info['eta_low']}–"
                        f"{vehicle_info['eta_high']} хв"
                    ),
                    (
                        f"🚍 Машина: "
                        f"{vehicle['gov_number'] or '—'}"
                    ),
                ]
            )

        elif live[
            "route_vehicle_count"
        ] > 0:

            lines.append(
                "🟡 GPS є, але напрямок "
                "машини не підтверджений"
            )

        else:

            lines.append(
                "⚪ Свіжого GPS зараз немає"
            )

        lines.append("")

    lines.extend(
        [
            "━━━━━━━━━━━━━━",
            (
                f"Знайдено прямих варіантів: "
                f"{len(options)}"
            ),
            "",
            "⭐ Перший — той, який бот "
            "зараз вважає найкращим.",
        ]
    )

    return "\n".join(lines)


# ==================================================
# LIVE REFRESH
# ==================================================

async def refresh_saved_route(
    user_data,
):
    saved = user_data.get(
        "selected_route"
    )

    if not saved:
        return None

    vehicles = await get_live_vehicles()

    live = analyze_live_route(
        saved,
        vehicles,
    )

    saved["live"] = live

    best_vehicle = live.get(
        "best_vehicle"
    )

    if best_vehicle:

        saved["wait_minutes"] = (
            best_vehicle[
                "eta_mid"
            ]
        )

    saved["total_minutes"] = (
        saved.get(
            "walk_to_minutes",
            0,
        )
        + saved.get(
            "wait_minutes",
            0,
        )
        + saved.get(
            "ride_minutes",
            0,
        )
        + saved.get(
            "walk_after_minutes",
            0,
        )
    )

    return {
        "route": saved,
        "updated_at": now(),
    }


def format_live_refresh(
    route_data,
    updated_at,
):
    route = route_data[
        "route"
    ]

    live = route_data[
        "live"
    ]

    lines = [
        "🛰 ДЕ ЗАРАЗ ТРАНСПОРТ?",
        "━━━━━━━━━━━━━━",
        (
            f"🚌 Маршрут №"
            f"{route['number']}"
        ),
        (
            f"🚏 Твоя зупинка: "
            f"«{route_data['origin']['name']}»"
        ),
        "",
    ]

    vehicle_info = live.get(
        "best_vehicle"
    )

    if vehicle_info:

        vehicle = vehicle_info[
            "vehicle"
        ]

        stops_before = vehicle_info[
            "stops_before"
        ]

        lines.append(
            f"🟢 Машина №"
            f"{vehicle['gov_number'] or '—'}"
        )

        lines.append(
            f"📍 Зараз біля "
            f"«{vehicle_info['nearest_route_stop']['name']}»"
        )

        if stops_before == 0:

            lines.append(
                "🚏 Вона вже біля твоєї зупинки."
            )

        else:

            lines.append(
                f"🚏 До тебе ще ≈ "
                f"{stops_before} "
                f"{plural_stops(stops_before)}"
            )

        lines.append(
            f"📏 Залишилось по маршруту ≈ "
            f"{format_distance(vehicle_info['route_distance'])}"
        )

        lines.append(
            f"⏱ Прибуття: ≈ "
            f"{vehicle_info['eta_low']}–"
            f"{vehicle_info['eta_high']} хв"
        )

        if vehicle[
            "speed"
        ] > 0:

            lines.append(
                f"💨 Швидкість зараз: "
                f"{round(vehicle['speed'])} км/год"
            )

        lines.append(
            f"🕒 GPS: "
            f"{format_age(vehicle['age'])}"
        )

        lines.extend(
            [
                "",
                leave_advice(
                    vehicle_info[
                        "eta_low"
                    ],
                    vehicle_info[
                        "eta_high"
                    ],
                    route_data[
                        "walk_to_minutes"
                    ],
                ),
            ]
        )

    elif live[
        "route_vehicle_count"
    ] > 0:

        lines.extend(
            [
                "🟡 GPS маршруту працює,",
                (
                    "але зараз не можу надійно "
                    "визначити машину, яка "
                    "наближається саме до твоєї зупинки."
                ),
            ]
        )

    else:

        lines.extend(
            [
                (
                    f"⚪ Свіжої GPS-позиції "
                    f"маршруту №{route['number']} "
                    "зараз немає."
                ),
                "",
                "Спробуй перевірити ще раз трохи пізніше.",
            ]
        )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━",
            (
                f"🕒 Перевірено: "
                f"{updated_at.strftime('%H:%M:%S')}"
            ),
        ]
    )

    return "\n".join(lines)


# ==================================================
# ADMIN
# ==================================================

async def get_database_diagnostics():
    if not DATABASE_URL:
        return {
            "env": "❌ відсутній",
            "host": "—",
            "port": "—",
            "dns": "❌ не перевірявся",
            "connection": "❌ неможливо перевірити",
            "error": "DATABASE_URL не заданий",
        }

    try:
        parsed = urlparse(DATABASE_URL)
        host = parsed.hostname or "невідомий"
        port = parsed.port or 5432
    except Exception as exc:
        return {
            "env": "✅ отримано",
            "host": "не вдалося прочитати",
            "port": "—",
            "dns": "❌ не перевірявся",
            "connection": "❌ помилка URL",
            "error": f"{type(exc).__name__}: {exc}",
        }

    dns_status = "❌ помилка"
    dns_error = None
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=8,
        )
        families = {item[0] for item in infos}
        family_names = []
        if socket.AF_INET in families:
            family_names.append("IPv4")
        if socket.AF_INET6 in families:
            family_names.append("IPv6")
        dns_status = "✅ " + (" + ".join(family_names) or "адресу знайдено")
    except Exception as exc:
        dns_error = f"{type(exc).__name__}: {exc}"

    connection_status = "❌ не підключено"
    connection_error = None
    test_conn = None
    try:
        test_conn = await asyncio.wait_for(
            asyncpg.connect(
                DATABASE_URL,
                command_timeout=10,
                statement_cache_size=0,
                ssl="require",
            ),
            timeout=15,
        )
        await test_conn.execute("SELECT 1")
        connection_status = "✅ PostgreSQL доступний"
    except Exception as exc:
        connection_error = f"{type(exc).__name__}: {exc}"
    finally:
        if test_conn is not None:
            try:
                await test_conn.close()
            except Exception:
                pass

    error = connection_error or dns_error or db_last_error or "немає"
    # Не дозволяємо випадково вивести пароль, навіть якщо драйвер включить URL у текст помилки.
    if DATABASE_URL and DATABASE_URL in error:
        error = error.replace(DATABASE_URL, "[DATABASE_URL приховано]")
    if len(error) > 500:
        error = error[:500] + "…"

    return {
        "env": "✅ отримано",
        "host": host,
        "port": str(port),
        "dns": dns_status,
        "connection": connection_status,
        "error": error,
    }


async def send_admin_debug(
    message: Message
):
    vehicles, db_diag = await asyncio.gather(
        get_live_vehicles(),
        get_database_diagnostics(),
    )

    active_route_numbers = {
        vehicle["route_short_name"]
        for vehicle in vehicles
        if vehicle.get("route_short_name")
    }

    await message.answer(
        "🔧 ДЕБАГ\n"
        "━━━━━━━━━━━━━━\n\n"
        "📦 GTFS\n"
        f"🚏 Зупинок: {len(stops)}\n"
        f"🚌 Маршрутів: {len(routes)}\n"
        f"🚍 Trips: {len(trips)}\n"
        f"🧭 Послідовностей: {len(trip_sequences)}\n\n"
        "🛰 REALTIME\n"
        f"🚍 Свіжих GPS-машин: {len(vehicles)}\n"
        f"🚌 Маршрутів із GPS: {len(active_route_numbers)}\n"
        f"⏱ Ліміт свіжості GPS: {MAX_GPS_AGE_SECONDS // 60} хв\n\n"
        "🗄 БАЗА ДАНИХ\n"
        f"DATABASE_URL: {db_diag['env']}\n"
        f"Host: {db_diag['host']}\n"
        f"Port: {db_diag['port']}\n"
        f"DNS: {db_diag['dns']}\n"
        f"PostgreSQL: {db_diag['connection']}\n"
        f"Помилка: {db_diag['error']}\n\n"
        "👤 ПОТОЧНА СЕСІЯ\n"
        f"Користувачів у пам'яті: {len(users)}\n\n"
        f"🕒 Перевірено: {now().strftime('%d.%m.%Y %H:%M:%S')}",
        reply_markup=admin_keyboard,
    )


@dp.message(F.text == "/admin")
async def admin_handler(
    message: Message
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await message.answer(
        "🛠 АДМІН-ПАНЕЛЬ\n"
        "━━━━━━━━━━━━━━\n\n"
        "Привіт, Богдане 👋\n\n"
        "📊 Статистика — показники бота\n"
        "👥 Користувачі — база користувачів\n"
        "📢 Розсилка — повідомлення користувачам\n"
        "🔧 Дебаг — стан GTFS та GPS\n"
        "📝 Форми та опитування — запитання й відповіді\n\n"
        "Обери потрібний розділ 👇",
        reply_markup=admin_keyboard,
    )


@dp.message(F.text == "/debug")
async def debug_handler(
    message: Message
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await send_admin_debug(
        message
    )


@dp.message(F.text == "🔧 Дебаг")
async def admin_debug_handler(
    message: Message
):
    if not is_admin(
        message.from_user.id
    ):
        return

    await send_admin_debug(
        message
    )


@dp.message(F.text == "📊 Статистика")
async def admin_statistics_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    stats = await get_admin_stats()
    if not stats:
        await message.answer(
            "📊 СТАТИСТИКА\n━━━━━━━━━━━━━━\n\n"
            "⚠️ База даних зараз недоступна.\n"
            "Сам транспортний бот продовжує працювати.",
            reply_markup=admin_keyboard,
        )
        return
    await message.answer(
        "📊 СТАТИСТИКА\n━━━━━━━━━━━━━━\n\n"
        f"👥 Усього користувачів: {stats['total_users']}\n"
        f"🟢 Активні сьогодні: {stats['active_today']}\n"
        f"🆕 Нові сьогодні: {stats['new_today']}\n"
        f"📅 Нові за 7 днів: {stats['new_7']}\n"
        f"🗓 Нові за 30 днів: {stats['new_30']}\n\n"
        f"🧭 Побудовано маршрутів: {stats['routes_total']}\n"
        f"↳ сьогодні: {stats['routes_today']}\n"
        f"🛰 Перевірок транспорту: {stats['gps_total']}\n"
        f"🚌 Натискань «Всі варіанти»: {stats['all_routes_total']}\n\n"
        f"💾 Сесій у пам'яті зараз: {len(users)}",
        reply_markup=admin_keyboard,
    )


@dp.message(F.text == "👥 Користувачі")
async def admin_users_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = await get_recent_users(10)
    if not db_pool:
        await message.answer(
            "👥 КОРИСТУВАЧІ\n━━━━━━━━━━━━━━\n\n⚠️ База даних зараз недоступна.",
            reply_markup=admin_keyboard,
        )
        return
    if not rows:
        await message.answer(
            "👥 КОРИСТУВАЧІ\n━━━━━━━━━━━━━━\n\nПоки база порожня.\n"
            "Користувачі з'являтимуться тут після взаємодії з оновленим ботом.",
            reply_markup=admin_keyboard,
        )
        return
    lines = ["👥 КОРИСТУВАЧІ", "━━━━━━━━━━━━━━", "", "Останні 10 за активністю:", ""]
    for row in rows:
        name = " ".join(filter(None, [row["first_name"], row["last_name"]])) or "Без імені"
        username = f"@{row['username']}" if row["username"] else "без username"
        last_seen = row["last_seen"].astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
        blocked = " 🚫" if row["is_blocked"] else ""
        lines.append(f"• {name}{blocked}\n  {username} · ID {row['telegram_id']}\n  🕒 {last_seen}")
    await message.answer("\n".join(lines), reply_markup=admin_keyboard)


@dp.message(F.text == "📢 Розсилка")
async def admin_broadcast_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not db_pool:
        await message.answer(
            "📢 РОЗСИЛКА\n━━━━━━━━━━━━━━\n\n⚠️ База даних зараз недоступна.",
            reply_markup=admin_keyboard,
        )
        return
    admin_broadcast_drafts[message.from_user.id] = {"state": "waiting_text"}
    await message.answer(
        "📢 РОЗСИЛКА\n━━━━━━━━━━━━━━\n\n"
        "Напиши текст повідомлення, яке потрібно надіслати користувачам.\n\n"
        "Я спочатку покажу попередній перегляд — без підтвердження нічого не відправиться.\n\n"
        "Для скасування напиши: Скасувати",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(F.text == "✅ Надіслати розсилку")
async def admin_broadcast_confirm_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    draft = admin_broadcast_drafts.get(message.from_user.id)
    if not draft or draft.get("state") != "confirm" or not draft.get("text"):
        await message.answer("Немає підготовленої розсилки.", reply_markup=admin_keyboard)
        return
    recipient_ids = await get_broadcast_user_ids()
    if not recipient_ids:
        admin_broadcast_drafts.pop(message.from_user.id, None)
        await message.answer("У базі поки немає одержувачів.", reply_markup=admin_keyboard)
        return
    status = await message.answer(f"📨 Починаю розсилку для {len(recipient_ids)} користувачів…")
    sent = 0
    blocked = 0
    failed = 0
    for telegram_id in recipient_ids:
        try:
            await bot.send_message(telegram_id, draft["text"])
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
            await mark_user_blocked(telegram_id)
        except TelegramBadRequest:
            failed += 1
        except Exception as exc:
            failed += 1
            print(f"BROADCAST ERROR {telegram_id}: {exc}", flush=True)
        await asyncio.sleep(0.05)
    admin_broadcast_drafts.pop(message.from_user.id, None)
    await status.edit_text(
        "📨 РОЗСИЛКА ЗАВЕРШЕНА\n━━━━━━━━━━━━━━\n\n"
        f"✅ Надіслано: {sent}\n🚫 Заблокували бота: {blocked}\n⚠️ Помилок: {failed}"
    )
    await message.answer("Адмін-панель 👇", reply_markup=admin_keyboard)


@dp.message(F.text == "❌ Скасувати розсилку")
async def admin_broadcast_cancel_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    admin_broadcast_drafts.pop(message.from_user.id, None)
    await message.answer("❌ Розсилку скасовано.", reply_markup=admin_keyboard)


@dp.message(
    F.text == "❌ Закрити адмін-панель"
)
async def close_admin_handler(
    message: Message
):
    if not is_admin(
        message.from_user.id
    ):
        return

    user_data = users.get(
        message.from_user.id
    )

    if (
        user_data
        and user_data.get(
            "selected_route"
        )
    ):
        keyboard = route_keyboard
    else:
        keyboard = start_keyboard

    await message.answer(
        "✅ Адмін-панель закрито.",
        reply_markup=keyboard,
    )


# ==================================================
# START
# ==================================================

@dp.message(CommandStart())
async def start_handler(
    message: Message
):
    await track_user(message)
    await log_event(message.from_user.id, "start")

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
        "Скажи, звідки вирушаєш — "
        "я допоможу знайти зручний транспорт.\n\n"
        "📍 знайду найближчу зупинку\n"
        "🚌 підберу маршрут\n"
        "🛰 знайду транспорт наживо\n"
        "⏱ оцінимо, коли він буде\n"
        "⚡ підкажу, коли виходити\n"
        "🚏 скажу, де вийти\n\n"
        "Звідки стартуємо? 👇",
        reply_markup=start_keyboard,
    )


# ==================================================
# LOCATION
# ==================================================

@dp.message(F.location)
async def location_handler(
    message: Message
):
    await track_user(message)
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
        "latitude": latitude,
        "longitude": longitude,
        "origin_stops": origin_stops,
    }

    if origin_stops:

        nearest = origin_stops[0]

        await message.answer(
            "📍 Знайшов тебе ✓\n"
            "━━━━━━━━━━━━━━\n"
            f"Найближча зупинка — "
            f"«{nearest['name']}»\n"
            f"🚶 {round(nearest['distance'])} м "
            f"≈ {walking_minutes(nearest['distance'])} хв\n\n"
            "Тепер найцікавіше 😎\n"
            "🏁 Куди їдемо?",
            reply_markup=ReplyKeyboardRemove(),
        )

    else:

        await message.answer(
            "📍 Геолокацію отримав ✓\n\n"
            "Поруч не знайшов зупинки.\n\n"
            "Все одно напиши, куди їдемо 👇",
            reply_markup=ReplyKeyboardRemove(),
        )


# ==================================================
# WHERE IS TRANSPORT
# ==================================================

@dp.message(
    F.text == "🛰 Де зараз транспорт?"
)
async def where_transport_handler(
    message: Message
):
    await track_user(message)
    await log_event(message.from_user.id, "live_gps")
    user_id = message.from_user.id

    user_data = users.get(user_id)

    if not user_data:

        await message.answer(
            "Спочатку побудуй маршрут 👇",
            reply_markup=start_keyboard,
        )
        return

    if not user_data.get(
        "selected_route"
    ):

        await message.answer(
            "Спочатку потрібно знайти маршрут.\n\n"
            "Натисни «🧭 Новий маршрут».",
            reply_markup=route_keyboard,
        )
        return

    status_message = await message.answer(
        "🛰 Перевіряю, де зараз транспорт..."
    )

    refreshed = await refresh_saved_route(
        user_data
    )

    if not refreshed:

        await status_message.edit_text(
            "Не вдалося оновити маршрут."
        )
        return

    text = format_live_refresh(
        refreshed["route"],
        refreshed["updated_at"],
    )

    await status_message.edit_text(
        text
    )


# ==================================================
# ALL ROUTES BUTTON
# ==================================================

@dp.message(
    F.text == "🚌 Всі варіанти"
)
async def all_routes_handler(
    message: Message
):
    await track_user(message)
    await log_event(message.from_user.id, "all_routes")
    user_id = message.from_user.id

    user_data = users.get(user_id)

    if not user_data:

        await message.answer(
            "Спочатку побудуй маршрут 👇",
            reply_markup=start_keyboard,
        )
        return

    options = user_data.get(
        "route_options"
    )

    destination_text = user_data.get(
        "destination_text",
        "пункт призначення",
    )

    if not options:

        await message.answer(
            "Спочатку потрібно знайти маршрут.",
            reply_markup=route_keyboard,
        )
        return

    text = format_all_routes(
        destination_text,
        options,
    )

    await message.answer(
        text,
        reply_markup=route_keyboard,
    )


# ==================================================
# TEXT
# ==================================================

@dp.message(F.text)
async def form_text_input_handler(message: Message):
    uid = message.from_user.id
    text = message.text.strip()
    draft = form_drafts.get(uid) if is_admin(uid) else None
    if draft:
        if text.casefold() == 'скасувати':
            form_drafts.pop(uid, None)
            await message.answer("Скасовано", reply_markup=admin_keyboard)
            return
        if not text or len(text) > 1000:
            await message.answer("Введи текст від 1 до 1000 символів")
            return
        action = draft['action']
        fid = draft.get('id')
        if action == 'new':
            fid = await db_pool.fetchval("INSERT INTO bot_forms(question,mode) VALUES($1,'text') RETURNING id", text)
        elif action == 'edit':
            await db_pool.execute("UPDATE bot_forms SET question=$2 WHERE id=$1 AND status='draft'", fid, text)
        elif action == 'add':
            row = await form_get(fid)
            opts = row['options']
            if isinstance(opts, str):
                opts = json.loads(opts)
            if len(opts) >= 8:
                await message.answer("Максимум 8 варіантів")
                return
            opts.append(text)
            await db_pool.execute("UPDATE bot_forms SET options=$2::jsonb,mode='mixed' WHERE id=$1 AND status='draft'", fid, json.dumps(opts, ensure_ascii=False))
        form_drafts.pop(uid, None)
        await form_show(uid, fid)
        return
    fid = form_text_waiting.get(uid)
    if fid is None:
        return
    if text.casefold() == 'скасувати':
        form_text_waiting.pop(uid, None)
        await message.answer("Введення відповіді скасовано")
        return
    row = await form_get(fid)
    form_text_waiting.pop(uid, None)
    if not row or row['status'] not in ('active', 'sending'):
        await message.answer("Це опитування вже завершено")
        return
    if len(text) > 2000:
        await message.answer("Відповідь завелика (максимум 2000 символів)")
        return
    await db_pool.execute("""INSERT INTO bot_form_answers(form_id,telegram_id,answer_type,answer_text)
        VALUES($1,$2,'text',$3) ON CONFLICT(form_id,telegram_id) DO UPDATE SET
        answer_type='text',answer_text=excluded.answer_text,answered_at=now()""", fid, uid, text)
    await message.answer("✅ Дякуємо! Відповідь збережено.")



@dp.message(F.text)
async def text_handler(
    message: Message
):
    await track_user(message)
    user_id = message.from_user.id
    text = message.text.strip()

    if is_admin(user_id):
        draft = admin_broadcast_drafts.get(user_id)
        if draft and draft.get("state") == "waiting_text":
            if text.casefold() == "скасувати":
                admin_broadcast_drafts.pop(user_id, None)
                await message.answer("❌ Розсилку скасовано.", reply_markup=admin_keyboard)
                return
            admin_broadcast_drafts[user_id] = {"state": "confirm", "text": text}
            confirm_keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="✅ Надіслати розсилку")],
                    [KeyboardButton(text="❌ Скасувати розсилку")],
                ],
                resize_keyboard=True,
            )
            await message.answer(
                "👁 ПОПЕРЕДНІЙ ПЕРЕГЛЯД\n━━━━━━━━━━━━━━\n\n" + text +
                "\n\n━━━━━━━━━━━━━━\nНадіслати це всім активним користувачам?",
                reply_markup=confirm_keyboard,
            )
            return

    if text == "🧭 Новий маршрут":

        users[user_id] = {
            "step": "waiting_origin"
        }

        await message.answer(
            "🧭 Новий маршрут\n\n"
            "Звідки вирушаємо? 👇",
            reply_markup=start_keyboard,
        )

        return

    if text == "🔎 Ввести адресу":

        users[user_id] = {
            "step": "waiting_origin_address"
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
            "👋 Натисни /start — "
            "і побудуємо маршрут."
        )
        return

    step = users[
        user_id
    ].get("step")

    if step == "waiting_origin_address":

        await message.answer(
            "🔎 Шукаю адресу..."
        )

        result = await geocode_address(
            text
        )

        if not result:

            await message.answer(
                "🤔 Не зміг знайти цю адресу.\n\n"
                "Спробуй написати вулицю "
                "та номер будинку."
            )
            return

        origin_stops = nearby_stops(
            result["latitude"],
            result["longitude"],
        )

        users[user_id] = {
            "step": "waiting_destination",
            "latitude": result["latitude"],
            "longitude": result["longitude"],
            "origin_stops": origin_stops,
        }

        if origin_stops:

            nearest = origin_stops[0]

            await message.answer(
                "📍 Знайшов ✓\n"
                "━━━━━━━━━━━━━━\n"
                f"Найближча зупинка — "
                f"«{nearest['name']}»\n"
                f"🚶 {round(nearest['distance'])} м "
                f"≈ {walking_minutes(nearest['distance'])} хв\n\n"
                "🏁 Куди їдемо?"
            )

        else:

            await message.answer(
                "📍 Адресу знайшов ✓\n\n"
                "🏁 Тепер напиши, куди їдемо."
            )

        return

    if step == "waiting_destination":

        searching = await message.answer(
            "🧭 Шукаю найкращий маршрут...\n"
            "🛰 Перевіряю транспорт наживо."
        )

        destination = await geocode_address(
            text
        )

        if not destination:

            await searching.edit_text(
                "🤔 Не знайшов цю адресу.\n\n"
                "Спробуй написати точніше, "
                "наприклад: Височана 18."
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

        result = await build_route_result(
            text,
            origin_stops,
            destination_stops,
        )

        if (
            not result
            or not result["routes"]
        ):

            users[
                user_id
            ]["step"] = "done"

            await searching.edit_text(
                "🤔 Зручного прямого маршруту "
                "поруч не знайшов.\n\n"
                "Ймовірно, сюди краще їхати "
                "з пересадкою."
            )

            await message.answer(
                "Що робимо далі? 👇",
                reply_markup=route_keyboard,
            )

            return

        options = result["routes"]
        best = options[0]

        users[user_id][
            "selected_route"
        ] = best

        users[user_id][
            "route_options"
        ] = options

        users[user_id][
            "destination_text"
        ] = text

        users[user_id][
            "destination"
        ] = destination

        users[user_id][
            "step"
        ] = "route_ready"

        await log_event(user_id, "route_built")

        formatted = format_route_result(
            text,
            options,
            result["updated_at"],
        )

        await searching.edit_text(
            formatted
        )

        await message.answer(
            "Що хочеш перевірити? 👇",
            reply_markup=route_keyboard,
        )

        return

    await message.answer(
        "Обери дію нижче 👇",
        reply_markup=route_keyboard,
    )


# ==================================================
# UNIVERSAL FORMS AND POLLS (admin-only management)
# ==================================================

form_drafts = {}
form_text_waiting = {}
form_sending_tasks = {}


def form_admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Створити форму", callback_data="fm:new")],
        [InlineKeyboardButton(text="📋 Мої форми", callback_data="fm:list")],
    ])


def form_choice_keyboard(form_id, options, mode):
    buttons = [[InlineKeyboardButton(text=str(option)[:60], callback_data=f"fa:{form_id}:{i}")]
               for i, option in enumerate(options)]
    if mode in ("mixed", "text"):
        buttons.append([InlineKeyboardButton(text="✍️ Власна відповідь", callback_data=f"fa:{form_id}:t")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def form_manage_keyboard(form_id, status):
    buttons = []
    if status == "draft":
        buttons += [[InlineKeyboardButton(text="✏️ Змінити запитання", callback_data=f"fm:edit:{form_id}")],
                    [InlineKeyboardButton(text="➕ Додати варіант", callback_data=f"fm:add:{form_id}")],
                    [InlineKeyboardButton(text="🧹 Прибрати всі варіанти", callback_data=f"fm:clear:{form_id}")],
                    [InlineKeyboardButton(text="🚀 Почати розсилку", callback_data=f"fm:send:{form_id}")]]
    else:
        buttons.append([InlineKeyboardButton(text="📊 Відповіді", callback_data=f"fm:answers:{form_id}")])
        if status in ("active", "sending"):
            buttons.append([InlineKeyboardButton(text="⏹ Зупинити", callback_data=f"fm:stop:{form_id}")])
        if status == "sending":
            buttons.append([InlineKeyboardButton(text="❌ Скасувати розсилку", callback_data=f"fm:cancel:{form_id}")])
        buttons.append([InlineKeyboardButton(text="🗑 Видалити повідомлення", callback_data=f"fm:delete:{form_id}")])
    buttons.append([InlineKeyboardButton(text="📋 Усі форми", callback_data="fm:list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def form_get(form_id):
    return await db_pool.fetchrow("SELECT * FROM bot_forms WHERE id=$1", form_id)


async def form_show(chat_id, form_id):
    form = await form_get(form_id)
    if not form:
        await bot.send_message(chat_id, "Форму не знайдено.")
        return
    count = await db_pool.fetchval("SELECT count(*) FROM bot_form_answers WHERE form_id=$1", form_id)
    sent = await db_pool.fetchval("SELECT count(*) FROM bot_form_deliveries WHERE form_id=$1 AND status='sent'", form_id)
    opts = form['options']
    if isinstance(opts, str):
        opts = json.loads(opts)
    desc = "\n".join(f"{i+1}. {v}" for i, v in enumerate(opts)) or "(лише текстова відповідь)"
    await bot.send_message(chat_id, f"📝 Форма #{form_id}\n\n{form['question']}\n\n{desc}\n\nСтатус: {form['status']}\n📨 Надіслано: {sent}\n💬 Відповіли: {count}", reply_markup=form_manage_keyboard(form_id, form['status']))


async def form_broadcast(form_id):
    try:
        recipients = await get_broadcast_user_ids()
        for uid in recipients:
            existing = await db_pool.fetchval("SELECT status FROM bot_form_deliveries WHERE form_id=$1 AND telegram_id=$2", form_id, uid)
            if existing in ("sent", "deleted"):
                continue
            row = await form_get(form_id)
            if not row or row['status'] != 'sending':
                break
            options = row['options']
            if isinstance(options, str):
                options = json.loads(options)
            mode = row['mode']
            try:
                msg = await bot.send_message(uid, "📝 Запитання від «МЕНІ ТУДИ»\n\n" + row['question'], reply_markup=form_choice_keyboard(form_id, options, mode))
                await db_pool.execute("""INSERT INTO bot_form_deliveries(form_id,telegram_id,message_id,status,delivered_at)
                    VALUES($1,$2,$3,'sent',now()) ON CONFLICT(form_id,telegram_id) DO UPDATE
                    SET message_id=excluded.message_id,status='sent',delivered_at=now()""", form_id, uid, msg.message_id)
            except TelegramForbiddenError:
                await mark_user_blocked(uid)
                await db_pool.execute("INSERT INTO bot_form_deliveries(form_id,telegram_id,status) VALUES($1,$2,'failed') ON CONFLICT(form_id,telegram_id) DO UPDATE SET status='failed'", form_id, uid)
            except Exception as exc:
                print(f"FORM SEND {form_id} {uid}: {exc}", flush=True)
            await asyncio.sleep(0.075)
        await db_pool.execute("UPDATE bot_forms SET status='active' WHERE id=$1 AND status='sending'", form_id)
        await bot.send_message(ADMIN_ID, f"📨 Розсилку форми #{form_id} завершено або зупинено.")
    except Exception as exc:
        print(f"FORM BROADCAST FAILED {form_id}: {exc}", flush=True)
        await bot.send_message(ADMIN_ID, f"⚠️ Помилка розсилки форми #{form_id}. Перевір логи Northflank.")
    finally:
        form_sending_tasks.pop(form_id, None)


@dp.message(F.text == "📝 Форми та опитування")
async def form_admin_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not db_pool:
        await message.answer("⚠️ Supabase недоступна.")
        return
    await message.answer("📝 Форми та опитування", reply_markup=form_admin_menu())


@dp.callback_query(F.data.startswith("fm:"))
async def form_admin_callback(query: CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    if not db_pool:
        await bot.send_message(query.from_user.id, "⚠️ База недоступна")
        return
    parts = query.data.split(":")
    action = parts[1]
    fid = int(parts[2]) if len(parts) > 2 else None
    if action == "new":
        form_drafts[query.from_user.id] = {"action": "new"}
        await bot.send_message(query.from_user.id, "Напиши запитання для нової форми. Для виходу напиши «Скасувати».")
        return
    if action == "list":
        rows = await db_pool.fetch("SELECT id,question,status FROM bot_forms ORDER BY id DESC LIMIT 15")
        if not rows:
            await bot.send_message(query.from_user.id, "Форм поки немає.", reply_markup=form_admin_menu())
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"#{r['id']} {r['question'][:32]} · {r['status']}", callback_data=f"fm:view:{r['id']}")] for r in rows])
        await bot.send_message(query.from_user.id, "📋 Останні форми:", reply_markup=kb)
        return
    row = await form_get(fid)
    if not row:
        await bot.send_message(query.from_user.id, "Форму не знайдено")
        return
    if action == "view":
        await form_show(query.from_user.id, fid)
    elif action in ("edit", "add") and row['status'] == 'draft':
        form_drafts[query.from_user.id] = {"action": action, "id": fid}
        await bot.send_message(query.from_user.id, "Напиши нове запитання:" if action == "edit" else "Напиши текст нового варіанта відповіді:")
    elif action == "clear" and row['status'] == 'draft':
        await db_pool.execute("UPDATE bot_forms SET options='[]'::jsonb,mode='text' WHERE id=$1", fid)
        await form_show(query.from_user.id, fid)
    elif action == "send" and row['status'] == 'draft':
        await bot.send_message(query.from_user.id, "⚠️ Надіслати форму всім активним користувачам?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Так, розіслати", callback_data=f"fm:confirm:{fid}")],
            [InlineKeyboardButton(text="Ні", callback_data=f"fm:view:{fid}")]]))
    elif action == "confirm" and row['status'] == 'draft':
        await db_pool.execute("UPDATE bot_forms SET status='sending',started_at=now() WHERE id=$1 AND status='draft'", fid)
        form_sending_tasks[fid] = asyncio.create_task(form_broadcast(fid))
        await bot.send_message(query.from_user.id, f"🚀 Розсилка форми #{fid} почалася.")
    elif action in ("stop", "cancel") and row['status'] in ('active', 'sending'):
        await db_pool.execute("UPDATE bot_forms SET status=$2,stopped_at=now() WHERE id=$1", fid, 'cancelled' if action == 'cancel' else 'stopped')
        await bot.send_message(query.from_user.id, "⏹ Форму зупинено. Нові відповіді не приймаються; незавершена розсилка припиняється.")
    elif action == "answers":
        rows = await db_pool.fetch("""SELECT a.telegram_id,a.answer_text,a.answered_at,u.username,u.first_name
            FROM bot_form_answers a LEFT JOIN bot_users u ON u.telegram_id=a.telegram_id
            WHERE a.form_id=$1 ORDER BY a.answered_at DESC LIMIT 30""", fid)
        if not rows:
            await bot.send_message(query.from_user.id, "Відповідей поки немає.")
            return
        lines = [f"📊 Відповіді на форму #{fid} (останні 30):"]
        for r in rows:
            who = (r['first_name'] or 'Користувач') + (f" @{r['username']}" if r['username'] else '')
            lines.append(f"\n{who} · ID {r['telegram_id']}\n↳ {r['answer_text'][:350]}")
        text = "\n".join(lines)
        for start in range(0, len(text), 3500):
            await bot.send_message(query.from_user.id, text[start:start+3500])
    elif action == "delete":
        await bot.send_message(query.from_user.id, "⚠️ Спробувати видалити надіслані повідомлення? Telegram зазвичай дозволяє це лише протягом 48 годин.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Так, видалити", callback_data=f"fm:delconfirm:{fid}")],
            [InlineKeyboardButton(text="Ні", callback_data=f"fm:view:{fid}")]]))
    elif action == "delconfirm":
        await db_pool.execute("UPDATE bot_forms SET status='cancelled',stopped_at=now() WHERE id=$1", fid)
        rows = await db_pool.fetch("SELECT telegram_id,message_id FROM bot_form_deliveries WHERE form_id=$1 AND status='sent' AND message_id IS NOT NULL", fid)
        deleted = 0
        for r in rows:
            try:
                await bot.delete_message(r['telegram_id'], r['message_id'])
                await db_pool.execute("UPDATE bot_form_deliveries SET status='deleted' WHERE form_id=$1 AND telegram_id=$2", fid, r['telegram_id'])
                deleted += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await bot.send_message(query.from_user.id, f"🗑 Видалено {deleted} із {len(rows)} повідомлень. Відповіді в базі збережені.")


@dp.callback_query(F.data.startswith("fa:"))
async def form_answer_callback(query: CallbackQuery):
    if not db_pool:
        await query.answer("Тимчасово недоступно", show_alert=True)
        return
    _, fid_s, selection = query.data.split(":", 2)
    fid = int(fid_s)
    row = await form_get(fid)
    if not row or row['status'] not in ('active', 'sending'):
        await query.answer("Опитування завершене", show_alert=True)
        return
    if selection == 't':
        form_text_waiting[query.from_user.id] = fid
        await query.answer()
        await bot.send_message(query.from_user.id, f"✍️ Напиши свою відповідь на запитання:\n\n{row['question']}\n\nДля скасування напиши «Скасувати».")
        return
    options = row['options']
    if isinstance(options, str):
        options = json.loads(options)
    try:
        answer = str(options[int(selection)])
    except (IndexError, ValueError):
        await query.answer("Невідомий варіант", show_alert=True)
        return
    await db_pool.execute("""INSERT INTO bot_form_answers(form_id,telegram_id,answer_type,answer_text)
        VALUES($1,$2,'choice',$3) ON CONFLICT(form_id,telegram_id) DO UPDATE SET
        answer_type='choice',answer_text=excluded.answer_text,answered_at=now()""", fid, query.from_user.id, answer)
    await query.answer("Відповідь збережена!", show_alert=True)



# ==================================================
# MAIN
# ==================================================

async def main():
    print(
        "MENI-TUDY ADMIN START",
        flush=True,
    )

    await init_database()
    if db_pool:
        try:
            pending = await db_pool.fetch("SELECT id FROM bot_forms WHERE status='sending'")
            for record in pending:
                fid = record['id']
                form_sending_tasks[fid] = asyncio.create_task(form_broadcast(fid))
        except Exception as exc:
            print(f"FORMS RECOVERY ERROR: {exc}", flush=True)

    loaded = await load_gtfs()

    if not loaded:

        print(
            "WARNING: GTFS NOT LOADED",
            flush=True,
        )

    print(
        "BOT POLLING START",
        flush=True,
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message", "callback_query"
            ],
        )
    finally:
        if db_pool:
            await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
