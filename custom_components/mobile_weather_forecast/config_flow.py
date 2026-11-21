"""Config flow for Mobile Weather Forecast."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_ENTITY_ID,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .util import extract_coordinates


def _interval_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_SCAN_INTERVAL,
            max=MAX_SCAN_INTERVAL,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )


class MobileWeatherForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configuration through the UI."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID]
            state = self.hass.states.get(entity_id)
            if extract_coordinates(state) is None:
                errors["base"] = "no_coordinates"
            else:
                await self.async_set_unique_id(entity_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_ENTITY_ID: entity_id},
                    options={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])},
                )

        default_name = "Météo" if self.hass.config.language.startswith("fr") else "Weather"
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=default_name): str,
                vol.Required(CONF_ENTITY_ID): EntitySelector(
                    EntitySelectorConfig(domain=["person", "device_tracker", "sensor"])
                ),
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): _interval_selector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return MobileWeatherForecastOptionsFlow()


class MobileWeatherForecastOptionsFlow(OptionsFlow):
    """Options: update interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schema = vol.Schema(
            {vol.Required(CONF_SCAN_INTERVAL, default=current): _interval_selector()}
        )
        return self.async_show_form(step_id="init", data_schema=schema)
