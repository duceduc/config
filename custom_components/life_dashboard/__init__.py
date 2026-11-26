"""The Life Dashboard integration.

Receives webhook payloads from the Life Dashboard apps, verifies the
signature, and dispatches the sensor updates a payload justifies.

The one rule worth knowing: an update is applied only when it is not older than the
value a sensor already holds. The app re-sends a batch after a failed delivery, from
its outbox at the next sync, and wholesale during a backfill, so the same record
arrives repeatedly with its original timestamps. Ordering on the moment a value
describes makes all of that harmless without keeping a record of uuids.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import voluptuous as vol
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .apps import AppRoster
from .apps import async_remove_store as async_remove_apps
from .const import (
    CONF_CLOUDHOOK_URL,
    CONF_MIN_WORKOUT_MINUTES,
    CONF_PLATFORM,
    CONF_SECRET,
    CONF_TAKE_OVER,
    CONF_WEBHOOK_ID,
    DEFAULT_MIN_WORKOUT_MINUTES,
    DOMAIN,
    PLATFORM_ANDROID,
    PLATFORM_IPHONE,
)
from .nutrition import NUTRITION_SENSORS, nutrition_updates
from .payload import (
    HEALTH_SOURCES,
    KEY_EXERCISE_TODAY,
    KEY_LAST_WORKOUT,
    KEY_SCREEN_TIME_APPS,
    SIGNATURE_HEADER,
    SOURCE_HEALTHKIT_IOS,
    SensorUpdate,
    day_total_zeros,
    frame_answer,
    newest_totals_day,
    parse_payload,
    response_body,
    verify_signature,
    wants_signed_body,
    writeback_request,
)
from .sleep import LAST_NIGHT_KEYS, sleep_nights, sleep_updates
from .statistics import HistoryWriter
from .statistics import async_remove_store as async_remove_history
from .workouts import (
    day_updates,
    identity,
    kept_workouts,
    newest_workout,
    payload_pool,
    refresh_last_workout,
    stored_workouts,
    workout_update,
)
from .writeback import BACKFILL_DEFAULT_DAYS, BACKFILL_MAX_DAYS, WritebackManager, parse_mapping
from .writeback import async_remove_store as async_remove_writeback
from .writeback_queue import WRITEBACK_TYPES

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_QUEUE_HISTORY = "queue_history"
ATTR_CONFIG_ENTRY = "config_entry"
ATTR_DAYS = "days"
ATTR_TYPES = "types"
QUEUE_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_DAYS, default=BACKFILL_DEFAULT_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=BACKFILL_MAX_DAYS)
        ),
        vol.Optional(ATTR_TYPES): vol.All(cv.ensure_list, [vol.In(WRITEBACK_TYPES)]),
    }
)

SERVICE_GET_SLEEP_NIGHTS = "get_sleep_nights"
ATTR_START = "start"
ATTR_END = "end"
GET_SLEEP_NIGHTS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_START): cv.date,
        vol.Optional(ATTR_END): cv.date,
    }
)

SERVICE_GET_WORKOUTS = "get_workouts"
GET_WORKOUTS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_START): cv.date,
        vol.Optional(ATTR_END): cv.date,
    }
)


def signal_update(entry_id: str) -> str:
    """The dispatcher signal carrying sensor updates for one entry."""
    return f"{DOMAIN}_update_{entry_id}"


@dataclass
class LifeDashboardData:
    """What a loaded entry keeps in memory.

    latest is the ordering rule's memory. Entities seed it from their restored state
    when they are added, so a restart does not reopen the door to old batches.
    """

    latest: dict[str, SensorUpdate] = field(default_factory=dict)
    app_version: str | None = None
    #: This integration's own version, announced to the phone in every answer.
    version: str = "0"
    #: The long-term statistics writer; None only before setup finished.
    history: HistoryWriter | None = None
    #: The queue of measurements for the phone; None only before setup finished.
    writeback: WritebackManager | None = None
    #: The apps with a screen time sensor of their own; None only before setup finished.
    apps: AppRoster | None = None

    def apply(self, update: SensorUpdate) -> bool:
        """Record an update, unless it describes a moment we are already past.

        Equal timestamps are accepted: a re-delivered batch carries the same value,
        and refusing it would only make the first delivery special.
        """
        current = self.latest.get(update.key)
        if current is not None and update.measured_at < current.measured_at:
            return False
        self.latest[update.key] = update
        return True


type LifeDashboardConfigEntry = ConfigEntry[LifeDashboardData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services; each is one per integration, not one per phone."""

    async def _queue_history(call: ServiceCall) -> None:
        entry = _entry_for_service(hass, call.data.get(ATTR_CONFIG_ENTRY))
        if not entry.runtime_data.writeback.receives:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="iphone_receives_nothing",
                translation_placeholders={"phone": entry.title},
            )
        await entry.runtime_data.writeback.async_queue_history(
            days=call.data[ATTR_DAYS], types=call.data.get(ATTR_TYPES)
        )

    async def _get_sleep_nights(call: ServiceCall) -> ServiceResponse:
        """The kept sleep sessions of a phone, for whoever builds on them."""
        entry = _entry_for_service(hass, call.data.get(ATTR_CONFIG_ENTRY))
        start = call.data.get(ATTR_START)
        end = call.data.get(ATTR_END)
        if start is not None and end is not None and end < start:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="end_before_start"
            )
        return {
            "nights": sleep_nights(
                entry.runtime_data.history.ledger.sleep,
                tz=dt_util.get_default_time_zone(),
                start=start,
                end=end,
            )
        }

    async def _get_workouts(call: ServiceCall) -> ServiceResponse:
        """The kept workouts of a phone, for whoever builds on them."""
        entry = _entry_for_service(hass, call.data.get(ATTR_CONFIG_ENTRY))
        start = call.data.get(ATTR_START)
        end = call.data.get(ATTR_END)
        if start is not None and end is not None and end < start:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="end_before_start"
            )
        ledger = entry.runtime_data.history.ledger
        return {
            "workouts": kept_workouts(
                ledger.workouts,
                ledger.past_workouts,
                tz=dt_util.get_default_time_zone(),
                pool=ledger.activity,
                start=start,
                end=end,
            )
        }

    hass.services.async_register(
        DOMAIN, SERVICE_QUEUE_HISTORY, _queue_history, schema=QUEUE_HISTORY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SLEEP_NIGHTS,
        _get_sleep_nights,
        schema=GET_SLEEP_NIGHTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_WORKOUTS,
        _get_workouts,
        schema=GET_WORKOUTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


def _entry_for_service(hass: HomeAssistant, entry_id: str | None) -> LifeDashboardConfigEntry:
    """The phone a service call means: the one named, or the only one there is."""
    if entry_id is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="phone_not_loaded"
            )
        return entry
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if len(loaded) != 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="choose_a_phone")
    return loaded[0]


async def async_setup_entry(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> bool:
    """Set up Life Dashboard from a config entry."""
    entry.runtime_data = LifeDashboardData()
    integration = await async_get_integration(hass, DOMAIN)
    entry.runtime_data.version = str(integration.version)
    history = HistoryWriter(hass, entry)
    await history.async_load()
    entry.runtime_data.history = history
    writeback = WritebackManager(hass, entry)
    await writeback.async_load()
    entry.runtime_data.writeback = writeback
    writeback.async_start()
    apps = AppRoster(hass, entry)
    await apps.async_load()
    entry.runtime_data.apps = apps
    # The per-app sensors restore nothing of their own (most are never added), so the
    # stored table is what reseeds the ordering rule for them.
    if apps.latest is not None:
        entry.runtime_data.latest[KEY_SCREEN_TIME_APPS] = apps.latest

    if history.available and (entry.data.get(CONF_TAKE_OVER) or history.needs_unit_migration):
        # The recorder works its queue only once Home Assistant has started, and waiting
        # for it here would hold the start up for good. Until then the webhook stays closed,
        # and the phone keeps what it sends in its outbox for a little while.

        @callback
        def _prepare(_hass: HomeAssistant) -> None:
            # After the start, so a tracked task holds nothing up; it can be waited for.
            entry.async_create_task(
                hass, _async_prepare_history(hass, entry), f"{DOMAIN} prepare history"
            )

        entry.async_on_unload(async_at_started(hass, _prepare))
    else:
        if entry.data.get(CONF_TAKE_OVER):
            await history.async_take_over(entry.data[CONF_TAKE_OVER])
            _drop_take_over(hass, entry)
        _open_webhook(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The exercise sensors are read from the workouts the ledger kept, so they are right
    # after a restart and with the option just changed, and they move on to the new day at
    # midnight rather than at the next sync.
    async_publish_exercise(hass, entry)
    async_publish_sleep(hass, entry)
    async_publish_nutrition(hass, entry)

    @callback
    def _new_day(_now: datetime) -> None:
        async_publish_exercise(hass, entry)
        async_publish_nutrition(hass, entry)
        async_publish_day_totals(hass, entry)

    entry.async_on_unload(async_track_time_change(hass, _new_day, hour=0, minute=0, second=5))
    return True


def min_workout_minutes(entry: ConfigEntry) -> int:
    """The shortest workout the exercise sensors count, in minutes."""
    return int(entry.options.get(CONF_MIN_WORKOUT_MINUTES, DEFAULT_MIN_WORKOUT_MINUTES))


@callback
def async_publish_exercise(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> None:
    """Send Exercise today and Exercise yesterday as of now, from the kept workouts.

    A phone that never sent a workout gets no sensors; once they exist they are kept up
    to date also when nothing is left to count, so a quiet day reads 0 rather than the
    day before. Last workout gets its details again when they changed, for the records
    that arrive after the workout itself.
    """
    runtime = entry.runtime_data
    if runtime.history is None:
        return
    ledger = runtime.history.ledger
    workouts = stored_workouts(ledger.workouts)
    updates = []
    if workouts or KEY_EXERCISE_TODAY in runtime.latest:
        updates = day_updates(
            workouts,
            now=dt_util.utcnow(),
            tz=dt_util.get_default_time_zone(),
            min_minutes=min_workout_minutes(entry),
            pool=ledger.activity,
        )
    refreshed = refresh_last_workout(
        runtime.latest.get(KEY_LAST_WORKOUT), workouts, ledger.activity
    )
    if refreshed is not None:
        updates.append(refreshed)
    for update in updates:
        if runtime.apply(update):
            async_dispatcher_send(hass, signal_update(entry.entry_id), update)


@callback
def async_publish_day_totals(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> None:
    """Start the day totals the phone sent before at 0 on a new day.

    Like the exercise sensors: at midnight they move on to the new day rather than
    showing yesterday until the phone syncs, which can be hours with a quiet period.
    """
    runtime = entry.runtime_data
    tz = dt_util.get_default_time_zone()
    for update in day_total_zeros(runtime.latest, dt_util.utcnow().astimezone(tz).date(), tz=tz):
        if runtime.apply(update):
            async_dispatcher_send(hass, signal_update(entry.entry_id), update)


@callback
def async_publish_nutrition(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> None:
    """Send what was eaten today, from the meals the ledger kept.

    Like the exercise sensors: right after a restart, back to 0 at midnight, and only for a
    phone that ever sent a meal.
    """
    runtime = entry.runtime_data
    if runtime.history is None:
        return
    tz = dt_util.get_default_time_zone()
    updates = nutrition_updates(
        runtime.history.ledger.day_totals,
        today=dt_util.utcnow().astimezone(tz).date(),
        tz=tz,
        known=frozenset(key for key in NUTRITION_SENSORS if key in runtime.latest),
    )
    for update in updates:
        if runtime.apply(update):
            async_dispatcher_send(hass, signal_update(entry.entry_id), update)


@callback
def async_publish_sleep(
    hass: HomeAssistant, entry: LifeDashboardConfigEntry, *, clear: bool = False
) -> None:
    """Send the last-night sensors as the kept nights say, when that changed.

    Not through the ordering rule: a correction or a deletion can make an older night the
    newest again, and the ledger is what is right about that. `clear` empties the sensors
    when no night is left, for a payload that deleted the last one; at startup a ledger
    without nights says nothing about the sensors' restored values.
    """
    runtime = entry.runtime_data
    if runtime.history is None:
        return
    tz = dt_util.get_default_time_zone()
    updates = sleep_updates(
        runtime.history.ledger.sleep, tz=tz, today=dt_util.utcnow().astimezone(tz).date()
    )
    if not updates and clear:
        now = dt_util.utcnow()
        updates = [
            SensorUpdate(key=key, value=None, measured_at=now)
            for key in LAST_NIGHT_KEYS
            if key in runtime.latest and runtime.latest[key].value is not None
        ]
    for update in updates:
        if runtime.latest.get(update.key) != update:
            runtime.latest[update.key] = update
            async_dispatcher_send(hass, signal_update(entry.entry_id), update)


async def _async_prepare_history(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> None:
    """Take over a removed phone's history and bring 0.12's units over, then take payloads.

    Both before the webhook is up, so no row of this phone lands in between.
    """
    history = entry.runtime_data.history
    assert history is not None
    if owner := entry.data.get(CONF_TAKE_OVER):
        await history.async_take_over(owner)
        _drop_take_over(hass, entry)
    await history.async_migrate_units()
    _open_webhook(hass, entry)


@callback
def _drop_take_over(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> None:
    data = {key: value for key, value in entry.data.items() if key != CONF_TAKE_OVER}
    hass.config_entries.async_update_entry(entry, data=data)


@callback
def _open_webhook(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> None:
    webhook.async_register(
        hass,
        DOMAIN,
        entry.title,
        entry.data[CONF_WEBHOOK_ID],
        _make_handler(entry),
        allowed_methods=["POST"],
    )
    # Registering the same id twice raises, so a reload has to unregister first.
    entry.async_on_unload(lambda: webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID]))


async def async_unload_entry(hass: HomeAssistant, entry: LifeDashboardConfigEntry) -> bool:
    """Unload a config entry."""
    if (history := entry.runtime_data.history) is not None:
        await history.async_flush()
    if (writeback := entry.runtime_data.writeback) is not None:
        await writeback.async_flush()
    if (apps := entry.runtime_data.apps) is not None:
        await apps.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete what the entry stored, and the cloudhook, so no public URL stays alive.

    The ledger, the queue for the phone and the app list are this phone's data and mean
    nothing without the entry; the long-term statistics stay in the recorder, as they do
    for any integration that is removed.
    """
    for remove in (async_remove_history, async_remove_writeback, async_remove_apps):
        await remove(hass, entry)
    if not entry.data.get(CONF_CLOUDHOOK_URL) or "cloud" not in hass.config.components:
        return
    with contextlib.suppress(ImportError):
        from homeassistant.components import cloud

        with contextlib.suppress(cloud.CloudNotAvailable, ValueError):
            await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])


def _make_handler(entry: LifeDashboardConfigEntry):
    """Build the webhook handler for one entry.

    Home Assistant turns an exception inside a handler into a silent HTTP 200, which
    the app would read as a successful delivery and never retry. So everything is
    caught here and answered with a status the app understands: 401 and 400 are
    permanent errors it logs without retrying, 200 means accepted.

    An accepted POST is answered with a signed JSON body: what this integration is,
    and, for a phone that asked, the measurements waiting for it (writeback.py). The
    signature is in the X-Signature header, and for a phone that asked, in the body
    too (payload.signed_body). The refusals carry no body at all.
    """

    async def handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        # Through Home Assistant Cloud this is a MockRequest, which has no read(). Inside a
        # try, because an exception here would be the silent 200 described above.
        try:
            body = await (
                request.read() if isinstance(request, web.Request) else request.content.read()
            )
        except web.HTTPException as err:
            _LOGGER.warning("Refused a payload for %s: %s", entry.title, err.reason)
            return web.Response(status=err.status)
        except Exception:
            _LOGGER.exception("Could not read a payload for %s", entry.title)
            return web.Response(status=400)
        signature = request.headers.get(SIGNATURE_HEADER)

        if not verify_signature(entry.data[CONF_SECRET], body, signature):
            _LOGGER.warning(
                "Refused a payload for %s: the signature is missing or does not match. "
                "Check that the secret in the app is the one this integration shows",
                entry.title,
            )
            return web.Response(status=401)

        try:
            data = json.loads(body)
        except ValueError:
            _LOGGER.warning("Refused a payload for %s: the body is not JSON", entry.title)
            return web.Response(status=400)

        if not isinstance(data, dict):
            _LOGGER.warning("Refused a payload for %s: the body is not a JSON object", entry.title)
            return web.Response(status=400)

        runtime = entry.runtime_data
        now = dt_util.utcnow()
        # One try for everything after the signature, the bytes of the answer and
        # its signature included: a bug anywhere in here must be a 400, not the
        # silent 200 the app would take as delivered.
        try:
            tz = dt_util.get_default_time_zone()
            updates = parse_payload(data, tz=tz)
            # History goes to long-term statistics, where a backfill can land on the
            # days it came from. First, because the workouts' details are read from the
            # records the ledger keeps, this payload's included.
            change = runtime.history.async_apply(data) if runtime.history is not None else None
            # The newest workout of the payload, with the details of its records: those
            # the ledger keeps, or for a workout older than those, the payload's own.
            newest = newest_workout(data.get("exercise"), min_minutes=min_workout_minutes(entry))
            if newest is not None and data.get("test") is not True:
                ledger = runtime.history.ledger if runtime.history is not None else None
                pool = (
                    ledger.activity
                    if ledger is not None and identity(newest) in ledger.workouts
                    else payload_pool(data, now=now)
                )
                updates.append(workout_update(newest, tz=tz, pool=pool))
            # A day total the newest day has no data for yet starts at 0 rather than
            # keeping the day before (app discussion #257).
            if (day := newest_totals_day(data)) is not None and data.get("test") is not True:
                sent = {update.key for update in updates}
                updates.extend(
                    zero
                    for zero in day_total_zeros(runtime.latest, day, tz=tz)
                    if zero.key not in sent
                )
            accepted = [update for update in updates if runtime.apply(update)]

            _note_platform(hass, entry, data)

            if (version := data.get("app_version")) and version != runtime.app_version:
                runtime.app_version = version
                _update_sw_version(hass, entry, version)

            for update in accepted:
                async_dispatcher_send(hass, signal_update(entry.entry_id), update)

            # The day sums, and Last workout when records for it came later.
            if change is not None and change.workouts:
                async_publish_exercise(hass, entry)
            if change is not None and change.nights:
                async_publish_sleep(hass, entry, clear=True)
            if change is not None and set(change.day_keys) & set(NUTRITION_SENSORS.values()):
                async_publish_nutrition(hass, entry)

            # What the phone confirmed and refused, then what waits for it. Its own
            # try inside this one: a bug in the direction to the phone must not turn
            # a sync that was taken into a permanent error on the phone, so it is
            # answered with the announcement alone, and logged.
            try:
                answer = runtime.writeback.async_respond(
                    writeback_request(data), in_reply_to=signature or "", now=now
                )
            except Exception:
                _LOGGER.exception(
                    "Could not prepare the readings for %s; answering without them", entry.title
                )
                answer = response_body(
                    version=runtime.version,
                    in_reply_to=signature or "",
                    issued_at=now,
                    configured=runtime.writeback.configured,
                )
            # In the body as well when the phone asked for that: Home Assistant Cloud
            # passes on the status, the body and the Content-Type, never the header.
            raw, answer_signature = frame_answer(
                entry.data[CONF_SECRET], answer, in_body=wants_signed_body(data)
            )
        except Exception:
            _LOGGER.exception("Could not read a payload for %s", entry.title)
            return web.Response(status=400)

        _LOGGER.debug("Took %s of %s updates for %s", len(accepted), len(updates), entry.title)
        return web.Response(
            status=200,
            body=raw,
            content_type="application/json",
            headers={SIGNATURE_HEADER: answer_signature},
        )

    return handle


def _note_platform(hass: HomeAssistant, entry: LifeDashboardConfigEntry, data: dict) -> None:
    """Remember whether the phone is an iPhone, from the source of a health payload.

    An iPhone has no Receive, so for one the write-back options, the button and the
    queue go away. A change reloads the entry when something is mapped, so the
    button and the listeners follow; the mapping itself stays in the options, for
    a phone that moves back to Android.
    """
    source = data.get("source")
    if source not in HEALTH_SOURCES:
        return
    platform = PLATFORM_IPHONE if source == SOURCE_HEALTHKIT_IOS else PLATFORM_ANDROID
    before = entry.data.get(CONF_PLATFORM)
    if before == platform:
        return
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PLATFORM: platform})
    # Only a change into or out of iPhone changes what is offered; the first Android
    # payload of an entry that already receives needs no reload.
    if (before == PLATFORM_IPHONE) != (platform == PLATFORM_IPHONE) and parse_mapping(
        entry.options
    ):
        hass.config_entries.async_schedule_reload(entry.entry_id)


def _update_sw_version(hass: HomeAssistant, entry: LifeDashboardConfigEntry, version: str) -> None:
    """Keep the device's firmware field on the app version the phone reports.

    Looked up through the entry rather than by identifier: identifiers are no longer
    unique across entries, and this entry owns exactly one device anyway. Before the
    first entity exists there is no device yet, and none is created here, because the
    entities built from this very payload read the version out of runtime_data.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        registry.async_update_device(device.id, sw_version=version)
