"""Config flow for Halthy."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    ACTIVITY_LOG_MODE_OFF,
    ACTIVITY_LOG_MODE_PER_ENTITY_VERBOSE,
    ACTIVITY_LOG_MODE_SESSION_SUMMARY,
    CONF_APP_USERNAME,
    CONF_ACTIVITY_LOG_MODE,
    CONF_DISPLAY_NAME,
    CONF_OWNER_USER_ID,
    CONF_STATISTICS_ENABLED,
    CONF_TEMPERATURE_UNIT,
    CONF_WORKOUT_ARCHIVE_RETENTION,
    DEFAULT_ACTIVITY_LOG_MODE,
    DEFAULT_STATISTICS_ENABLED,
    DEFAULT_TEMPERATURE_UNIT,
    DEFAULT_WORKOUT_ARCHIVE_RETENTION,
    DOMAIN,
    MAX_WORKOUT_ARCHIVE_RETENTION,
    MIN_WORKOUT_ARCHIVE_RETENTION,
    TEMPERATURE_UNIT_CELSIUS,
    TEMPERATURE_UNIT_FAHRENHEIT,
    TEMPERATURE_UNIT_SYSTEM,
    VALID_ACTIVITY_LOG_MODES,
    VALID_TEMPERATURE_UNITS,
)


def _normalize_username(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def _bounded_workout_archive_retention(value: Any) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = DEFAULT_WORKOUT_ARCHIVE_RETENTION
    return max(
        MIN_WORKOUT_ARCHIVE_RETENTION,
        min(MAX_WORKOUT_ARCHIVE_RETENTION, normalized),
    )


def _entry_app_username(config_entry: config_entries.ConfigEntry) -> str:
    username = str(config_entry.data.get(CONF_APP_USERNAME) or "").strip()
    if username:
        return username
    return str(config_entry.data.get(CONF_DISPLAY_NAME) or config_entry.title or "").strip()


def _entry_display_name(config_entry: config_entries.ConfigEntry) -> str:
    return str(config_entry.data.get(CONF_DISPLAY_NAME) or "").strip()


class HalthyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Halthy."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return the options flow handler."""
        return HalthyOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            username = str(user_input[CONF_APP_USERNAME]).strip()
            display_name = str(user_input.get(CONF_DISPLAY_NAME, "")).strip()
            statistics_enabled = bool(
                user_input.get(CONF_STATISTICS_ENABLED, DEFAULT_STATISTICS_ENABLED)
            )
            normalized_username = _normalize_username(username)
            if not normalized_username:
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_APP_USERNAME, default=username): str,
                            vol.Optional(CONF_DISPLAY_NAME, default=display_name): str,
                            vol.Required(
                                CONF_STATISTICS_ENABLED,
                                default=statistics_enabled,
                            ): bool,
                        }
                    ),
                    errors={"base": "invalid_username"},
                )

            await self.async_set_unique_id(normalized_username)
            self._abort_if_unique_id_configured()

            owner_user_id = self.context.get("user_id")
            if not isinstance(owner_user_id, str) or not owner_user_id.strip():
                owner_user_id = None

            return self.async_create_entry(
                title=display_name or username,
                data={
                    CONF_APP_USERNAME: username,
                    CONF_DISPLAY_NAME: display_name,
                    CONF_OWNER_USER_ID: owner_user_id,
                    CONF_STATISTICS_ENABLED: statistics_enabled,
                },
            )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_APP_USERNAME): str,
                vol.Optional(CONF_DISPLAY_NAME): str,
                vol.Required(
                    CONF_STATISTICS_ENABLED,
                    default=DEFAULT_STATISTICS_ENABLED,
                ): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema)


class HalthyOptionsFlow(config_entries.OptionsFlow):
    """Handle Halthy options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    def _username_already_configured(self, normalized_username: str) -> bool:
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == self._config_entry.entry_id:
                continue
            if entry.unique_id == normalized_username:
                return True
            if _normalize_username(_entry_app_username(entry)) == normalized_username:
                return True
        return False

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        current_username = _entry_app_username(self._config_entry)
        current_display_name = _entry_display_name(self._config_entry)
        current_unit = str(
            self._config_entry.options.get(CONF_TEMPERATURE_UNIT, DEFAULT_TEMPERATURE_UNIT)
        ).strip()
        if current_unit not in VALID_TEMPERATURE_UNITS:
            current_unit = DEFAULT_TEMPERATURE_UNIT
        current_statistics_enabled = bool(
            self._config_entry.options.get(
                CONF_STATISTICS_ENABLED,
                self._config_entry.data.get(
                    CONF_STATISTICS_ENABLED,
                    DEFAULT_STATISTICS_ENABLED,
                ),
            )
        )
        current_activity_log_mode = str(
            self._config_entry.options.get(CONF_ACTIVITY_LOG_MODE, DEFAULT_ACTIVITY_LOG_MODE)
        ).strip()
        if current_activity_log_mode not in VALID_ACTIVITY_LOG_MODES:
            current_activity_log_mode = DEFAULT_ACTIVITY_LOG_MODE
        current_workout_archive_retention = _bounded_workout_archive_retention(
            self._config_entry.options.get(
                CONF_WORKOUT_ARCHIVE_RETENTION,
                DEFAULT_WORKOUT_ARCHIVE_RETENTION,
            )
        )

        if user_input is not None:
            username = str(user_input.get(CONF_APP_USERNAME, current_username)).strip()
            display_name = str(user_input.get(CONF_DISPLAY_NAME, current_display_name)).strip()
            normalized_username = _normalize_username(username)
            errors: dict[str, str] = {}

            if not normalized_username:
                errors["base"] = "invalid_username"
            elif self._username_already_configured(normalized_username):
                errors["base"] = "already_configured"

            selected_unit = str(
                user_input.get(CONF_TEMPERATURE_UNIT, DEFAULT_TEMPERATURE_UNIT)
            ).strip()
            if selected_unit not in VALID_TEMPERATURE_UNITS:
                selected_unit = DEFAULT_TEMPERATURE_UNIT
            selected_statistics_enabled = bool(
                user_input.get(CONF_STATISTICS_ENABLED, current_statistics_enabled)
            )
            selected_activity_log_mode = str(
                user_input.get(CONF_ACTIVITY_LOG_MODE, DEFAULT_ACTIVITY_LOG_MODE)
            ).strip()
            if selected_activity_log_mode not in VALID_ACTIVITY_LOG_MODES:
                selected_activity_log_mode = DEFAULT_ACTIVITY_LOG_MODE
            selected_workout_archive_retention = _bounded_workout_archive_retention(
                user_input.get(
                    CONF_WORKOUT_ARCHIVE_RETENTION,
                    current_workout_archive_retention,
                )
            )

            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._options_schema(
                        username=username,
                        display_name=display_name,
                        temperature_unit=selected_unit,
                        activity_log_mode=selected_activity_log_mode,
                        statistics_enabled=selected_statistics_enabled,
                        workout_archive_retention=selected_workout_archive_retention,
                    ),
                    errors=errors,
                )

            next_data = dict(self._config_entry.data)
            next_data[CONF_APP_USERNAME] = username
            next_data[CONF_DISPLAY_NAME] = display_name
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data=next_data,
                title=display_name or username,
                unique_id=normalized_username,
            )

            return self.async_create_entry(
                title="",
                data={
                    CONF_TEMPERATURE_UNIT: selected_unit,
                    CONF_ACTIVITY_LOG_MODE: selected_activity_log_mode,
                    CONF_STATISTICS_ENABLED: selected_statistics_enabled,
                    CONF_WORKOUT_ARCHIVE_RETENTION: selected_workout_archive_retention,
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(
                username=current_username,
                display_name=current_display_name,
                temperature_unit=current_unit,
                activity_log_mode=current_activity_log_mode,
                statistics_enabled=current_statistics_enabled,
                workout_archive_retention=current_workout_archive_retention,
            ),
        )

    def _options_schema(
        self,
        *,
        username: str,
        display_name: str,
        temperature_unit: str,
        activity_log_mode: str,
        statistics_enabled: bool,
        workout_archive_retention: int,
    ) -> vol.Schema:
        data_schema = vol.Schema(
            {
                vol.Required(CONF_APP_USERNAME, default=username): str,
                vol.Optional(CONF_DISPLAY_NAME, default=display_name): str,
                vol.Required(
                    CONF_STATISTICS_ENABLED,
                    default=statistics_enabled,
                ): bool,
                vol.Required(CONF_TEMPERATURE_UNIT, default=temperature_unit): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            TEMPERATURE_UNIT_SYSTEM,
                            TEMPERATURE_UNIT_CELSIUS,
                            TEMPERATURE_UNIT_FAHRENHEIT,
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="temperature_unit",
                    )
                ),
                vol.Required(
                    CONF_ACTIVITY_LOG_MODE,
                    default=activity_log_mode,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            ACTIVITY_LOG_MODE_OFF,
                            ACTIVITY_LOG_MODE_SESSION_SUMMARY,
                            ACTIVITY_LOG_MODE_PER_ENTITY_VERBOSE,
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="activity_log_mode",
                    )
                ),
                vol.Required(
                    CONF_WORKOUT_ARCHIVE_RETENTION,
                    default=workout_archive_retention,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_WORKOUT_ARCHIVE_RETENTION,
                        max=MAX_WORKOUT_ARCHIVE_RETENTION,
                        step=25,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return data_schema
