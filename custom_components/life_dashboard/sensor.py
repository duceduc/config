"""Sensor platform for Life Dashboard.

Entities are created on the first value rather than all thirty up front, so a user
who syncs four data types sees four sensors. That is how the app's MQTT Discovery
behaves as well, and it keeps a phone's absent types out of the entity list.

Across a restart the entities come back before any payload does: they are rebuilt
from the entity registry and restore their own last value, which also reseeds the
ordering rule so a backfill arriving right after startup cannot overwrite newer
readings.

Screen time also gets one sensor per app, created disabled so the device page stays as
it was: the user enables the apps worth a sensor. Those are rebuilt from apps.py rather
than restored, because a disabled entity never holds a state to restore.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LifeDashboardConfigEntry, signal_update
from .apps import app_unique_id
from .const import DOMAIN, MANUFACTURER, MODEL
from .payload import (
    KEY_SCREEN_TIME_APPS,
    SENSOR_SPECS,
    SensorSpec,
    SensorUpdate,
    is_fallback_label,
    parse_instant,
    state_value,
    tile_hours,
)
from .workout_details import DETAIL_KEYS

_LOGGER = logging.getLogger(__name__)

# The attributes a payload can put on a sensor. Restoring by an allowlist keeps
# Home Assistant's own additions (friendly_name, unit_of_measurement, device_class
# and so on) out of the restored update, which a blocklist would keep missing as
# Home Assistant adds more of them.
_PAYLOAD_ATTRIBUTES = frozenset(
    {
        "source",
        "uuid",
        "date",
        "sample_count",
        "min",
        "max",
        "sources",
        "app_count",
        "top_apps",
        "apps",
        "all_apps_minutes",
        "package",
        "minutes",
        "app_version",
        "backfill",
        "record_count",
        "device",
        "start",
        "end",
        "duration_minutes",
        "type_code",
        "sessions",
        # The last-night sensors (sleep.py).
        "night",
        "stages",
        "asleep_minutes",
        "deep_minutes",
        "light_minutes",
        "rem_minutes",
        "awake_minutes",
        # The details of Last workout (workout_details.DETAIL_KEYS).
        *DETAIL_KEYS,
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LifeDashboardConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors, and keep adding them as new data types arrive."""
    known: set[str] = set()

    # Entities that already existed keep existing, so history and dashboards
    # survive a restart even before the phone syncs again.
    prefix = f"{entry.entry_id}_"
    restored = [
        registry_entry.unique_id.removeprefix(prefix)
        for registry_entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if registry_entry.domain == "sensor"
        and registry_entry.unique_id.startswith(prefix)
        and registry_entry.unique_id.removeprefix(prefix) in SENSOR_SPECS
    ]
    if restored:
        known.update(restored)
        async_add_entities(LifeDashboardSensor(entry, key) for key in restored)

    # The app sensors come back from the roster, with the label they had, also the
    # disabled ones: the registry entry takes its name from the entity every startup.
    # Those the user deleted, also while the entry was not loaded, stay deleted.
    apps = entry.runtime_data.apps
    entry.async_on_unload(apps.async_track_registry(hass))
    if apps.labels:
        async_add_entities(
            AppScreenTimeSensor(entry, package, label) for package, label in apps.labels.items()
        )

    @callback
    def _new_key(update: SensorUpdate) -> None:
        """Add an entity the first time its data type shows up.

        Entities that already exist get the same signal themselves, so there is
        nothing to hand over here.
        """
        if update.key == KEY_SCREEN_TIME_APPS:
            if added := apps.async_take(update):
                async_add_entities(
                    AppScreenTimeSensor(entry, package, apps.labels[package]) for package in added
                )
            return
        if update.key in known or update.key not in SENSOR_SPECS:
            return
        known.add(update.key)
        async_add_entities([LifeDashboardSensor(entry, update.key)])

    entry.async_on_unload(async_dispatcher_connect(hass, signal_update(entry.entry_id), _new_key))


def _device_info(entry: LifeDashboardConfigEntry) -> DeviceInfo:
    """The one device of the phone, which every sensor hangs under."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=MODEL,
        sw_version=entry.runtime_data.app_version,
    )


# Sensors in minutes before 1.0.0 and in hours since, so a tile reads 7h 30m.
_HOURS_SINCE_1_0: Final = frozenset(
    {"sleep_duration", "screen_time_today", "screen_time_yesterday"}
)


class LifeDashboardSensor(RestoreSensor):
    """One value from the phone."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # A night's stages and a day's apps can be over the size the recorder keeps of a
    # state's attributes; the long-term statistics hold their history instead.
    _unrecorded_attributes = frozenset({"stages", "apps"})

    def __init__(self, entry: LifeDashboardConfigEntry, key: str) -> None:
        """Set up the sensor from its spec."""
        spec: SensorSpec = SENSOR_SPECS[key]
        self._key = key
        self._entry = entry

        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = (
            SensorDeviceClass(spec.device_class) if spec.device_class else None
        )
        self._attr_state_class = SensorStateClass(spec.state_class) if spec.state_class else None
        self._attr_options = list(spec.options) if spec.options else None
        self._attr_suggested_display_precision = spec.precision
        self._attr_entity_category = EntityCategory.DIAGNOSTIC if spec.diagnostic else None
        self._attr_entity_registry_enabled_default = spec.enabled
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Take the value we have, or the one we had before the restart."""
        await super().async_added_to_hass()

        if (held := self._entry.runtime_data.latest.get(self._key)) is not None:
            # The payload that created this entity is already in.
            self._apply(held)
        elif (restored := await self._restore()) is not None:
            self._apply(restored)
            # Seed the ordering rule, so an old batch arriving now is still old.
            self._entry.runtime_data.latest.setdefault(self._key, restored)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry.entry_id), self._handle_update
            )
        )

    async def _restore(self) -> SensorUpdate | None:
        """Rebuild the last update from what Home Assistant kept for us.

        Without a measured_at there is nothing to order on, so the value is dropped
        rather than treated as infinitely old or infinitely new.
        """
        stored = await self.async_get_last_sensor_data()
        state = await self.async_get_last_state()
        if stored is None or state is None or stored.native_value is None:
            return None

        try:
            measured_at = parse_instant(state.attributes.get("measured_at"))
        except ValueError:
            return None

        last_reset = None
        if raw_reset := state.attributes.get("last_reset"):
            try:
                last_reset = parse_instant(raw_reset)
            except ValueError:
                last_reset = None

        attributes = {
            name: value for name, value in state.attributes.items() if name in _PAYLOAD_ATTRIBUTES
        }
        value = stored.native_value
        # Kept before 1.0.0, when these sensors were still in minutes and metres.
        if (
            self._key in _HOURS_SINCE_1_0
            and stored.native_unit_of_measurement == UnitOfTime.MINUTES
            and isinstance(value, int | float)
        ):
            value = value / 60
        if (
            self._key == "distance_today"
            and stored.native_unit_of_measurement == UnitOfLength.METERS
            and isinstance(value, int | float)
        ):
            value = value / 1000

        return SensorUpdate(
            key=self._key,
            value=value,
            measured_at=measured_at,
            attributes=attributes,
            last_reset=last_reset,
        )

    @callback
    def _handle_update(self, update: SensorUpdate) -> None:
        """Take an update meant for this sensor."""
        if update.key != self._key:
            return
        self._apply(update)
        self.async_write_ha_state()

    @callback
    def _apply(self, update: SensorUpdate) -> None:
        """Hold a value, without writing state: callers decide when to do that."""
        self._attr_native_value = state_value(self._key, update.value)
        self._attr_extra_state_attributes = {
            **update.attributes,
            "measured_at": update.measured_at.isoformat(),
        }


class AppScreenTimeSensor(SensorEntity):
    """One app's minutes on the newest day the phone sent.

    Its state comes from the table of every app, so an app that is missing from a newer
    day, whether unused or left out by the app filter since, reads 0 for that day
    instead of keeping the day before. Like Screen time today it has no state class:
    its history is the recorder's state history, not long-term statistics.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "app_screen_time"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_suggested_display_precision = 3
    # Fifty of these on the device page would bury the rest; the user picks a few.
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: LifeDashboardConfigEntry, package: str, label: str) -> None:
        """Set up the sensor for one package; the label only names it."""
        self._entry = entry
        self._package = package
        self._attr_unique_id = app_unique_id(entry.entry_id, package)
        self._attr_translation_placeholders = {"app": label}
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Take the newest table there is, also one stored before a restart."""
        await super().async_added_to_hass()
        if (held := self._entry.runtime_data.latest.get(KEY_SCREEN_TIME_APPS)) is not None:
            self._apply(held)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry.entry_id), self._handle_update
            )
        )

    @callback
    def _handle_update(self, update: SensorUpdate) -> None:
        if update.key != KEY_SCREEN_TIME_APPS:
            return
        self._apply(update)
        self.async_write_ha_state()

    def _label(self, row: dict[str, Any] | None) -> str:
        """The app's name now: from the table, unless that is only the stand-in the phone
        sends for an app uninstalled since; then the one the roster kept."""
        if row and row.get("name") and not is_fallback_label(self._package, row["name"]):
            return row["name"]
        return self._entry.runtime_data.apps.labels.get(self._package, self._package)

    @callback
    def _apply(self, update: SensorUpdate) -> None:
        """Hold this app's row of a table, or 0 when the day does not have it."""
        row = update.attributes.get("apps", {}).get(self._package)
        # In hours, so a tile reads 1h 25m; the attributes keep minutes.
        self._attr_native_value = tile_hours(row["minutes"]) if row and row["minutes"] else 0
        self._attr_extra_state_attributes = {
            # The current label: the entity name takes a new one when it is rebuilt.
            "app": self._label(row),
            "package": self._package,
            "date": update.attributes.get("date"),
            "week_minutes": row["week_minutes"] if row else 0,
        }
