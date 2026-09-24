"""Home Assistant entry point for Eufy Mega Security.

This package keeps Home Assistant deliberately thin. The add-on gateway owns
Eufy authentication, event decoding, snapshot persistence, and media sessions.
The integration creates one authenticated HTTP/SSE client and one coordinator,
forwards the camera, detection, HomeBase security, settings, and diagnostic
platforms, and listens for normalized gateway updates. It never stores the
Eufy account password or reimplements a Mega endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import GatewayClient
from .const import CONF_API_TOKEN, DOMAIN, PLATFORMS
from .coordinator import EufyGatewayCoordinator


@dataclass
class GatewayRuntimeData:
    """Own the coordinator shared by every platform for one config entry.

    Home Assistant creates this container after the first successful gateway
    refresh and releases it when the entry unloads. Platform entities borrow
    the coordinator; they do not create clients or background listeners.
    """

    coordinator: EufyGatewayCoordinator


EufyGatewayConfigEntry = ConfigEntry[GatewayRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: EufyGatewayConfigEntry) -> bool:
    """Connect once, publish runtime data, and start the entry's platforms.

    The initial refresh is deliberately completed before platform forwarding so
    entity factories can make capability decisions from a complete inventory.
    The long-lived event listener starts only after those platforms subscribe.
    """
    client = GatewayClient(
        async_get_clientsession(hass),
        entry.data["url"],
        entry.data.get(CONF_API_TOKEN, ""),
    )
    coordinator = EufyGatewayCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    _remove_t817l_battery_entities(hass, coordinator)
    entry.runtime_data = GatewayRuntimeData(coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start_event_listener()
    return True


def _remove_t817l_battery_entities(
    hass: HomeAssistant, coordinator: EufyGatewayCoordinator
) -> None:
    """Remove entities created from T817L's non-battery compatibility fields.

    Version 0.1.28 treated three fields reported by this USB-C-powered model as
    battery capabilities. Removing only its known unique IDs repairs upgraded
    installations without touching genuine battery cameras or standalone
    sensors.
    """
    registry = er.async_get(hass)
    for serial, camera in coordinator.cameras.items():
        model = camera.get("model")
        if not isinstance(model, str) or not model.upper().startswith("T817L"):
            continue
        for platform, suffix in (
            ("sensor", "battery_level"),
            ("sensor", "battery_health"),
            ("sensor", "battery_temperature"),
            ("binary_sensor", "battery_charging"),
        ):
            entity_id = registry.async_get_entity_id(
                platform, DOMAIN, f"{serial}_{suffix}"
            )
            if entity_id is not None:
                registry.async_remove(entity_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: EufyGatewayConfigEntry
) -> bool:
    """Cancel entry-owned background work and unload all entity platforms."""
    await entry.runtime_data.coordinator.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
