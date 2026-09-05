"""Calendar platform for archived Halthy workouts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .runtime import HalthyWorkoutRecord, IntegrationRuntime, runtime_device_identifiers
from .const import DOMAIN, MANUFACTURER, workout_calendar_updated_signal
from .naming import sanitize_identifier


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one read-only workout calendar per Halthy person."""

    runtime: IntegrationRuntime = hass.data[DOMAIN]["entries"][entry.entry_id]
    async_add_entities([HalthyWorkoutCalendar(runtime, entry.entry_id)])


def _first_number(metadata: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw_value = metadata.get(key)
        if isinstance(raw_value, bool) or raw_value is None:
            continue
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            continue
    return None


def _format_duration(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min"
    return f"{seconds} sec"


def _workout_description(record: HalthyWorkoutRecord) -> str | None:
    metadata = record.metadata
    lines: list[str] = []

    duration = _first_number(
        metadata,
        "workout_duration_s",
        "duration_s",
        "duration_seconds",
        "duration",
    )
    if duration is None:
        duration = (record.end - record.start).total_seconds()
    lines.append(f"Duration: {_format_duration(duration)}")

    distance = _first_number(metadata, "workout_distance_m", "distance_m", "distance")
    if distance is not None and distance > 0:
        formatted_distance = f"{distance / 1000:.2f} km" if distance >= 1000 else f"{distance:.0f} m"
        lines.append(f"Distance: {formatted_distance}")

    energy = _first_number(
        metadata,
        "workout_active_energy_kcal",
        "active_energy_kcal",
        "energy_kcal",
    )
    if energy is not None and energy > 0:
        lines.append(f"Active energy: {energy:.0f} kcal")

    heart_rate = _first_number(
        metadata,
        "workout_avg_heart_rate_bpm",
        "avg_heart_rate_bpm",
    )
    if heart_rate is not None and heart_rate > 0:
        lines.append(f"Average heart rate: {heart_rate:.0f} bpm")

    cadence = _first_number(metadata, "cadence_spm", "avg_cadence_spm")
    if cadence is not None and cadence > 0:
        lines.append(f"Average cadence: {cadence:.0f} spm")

    speed = _first_number(
        metadata,
        "workout_avg_speed_mps",
        "avg_speed_mps",
        "average_speed_mps",
    )
    if speed is not None and speed > 0:
        lines.append(f"Average speed: {speed * 3.6:.1f} km/h")

    return "\n".join(lines) if lines else None


def workout_calendar_event(record: HalthyWorkoutRecord) -> CalendarEvent:
    """Convert a stored workout to Home Assistant's calendar model."""

    return CalendarEvent(
        start=record.start,
        end=record.end,
        summary=record.summary,
        description=_workout_description(record),
        uid=record.uid,
    )


class HalthyWorkoutCalendar(CalendarEntity):
    """Read-only calendar containing workouts for one configured person."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:calendar-heart"

    def __init__(self, runtime: IntegrationRuntime, entry_id: str) -> None:
        self._runtime = runtime
        self._entry_id = entry_id
        username = sanitize_identifier(runtime.configured_username)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_workouts"
        self._attr_suggested_object_id = f"{username}_workouts"
        self._attr_name = f"{runtime.display_name} workouts"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers=runtime_device_identifiers(self._runtime),
            manufacturer=MANUFACTURER,
            model="iOS App",
            name=self._runtime.display_name,
        )

    def _ordered_records(self) -> list[HalthyWorkoutRecord]:
        return sorted(
            self._runtime.workouts.values(),
            key=lambda item: (item.start, item.end, item.record_id),
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the active or next workout, if one exists."""

        now = dt_util.now()
        record = min(
            (item for item in self._runtime.workouts.values() if item.end > now),
            key=lambda item: (item.start, item.end, item.record_id),
            default=None,
        )
        return workout_calendar_event(record) if record is not None else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return workouts intersecting the requested calendar range."""

        return [
            workout_calendar_event(record)
            for record in self._ordered_records()
            if record.end > start_date and record.start < end_date
        ]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _handle_workout_update() -> None:
            self.async_write_ha_state()
            self.async_update_event_listeners()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                workout_calendar_updated_signal(self._entry_id),
                _handle_workout_update,
            )
        )
