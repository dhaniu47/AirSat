import json
import os
import math
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from functools import wraps

import pandas as pd
import requests
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Avg, Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from sklearn.linear_model import LinearRegression

from .models import PollutionData


CPCB_RSS_FEED = "https://airquality.cpcb.gov.in/caaqms/rss_feed"
CPCB_CACHE_TTL = 5 * 60
CPCB_MAX_STATION_DISTANCE_KM = 25
CPCB_IST = timezone(timedelta(hours=5, minutes=30))
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "").strip()
_cpcb_cache = None


def api_login_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"error": "Authentication required"},
                status=401,
            )
        return view_func(request, *args, **kwargs)

    return wrapped


def parse_json(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------
# AQI CATEGORY
# ---------------------------------------------------------

def get_level(aqi):
    aqi = float(aqi)

    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Satisfactory"
    if aqi <= 200:
        return "Moderate"
    if aqi <= 300:
        return "Poor"
    if aqi <= 400:
        return "Very Poor"
    return "Severe"


# ---------------------------------------------------------
# CPCB AQI BREAKPOINTS
#
# Concentrations are based on CPCB AQI breakpoint ranges.
#
# Format:
# (
#     concentration_low,
#     concentration_high,
#     aqi_low,
#     aqi_high
# )
# ---------------------------------------------------------

CPCB_BREAKPOINTS = {
    "pm10": [
        (0, 50, 0, 50),
        (51, 100, 51, 100),
        (101, 250, 101, 200),
        (251, 350, 201, 300),
        (351, 430, 301, 400),
        (431, 10000, 401, 500),
    ],

    "pm2_5": [
        (0, 30, 0, 50),
        (31, 60, 51, 100),
        (61, 90, 101, 200),
        (91, 120, 201, 300),
        (121, 250, 301, 400),
        (251, 10000, 401, 500),
    ],

    "no2": [
        (0, 40, 0, 50),
        (41, 80, 51, 100),
        (81, 180, 101, 200),
        (181, 280, 201, 300),
        (281, 400, 301, 400),
        (401, 10000, 401, 500),
    ],

    "so2": [
        (0, 40, 0, 50),
        (41, 80, 51, 100),
        (81, 380, 101, 200),
        (381, 800, 201, 300),
        (801, 1600, 301, 400),
        (1601, 10000, 401, 500),
    ],

    "o3": [
        (0, 50, 0, 50),
        (51, 100, 51, 100),
        (101, 168, 101, 200),
        (169, 208, 201, 300),
        (209, 748, 301, 400),
        (749, 10000, 401, 500),
    ],

    "nh3": [
        (0, 200, 0, 50),
        (201, 400, 51, 100),
        (401, 800, 101, 200),
        (801, 1200, 201, 300),
        (1201, 1800, 301, 400),
        (1801, 10000, 401, 500),
    ],

    # OpenWeather gives CO in micrograms/m3.
    # We convert it to mg/m3 before using these ranges.
    "co": [
        (0, 1.0, 0, 50),
        (1.1, 2.0, 51, 100),
        (2.1, 10.0, 101, 200),
        (10.1, 17.0, 201, 300),
        (17.1, 34.0, 301, 400),
        (34.1, 1000, 401, 500),
    ],
}


def calculate_sub_index(concentration, breakpoints):
    """
    Calculate the AQI sub-index using linear interpolation
    between the CPCB concentration breakpoints.
    """

    if concentration is None:
        return None

    try:
        concentration = float(concentration)
    except (TypeError, ValueError):
        return None

    if concentration < 0:
        return None

    for c_low, c_high, i_low, i_high in breakpoints:
        if c_low <= concentration <= c_high:
            if c_high == c_low:
                return float(i_high)

            sub_index = (
                (i_high - i_low)
                / (c_high - c_low)
            ) * (concentration - c_low) + i_low

            return round(sub_index, 2)

    # If concentration is above our final breakpoint,
    # cap the AQI at 500.
    return 500.0


def calculate_cpcb_aqi(components):
    """
    Calculate the overall AQI from the available pollutant
    concentrations.

    The highest pollutant sub-index becomes the overall AQI.
    """

    pollutants = {
        "pm10": components.get("pm10"),
        "pm2_5": components.get("pm2_5"),
        "no2": components.get("no2"),
        "so2": components.get("so2"),
        "o3": components.get("o3"),
        "nh3": components.get("nh3"),

        # OpenWeather CO is μg/m3.
        # CPCB CO breakpoint is mg/m3.
        "co": (
            float(components["co"]) / 1000.0
            if components.get("co") is not None
            else None
        ),
    }

    sub_indices = {}

    for pollutant, concentration in pollutants.items():
        value = calculate_sub_index(
            concentration,
            CPCB_BREAKPOINTS[pollutant],
        )

        if value is not None:
            sub_indices[pollutant] = value

    if not sub_indices:
        return None, {}

    overall_aqi = max(sub_indices.values())

    return round(overall_aqi), sub_indices


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------

@ensure_csrf_cookie
def index(request):
    return render(request, "index.html")


# ---------------------------------------------------------
# LOGIN
# ---------------------------------------------------------

@require_POST
def login_view(request):
    data = parse_json(request)

    if data is None:
        return JsonResponse(
            {"error": "Invalid JSON"},
            status=400,
        )

    username = str(data.get("username", "")).strip()
    password = data.get("password", "")

    if not username or not password:
        return JsonResponse(
            {"error": "Username and password are required"},
            status=400,
        )

    user = authenticate(
        request,
        username=username,
        password=password,
    )

    if user is None:
        return JsonResponse(
            {"status": "failed"},
            status=401,
        )

    login(request, user)

    return JsonResponse(
        {
            "status": "success",
            "username": user.username,
        }
    )


# ---------------------------------------------------------
# LOGOUT
# ---------------------------------------------------------

@require_POST
def logout_view(request):
    logout(request)

    return JsonResponse(
        {"status": "logged_out"}
    )


# ---------------------------------------------------------
# REGISTER
# ---------------------------------------------------------

@require_POST
def register(request):
    data = parse_json(request)

    if data is None:
        return JsonResponse(
            {"error": "Invalid JSON"},
            status=400,
        )

    username = str(data.get("username", "")).strip()
    password = data.get("password", "")

    if not username or not password:
        return JsonResponse(
            {
                "error": "Username and password are required"
            },
            status=400,
        )

    if User.objects.filter(username=username).exists():
        return JsonResponse(
            {"status": "exists"},
            status=409,
        )

    user = User(username=username)

    try:
        validate_password(password, user)

    except ValidationError as exc:
        return JsonResponse(
            {"error": exc.messages},
            status=400,
        )

    user.set_password(password)
    user.save()

    return JsonResponse(
        {"status": "created"},
        status=201,
    )


# ---------------------------------------------------------
# RESET PASSWORD
# ---------------------------------------------------------

@require_POST
def reset_password(request):
    data = parse_json(request)

    if data is None:
        return JsonResponse(
            {"error": "Invalid JSON"},
            status=400,
        )

    current_password = data.get(
        "current_password",
        "",
    )

    new_password = data.get(
        "new_password",
        "",
    )

    if not current_password or not new_password:
        return JsonResponse(
            {
                "error": (
                    "Current password and new password "
                    "are required"
                )
            },
            status=400,
        )

    if not request.user.check_password(
        current_password
    ):
        return JsonResponse(
            {
                "error": (
                    "Current password is incorrect"
                )
            },
            status=400,
        )

    try:
        validate_password(
            new_password,
            request.user,
        )

    except ValidationError as exc:
        return JsonResponse(
            {"error": exc.messages},
            status=400,
        )

    request.user.set_password(new_password)
    request.user.save()

    logout(request)

    return JsonResponse(
        {"status": "updated"}
    )


# ---------------------------------------------------------
# OFFICIAL CPCB REAL-TIME / LATEST STATION AIR QUALITY
# ---------------------------------------------------------

def _haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return radius * 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a),
    )


def _cpcb_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cpcb_observed_at(value):
    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%d-%m-%Y %H:%M:%S",
        ).replace(
            tzinfo=CPCB_IST
        ).isoformat()

    except ValueError:
        return None


def _fetch_cpcb_stations():
    global _cpcb_cache

    now = time.monotonic()

    if (
        _cpcb_cache is not None
        and now - _cpcb_cache["fetched_at"] < CPCB_CACHE_TTL
    ):
        return _cpcb_cache["stations"]

    response = requests.get(
        CPCB_RSS_FEED,
        timeout=8,
    )

    response.raise_for_status()

    root = ET.fromstring(
        response.content
    )

    stations = []

    for station in root.iter("Station"):

        latitude = _cpcb_number(
            station.get("latitude")
        )

        longitude = _cpcb_number(
            station.get("longitude")
        )

        if latitude is None or longitude is None:
            continue

        aqi_node = station.find(
            "Air_Quality_Index"
        )

        if aqi_node is None:
            continue

        station_aqi = _cpcb_number(
            aqi_node.get("Value")
        )

        if station_aqi is None:
            continue

        sub_indices = {}

        for pollutant in station.iter(
            "Pollutant_Index"
        ):
            pollutant_id = pollutant.get("id")
            pollutant_avg = _cpcb_number(
                pollutant.get("Avg")
            )

            if (
                pollutant_id
                and pollutant_avg is not None
            ):
                sub_indices[
                    pollutant_id
                ] = pollutant_avg

        stations.append(
            {
                "name": station.get(
                    "id"
                )
                or (
                    f"{latitude:.4f},"
                    f"{longitude:.4f}"
                ),
                "latitude": latitude,
                "longitude": longitude,
                "observed_at": _cpcb_observed_at(
                    station.get(
                        "lastupdate"
                    )
                ),
                "aqi": round(
                    station_aqi
                ),
                "predominant": (
                    aqi_node.get(
                        "Predominant_Parameter"
                    )
                ),
                "sub_indices": sub_indices,
            }
        )

    _cpcb_cache = {
        "fetched_at": now,
        "stations": stations,
    }

    return stations


def _openweather_current(lat, lon):
    if not OPENWEATHER_API_KEY:
        return None

    try:
        response = requests.get(
            "https://api.openweathermap.org/data/2.5/air_pollution",
            params={
                "lat": lat,
                "lon": lon,
                "appid": OPENWEATHER_API_KEY,
            },
            timeout=8,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print("OpenWeather AQI unavailable; trying Open-Meteo:", exc)
        return None

    payload = response.json()
    item = (payload.get("list") or [None])[0]
    if not item:
        return None

    components = item.get("components") or {}
    estimated_aqi, sub_indices = calculate_cpcb_aqi(components)
    if estimated_aqi is None:
        return None

    return {
        "aqi": estimated_aqi,
        "level": get_level(estimated_aqi),
        "lat": lat,
        "lon": lon,
        "city": "Current location",
        "station": "OpenWeather current air-quality model",
        "station_lat": lat,
        "station_lon": lon,
        "station_distance_km": 0,
        "station_within_25km": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "predominant_pollutant": None,
        "sub_indices": sub_indices,
        "source": "OpenWeather current Air Pollution API",
        "aqi_method": "CPCB-breakpoint-based estimate from current OpenWeather pollutant concentrations",
        "openweather_aqi": (item.get("main") or {}).get("aqi"),
        "components": components,
    }


def _openmeteo_current(lat, lon):
    response = requests.get(
        "https://air-quality-api.open-meteo.com/v1/air-quality",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone",
            "timezone": "UTC",
        },
        timeout=8,
    )
    response.raise_for_status()

    payload = response.json()
    current = payload.get("current") or {}

    components = {
        "pm10": current.get("pm10"),
        "pm2_5": current.get("pm2_5"),
        "co": current.get("carbon_monoxide"),
        "no2": current.get("nitrogen_dioxide"),
        "so2": current.get("sulphur_dioxide"),
        "o3": current.get("ozone"),
    }

    estimated_aqi, sub_indices = calculate_cpcb_aqi(components)
    if estimated_aqi is None:
        return None

    observed_at = current.get("time")

    return {
        "aqi": estimated_aqi,
        "level": get_level(estimated_aqi),
        "lat": lat,
        "lon": lon,
        "city": "Current location",
        "station": "Open-Meteo air-quality model",
        "station_lat": payload.get("latitude", lat),
        "station_lon": payload.get("longitude", lon),
        "station_distance_km": 0,
        "station_within_25km": False,
        "observed_at": (
            f"{observed_at}Z"
            if observed_at and not observed_at.endswith("Z")
            else observed_at
        ),
        "predominant_pollutant": None,
        "sub_indices": sub_indices,
        "source": "Open-Meteo Air Quality API",
        "aqi_method": "CPCB-breakpoint-based estimate from current Open-Meteo pollutant concentrations",
        "openweather_aqi": None,
        "components": components,
    }


def _nearest_cpcb_station(
    latitude,
    longitude,
):
    stations = _fetch_cpcb_stations()

    if not stations:
        return None

    ranked = []

    for station in stations:
        distance = _haversine_km(
            latitude,
            longitude,
            station["latitude"],
            station["longitude"],
        )

        ranked.append(
            (
                distance,
                station,
            )
        )

    ranked.sort(
        key=lambda item: item[0]
    )

    distance, station = ranked[0]

    return {
        **station,
        "distance_km": round(
            distance,
            2,
        ),
        "within_25km": (
            distance <= CPCB_MAX_STATION_DISTANCE_KM
        ),
    }


@require_GET
def pollution(request):

    lat = request.GET.get("lat")
    lon = request.GET.get("lon")

    if not lat or not lon:
        return JsonResponse({"error": "lat/lon required"}, status=400)

    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return JsonResponse({"error": "lat/lon must be numeric"}, status=400)

    if not (-90 <= lat <= 90):
        return JsonResponse({"error": "Invalid latitude"}, status=400)

    if not (-180 <= lon <= 180):
        return JsonResponse({"error": "Invalid longitude"}, status=400)

    # For a map click, use the exact clicked coordinates first.
    # This avoids making every click wait for the large CPCB station feed.
    try:
        fallback = _openweather_current(lat, lon)

        if fallback is None:
            try:
                fallback = _openmeteo_current(lat, lon)
            except requests.RequestException as exc:
                print("Open-Meteo AQI unavailable:", exc)
                fallback = None

        if fallback is not None:
            PollutionData.objects.create(
                lat=lat,
                lon=lon,
                no2=fallback["components"].get("no2", 0),
                pm25=fallback["components"].get("pm2_5", 0),
                city="Current location",
                level=fallback["level"],
            )

            return JsonResponse(fallback)

        # If OpenWeather is unavailable, fall back to the nearest CPCB station.
        station = _nearest_cpcb_station(lat, lon)

        if station is not None:
            aqi = station["aqi"]

            result = {
                "aqi": aqi,
                "level": get_level(aqi),
                "lat": lat,
                "lon": lon,
                "city": station["name"],
                "station": station["name"],
                "station_lat": station["latitude"],
                "station_lon": station["longitude"],
                "station_distance_km": station["distance_km"],
                "station_within_25km": station["within_25km"],
                "observed_at": station["observed_at"],
                "predominant_pollutant": station["predominant"],
                "sub_indices": station["sub_indices"],
                "source": "Central Pollution Control Board (CPCB) CAAQMS live feed",
                "aqi_method": "Official CPCB National AQI reported by the selected CAAQMS monitoring station",
            }

            PollutionData.objects.create(
                lat=lat,
                lon=lon,
                no2=station["sub_indices"].get("NO2", 0),
                pm25=station["sub_indices"].get("PM2.5", 0),
                city=station["name"],
                level=get_level(aqi),
            )

            return JsonResponse(result)

        return JsonResponse(
            {"error": "Current AQI data is temporarily unavailable."},
            status=503,
        )

    except requests.RequestException as exc:
        print("AQI provider error:", exc)
        return JsonResponse(
            {"error": "Current AQI data is temporarily unavailable."},
            status=502,
        )
    except (ET.ParseError, KeyError, IndexError, TypeError, ValueError) as exc:
        print("AQI processing error:", exc)
        return JsonResponse(
            {"error": "Current AQI data is temporarily unavailable."},
            status=502,
        )


# ---------------------------------------------------------
# ALL CURRENT CPCB CAAQMS STATIONS
# ---------------------------------------------------------

@require_GET
def cpcb_stations(request):

    try:
        stations = _fetch_cpcb_stations()

        return JsonResponse(
            {
                "source": (
                    "Central Pollution "
                    "Control Board (CPCB) "
                    "CAAQMS live feed"
                ),
                "stations": stations,
                "available": True,
            }
        )

    except (
        requests.RequestException,
        ET.ParseError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as exc:

        print("CPCB station feed unavailable:", exc)

        return JsonResponse(
            {
                "source": "CPCB CAAQMS live feed unavailable",
                "stations": [],
                "available": False,
            }
        )



# ---------------------------------------------------------
# POLLUTION DATA
# ---------------------------------------------------------

@require_GET
def get_pollution_data(request):

    data = PollutionData.objects.order_by(
        "-created_at"
    )[:300]

    result = [
        {
            "lat": d.lat,
            "lon": d.lon,
            "no2": d.no2,
        }
        for d in data
    ]

    return JsonResponse(
        {"data": result}
    )


# ---------------------------------------------------------
# FUTURE AQI PREDICTION
# ---------------------------------------------------------

def predict_future():

    data = PollutionData.objects.order_by(
        "created_at"
    )[:200]

    if len(data) < 10:
        return 80

    df = pd.DataFrame(
        list(
            data.values("pm25")
        )
    )

    df["time"] = range(
        len(df)
    )

    model = LinearRegression()

    model.fit(
        df[["time"]],
        df["pm25"].fillna(0),
    )

    prediction = model.predict(
        [[len(df) + 10]]
    )[0]

    return max(
        0,
        round(
            prediction,
            2,
        ),
    )


@require_GET
def predict_api(request):

    return JsonResponse(
        {
            "prediction": predict_future()
        }
    )


# ---------------------------------------------------------
# ANALYTICS
# ---------------------------------------------------------

@require_GET
def analytics_data(request):

    data = PollutionData.objects.all()

    return JsonResponse(
        {
            "avg": (
                data.aggregate(
                    Avg("no2")
                )["no2__avg"]
                or 0
            ),

            "max": (
                data.aggregate(
                    Max("no2")
                )["no2__max"]
                or 0
            ),
        }
    )


# ---------------------------------------------------------
# SAVE DATA
# ---------------------------------------------------------

@require_POST
def save_data(request):

    return JsonResponse(
        {"message": "ok"}
    )


# ---------------------------------------------------------
# TILES
# ---------------------------------------------------------

@require_GET
def tiles(request):

    return JsonResponse(
        {"message": "ok"}
    )


# ---------------------------------------------------------
# HISTORY
# ---------------------------------------------------------

@require_GET
def history(request):

    return JsonResponse(
        {"message": "ok"}
    )


# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------

@require_GET
def dashboard(request):

    return JsonResponse(
        {"message": "ok"}
    )