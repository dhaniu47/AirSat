import json
import os
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


OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")


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

@api_login_required
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
# REAL-TIME AIR QUALITY
# ---------------------------------------------------------

@api_login_required
@require_GET
def pollution(request):

    lat = request.GET.get("lat")
    lon = request.GET.get("lon")

    if not lat or not lon:
        return JsonResponse(
            {"error": "lat/lon required"},
            status=400,
        )

    try:
        lat = float(lat)
        lon = float(lon)

    except (TypeError, ValueError):
        return JsonResponse(
            {"error": "lat/lon must be numeric"},
            status=400,
        )

    if not (-90 <= lat <= 90):
        return JsonResponse(
            {"error": "Invalid latitude"},
            status=400,
        )

    if not (-180 <= lon <= 180):
        return JsonResponse(
            {"error": "Invalid longitude"},
            status=400,
        )

    if not OPENWEATHER_API_KEY:
        return JsonResponse(
            {
                "error": (
                    "OpenWeather API key is not configured"
                )
            },
            status=503,
        )

    # -----------------------------------------------------
    # IMPORTANT:
    # Use the exact coordinates instead of rounding them
    # to 2 decimal places.
    #
    # This prevents nearby map clicks from being treated
    # as the same location.
    # -----------------------------------------------------

    cache_key = (
        f"airsat_aqi_"
        f"{lat:.5f}_"
        f"{lon:.5f}"
    )

    cached = cache.get(cache_key)

    if cached:
        return JsonResponse(cached)

    try:

        # -------------------------------------------------
        # OPENWEATHER CURRENT AIR POLLUTION API
        # -------------------------------------------------

        url = (
            "https://api.openweathermap.org/data/2.5/"
            "air_pollution"
        )

        params = {
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_API_KEY,
        }

        res = requests.get(
            url,
            params=params,
            timeout=15,
        )

        res.raise_for_status()

        payload = res.json()

        if not payload.get("list"):
            return JsonResponse(
                {
                    "error": (
                        "No air pollution data "
                        "available for this location"
                    )
                },
                status=502,
            )

        air_data = payload["list"][0]

        # -------------------------------------------------
        # OPENWEATHER'S OWN AQI
        #
        # This is ONLY 1-5.
        # -------------------------------------------------

        openweather_aqi = air_data.get(
            "main",
            {},
        ).get("aqi")

        # -------------------------------------------------
        # ACTUAL POLLUTANT CONCENTRATIONS
        # -------------------------------------------------

        raw_components = air_data.get(
            "components",
            {},
        )

        components = {
            "co": raw_components.get("co"),
            "no": raw_components.get("no"),
            "no2": raw_components.get("no2"),
            "o3": raw_components.get("o3"),
            "so2": raw_components.get("so2"),
            "pm2_5": raw_components.get("pm2_5"),
            "pm10": raw_components.get("pm10"),
            "nh3": raw_components.get("nh3"),
        }

        # -------------------------------------------------
        # CALCULATE NUMERIC AQI
        # -------------------------------------------------

        aqi, sub_indices = calculate_cpcb_aqi(
            components
        )

        if aqi is None:
            return JsonResponse(
                {
                    "error": (
                        "Unable to calculate AQI "
                        "from pollutant data"
                    )
                },
                status=502,
            )

        # -------------------------------------------------
        # REVERSE GEOCODING
        # -------------------------------------------------

        geo_url = (
            "https://api.openweathermap.org/geo/1.0/reverse"
        )

        geo_params = {
            "lat": lat,
            "lon": lon,
            "limit": 1,
            "appid": OPENWEATHER_API_KEY,
        }

        geo_res = requests.get(
            geo_url,
            params=geo_params,
            timeout=15,
        )

        geo_res.raise_for_status()

        geo = geo_res.json()

        if geo:
            city = geo[0].get(
                "name",
                "Unknown",
            )

            state = geo[0].get(
                "state",
                "",
            )

            country = geo[0].get(
                "country",
                "",
            )

        else:
            city = "Unknown"
            state = ""
            country = ""

        # -------------------------------------------------
        # DATA TIMESTAMP FROM OPENWEATHER
        # -------------------------------------------------

        data_timestamp = air_data.get("dt")

        # -------------------------------------------------
        # RESULT SENT TO FRONTEND
        # -------------------------------------------------

        result = {
            "aqi": aqi,
            "level": get_level(aqi),

            # OpenWeather's original 1-5 index
            "openweather_aqi": openweather_aqi,

            # Exact coordinates requested by the user
            "lat": lat,
            "lon": lon,

            "city": city,
            "state": state,
            "country": country,

            # Pollutants
            "pm25": components.get("pm2_5"),
            "pm10": components.get("pm10"),
            "no2": components.get("no2"),
            "so2": components.get("so2"),
            "o3": components.get("o3"),
            "co": components.get("co"),
            "nh3": components.get("nh3"),
            "no": components.get("no"),

            # Individual pollutant AQI contributions
            "sub_indices": sub_indices,

            # Unix timestamp of OpenWeather's data
            "data_timestamp": data_timestamp,

            # Explanation for frontend/debugging
            "source": "OpenWeather Air Pollution API",
            "aqi_method": (
                "CPCB breakpoint-based calculation "
                "from current OpenWeather pollutant "
                "concentrations"
            ),
        }

        # -------------------------------------------------
        # SAVE TO DATABASE
        # -------------------------------------------------

        PollutionData.objects.create(
            lat=lat,
            lon=lon,
            no2=components.get("no2") or 0,
            pm25=components.get("pm2_5") or 0,
            city=city,
            level=get_level(aqi),
        )

        # -------------------------------------------------
        # CACHE
        #
        # OpenWeather recommends avoiding excessive
        # requests for the same location.
        # -------------------------------------------------

        cache.set(
            cache_key,
            result,
            timeout=120,
        )

        return JsonResponse(result)

    except requests.RequestException as exc:
        print(
            "OpenWeather request error:",
            exc,
        )

        return JsonResponse(
            {
                "error": (
                    "Unable to fetch current "
                    "air quality data"
                )
            },
            status=502,
        )

    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as exc:

        print(
            "AQI data processing error:",
            exc,
        )

        return JsonResponse(
            {
                "error": (
                    "AQI data processing failed"
                )
            },
            status=502,
        )


# ---------------------------------------------------------
# POLLUTION DATA
# ---------------------------------------------------------

@api_login_required
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


@api_login_required
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

@api_login_required
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

@api_login_required
@require_POST
def save_data(request):

    return JsonResponse(
        {"message": "ok"}
    )


# ---------------------------------------------------------
# TILES
# ---------------------------------------------------------

@api_login_required
@require_GET
def tiles(request):

    return JsonResponse(
        {"message": "ok"}
    )


# ---------------------------------------------------------
# HISTORY
# ---------------------------------------------------------

@api_login_required
@require_GET
def history(request):

    return JsonResponse(
        {"message": "ok"}
    )


# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------

@api_login_required
@require_GET
def dashboard(request):

    return JsonResponse(
        {"message": "ok"}
    )