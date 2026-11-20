"""Diagnostics for Life Dashboard: what a bug report needs, without the health data.

Settings > Devices & services > Life Dashboard > three dots > Download diagnostics.
The secret and the webhook id are redacted; sensor values are not included at all,
only which sensors exist and when each was last measured, how many apps have a sensor
of their own but not which, how much history the ledger holds per statistic and how many
workouts it keeps, and for the readings that go to the phone which entities are mapped and
how many wait or were written, never a value or a measured moment.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CLOUDHOOK_URL, CONF_SECRET, CONF_WEBHOOK_ID

TO_REDACT = {CONF_SECRET, CONF_WEBHOOK_ID, CONF_CLOUDHOOK_URL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    ledger = runtime.history.ledger if runtime.history else None
    writeback = runtime.writeback

    def span(keys: dict[str, Any]) -> dict[str, Any]:
        ordered = sorted(keys)
        return (
            {"count": len(ordered), "first": ordered[0], "last": ordered[-1]}
            if ordered
            else {"count": 0}
        )

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "app_version": runtime.app_version,
        "sensors": {
            key: {"measured_at": update.measured_at.isoformat()}
            for key, update in sorted(runtime.latest.items())
        },
        # How many apps have a sensor of their own, never which.
        "app_sensors": len(runtime.apps.labels) if runtime.apps else 0,
        "history": {
            "recorder_available": bool(runtime.history and runtime.history.available),
            "days": {key: span(days) for key, days in sorted(ledger.days.items())}
            if ledger
            else {},
            "sessions": {key: len(records) for key, records in sorted(ledger.sessions.items())}
            if ledger
            else {},
            "hours": {key: span(hours) for key, hours in sorted(ledger.hours.items())}
            if ledger
            else {},
            # How many workouts of yesterday and today are kept, never what they were.
            "workouts": len(ledger.workouts) if ledger else 0,
            # How many records their details are read from, per series; never the values.
            "workout_records": _workout_records(ledger.activity) if ledger else {},
            # How many sleep sessions have their stages kept, never what they were.
            "sleep_nights": len(ledger.sleep.get("nights", {})) if ledger else 0,
            # The day the ledger keeps from once it was pruned; never what it carries.
            "kept_from": ledger.kept_from if ledger else None,
        },
        "writeback": {
            "configured": {
                kind: {
                    "entities": dict(mapping.entities),
                    "time_entity": mapping.time_entity,
                    **mapping.extra,
                }
                for kind, mapping in writeback.mappings.items()
            }
            if writeback
            else {},
            **(writeback.queue.counts() if writeback else {}),
        },
    }


def _workout_records(activity: dict[str, Any]) -> dict[str, int]:
    counts = {series: len(rows) for series, rows in sorted(activity.get("add", {}).items())}
    if heart_rate := activity.get("heart_rate"):
        counts["heart_rate"] = len(heart_rate)
    return counts
