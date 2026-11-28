"""A repair for a phone that has not sent anything a day after it was added.

Sensors are created on the first value (sensor.py), so a phone with no sensor in the
entity registry has never synced, also across a restart. Such a phone gets a repair a
day after it was added that says where to get the app and how to show the QR code
again; the first values take it away.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import DOMAIN, TROUBLESHOOTING_URL, store_links

NO_DATA_AFTER = timedelta(hours=24)


def issue_id(entry: ConfigEntry) -> str:
    return f"no_data_{entry.entry_id}"


def has_synced(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """True once the phone sent values: it then has at least one sensor."""
    registry = er.async_get(hass)
    return any(
        item.domain == "sensor"
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    )


@callback
def async_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise the repair for a phone without values, or clear it for one with them."""
    if has_synced(hass, entry):
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="no_data",
        translation_placeholders={
            "phone": entry.title,
            "help": TROUBLESHOOTING_URL,
            **store_links("ha-repair"),
        },
    )


@callback
def async_watch(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Check now if the phone was added more than a day ago, otherwise when it was."""
    if has_synced(hass, entry):
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
        return
    due = entry.created_at + NO_DATA_AFTER
    if dt_util.utcnow() >= due:
        async_check(hass, entry)
        return

    @callback
    def _due(_now: datetime) -> None:
        async_check(hass, entry)

    entry.async_on_unload(async_track_point_in_utc_time(hass, _due, due))


@callback
def async_synced(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The phone sent values: the repair, if any, goes."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
