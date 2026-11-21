"""Sensors for Mobile Weather Forecast."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONDITION_OPTIONS, DOMAIN, KEY_BY_CODE, UNKNOWN_KEY, icon_from_code
from .coordinator import MobileWeatherForecastConfigEntry, MobileWeatherForecastCoordinator


@dataclass(frozen=True, kw_only=True)
class MobileWeatherForecastSensorDescription(SensorEntityDescription):
    """Sensor description with its value reader."""

    value_fn: Callable[[dict[str, Any]], Any]
    icon_fn: Callable[[dict[str, Any]], str] | None = None


def _code(current: dict[str, Any]) -> int | None:
    return current.get("weather_code")


SENSORS: tuple[MobileWeatherForecastSensorDescription, ...] = (
    MobileWeatherForecastSensorDescription(
        key="condition",
        translation_key="condition",
        device_class=SensorDeviceClass.ENUM,
        options=CONDITION_OPTIONS,
        value_fn=lambda c: (
            KEY_BY_CODE.get(int(_code(c)), UNKNOWN_KEY) if _code(c) is not None else None
        ),
        icon_fn=lambda c: icon_from_code(_code(c), bool(c.get("is_day", 1))),
    ),
    MobileWeatherForecastSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("temperature_2m"),
    ),
    MobileWeatherForecastSensorDescription(
        key="apparent_temperature",
        translation_key="apparent_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("apparent_temperature"),
    ),
    MobileWeatherForecastSensorDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("relative_humidity_2m"),
    ),
    MobileWeatherForecastSensorDescription(
        key="precipitation",
        translation_key="precipitation",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        value_fn=lambda c: c.get("precipitation"),
    ),
    MobileWeatherForecastSensorDescription(
        key="wind_speed",
        translation_key="wind_speed",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("wind_speed_10m"),
    ),
    MobileWeatherForecastSensorDescription(
        key="wind_gusts",
        translation_key="wind_gusts",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("wind_gusts_10m"),
    ),
    MobileWeatherForecastSensorDescription(
        key="pressure",
        translation_key="pressure",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.get("pressure_msl"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MobileWeatherForecastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(MobileWeatherForecastSensor(coordinator, entry, d) for d in SENSORS)


class MobileWeatherForecastSensor(CoordinatorEntity[MobileWeatherForecastCoordinator], SensorEntity):
    """Weather sensor for the tracked position."""

    _attr_has_entity_name = True
    entity_description: MobileWeatherForecastSensorDescription

    def __init__(
        self,
        coordinator: MobileWeatherForecastCoordinator,
        entry: MobileWeatherForecastConfigEntry,
        description: MobileWeatherForecastSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Open-Meteo",
            sw_version=coordinator.sw_version,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data["current"])

    @property
    def icon(self) -> str | None:
        if self.entity_description.icon_fn is not None:
            return self.entity_description.icon_fn(self.coordinator.data["current"])
        return super().icon
