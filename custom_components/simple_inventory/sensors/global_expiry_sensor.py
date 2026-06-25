"""Global expiry notification sensor for Simple Inventory."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import Event, HomeAssistant, callback

from ..const import DOMAIN
from ..coordinator import SimpleInventoryCoordinator

_LOGGER = logging.getLogger(__name__)


class GlobalItemsExpiringSoonSensor(SensorEntity):
    """Sensor to track items nearing expiry across all inventories."""

    def __init__(self, hass: HomeAssistant, coordinator: SimpleInventoryCoordinator) -> None:
        """Initialize the global sensor."""
        self.hass = hass
        self.coordinator = coordinator
        self._attr_name = "All Items Expiring Soon"
        self._attr_unique_id = "simple_inventory_all_expiring_items"
        self._attr_icon = "mdi:calendar-alert"
        self._attr_native_unit_of_measurement = "items"
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "global_expiry_tracker")},
            "name": "All Items Expiring Soon",
        }

    async def async_added_to_hass(self) -> None:
        """Register callbacks and perform initial refresh."""
        await self._async_update_state()

        # The general DOMAIN_updated event fires on every inventory change, including
        # inventories added after startup — so per-inventory listeners are not needed.
        self.async_on_remove(self.hass.bus.async_listen(f"{DOMAIN}_updated", self._handle_update))
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self, _event: Event | None = None) -> None:
        """Invalidate the global expiry cache and schedule refresh."""
        expiry_cache = getattr(self.coordinator, "_expiry_cache", None)
        if expiry_cache is not None:
            expiry_cache.pop(None, None)
        self.hass.async_create_task(self._async_update_state())

    async def _async_update_state(self) -> None:
        """Aggregate expiring items across inventories."""
        try:
            all_items = await self.coordinator.async_get_items_expiring_soon()
        except Exception as err:
            _LOGGER.error("Failed to refresh global expiry sensor: %s", err)
            return

        for item in all_items:
            item["inventory"] = self._get_inventory_name(item["inventory_id"])

        expired_items = [item for item in all_items if item["days_until_expiry"] < 0]
        expiring_items = [item for item in all_items if item["days_until_expiry"] >= 0]

        inventories_count = len({item["inventory_id"] for item in all_items})

        self._attr_native_value = len(expiring_items)
        self._attr_extra_state_attributes = {
            "expiring_items": expiring_items,
            "expired_items": expired_items,
            "total_expiring": len(expiring_items),
            "total_expired": len(expired_items),
            "next_expiring": expiring_items[0] if expiring_items else None,
            "oldest_expired": expired_items[0] if expired_items else None,
            "inventories_count": inventories_count,
        }

        if expiring_items:
            most_urgent_days = expiring_items[0]["days_until_expiry"] if expiring_items else 7
            if most_urgent_days <= 1:
                self._attr_icon = "mdi:calendar-alert"
            elif most_urgent_days <= 3:
                self._attr_icon = "mdi:calendar-clock"
            else:
                self._attr_icon = "mdi:calendar-week"
        else:
            self._attr_icon = "mdi:calendar-check"

        self.async_write_ha_state()

    def _get_inventory_name(self, inventory_id: str) -> str:
        """Resolve inventory name from config entries."""
        try:
            config_entries = self.hass.config_entries.async_entries(DOMAIN)
            for entry in config_entries:
                if entry.entry_id == inventory_id:
                    return str(entry.data.get("name", "Unknown Inventory"))
        except Exception:
            pass
        return "Unknown Inventory"
