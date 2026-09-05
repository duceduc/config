"""Sensor platform for Halthy."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .runtime import HalthySensorState, IntegrationRuntime, runtime_device_identifiers
from .const import (
    DOMAIN,
    MANUFACTURER,
    new_sensor_signal,
    remove_sensor_signal,
    update_sensor_signal,
)
from .naming import sanitize_identifier
from .units import (
    duration_suggested_display_precision,
    is_duration_metric,
    is_timestamp_metric,
)

_LOGGER = logging.getLogger(__name__)


def _parse_timestamp_state(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            normalized = f"{raw[:-1]}+00:00"
        elif re.search(r"[+-]\d{4}$", raw):
            normalized = f"{raw[:-5]}{raw[-5:-2]}:{raw[-2:]}"
        else:
            normalized = raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up bridge sensors from config entry."""
    domain_data = hass.data[DOMAIN]
    runtime: IntegrationRuntime = domain_data["entries"][entry.entry_id]
    entities: dict[str, HalthySensor] = {}

    @callback
    def async_add_sensor(unique_id: str) -> None:
        if unique_id in entities:
            return
        if unique_id not in runtime.sensors:
            return
        entity = HalthySensor(runtime=runtime, entry_id=entry.entry_id, unique_id=unique_id)
        entities[unique_id] = entity
        async_add_entities([entity])

    for unique_id in sorted(runtime.sensors):
        async_add_sensor(unique_id)

    remove_listener = async_dispatcher_connect(hass, new_sensor_signal(entry.entry_id), async_add_sensor)
    entry.async_on_unload(remove_listener)


class HalthySensor(SensorEntity):
    """Represents a dynamically-created sensor pushed from the iOS app."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, runtime: IntegrationRuntime, entry_id: str, unique_id: str) -> None:
        self._runtime = runtime
        self._entry_id = entry_id
        self._sensor_unique_id = unique_id
        self._sensor_state = runtime.sensors[unique_id]

        self._attr_unique_id = unique_id
        self._attr_suggested_object_id = (
            f"{sanitize_identifier(self._runtime.configured_username)}_"
            f"{sanitize_identifier(self._sensor_state.metric_key)}"
        )
        self._apply_state(self._sensor_state)

    @callback
    def _apply_state(self, state: HalthySensorState) -> None:
        self._sensor_state = state
        self._attr_name = state.name
        self._attr_native_value = state.state
        self._attr_native_unit_of_measurement = state.unit
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC
            if state.metric_key in {"last_update", "last_full_sync", "daily_upload_count"}
            else None
        )
        self._attr_state_class = None
        is_numeric_state = isinstance(state.state, (int, float)) and not isinstance(state.state, bool)
        self._attr_suggested_display_precision = (
            0
            if is_numeric_state
            else None
        )

        if is_duration_metric(state.metric_key, state.unit):
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = duration_suggested_display_precision(
                state.metric_key, state.unit
            )
        elif is_timestamp_metric(state.metric_key):
            parsed_timestamp = _parse_timestamp_state(state.state)
            if parsed_timestamp is not None:
                self._attr_native_value = parsed_timestamp
                self._attr_device_class = SensorDeviceClass.TIMESTAMP
                self._attr_suggested_display_precision = None
            else:
                # Keep the raw payload visible instead of making the entity unavailable.
                self._attr_device_class = None
        elif "temperature" in state.metric_key.split("_"):
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
        else:
            self._attr_device_class = None

        # Export numeric metrics as measurement sensors so HA recorder can build long-term statistics
        # on the sensor.* entities (for ApexCharts statistics mode).
        if (
            self._attr_state_class is None
            and is_numeric_state
            and self._attr_entity_category != EntityCategory.DIAGNOSTIC
            and not is_timestamp_metric(state.metric_key)
        ):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = state.icon

        attrs: dict[str, Any] = dict(state.attributes)
        attrs.setdefault("metric_key", state.metric_key)
        attrs.setdefault("username", state.username)
        attrs.setdefault("device_id", state.device_id)
        attrs["last_pushed"] = state.updated_at.isoformat()
        self._attr_extra_state_attributes = attrs

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers=runtime_device_identifiers(self._runtime),
            manufacturer=MANUFACTURER,
            model="iOS App",
            name=self._runtime.display_name,
        )

    async def async_added_to_hass(self) -> None:
        """Register update callback."""
        await super().async_added_to_hass()
        await self._async_migrate_entity_id_if_needed()

        @callback
        def async_handle_sensor_update() -> None:
            state = self._runtime.sensors.get(self._sensor_unique_id)
            if state is None:
                return
            self._apply_state(state)
            self.async_write_ha_state()
            # Metric key normalization can change suggested object id; migrate opportunistically.
            self.hass.async_create_task(self._async_migrate_entity_id_if_needed())

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                update_sensor_signal(self._entry_id, self._sensor_unique_id),
                async_handle_sensor_update,
            )
        )

        @callback
        def async_handle_sensor_remove() -> None:
            self.hass.async_create_task(self._async_remove_entity_if_present())

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                remove_sensor_signal(self._entry_id, self._sensor_unique_id),
                async_handle_sensor_remove,
            )
        )

    async def _async_migrate_entity_id_if_needed(self) -> None:
        if self.entity_id is None:
            return

        desired_entity_id = (
            f"sensor.{sanitize_identifier(self._runtime.configured_username)}_"
            f"{sanitize_identifier(self._sensor_state.metric_key)}"
        )
        if self.entity_id == desired_entity_id:
            return

        registry = er.async_get(self.hass)
        if registry.async_get(desired_entity_id) is not None:
            return

        try:
            registry.async_update_entity(self.entity_id, new_entity_id=desired_entity_id)
        except ValueError:
            # Ignore conflicts and keep current id if HA rejects rename.
            return

    async def _async_remove_entity_if_present(self) -> None:
        if self.entity_id is not None:
            registry = er.async_get(self.hass)
            if registry.async_get(self.entity_id) is not None:
                registry.async_remove(self.entity_id)
        try:
            await self.async_remove(force_remove=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to remove sensor entity '%s' for unique_id '%s': %s",
                self.entity_id,
                self._sensor_unique_id,
                err,
            )
            return
