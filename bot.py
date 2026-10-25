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
MAX_WALK_METERS = 850

# Машина повинна бути достатньо близько
# до траєкторії потрібного напрямку.
MAX_ROUTE_MATCH_METERS = 550

# Для приблизного ETA.
# Не використовуємо миттєву GPS-швидкість як єдине джерело,
# бо автобус часто стоїть на світлофорі/зупинці.
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

new_route_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="🧭 Новий маршрут",
            )
        ],
        [
            KeyboardButton(
                text="📍 Оновити геолокацію",
                request_location=True,
            )
        ],
    ],
    resize_keyboard=True,
)


# ==================================================
# GEO HELPERS
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


def walking_minutes(distance):
    return max(
        1,
        round(distance / 80),
    )


def current_time():
    return datetime.now(KYIV_TZ)


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


def default_headers():
    return {
        "User-Agent": "MeniTudyTelegramBot/3.0",
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
                    "latitude": float(data[0]["lat"]),
                    "longitude": float(data[0]["lon"]),
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
# GTFS
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

    timeout = aiohttp.ClientTimeout(total=60)

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
                        f"GTFS HTTP ERROR: {response.status}",
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

        # ------------------------------------------
        # ROUTES
        # ------------------------------------------

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

        # ------------------------------------------
        # TRIPS
        # ------------------------------------------

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

        # ------------------------------------------
        # STOP_TIMES
        #
        # Тримаємо тільки:
        # (sequence, stop_id)
        #
        # Це значно легше для RAM.
        # ------------------------------------------

        new_sequences = defaultdict(list)

        print(
            "READING STOP_TIMES...",
            flush=True,
        )

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

        # ------------------------------------------
        # INDEX
        # ------------------------------------------

        new_stop_to_trips = defaultdict(list)

        for trip_id, sequence in new_sequences.items():

            sequence.sort(
                key=lambda x: x[0]
            )

            for position, item in enumerate(
                sequence
            ):
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
            f"{len(trips)} trips,",
            f"{len(trip_sequences)} sequences",
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
            raw.decode("utf-8-sig")
        )

        if not isinstance(data, list):
            return []

        now = current_time()
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
                now - gps_time
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
                    "satellites": item.get(
                        "satellites"
                    ),
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
# STOPS
# ==================================================

def nearby_stops(
    latitude,
    longitude,
    radius=1000,
    limit=25,
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

                total_walk = (
                    origin["distance"]
                    + destination["distance"]
                )

                # Базовий рейтинг без GPS.
                #
                # Ходьба після виходу карається
                # сильніше, ніж кількість зупинок.
                score = (
                    origin["distance"] * 1.1
                    + destination["distance"] * 1.5
                    + stop_count * 22
                )

                if origin["distance"] > 450:
                    score += 250

                if destination["distance"] > 450:
                    score += 400

                if destination["distance"] > 650:
                    score += 500

                candidates.append(
                    {
                        "score": score,
                        "route": info,
                        "trip_id": trip_id,
                        "origin": origin,
                        "destination": destination,
                        "origin_position": origin_position,
                        "destination_position": (
                            destination_position
                        ),
                        "stop_count": stop_count,
                    }
                )

                break

    # Тут важливо НЕ залишати лише один trip
    # на route_id занадто рано.
    #
    # Один номер маршруту може мати
    # різні напрямки.
    best = {}

    for item in candidates:

        key = (
            item["route"]["route_id"],
            item["route"]["direction_id"],
            item["origin"]["id"],
            item["destination"]["id"],
        )

        previous = best.get(key)

        if (
            previous is None
            or item["score"] < previous["score"]
        ):
            best[key] = item

    result = list(best.values())

    result.sort(
        key=lambda x: x["score"]
    )

    return result


# ==================================================
# VEHICLE / ROUTE MATCHING
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

    vehicle_number = str(
        vehicle.get(
            "route_short_name",
            "",
        )
    ).strip()

    route_id = str(
        route.get(
            "route_id",
            "",
        )
    ).strip()

    route_number = str(
        route.get(
            "number",
            "",
        )
    ).strip()

    if (
        vehicle_route_id
        and route_id
        and vehicle_route_id == route_id
    ):
        return True

    if (
        vehicle_number
        and route_number
        and vehicle_number == route_number
    ):
        return True

    return False


# ==================================================
# SEQUENCE HELPERS
# ==================================================

def get_sequence_stop_data(
    trip_id
):
    sequence = trip_sequences.get(
        trip_id,
        [],
    )

    result = []

    for index, item in enumerate(sequence):

        stop_id = item[1]

        stop = stops.get(
            stop_id
        )

        if not stop:
            continue

        name, lat, lon = stop

        result.append(
            {
                "index": index,
                "stop_id": stop_id,
                "name": name,
                "latitude": lat,
                "longitude": lon,
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
        []
    )

    if (
        start_position >= end_position
        or start_position < 0
        or end_position >= len(sequence)
    ):
        return 0

    total = 0

    for index in range(
        start_position,
        end_position,
    ):
        stop_a_id = sequence[index][1]
        stop_b_id = sequence[index + 1][1]

        stop_a = stops.get(stop_a_id)
        stop_b = stops.get(stop_b_id)

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
    route_distance_meters,
    vehicle_speed,
):
    # GPS speed 0 не означає, що автобус
    # реально стоятиме весь час.
    #
    # Якщо він рухається нормально,
    # трохи враховуємо фактичну швидкість,
    # але обмежуємо її розумним діапазоном.

    if vehicle_speed >= 7:

        effective_speed = min(
            28,
            max(
                14,
                vehicle_speed * 0.75,
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

    moving_minutes = (
        route_distance_meters
        / meters_per_minute
    )

    # Додаємо запас на зупинки,
    # світлофори та посадку.
    estimated = (
        moving_minutes * 1.22
        + 0.8
    )

    estimated = max(
        1,
        estimated,
    )

    low = max(
        1,
        math.floor(
            estimated * 0.78
        ),
    )

    high = max(
        low + 1,
        math.ceil(
            estimated * 1.28
        ),
    )

    return (
        low,
        high,
        estimated,
    )


# ==================================================
# ANALYZE LIVE VEHICLES
# ==================================================

def analyze_live_route(
    candidate,
    all_vehicles,
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

    matching_vehicles = [
        vehicle
        for vehicle in all_vehicles
        if vehicle_matches_route(
            vehicle,
            route,
        )
    ]

    if not matching_vehicles:
        return {
            "route_vehicle_count": 0,
            "approaching_count": 0,
            "best_vehicle": None,
        }

    approaching = []

    for vehicle in matching_vehicles:

        nearest = (
            nearest_sequence_position(
                vehicle,
                sequence_data,
            )
        )

        if not nearest:
            continue

        # Машина надто далеко від зупинок
        # цього конкретного напрямку.
        if (
            nearest["distance"]
            > MAX_ROUTE_MATCH_METERS
        ):
            continue

        vehicle_position = nearest[
            "index"
        ]

        # Якщо найближча точка маршруту вже
        # після посадкової зупинки,
        # цю машину не вважаємо тією,
        # що наближається.
        if (
            vehicle_position
            > origin_position
        ):
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

        # Додаємо відстань від GPS-машини
        # до найближчої GTFS-зупинки.
        route_distance += nearest[
            "distance"
        ]

        eta_low, eta_high, eta_mid = (
            estimate_eta_minutes(
                route_distance,
                vehicle["speed"],
            )
        )

        approaching.append(
            {
                "vehicle": vehicle,
                "nearest_route_stop": (
                    nearest
                ),
                "vehicle_position": (
                    vehicle_position
                ),
                "stops_before": (
                    stops_before
                ),
                "route_distance": (
                    route_distance
                ),
                "eta_low": eta_low,
                "eta_high": eta_high,
                "eta_mid": eta_mid,
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
            matching_vehicles
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
# ROUTE RANKING
# ==================================================

async def build_route_result(
    destination_text,
    origin_stops,
    destination_stops,
):
    direct_routes = find_direct_routes(
        origin_stops,
        destination_stops,
    )

    if not direct_routes:
        return None

    vehicles = await get_live_vehicles()

    enriched = []

    # Аналізуємо обмежену кількість,
    # щоб не навантажувати 128 MB / CPU.
    for candidate in direct_routes[:15]:

        live = analyze_live_route(
            candidate,
            vehicles,
        )

        candidate["live"] = live

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

        # Орієнтовний час самої поїздки.
        # Тут нам важливіше ранжування,
        # а не показ "точного" часу.
        ride_estimate = max(
            4,
            candidate[
                "stop_count"
            ] * 1.6,
        )

        best_vehicle = live.get(
            "best_vehicle"
        )

        if best_vehicle:

            wait_estimate = (
                best_vehicle[
                    "eta_mid"
                ]
            )

            has_approaching_vehicle = True

        else:

            # Якщо GPS потрібного напрямку
            # не підтверджений — не вигадуємо
            # очікування, а даємо штраф.
            wait_estimate = 12
            has_approaching_vehicle = False

        total_estimate = (
            walk_to
            + wait_estimate
            + ride_estimate
            + walk_after
        )

        # Рейтинг:
        # час + додатковий штраф за ходьбу.
        final_score = (
            total_estimate
            + walk_to * 0.5
            + walk_after * 1.0
        )

        if not has_approaching_vehicle:
            final_score += 8

        if (
            candidate[
                "destination"
            ]["distance"]
            > 500
        ):
            final_score += 5

        candidate[
            "walk_to_minutes"
        ] = walk_to

        candidate[
            "walk_after_minutes"
        ] = walk_after

        candidate[
            "ride_estimate"
        ] = ride_estimate

        candidate[
            "total_estimate"
        ] = total_estimate

        candidate[
            "final_score"
        ] = final_score

        enriched.append(
            candidate
        )

    # Залишаємо один найкращий
    # напрямок кожного номера маршруту.
    best_by_number = {}

    for item in enriched:

        number = item[
            "route"
        ]["number"]

        previous = best_by_number.get(
            number
        )

        if (
            previous is None
            or item["final_score"]
            < previous["final_score"]
        ):
            best_by_number[
                number
            ] = item

    final_routes = list(
        best_by_number.values()
    )

    final_routes.sort(
        key=lambda x: x[
            "final_score"
        ]
    )

    return {
        "destination": destination_text,
        "vehicles_total": len(
            vehicles
        ),
        "routes": final_routes,
    }


# ==================================================
# FORMAT HELPERS
# ==================================================

def format_gps_age(seconds):
    seconds = max(
        0,
        int(seconds),
    )

    if seconds < 60:
        return f"{seconds} с тому"

    return (
        f"{seconds // 60} хв тому"
    )


def format_distance(meters):
    if meters < 1000:
        return f"{round(meters)} м"

    return (
        f"{meters / 1000:.1f} км"
    )


def plural_stops(number):
    if number == 1:
        return "зупинка"

    if 2 <= number <= 4:
        return "зупинки"

    return "зупинок"


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
        "я спробую знайти найзручніший шлях.\n\n"
        "📍 знайду найближчу зупинку\n"
        "🚌 підберу транспорт\n"
        "🛰 перевірю машини наживо\n"
        "⏱ оцінимо, скільки чекати\n"
        "🚏 скажу, де виходити\n\n"
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
            "Тепер найцікавіше — "
            "куди їдемо? 👇",
            reply_markup=ReplyKeyboardRemove(),
        )

    else:

        await message.answer(
            "📍 Геолокацію отримав ✓\n\n"
            "Поруч не бачу зупинки "
            "в межах приблизно 1 км.\n\n"
            "Все одно напиши, куди їдемо 👇",
            reply_markup=ReplyKeyboardRemove(),
        )


# ==================================================
# GPS DEBUG
# ==================================================

@dp.message(F.text == "/gpsdebug")
async def gps_debug_handler(
    message: Message
):
    vehicles = await get_live_vehicles()

    route_counts = defaultdict(int)

    for vehicle in vehicles:
        route_counts[
            vehicle[
                "route_short_name"
            ]
        ] += 1

    route_counts = sorted(
        route_counts.items(),
        key=lambda x: (
            -x[1],
            x[0],
        ),
    )

    lines = [
        "🛰 GPS DEBUG",
        "",
        f"🕙 {current_time().strftime('%H:%M:%S')}",
        f"✅ Свіжих машин: {len(vehicles)}",
        "",
    ]

    for number, count in route_counts[:30]:
        lines.append(
            f"№{number or '?'} — {count}"
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
    vehicles = await get_live_vehicles()

    await message.answer(
        "🔧 DEBUG\n\n"
        f"🚏 Зупинок: {len(stops)}\n"
        f"🚌 Маршрутів: {len(routes)}\n"
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
    user_id = message.from_user.id
    text = message.text.strip()

    # ----------------------------------------------
    # NEW ROUTE
    # ----------------------------------------------

    if text == "🧭 Новий маршрут":

        users[user_id] = {
            "step": "waiting_origin"
        }

        await message.answer(
            "🧭 Будуємо новий маршрут.\n\n"
            "Звідки вирушаємо? 👇",
            reply_markup=start_keyboard,
        )

        return

    # ----------------------------------------------
    # MANUAL ORIGIN
    # ----------------------------------------------

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

    # ----------------------------------------------
    # ORIGIN ADDRESS
    # ----------------------------------------------

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

    # ----------------------------------------------
    # DESTINATION
    # ----------------------------------------------

    if step == "waiting_destination":

        await message.answer(
            "🧭 Шукаю найкращий варіант...\n"
            "🛰 Перевіряю транспорт наживо."
        )

        destination = await geocode_address(
            text
        )

        if not destination:

            await message.answer(
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

            await message.answer(
                "🤔 Зручного прямого маршруту "
                "поруч не знайшов.\n\n"
                "Ймовірно, сюди краще їхати "
                "з пересадкою.\n\n"
                "🔄 Пошук із пересадками "
                "додамо наступним етапом.",
                reply_markup=new_route_keyboard,
            )

            return

        options = result["routes"]
        best = options[0]

        route = best["route"]
        live = best["live"]

        lines = [
            "⚡ НАЙКРАЩИЙ ВАРІАНТ",
            "━━━━━━━━━━━━━━",
            f"🏁 {text}",
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
            ]
        )

        best_vehicle = live.get(
            "best_vehicle"
        )

        # ------------------------------------------
        # APPROACHING VEHICLE FOUND
        # ------------------------------------------

        if best_vehicle:

            vehicle = best_vehicle[
                "vehicle"
            ]

            stops_before = best_vehicle[
                "stops_before"
            ]

            eta_low = best_vehicle[
                "eta_low"
            ]

            eta_high = best_vehicle[
                "eta_high"
            ]

            lines.extend(
                [
                    "🛰 ТРАНСПОРТ НАЖИВО",
                    (
                        f"🟢 Знайшов машину №"
                        f"{vehicle['gov_number'] or '—'}, "
                        "яка ймовірно рухається "
                        "в потрібному напрямку."
                    ),
                    (
                        f"📍 Зараз біля "
                        f"«{best_vehicle['nearest_route_stop']['name']}»"
                    ),
                ]
            )

            if stops_before == 0:

                lines.append(
                    "🚏 Вона вже в районі "
                    "твоєї посадкової зупинки."
                )

            else:

                lines.append(
                    f"🚏 До твоєї зупинки орієнтовно "
                    f"{stops_before} "
                    f"{plural_stops(stops_before)}"
                )

            lines.extend(
                [
                    (
                        f"📏 По маршруту ≈ "
                        f"{format_distance(best_vehicle['route_distance'])}"
                    ),
                    (
                        f"⏱ Орієнтовне прибуття: "
                        f"{eta_low}–{eta_high} хв"
                    ),
                    (
                        f"🕒 GPS оновлено "
                        f"{format_gps_age(vehicle['age'])}"
                    ),
                ]
            )

            # --------------------------------------
            # WHEN TO LEAVE
            # --------------------------------------

            walk = best[
                "walk_to_minutes"
            ]

            safe_eta = eta_low

            leave_in = (
                safe_eta
                - walk
                - 2
            )

            lines.append("")

            if leave_in <= 0:

                lines.append(
                    "⚡ ВИХОДЬ ЗАРАЗ"
                )

                lines.append(
                    "Щоб не ризикувати пропустити транспорт."
                )

            elif leave_in <= 3:

                lines.append(
                    "⚡ МОЖНА ВЖЕ ВИХОДИТИ"
                )

                lines.append(
                    f"До зупинки тобі ≈ {walk} хв."
                )

            else:

                lines.append(
                    f"⚡ Можеш виходити приблизно "
                    f"через {round(leave_in)} хв"
                )

                lines.append(
                    f"До зупинки тобі ≈ {walk} хв."
                )

        # ------------------------------------------
        # ROUTE HAS GPS BUT DIRECTION UNCERTAIN
        # ------------------------------------------

        elif live[
            "route_vehicle_count"
        ] > 0:

            count = live[
                "route_vehicle_count"
            ]

            lines.extend(
                [
                    "🛰 ТРАНСПОРТ НАЖИВО",
                    (
                        f"🟡 На маршруті №{route['number']} "
                        f"бачу {count} GPS-маш."
                    ),
                    (
                        "Але зараз не можу достатньо "
                        "надійно підтвердити, що одна з них "
                        "ще їде саме до твоєї зупинки."
                    ),
                ]
            )

        # ------------------------------------------
        # NO GPS
        # ------------------------------------------

        else:

            lines.extend(
                [
                    "🛰 ТРАНСПОРТ НАЖИВО",
                    (
                        f"⚪ Свіжої GPS-позиції "
                        f"маршруту №{route['number']} "
                        "зараз не бачу."
                    ),
                ]
            )

        # ------------------------------------------
        # TRIP
        # ------------------------------------------

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
                (
                    f"«{best['destination']['name']}»"
                ),
                (
                    f"🚶 Звідти до адреси "
                    f"{round(best['destination']['distance'])} м "
                    f"≈ {best['walk_after_minutes']} хв"
                ),
            ]
        )

        # ------------------------------------------
        # ALTERNATIVES
        # ------------------------------------------

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
                        f"🟢 ≈ "
                        f"{alt_vehicle['eta_low']}–"
                        f"{alt_vehicle['eta_high']} хв"
                    )

                elif (
                    alt_live[
                        "route_vehicle_count"
                    ] > 0
                ):

                    status = (
                        "🟡 GPS є, напрямок неясний"
                    )

                else:

                    status = "⚪ GPS немає"

                lines.append(
                    f"• №{alt_route['number']} — "
                    f"{round(alternative['origin']['distance'])} м "
                    f"до посадки; "
                    f"{round(alternative['destination']['distance'])} м "
                    f"після виходу; {status}"
                )

        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━",
                "ℹ️ ETA поки орієнтовний: "
                "враховує GPS, положення машини "
                "відносно зупинок і міський рух.",
            ]
        )

        await message.answer(
            "\n".join(lines),
            reply_markup=new_route_keyboard,
        )

        users[
            user_id
        ]["step"] = "done"

        return

    await message.answer(
        "🧭 Готовий побудувати новий маршрут.",
        reply_markup=new_route_keyboard,
    )


# ==================================================
# MAIN
# ==================================================

async def main():

    print(
        "MENI-TUDY DIRECTION + ETA START",
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
