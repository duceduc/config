"""Select platform for Halthy."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import (
    FORCE_UPLOAD_INTERVAL_OPTIONS,
    async_update_force_upload_interval,
    force_upload_interval_label,
    force_upload_interval_seconds_from_label,
)
from .const import DOMAIN, MANUFACTURER
from .naming import sanitize_identifier
from .runtime import IntegrationRuntime, runtime_device_identifiers


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Halthy force-upload interval selector."""
    domain_data = hass.data[DOMAIN]
    runtime: IntegrationRuntime = domain_data["entries"][entry.entry_id]
    async_add_entities([HalthyForceUploadIntervalSelect(runtime=runtime, entry_id=entry.entry_id)])


class HalthyForceUploadIntervalSelect(SelectEntity):
    """Select entity controlling periodic halthy.force_upload command cadence."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, runtime: IntegrationRuntime, entry_id: str) -> None:
        self._runtime = runtime
        self._entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_force_upload_interval"
        self._attr_name = "Upload interval"
        self._attr_icon = "mdi:timer-cog-outline"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_options = [label for label, _ in FORCE_UPLOAD_INTERVAL_OPTIONS]
        self._attr_current_option = force_upload_interval_label(runtime.force_upload_interval_seconds)
        self._attr_suggested_object_id = (
            f"{sanitize_identifier(runtime.configured_username)}_force_upload_interval"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers=runtime_device_identifiers(self._runtime),
            manufacturer=MANUFACTURER,
            model="iOS App",
            name=self._runtime.display_name,
        )

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ValueError(f"Unsupported force upload interval option: {option}")
        interval_seconds = force_upload_interval_seconds_from_label(option)
        normalized_seconds = await async_update_force_upload_interval(
            self.hass,
            self._entry_id,
            interval_seconds,
        )
        self._attr_current_option = force_upload_interval_label(normalized_seconds)
        self.async_write_ha_state()
