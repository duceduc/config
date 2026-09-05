"""Unit normalization and temperature conversion helpers for Halthy."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.const import UnitOfTemperature

from .const import TEMPERATURE_UNIT_CELSIUS, TEMPERATURE_UNIT_FAHRENHEIT
from .naming import normalize_metric_key

_UNIT_ALIASES = {
    "%": "%",
    "/min": "/min",
    "breaths/min": "breaths/min",
    "count": "count",
    "count/min": "/min",
    "count/minute": "/min",
    "counts/min": "/min",
    "count/s": "/s",
    "count/sec": "/s",
    "count/second": "/s",
    "dba": "dBA",
    "dbaspl": "dBA",
    "degc": "°C",
    "degf": "°F",
    "celsius": "°C",
    "fahrenheit": "°F",
    "hr": "h",
    "hour": "h",
    "hours": "h",
    "hrs": "h",
    "kg": "kg",
    "kcal": "kcal",
    "m": "m",
    "mg/dl": "mg/dL",
    "min": "min",
    "mins": "min",
    "minute": "min",
    "minutes": "min",
    "ml/(kg*min)": "mL/kg/min",
    "ml/kg/min": "mL/kg/min",
    "mmhg": "mmHg",
    "ms": "ms",
    "s": "s",
    "w": "W",
}

_METRIC_UNIT_OVERRIDES = {
    "blood_pressure_diastolic": "mmHg",
    "blood_pressure_systolic": "mmHg",
    "bp_diastolic": "mmHg",
    "bp_systolic": "mmHg",
    "flights": "floors",
    "flights_climbed": "floors",
    "heart_rate": "bpm",
    "heart_rate_recovery_one_minute": "bpm",
    "respiratory_rate": "breaths/min",
    "resting_heart_rate": "bpm",
    "sleep_change": "h",
    "sleep_duration": "h",
    "sleep_in_bed": "h",
    "sleep_score": "%",
    "step_count": "steps",
    "steps": "steps",
    "walking_heart_rate_average": "bpm",
    "walking_hr_avg": "bpm",
    "workout_duration": "min",
    "workout_duration_minutes": "min",
}

_DURATION_UNITS = {"d", "h", "min", "s", "ms", "µs"}

_DURATION_METRIC_KEYS = {
    "exercise_time",
    "stand_time",
    "move_time",
    "sleep_duration",
    "sleep_in_bed",
    "sleep_change",
    "workout_duration",
    "workout_duration_minutes",
    "time_in_daylight",
}

_TIMESTAMP_METRIC_KEYS = {
    "last_full_sync",
    "last_update",
    "workout_start",
    "workout_end",
    "workout_route_start",
    "workout_route_end",
}


def canonical_unit(metric_key: str, provided_unit: Any) -> str | None:
    key = normalize_metric_key(metric_key)
    tokens = set(key.split("_"))

    if key in _METRIC_UNIT_OVERRIDES:
        return _METRIC_UNIT_OVERRIDES[key]

    if "flight" in key or "floor" in key:
        return "floors"
    if ({"step", "steps"} & tokens) and not ({"length", "speed", "stride"} & tokens):
        return "steps"
    if (
        "heart" in tokens
        and "rate" in tokens
        and "variability" not in tokens
        and "sdnn" not in tokens
    ):
        return "bpm"
    if "respiratory" in tokens and "rate" in tokens:
        return "breaths/min"
    if key.startswith("bp_") or ("blood" in tokens and "pressure" in tokens):
        return "mmHg"

    unit = str(provided_unit).strip() if provided_unit is not None else ""
    if not unit:
        return None

    compact_unit = re.sub(r"\s+", "", unit).lower().replace("\N{DEGREE SIGN}", "deg")
    return _UNIT_ALIASES.get(compact_unit, unit)


def _is_temperature_metric(metric_key: str) -> bool:
    normalized_key = normalize_metric_key(metric_key)
    return "temperature" in normalized_key.split("_")


def _target_temperature_unit(preference: str, hass_temperature_unit: str) -> str:
    if preference == TEMPERATURE_UNIT_CELSIUS:
        return UnitOfTemperature.CELSIUS
    if preference == TEMPERATURE_UNIT_FAHRENHEIT:
        return UnitOfTemperature.FAHRENHEIT
    if hass_temperature_unit == UnitOfTemperature.FAHRENHEIT:
        return UnitOfTemperature.FAHRENHEIT
    return UnitOfTemperature.CELSIUS


def _convert_temperature_value(value: float, source_unit: str, target_unit: str) -> float:
    if source_unit == target_unit:
        return value
    if source_unit == UnitOfTemperature.CELSIUS and target_unit == UnitOfTemperature.FAHRENHEIT:
        return (value * 9 / 5) + 32
    if source_unit == UnitOfTemperature.FAHRENHEIT and target_unit == UnitOfTemperature.CELSIUS:
        return (value - 32) * 5 / 9
    return value


def resolve_temperature_state(
    metric_key: str,
    state: str | float | int | bool,
    unit: str | None,
    preference: str,
    hass_temperature_unit: str,
) -> tuple[str | float | int | bool, str | None]:
    if not _is_temperature_metric(metric_key):
        return state, unit

    source_unit = canonical_unit(metric_key, unit)
    if source_unit not in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT):
        return state, source_unit

    if isinstance(state, bool) or not isinstance(state, (int, float)):
        return state, source_unit

    target_unit = _target_temperature_unit(preference, hass_temperature_unit)
    converted_value = _convert_temperature_value(float(state), source_unit, target_unit)
    return round(converted_value, 2), target_unit


def is_duration_metric(metric_key: str, unit: str | None) -> bool:
    key = normalize_metric_key(metric_key)
    if key in _DURATION_METRIC_KEYS:
        return True

    if key.endswith("_time") or "duration" in key.split("_"):
        normalized_unit = canonical_unit(key, unit)
        return normalized_unit in _DURATION_UNITS

    return False


def duration_suggested_display_precision(metric_key: str, unit: str | None) -> int | None:
    if not is_duration_metric(metric_key, unit):
        return None

    normalized_unit = canonical_unit(metric_key, unit)
    if normalized_unit == "h":
        return 2
    if normalized_unit == "min":
        return 1
    return 0


def is_timestamp_metric(metric_key: str) -> bool:
    key = normalize_metric_key(metric_key)
    return key in _TIMESTAMP_METRIC_KEYS
