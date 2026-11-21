"""Weather entity for Mobile Weather Forecast."""
from __future__ import annotations

from typing import Any

from homeassistant.components.weather import (
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_ENTITY_ID, DOMAIN, condition_from_code
from .coordinator import MobileWeatherForecastConfigEntry, MobileWeatherForecastCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MobileWeatherForecastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([MobileWeatherForecastWeather(entry.runtime_data, entry)])


def _iso(ts: int | float) -> str:
    return dt_util.utc_from_timestamp(ts).isoformat()


class MobileWeatherForecastWeather(CoordinatorEntity[MobileWeatherForecastCoordinator], WeatherEntity):
    """Weather for the position of the tracked entity."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_attribution = "Weather data by Open-Meteo.com"
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    def __init__(self, coordinator: MobileWeatherForecastCoordinator, entry: MobileWeatherForecastConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_weather"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Open-Meteo",
            sw_version=coordinator.sw_version,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._tracked = entry.data[CONF_ENTITY_ID]

    # --- current conditions ------------------------------------------
    @property
    def _current(self) -> dict[str, Any]:
        return self.coordinator.data["current"]

    @property
    def condition(self) -> str | None:
        return condition_from_code(
            self._current.get("weather_code"), bool(self._current.get("is_day", 1))
        )

    @property
    def native_temperature(self) -> float | None:
        return self._current.get("temperature_2m")

    @property
    def native_apparent_temperature(self) -> float | None:
        return self._current.get("apparent_temperature")

    @property
    def humidity(self) -> float | None:
        return self._current.get("relative_humidity_2m")

    @property
    def native_pressure(self) -> float | None:
        return self._current.get("pressure_msl")

    @property
    def native_wind_speed(self) -> float | None:
        return self._current.get("wind_speed_10m")

    @property
    def native_wind_gust_speed(self) -> float | None:
        return self._current.get("wind_gusts_10m")

    @property
    def wind_bearing(self) -> float | None:
        return self._current.get("wind_direction_10m")

    @property
    def cloud_coverage(self) -> float | None:
        return self._current.get("cloud_cover")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "tracked_entity": self._tracked,
            "tracked_latitude": data["latitude"],
            "tracked_longitude": data["longitude"],
            "tracked_source": data["source"],
            "locality": data["locality"],
        }

    # --- forecasts ---------------------------------------------------
    async def async_forecast_daily(self) -> list[Forecast] | None:
        forecasts: list[Forecast] = []
        for item in self.coordinator.data["daily"]:
            forecasts.append(
                Forecast(
                    datetime=_iso(item["time"]),
                    condition=condition_from_code(item["weather_code"], True),
                    native_temperature=item["temperature_2m_max"],
                    native_templow=item["temperature_2m_min"],
                    native_precipitation=item["precipitation_sum"],
                    precipitation_probability=item["precipitation_probability_max"],
                    native_wind_speed=item["wind_speed_10m_max"],
                    wind_bearing=item["wind_direction_10m_dominant"],
                )
            )
        return forecasts

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        start = int(dt_util.utcnow().timestamp() // 3600 * 3600)
        forecasts: list[Forecast] = []
        for item in self.coordinator.data["hourly"]:
            if item["time"] < start:
                continue
            forecasts.append(
                Forecast(
                    datetime=_iso(item["time"]),
                    condition=condition_from_code(
                        item["weather_code"], bool(item["is_day"])
                    ),
                    native_temperature=item["temperature_2m"],
                    native_precipitation=item["precipitation"],
                    precipitation_probability=item["precipitation_probability"],
                    native_wind_speed=item["wind_speed_10m"],
                    wind_bearing=item["wind_direction_10m"],
                )
            )
        return forecasts

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self.hass.async_create_task(self.async_update_listeners(("daily", "hourly")))
