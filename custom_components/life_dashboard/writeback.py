"""Receiving measurements on the phone: the Home Assistant half.

writeback_queue.py decides what a measurement is and what the phone is told; this
module is the thin layer around it that listens to the mapped entities, turns a state
into a reading in the units Health Connect wants, keeps the queue in .storage and
answers the phone's request. The options flow stores the mapping under
entry.options["writeback"]; a change there reloads the entry, which sets the
listeners again.

What a state event becomes:

- A state that is unknown, unavailable or not a number is skipped. So is the first
  state of an entity and a state Home Assistant restored at startup: a restored
  value's last_changed is the restart, not a measurement.
- The measured moment is the timestamp entity's value when one is mapped and it
  changed within the window around the event; otherwise last_changed (last_reported
  for an unchanged value that was reported again), marked time_source "state".
  Never last_updated. A timestamp entity that changes just after its value moves the
  reading it belongs to.
- Units are converted per event with Home Assistant's own converters, on the unit the
  state carries at that moment, without rounding. An entity with a unit outside the
  family (a weight in litres) is skipped and warned about once.
- Blood pressure is two entities; the two halves within ninety seconds of each other
  are one reading, timed on the systolic one.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_RESTORED,
    ATTR_UNIT_OF_MEASUREMENT,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfLength,
    UnitOfMass,
    UnitOfPressure,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    DistanceConverter,
    MassConverter,
    PressureConverter,
)

from .const import CONF_PLATFORM, DOMAIN, PLATFORM_IPHONE
from .payload import parse_instant, response_body
from .writeback_queue import (
    CODE_TOO_OLD,
    FIELD_DIASTOLIC,
    FIELD_SYSTOLIC,
    MAX_AGE,
    RECORDING_ACTIVE,
    RECORDING_AUTO,
    RECORDING_MANUAL,
    TIME_SOURCE_STATE,
    TYPE_BLOOD_PRESSURE,
    TYPE_BODY_FAT,
    TYPE_HEIGHT,
    VALUE_FIELDS,
    WRITEBACK_TYPES,
    Reading,
    WritebackQueue,
    configured_types,
    out_of_range,
    reading_id,
)

_LOGGER = logging.getLogger(__name__)

OPTION_WRITEBACK: Final = "writeback"
#: Requests in a row that left a mapped type out before the repair appears: one is a
#: moment, three is a phone where the type was switched off or lost its permission.
NOT_ASKED_ROUNDS: Final = 3
CONF_ENTITY: Final = "entity"
CONF_TIME_ENTITY: Final = "time_entity"
CONF_SYSTOLIC: Final = FIELD_SYSTOLIC
CONF_DIASTOLIC: Final = FIELD_DIASTOLIC
CONF_BODY_POSITION: Final = "body_position"
CONF_MEASUREMENT_LOCATION: Final = "measurement_location"

STORAGE_VERSION: Final = 1
SAVE_DELAY_SECONDS: Final = 5

#: The two halves of a blood pressure reading, and a timestamp entity and its value,
#: belong together when they are this close.
WINDOW: Final = timedelta(seconds=90)

DEVICE_TYPE_SCALE: Final = "scale"
DEVICE_TYPE_UNKNOWN: Final = "unknown"
DEVICE_CLASS_WEIGHT: Final = "weight"

#: The backfill: the default window, the most the service accepts (the queue keeps
#: no more than that anyway), and the most states taken per entity per call.
BACKFILL_DEFAULT_DAYS: Final = 30
BACKFILL_MAX_DAYS: Final = MAX_AGE.days
BACKFILL_MAX_PER_ENTITY: Final = 1000


# --- Units -----------------------------------------------------------------------


@dataclass(frozen=True)
class UnitFamily:
    """How a type's values reach the unit Health Connect wants."""

    name: str
    target: str
    error: str
    converter: type[MassConverter] | type[DistanceConverter] | type[PressureConverter] | None

    def accepts(self, unit: str | None) -> bool:
        if self.converter is None:
            return unit == self.target
        return unit in self.converter.VALID_UNITS

    def convert(self, value: float, unit: str | None) -> float | None:
        if not self.accepts(unit):
            return None
        if self.converter is None:
            return value
        return self.converter.convert(value, unit, self.target)


_MASS = UnitFamily("mass", UnitOfMass.KILOGRAMS, "unit_not_mass", MassConverter)
_LENGTH = UnitFamily("length", UnitOfLength.METERS, "unit_not_length", DistanceConverter)
_PERCENT = UnitFamily("percentage", PERCENTAGE, "unit_not_percentage", None)
_PRESSURE = UnitFamily("pressure", UnitOfPressure.MMHG, "unit_not_pressure", PressureConverter)

UNIT_FAMILIES: Final[dict[str, UnitFamily]] = {
    TYPE_HEIGHT: _LENGTH,
    TYPE_BODY_FAT: _PERCENT,
    TYPE_BLOOD_PRESSURE: _PRESSURE,
    **{
        kind: _MASS
        for kind in WRITEBACK_TYPES
        if kind not in (TYPE_HEIGHT, TYPE_BODY_FAT, TYPE_BLOOD_PRESSURE)
    },
}


def unit_error(kind: str, unit: str | None) -> str | None:
    """The options flow error for an entity whose unit does not fit the type, or None."""
    family = UNIT_FAMILIES[kind]
    return None if family.accepts(unit) else family.error


# --- The mapping -----------------------------------------------------------------


@dataclass(frozen=True)
class TypeMapping:
    """One type as the options map it: which entities, and the fixed fields."""

    kind: str
    #: role -> entity_id. "entity" for a single value; "systolic" and "diastolic".
    entities: dict[str, str]
    time_entity: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def anchor(self) -> str:
        """The entity the id is formed on: the value, or the systolic half."""
        return self.entities.get(CONF_ENTITY) or self.entities[CONF_SYSTOLIC]


def parse_mapping(options: dict[str, Any] | None) -> dict[str, TypeMapping]:
    """The mappings the options hold, only for types that are complete."""
    raw = (options or {}).get(OPTION_WRITEBACK)
    mappings: dict[str, TypeMapping] = {}
    for kind in configured_types(raw):
        slots = raw[kind]
        time_entity = slots.get(CONF_TIME_ENTITY) or None
        if kind == TYPE_BLOOD_PRESSURE:
            extra = {
                name: slots[name]
                for name in (CONF_BODY_POSITION, CONF_MEASUREMENT_LOCATION)
                if isinstance(slots.get(name), str) and slots[name]
            }
            mappings[kind] = TypeMapping(
                kind,
                {CONF_SYSTOLIC: slots[CONF_SYSTOLIC], CONF_DIASTOLIC: slots[CONF_DIASTOLIC]},
                time_entity,
                extra,
            )
        else:
            mappings[kind] = TypeMapping(kind, {CONF_ENTITY: slots[CONF_ENTITY]}, time_entity)
    return mappings


# --- Blood pressure -------------------------------------------------------------


@dataclass
class _Half:
    role: str
    value: float
    at: datetime


class BloodPressurePairer:
    """Joins the systolic and diastolic halves that arrive as separate events.

    The first half opens a window; the other half inside it closes it and yields the
    pair, timed on the systolic event. A half that is never joined is dropped by the
    next one. The same class serves the live listeners and the backfill, so both form
    the same readings from the same events.
    """

    def __init__(self, window: timedelta = WINDOW) -> None:
        self._window = window
        self._half: _Half | None = None

    def offer(self, role: str, value: float, at: datetime) -> tuple[float, float, datetime] | None:
        half = self._half
        if half is not None and half.role != role and abs(at - half.at) <= self._window:
            self._half = None
            systolic, diastolic = (half, _Half(role, value, at))
            if role == CONF_SYSTOLIC:
                systolic, diastolic = diastolic, systolic
            return systolic.value, diastolic.value, systolic.at
        self._half = _Half(role, value, at)
        return None


# --- The manager -------------------------------------------------------------------


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.writeback")


async def async_remove_store(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the queue with the entry: it holds the values of readings for the phone."""
    await _store(hass, entry.entry_id).async_remove()


class WritebackManager:
    """Keeps one phone's queue and feeds it from the mapped entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store = _store(hass, entry.entry_id)
        self.queue = WritebackQueue()
        #: False for an iPhone: the iOS app has no Receive, so nothing is queued for it
        #: and the mapping, kept in the options, is left alone.
        self.receives = entry.data.get(CONF_PLATFORM) != PLATFORM_IPHONE
        self.mappings = parse_mapping(entry.options) if self.receives else {}
        self._pairers: dict[str, BloodPressurePairer] = {}
        self._warned_units: set[str] = set()
        #: entity_id -> (kind, role) for the value entities.
        self._roles: dict[str, tuple[str, str]] = {}
        #: time entity -> kinds it times.
        self._timed: dict[str, list[str]] = {}
        for kind, mapping in self.mappings.items():
            for role, entity_id in mapping.entities.items():
                self._roles[entity_id] = (kind, role)
            if mapping.time_entity:
                self._timed.setdefault(mapping.time_entity, []).append(kind)

    @property
    def configured(self) -> list[str]:
        return list(self.mappings)

    @property
    def version(self) -> str:
        return self._entry.runtime_data.version

    # --- Lifecycle -------------------------------------------------------------------

    async def async_load(self) -> None:
        self.queue = WritebackQueue.from_dict(await self._store.async_load())

    async def async_flush(self) -> None:
        await self._store.async_save(self.queue.to_dict())

    @callback
    def async_start(self) -> None:
        """Listen to the mapped entities, changed and reported alike.

        A type whose mapping is gone loses its pending readings first: taking a
        mapping away is the way out of a queue that got stuck on one.
        """
        for kind in {reading.type for reading in self.queue.pending.values()}:
            if kind not in self.mappings and (gone := self.queue.clear_type(kind)):
                _LOGGER.info(
                    "Dropped %s pending %s readings for %s: %s",
                    gone,
                    kind,
                    self._entry.title,
                    "the type is no longer mapped" if self.receives else "an iPhone has no Receive",
                )
                self._save()
        self._seed()
        self._sync_issues()
        entity_ids = [*self._roles, *self._timed]
        if not entity_ids:
            return
        self._entry.async_on_unload(
            async_track_state_change_event(self._hass, entity_ids, self._on_changed)
        )
        self._entry.async_on_unload(
            async_track_state_report_event(self._hass, entity_ids, self._on_reported)
        )

    def _seed(self) -> None:
        """Let the debounce start from the values the entities hold right now.

        Polled integrations write their state again after a restart without anything
        having been measured. A value equal to the last measurement of that entity
        continues it, so the first reports after startup do not become a reading.
        """
        now = dt_util.utcnow()
        for entity_id, (kind, _) in self._roles.items():
            state = self._hass.states.get(entity_id)
            if state is None or (seen := self.queue.seen.get(entity_id)) is None:
                continue
            value = self._converted(kind, entity_id, state)
            if value is None:
                continue
            field_name = FIELD_SYSTOLIC if kind == TYPE_BLOOD_PRESSURE else VALUE_FIELDS[kind]
            if seen.values.get(field_name) == value and seen.time < now:
                seen.time = now

    def _save(self) -> None:
        self._store.async_delay_save(self.queue.to_dict, SAVE_DELAY_SECONDS)

    # --- Events -----------------------------------------------------------------------

    @callback
    def _on_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        old_state = event.data["old_state"]
        if new_state is None or old_state is None or new_state.attributes.get(ATTR_RESTORED):
            return
        entity_id = event.data["entity_id"]
        if entity_id in self._timed:
            self._on_time_entity(entity_id, new_state)
        if entity_id in self._roles:
            # An unchanged value whose attributes changed is a report of that value.
            moment = (
                new_state.last_changed
                if old_state.state != new_state.state
                else new_state.last_reported
            )
            self._on_value(entity_id, new_state, moment)

    @callback
    def _on_reported(self, event: Event[EventStateReportedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id in self._roles:
            self._on_value(entity_id, event.data["new_state"], event.data["last_reported"])

    def _on_value(self, entity_id: str, state: State, moment: datetime) -> None:
        kind, role = self._roles[entity_id]
        value = self._converted(kind, entity_id, state)
        if value is None:
            return
        mapping = self.mappings[kind]

        if kind == TYPE_BLOOD_PRESSURE:
            pairer = self._pairers.setdefault(kind, BloodPressurePairer())
            pair = pairer.offer(role, value, moment)
            if pair is None:
                return
            systolic, diastolic, moment = pair
            values = {FIELD_SYSTOLIC: systolic, FIELD_DIASTOLIC: diastolic}
        else:
            values = {VALUE_FIELDS[kind]: value}

        measured_at, time_source = self._measured_time(mapping, moment)
        self._offer(mapping, values, measured_at, time_source)

    def _on_time_entity(self, entity_id: str, state: State) -> None:
        """The timestamp entity changed: move the reading it belongs to, if any."""
        try:
            measured_at = parse_instant(state.state)
        except ValueError:
            return
        for kind in self._timed[entity_id]:
            anchor = self.mappings[kind].anchor
            recent = [
                reading
                for reading in self.queue.pending.values()
                if reading.entity_id == anchor
                and reading.time_source == TIME_SOURCE_STATE
                and abs(state.last_changed - reading.time) <= WINDOW
            ]
            if not recent:
                continue
            latest = max(recent, key=lambda reading: reading.time)
            if self.queue.rekey(latest.id, measured_at) is not None:
                _LOGGER.debug("Moved a %s reading of %s to its timestamp", kind, anchor)
                self._save()

    # --- From a state to a reading --------------------------------------------------

    def _converted(self, kind: str, entity_id: str, state: State) -> float | None:
        """The state as a number in the unit Health Connect wants, or None to skip."""
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if not math.isfinite(value):
            # "nan" and "inf" parse as floats; neither is a measurement, and either
            # would make the answer unreadable for the phone.
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        converted = UNIT_FAMILIES[kind].convert(value, unit)
        if converted is None and entity_id not in self._warned_units:
            self._warned_units.add(entity_id)
            _LOGGER.warning(
                "%s has unit %r, which is not a %s unit; its readings are not sent to %s",
                entity_id,
                unit,
                UNIT_FAMILIES[kind].name,
                self._entry.title,
            )
        return converted

    def _measured_time(self, mapping: TypeMapping, moment: datetime) -> tuple[datetime, str | None]:
        """The timestamp entity's value if it changed within the window, else the event."""
        candidates: list[tuple[datetime, datetime]] = []
        if mapping.time_entity and (state := self._hass.states.get(mapping.time_entity)):
            candidates = _timestamps([state])
        return _time_from(candidates, moment)

    def _offer(
        self,
        mapping: TypeMapping,
        values: dict[str, float],
        measured_at: datetime,
        time_source: str | None,
    ) -> Reading | None:
        anchor = mapping.anchor
        if (bad := out_of_range(mapping.kind, values)) is not None:
            # The app would refuse it as out_of_range; a 0 from a scale that has just
            # been created, or a helper's initial value, is not worth a round trip.
            _LOGGER.debug(
                "Skipped a %s reading from %s: %s is out of range", mapping.kind, anchor, bad
            )
            return None
        reading = Reading(
            id=reading_id(anchor, measured_at),
            version=1,
            type=mapping.kind,
            entity_id=anchor,
            time=measured_at,
            values=values,
            zone_offset=_zone_offset(measured_at),
            recording_method=_recording_method(mapping.kind, anchor),
            device=self._device(anchor),
            time_source=time_source,
            extra=dict(mapping.extra),
        )
        stored = self.queue.offer(reading)
        if stored is None:
            return None
        _LOGGER.debug(
            "Queued %s from %s for %s (version %s)",
            mapping.kind,
            anchor,
            self._entry.title,
            stored.version,
        )
        self._save()
        return stored

    def _device(self, entity_id: str) -> dict[str, str] | None:
        """Manufacturer and model from the device registry; a scale when it weighs."""
        registry_entry = er.async_get(self._hass).async_get(entity_id)
        state = self._hass.states.get(entity_id)
        device_class = (state.attributes.get(ATTR_DEVICE_CLASS) if state else None) or (
            registry_entry.device_class or registry_entry.original_device_class
            if registry_entry
            else None
        )
        device_type = DEVICE_TYPE_SCALE if device_class == DEVICE_CLASS_WEIGHT else None
        device = (
            dr.async_get(self._hass).async_get(registry_entry.device_id)
            if registry_entry and registry_entry.device_id
            else None
        )
        if device is None and device_type is None:
            return None
        info: dict[str, str] = {"type": device_type or DEVICE_TYPE_UNKNOWN}
        if device is not None:
            if device.manufacturer:
                info["manufacturer"] = device.manufacturer
            if device.model:
                info["model"] = device.model
        return info

    # --- The backfill -----------------------------------------------------------------

    async def async_queue_history(self, *, days: int, types: list[str] | None = None) -> int:
        """Offer the recorder's past states of the mapped entities, as if they were live.

        The same id rule and the same pairing as the listeners use, so a reading the
        live path already queued, or the phone already wrote or refused, is
        recognised and not queued again, and a second press changes nothing. From the
        recorder's states, not from long-term statistics: an hourly mean is not a
        measurement.

        The window is at most what the queue keeps, and of the changes inside it the
        newest BACKFILL_MAX_PER_ENTITY count. Home Assistant's history only reverses
        a query's result in Python, so a limit in the query would take the oldest
        rows; the cap is applied here instead. The state the entity held when the
        window opened is asked for as well: it is not a reading (it is older than the
        window), but it says that the entity existed before, and for blood pressure
        it is the last known value of a half that did not change. Without it, the
        first change in the window is the entity's creation and is skipped, as the
        listeners skip an entity's first state.
        """
        if "recorder" not in self._hass.config.components:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="recorder_missing"
            )
        # Imported here: recorder is an after_dependency and may be absent.
        from homeassistant.components.recorder import get_instance, history

        now = dt_util.utcnow()
        days = max(1, min(days, BACKFILL_MAX_DAYS))
        start = now - timedelta(days=days)
        wanted = [kind for kind in (types or self.configured) if kind in self.mappings]
        offered_ids: list[str] = []

        async def _states(entity_id: str) -> tuple[State | None, list[State]]:
            """The state at the window's start, if any, and the changes inside it."""
            found = await get_instance(self._hass).async_add_executor_job(
                partial(
                    history.state_changes_during_period,
                    self._hass,
                    start,
                    now,
                    entity_id,
                    include_start_time_state=True,
                )
            )
            rows = found.get(entity_id, [])
            before = [row for row in rows if row.last_changed <= start]
            inside = [row for row in rows if row.last_changed > start]
            if not before and inside:
                inside = inside[1:]
            return (before[-1] if before else None), inside[-BACKFILL_MAX_PER_ENTITY:]

        for kind in wanted:
            mapping = self.mappings[kind]
            timestamps: list[tuple[datetime, datetime]] = []
            if mapping.time_entity:
                timestamps = _timestamps((await _states(mapping.time_entity))[1])
            events: list[tuple[datetime, str, State]] = []
            last_known: dict[str, float] = {}
            for role, entity_id in mapping.entities.items():
                opening, changes = await _states(entity_id)
                if opening is not None:
                    value = self._converted(kind, entity_id, opening)
                    if value is not None:
                        last_known[role] = value
                events.extend((state.last_changed, role, state) for state in changes)
            events.sort(key=lambda event: event[0])

            # Merged per id first, the last value winning: several rows within the
            # window of one timestamp entity value (a correction, a weight and then
            # the weight with impedance) are one reading, and offering them one by
            # one would bump the version on every press of the button.
            merged: dict[str, tuple[dict[str, float], datetime, str | None]] = {}
            for values, moment in self._readings_from(kind, events, last_known):
                measured_at, time_source = _time_from(timestamps, moment)
                merged[reading_id(mapping.anchor, measured_at)] = (values, measured_at, time_source)
            for values, measured_at, time_source in merged.values():
                if (stored := self._offer(mapping, values, measured_at, time_source)) is not None:
                    offered_ids.append(stored.id)

        # What the queue will not keep does not count.
        self.queue.prune(now)
        queued = sum(1 for reading_key in offered_ids if reading_key in self.queue.pending)
        _LOGGER.info(
            "Queued %s readings from the last %s days for %s", queued, days, self._entry.title
        )
        return queued

    def _readings_from(
        self, kind: str, events: list[tuple[datetime, str, State]], last_known: dict[str, float]
    ) -> list[tuple[dict[str, float], datetime]]:
        """Values and moments from recorded changes, in time order.

        A row that repeats the value of the row before it is not a change: the
        recorder never writes one for an unchanged value, so such a row is the state
        Home Assistant restored after a restart, stamped with the boot time, and the
        listeners skip that too. A row that still carries the restored attribute is
        skipped for the same reason.

        Blood pressure is two entities, and the recorder keeps a row only for a value
        that changed: a reading whose diastolic equals the previous one has a
        systolic row and no diastolic row. A change of one half within the window of
        the other pairs with it, timed on the systolic; a change on its own takes the
        last known value of the other half.
        """
        readings: list[tuple[dict[str, float], datetime]] = []
        changes: list[tuple[datetime, str, float]] = []
        previous: dict[str, float] = dict(last_known)
        for moment, role, state in events:
            if state.attributes.get(ATTR_RESTORED):
                continue
            value = self._converted(kind, state.entity_id, state)
            if value is None or previous.get(role) == value:
                continue
            previous[role] = value
            changes.append((moment, role, value))

        if kind != TYPE_BLOOD_PRESSURE:
            return [({VALUE_FIELDS[kind]: value}, moment) for moment, _, value in changes]

        consumed: set[int] = set()
        for index, (moment, role, value) in enumerate(changes):
            if index in consumed:
                continue
            last_known[role] = value
            other_role = CONF_DIASTOLIC if role == CONF_SYSTOLIC else CONF_SYSTOLIC
            partner: tuple[int, float, datetime] | None = None
            for later in range(index + 1, len(changes)):
                later_moment, later_role, later_value = changes[later]
                if later_moment - moment > WINDOW:
                    break
                if later not in consumed and later_role == other_role:
                    partner = (later, later_value, later_moment)
                    break
            if partner is not None:
                consumed.add(partner[0])
                last_known[other_role] = partner[1]
                other_value, other_moment = partner[1], partner[2]
            elif other_role in last_known:
                other_value, other_moment = last_known[other_role], moment
            else:
                continue
            if role == CONF_SYSTOLIC:
                values = {FIELD_SYSTOLIC: value, FIELD_DIASTOLIC: other_value}
                readings.append((values, moment))
            else:
                values = {FIELD_SYSTOLIC: other_value, FIELD_DIASTOLIC: value}
                readings.append((values, other_moment))
        return readings

    # --- The answer -------------------------------------------------------------------

    @callback
    def async_respond(
        self, request: dict[str, Any] | None, *, in_reply_to: str, now: datetime
    ) -> dict[str, Any]:
        """Take what the phone reported and answer with what waits for it.

        Acks and failures are applied before the page is formed, so a reading the
        phone just confirmed is not offered once more. pending and more appear only
        when the request carried writeback.types, and hold only those types.
        """
        types: list[Any] | None = None
        if request is not None:
            if (protocol := request.get("protocol")) not in (None, 1):
                _LOGGER.debug(
                    "%s speaks writeback protocol %s; answering with protocol 1",
                    self._entry.title,
                    protocol,
                )
            ack = request.get("ack")
            if isinstance(ack, list) and ack:
                acked = self.queue.ack(ack, now)
                _LOGGER.debug("%s wrote %s of %s readings", self._entry.title, len(acked), len(ack))
            failed = request.get("failed")
            if isinstance(failed, list) and failed:
                self._report(self.queue.fail(failed, now))
            if ack or failed:
                # Saved before the page is formed: what the phone reported must not
                # be lost to a bug further down that answers without readings.
                self._save()
            if isinstance(raw_types := request.get("types"), list):
                types = raw_types
                self.queue.note_asked(
                    {kind for kind in raw_types if isinstance(kind, str)}, set(self.mappings)
                )

        self.queue.prune(now)
        pending: list[dict[str, Any]] | None = None
        more: bool | None = None
        if types is not None:
            readings, more = self.queue.page(types)
            pending = [reading.to_wire() for reading in readings]
            if pending:
                _LOGGER.debug(
                    "Offering %s readings to %s%s",
                    len(pending),
                    self._entry.title,
                    ", more waiting" if more else "",
                )
        self._sync_issues()
        self._save()
        return response_body(
            version=self.version,
            in_reply_to=in_reply_to,
            issued_at=now,
            configured=self.configured,
            pending=pending,
            more=more,
        )

    def _report(self, failures: list) -> None:
        """Log what the phone refused: the entity and the code, never the value."""
        for failure in failures:
            reading = failure.reading
            where = reading.entity_id if reading else failure.id
            kind = reading.type if reading else "?"
            if not failure.permanent:
                _LOGGER.debug(
                    "%s could not write %s from %s right now (%s); offered again next time",
                    self._entry.title,
                    kind,
                    where,
                    failure.code,
                )
                continue
            hint = (
                ". Older than 30 days: turn on Accept older measurements in the app"
                if failure.code == CODE_TOO_OLD
                else ""
            )
            _LOGGER.warning(
                "%s did not write %s from %s: %s%s",
                self._entry.title,
                kind,
                where,
                failure.code,
                hint,
            )

    def _sync_issues(self) -> None:
        """One repair per type the phone refused for a reason the user can fix, and one
        per mapped type the phone stopped asking for."""
        for kind in WRITEBACK_TYPES:
            issue_id = f"writeback_{self._entry.entry_id}_{kind}_not_asked"
            if kind in self.mappings and self.queue.not_asked.get(kind, 0) >= NOT_ASKED_ROUNDS:
                ir.async_create_issue(
                    self._hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="writeback_not_asked",
                    translation_placeholders={"phone": self._entry.title, "type": kind},
                )
            else:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            issue_id = f"writeback_{self._entry.entry_id}_{kind}"
            code = self.queue.refused.get(kind)
            if code is None or kind not in self.mappings:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=f"writeback_{code}",
                translation_placeholders={"phone": self._entry.title, "type": kind},
            )


def _timestamps(states: list[State]) -> list[tuple[datetime, datetime]]:
    """(changed at, value) for the states of a timestamp entity that hold one."""
    found: list[tuple[datetime, datetime]] = []
    for state in states:
        try:
            found.append((state.last_changed, parse_instant(state.state)))
        except ValueError:
            continue
    return found


def _time_from(
    candidates: list[tuple[datetime, datetime]], moment: datetime
) -> tuple[datetime, str | None]:
    """The timestamp that changed closest to the moment, inside the window, or the moment."""
    inside = [
        (abs(changed - moment), value)
        for changed, value in candidates
        if abs(changed - moment) <= WINDOW
    ]
    if inside:
        return min(inside)[1], None
    return moment, TIME_SOURCE_STATE


def _zone_offset(moment: datetime) -> str:
    """The configured zone's offset at that moment, as +02:00."""
    raw = moment.astimezone(dt_util.get_default_time_zone()).strftime("%z")
    return f"{raw[:3]}:{raw[3:]}" if len(raw) == 5 else raw


def _recording_method(kind: str, entity_id: str) -> str:
    if kind == TYPE_BLOOD_PRESSURE:
        return RECORDING_ACTIVE
    if entity_id.startswith("input_number."):
        return RECORDING_MANUAL
    return RECORDING_AUTO
