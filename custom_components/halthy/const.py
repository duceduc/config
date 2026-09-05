"""Constants for Halthy integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "halthy"
MANUFACTURER = "Halthy"
CONF_APP_USERNAME = "app_username"
CONF_DISPLAY_NAME = "name"
CONF_OWNER_USER_ID = "owner_user_id"
CONF_TEMPERATURE_UNIT = "temperature_unit"
CONF_ACTIVITY_LOG_MODE = "activity_log_mode"
CONF_STATISTICS_ENABLED = "statistics_enabled"
CONF_WORKOUT_ARCHIVE_RETENTION = "workout_archive_retention"

DEFAULT_WORKOUT_ARCHIVE_RETENTION = 250
MIN_WORKOUT_ARCHIVE_RETENTION = 25
MAX_WORKOUT_ARCHIVE_RETENTION = 2000

TEMPERATURE_UNIT_SYSTEM = "home_assistant"
TEMPERATURE_UNIT_CELSIUS = "celsius"
TEMPERATURE_UNIT_FAHRENHEIT = "fahrenheit"
DEFAULT_TEMPERATURE_UNIT = TEMPERATURE_UNIT_SYSTEM
VALID_TEMPERATURE_UNITS = (
    TEMPERATURE_UNIT_SYSTEM,
    TEMPERATURE_UNIT_CELSIUS,
    TEMPERATURE_UNIT_FAHRENHEIT,
)

ACTIVITY_LOG_MODE_OFF = "off"
ACTIVITY_LOG_MODE_SESSION_SUMMARY = "session_summary"
ACTIVITY_LOG_MODE_PER_ENTITY_VERBOSE = "per_entity_verbose"
DEFAULT_ACTIVITY_LOG_MODE = ACTIVITY_LOG_MODE_OFF
DEFAULT_STATISTICS_ENABLED = True
VALID_ACTIVITY_LOG_MODES = (
    ACTIVITY_LOG_MODE_OFF,
    ACTIVITY_LOG_MODE_SESSION_SUMMARY,
    ACTIVITY_LOG_MODE_PER_ENTITY_VERBOSE,
)

ENDPOINT_PATH = "/api/halthy/push"
ENDPOINT_NAME = "api:halthy:push"
COMMAND_ENDPOINT_PATH = "/api/halthy/command"
COMMAND_ENDPOINT_NAME = "api:halthy:command"
COMMAND_ACK_ENDPOINT_PATH = "/api/halthy/command_ack"
COMMAND_ACK_ENDPOINT_NAME = "api:halthy:command_ack"
WORKOUT_ARCHIVE_ENDPOINT_PATH = "/api/halthy/workouts"
WORKOUT_ARCHIVE_ENDPOINT_NAME = "api:halthy:workouts"
WORKOUT_ARCHIVE_IMAGE_ENDPOINT_PATH = "/api/halthy/workout_image"
WORKOUT_ARCHIVE_IMAGE_ENDPOINT_NAME = "api:halthy:workout_image"
SERVICE_FORCE_UPLOAD = "force_upload"
SERVICE_FORCE_INFLUX_BACKFILL = "force_influx_backfill"

_IMAGE_PLATFORM = getattr(Platform, "IMAGE", "image")
_SELECT_PLATFORM = getattr(Platform, "SELECT", "select")
_BUTTON_PLATFORM = getattr(Platform, "BUTTON", "button")
_CALENDAR_PLATFORM = getattr(Platform, "CALENDAR", "calendar")
PLATFORMS: list[Platform | str] = [
    Platform.SENSOR,
    _IMAGE_PLATFORM,
    _SELECT_PLATFORM,
    _BUTTON_PLATFORM,
    _CALENDAR_PLATFORM,
]


def new_sensor_signal(entry_id: str) -> str:
    """Signal name emitted when a brand-new sensor is seen."""
    return f"{DOMAIN}_new_sensor_{entry_id}"


def update_sensor_signal(entry_id: str, unique_id: str) -> str:
    """Signal name emitted when a sensor state changes."""
    return f"{DOMAIN}_update_sensor_{entry_id}_{unique_id}"


def remove_sensor_signal(entry_id: str, unique_id: str) -> str:
    """Signal name emitted when a sensor should be removed."""
    return f"{DOMAIN}_remove_sensor_{entry_id}_{unique_id}"


def new_image_signal(entry_id: str) -> str:
    """Signal name emitted when a brand-new image is seen."""
    return f"{DOMAIN}_new_image_{entry_id}"


def update_image_signal(entry_id: str, unique_id: str) -> str:
    """Signal name emitted when an image state changes."""
    return f"{DOMAIN}_update_image_{entry_id}_{unique_id}"


def remove_image_signal(entry_id: str, unique_id: str) -> str:
    """Signal name emitted when an image should be removed."""
    return f"{DOMAIN}_remove_image_{entry_id}_{unique_id}"


def workout_calendar_updated_signal(entry_id: str) -> str:
    """Signal emitted when an entry's workout calendar changes."""
    return f"{DOMAIN}_workout_calendar_updated_{entry_id}"
