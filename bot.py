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


# ==================================================
# BASIC HELPERS
# ==================================================

def now():
    return datetime.now(KYIV_TZ)


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
# ALL ROUTES — NEW
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
# DEBUG
# ==================================================

@dp.message(F.text == "/debug")
async def debug_handler(
    message: Message
):
    vehicles = await get_live_vehicles()

    await message.answer(
        "🔧 DEBUG\n\n"
        f"🚏 Зупинок: {len(stops)}\n"
        f"🚌 Маршрутів: {len(routes)}\n"
        f"🚍 Trips: {len(trips)}\n"
        f"🛰 Свіжих GPS-машин: {len(vehicles)}"
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
# ALL ROUTES BUTTON — NEW
# ==================================================

@dp.message(
    F.text == "🚌 Всі варіанти"
)
async def all_routes_handler(
    message: Message
):
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
async def text_handler(
    message: Message
):
    user_id = message.from_user.id
    text = message.text.strip()

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

        # НОВЕ:
        # зберігаємо всі знайдені варіанти,
        # щоб кнопка "Всі варіанти"
        # могла показати їх без нового пошуку.
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
# MAIN
# ==================================================

async def main():
    print(
        "MENI-TUDY ALL ROUTES START",
        flush=True,
    )

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

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message"
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
