"""Open-Meteo data coordinator for the position of an entity."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_URL,
    CONF_ENTITY_ID,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MOVE_THRESHOLD_KM,
)
from .util import distance_km, extract_coordinates, extract_locality

_LOGGER = logging.getLogger(__name__)

type MobileWeatherForecastConfigEntry = ConfigEntry[MobileWeatherForecastCoordinator]

CURRENT_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "is_day",
    "precipitation",
    "weather_code",
    "cloud_cover",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
]
HOURLY_VARS = [
    "temperature_2m",
    "precipitation_probability",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "wind_direction_10m",
    "is_day",
]
DAILY_VARS = [
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "precipitation_probability_max",
    "wind_speed_10m_max",
    "wind_direction_10m_dominant",
]


def _zip_series(block: dict[str, list], keys: list[str]) -> list[dict[str, Any]]:
    """Turn {"time": [...], "var": [...]} into a list of dictionaries."""
    times = block.get("time", [])
    result: list[dict[str, Any]] = []
    for i, ts in enumerate(times):
        item: dict[str, Any] = {"time": ts}
        for key in keys:
            values = block.get(key)
            item[key] = values[i] if values and i < len(values) else None
        result.append(item)
    return result


class MobileWeatherForecastCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch the weather for the current position of the tracked entity."""

    config_entry: MobileWeatherForecastConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MobileWeatherForecastConfigEntry) -> None:
        minutes = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=minutes),
        )
        self.entity_id_tracked: str = entry.data[CONF_ENTITY_ID]
        self._last_coords: tuple[float, float] | None = None
        # Filled in at setup: "<version> (build <build>)", shown on the device page.
        self.sw_version: str | None = None

    # ------------------------------------------------------------------
    # Movement tracking
    # ------------------------------------------------------------------
    @callback
    def async_start_tracking(self) -> CALLBACK_TYPE:
        """Listen to the tracked entity and refresh when it moves away."""
        return async_track_state_change_event(
            self.hass, [self.entity_id_tracked], self._handle_tracked_change
        )

    @callback
    def _handle_tracked_change(self, event: Event[EventStateChangedData]) -> None:
        coords = extract_coordinates(event.data["new_state"])
        if coords is None or self._last_coords is None:
            return
        moved = distance_km(self._last_coords, coords)
        if moved >= MOVE_THRESHOLD_KM:
            _LOGGER.debug(
                "%s moved %.1f km, refreshing the weather",
                self.entity_id_tracked,
                moved,
            )
            self.hass.async_create_task(self.async_request_refresh())

    # ------------------------------------------------------------------
    # Data retrieval
    # ------------------------------------------------------------------
    def _resolve_position(self) -> tuple[tuple[float, float], str, str | None]:
        """Return (coordinates, source, locality)."""
        state = self.hass.states.get(self.entity_id_tracked)
        coords = extract_coordinates(state)
        if coords is not None:
            return coords, "entity", extract_locality(state)
        _LOGGER.debug(
            "No coordinates for %s, falling back to the home position",
            self.entity_id_tracked,
        )
        home = (self.hass.config.latitude, self.hass.config.longitude)
        return home, "home", None

    async def _async_update_data(self) -> dict[str, Any]:
        coords, source, locality = self._resolve_position()
        params = {
            "latitude": round(coords[0], 2),
            "longitude": round(coords[1], 2),
            "current": ",".join(CURRENT_VARS),
            "hourly": ",".join(HOURLY_VARS),
            "daily": ",".join(DAILY_VARS),
            "timezone": "auto",
            "timeformat": "unixtime",
            "forecast_days": 7,
            "wind_speed_unit": "kmh",
        }
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(20):
                response = await session.get(API_URL, params=params)
                response.raise_for_status()
                raw = await response.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Open-Meteo request error: {err}") from err

        if "current" not in raw:
            raise UpdateFailed(f"Unexpected Open-Meteo response: {raw.get('reason', raw)}")

        self._last_coords = coords
        _LOGGER.debug("Weather updated for %s (source %s)", coords, source)
        return {
            "current": raw["current"],
            "hourly": _zip_series(raw.get("hourly", {}), HOURLY_VARS),
            "daily": _zip_series(raw.get("daily", {}), DAILY_VARS),
            "latitude": coords[0],
            "longitude": coords[1],
            "source": source,
            "locality": locality,
        }
