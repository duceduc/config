"""Mobile Weather Forecast: Open-Meteo weather for the position of a tracked entity."""
from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import BUILD, DOMAIN
from .coordinator import MobileWeatherForecastConfigEntry, MobileWeatherForecastCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.WEATHER]


async def async_setup_entry(hass: HomeAssistant, entry: MobileWeatherForecastConfigEntry) -> bool:
    """Set up a config entry."""
    coordinator = MobileWeatherForecastCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    integration = await async_get_integration(hass, DOMAIN)
    coordinator.sw_version = f"{integration.version} (build {BUILD})"
    _LOGGER.info("Mobile Weather Forecast %s", coordinator.sw_version)

    entry.async_on_unload(coordinator.async_start_tracking())
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MobileWeatherForecastConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_on_update(hass: HomeAssistant, entry: MobileWeatherForecastConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
