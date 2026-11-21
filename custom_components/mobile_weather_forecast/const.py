"""Constants for the Mobile Weather Forecast integration."""
from __future__ import annotations

DOMAIN = "mobile_weather_forecast"

# Build identifier: date of the build + increment (YYYYMMDD-N).
# The version number itself lives in manifest.json (single source of truth).
BUILD = "20261119-1"

CONF_ENTITY_ID = "tracked_entity"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 15  # minutes
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 180

# Distance (km) above which a move of the tracked entity triggers a new
# request without waiting for the normal update interval.
MOVE_THRESHOLD_KM = 5.0

API_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes -> Home Assistant conditions
CONDITION_BY_CODE: dict[int, str] = {
    0: "sunny",
    1: "sunny",
    2: "partlycloudy",
    3: "cloudy",
    45: "fog",
    48: "fog",
    51: "rainy",
    53: "rainy",
    55: "rainy",
    56: "snowy-rainy",
    57: "snowy-rainy",
    61: "rainy",
    63: "rainy",
    65: "pouring",
    66: "snowy-rainy",
    67: "snowy-rainy",
    71: "snowy",
    73: "snowy",
    75: "snowy",
    77: "snowy",
    80: "rainy",
    81: "rainy",
    82: "pouring",
    85: "snowy",
    86: "snowy",
    95: "lightning-rainy",
    96: "hail",
    99: "hail",
}

# WMO codes -> state keys of the "Condition" sensor (translated via translations/*.json)
KEY_BY_CODE: dict[int, str] = {
    0: "clear_sky",
    1: "mainly_clear",
    2: "partly_cloudy",
    3: "overcast",
    45: "fog",
    48: "rime_fog",
    51: "light_drizzle",
    53: "drizzle",
    55: "heavy_drizzle",
    56: "freezing_drizzle",
    57: "heavy_freezing_drizzle",
    61: "light_rain",
    63: "rain",
    65: "heavy_rain",
    66: "freezing_rain",
    67: "heavy_freezing_rain",
    71: "light_snow",
    73: "snow",
    75: "heavy_snow",
    77: "snow_grains",
    80: "light_showers",
    81: "showers",
    82: "violent_showers",
    85: "snow_showers",
    86: "heavy_snow_showers",
    95: "thunderstorm",
    96: "thunderstorm_hail",
    99: "thunderstorm_heavy_hail",
}
UNKNOWN_KEY = "unknown_code"
CONDITION_OPTIONS: list[str] = [*KEY_BY_CODE.values(), UNKNOWN_KEY]


def condition_from_code(code: int | None, is_day: bool = True) -> str | None:
    """Convert a WMO code to a Home Assistant weather condition."""
    if code is None:
        return None
    condition = CONDITION_BY_CODE.get(int(code))
    if condition == "sunny" and not is_day:
        return "clear-night"
    return condition


def icon_from_code(code: int | None, is_day: bool = True) -> str:
    """MDI icon matching a WMO code."""
    if code is None:
        return "mdi:weather-cloudy-alert"
    code = int(code)
    if code in (0, 1):
        return "mdi:weather-sunny" if is_day else "mdi:weather-night"
    if code == 2:
        return "mdi:weather-partly-cloudy" if is_day else "mdi:weather-night-partly-cloudy"
    if code == 3:
        return "mdi:weather-cloudy"
    if code in (45, 48):
        return "mdi:weather-fog"
    if code in (51, 53, 55, 56, 57, 61, 63, 80, 81):
        return "mdi:weather-rainy"
    if code in (65, 82):
        return "mdi:weather-pouring"
    if code in (66, 67):
        return "mdi:weather-snowy-rainy"
    if code in (71, 73, 75, 77, 85, 86):
        return "mdi:weather-snowy"
    if code == 95:
        return "mdi:weather-lightning"
    if code in (96, 99):
        return "mdi:weather-lightning-rainy"
    return "mdi:weather-cloudy-alert"
