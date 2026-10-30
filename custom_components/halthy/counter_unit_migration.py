"""Relabel legacy count statistics without converting or deleting any samples."""

from __future__ import annotations

import asyncio
from functools import partial
import inspect
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .const import DOMAIN
from .statistics import statistics_id_for_metric
from .units import canonical_unit, is_timestamp_metric

_LOGGER = logging.getLogger(__name__)
SERVICE_MIGRATE_COUNTER_STATISTICS = "migrate_counter_statistics"
_TIMEOUT = 30
_LEGACY_UNITS = {"count", "counts"}


def _candidate_statistics(hass: HomeAssistant, entry_id: str | None) -> dict[str, str]:
    """Use actual registry IDs, never assume an entity's username-based name."""
    entries = hass.data[DOMAIN]["entries"]
    if entry_id is not None and entry_id not in entries:
        raise HomeAssistantError("The selected Halthy entry is not loaded")
    registry = er.async_get(hass)
    candidates: dict[str, str] = {}
    for current_id, runtime in entries.items():
        if entry_id is not None and entry_id != current_id:
            continue
        counters = {
            unique_id: sensor for unique_id, sensor in runtime.sensors.items()
            if canonical_unit(sensor.metric_key, sensor.unit) is None
            and not is_timestamp_metric(sensor.metric_key)
        }
        for entity in er.async_entries_for_config_entry(registry, current_id):
            if entity.domain == "sensor" and entity.platform == DOMAIN and entity.unique_id in counters:
                candidates[entity.entity_id] = "recorder"
        # External halthy: statistics have separate metadata from sensor.* history.
        for sensor in counters.values():
            candidates[statistics_id_for_metric(runtime.configured_username, sensor.metric_key)] = DOMAIN
    return candidates


async def async_migrate_counter_statistics(
    hass: HomeAssistant, entry_id: str | None = None, *, dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate only count -> no-unit metadata; safe to repeat after any reload."""
    if "recorder" not in getattr(hass.config, "components", set()):
        return {"status": "recorder_not_loaded", "dry_run": dry_run, "statistic_ids": []}

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import get_metadata

    lock = hass.data[DOMAIN].setdefault("counter_unit_migration_lock", asyncio.Lock())
    async with lock:
        candidates = _candidate_statistics(hass, entry_id)
        if not candidates:
            return {"status": "no_changes", "dry_run": dry_run, "statistic_ids": []}
        recorder = get_instance(hass)
        try:
            if not await asyncio.wait_for(asyncio.shield(recorder.async_db_ready), _TIMEOUT):
                raise HomeAssistantError("Recorder database is not ready; retry the migration later")
            await asyncio.wait_for(recorder.async_recorder_ready.wait(), _TIMEOUT)

            async def metadata() -> dict[str, Any]:
                return await recorder.async_add_executor_job(
                    partial(get_metadata, hass, statistic_ids=set(candidates))
                )

            before = await metadata()
            pending = [
                statistic_id for statistic_id, (_, meta) in before.items()
                if meta["source"] == candidates[statistic_id]
                and meta["unit_of_measurement"] in _LEGACY_UNITS
                and meta.get("unit_class") is None
            ]
            if dry_run:
                return {"status": "preview", "dry_run": True, "statistic_ids": sorted(pending)}

            # Use Recorder's queued API: on_done runs on its thread after the write.
            # Modern HA also requires an explicit unit_class; older releases lack it.
            update = recorder.async_update_statistics_metadata
            kwargs: dict[str, Any] = {"new_unit_of_measurement": None}
            if "new_unit_class" in inspect.signature(update).parameters:
                kwargs["new_unit_class"] = None
            events = [asyncio.Event() for _ in pending]
            for statistic_id, event in zip(pending, events, strict=True):
                def done(event: asyncio.Event = event) -> None:
                    hass.loop.call_soon_threadsafe(event.set)

                update(statistic_id, on_done=done, **kwargs)
            if events:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in events)), _TIMEOUT)
            after = await metadata() if pending else before
            failed = [statistic_id for statistic_id in pending
                      if statistic_id not in after
                      or after[statistic_id][1]["unit_of_measurement"] is not None
                      or after[statistic_id][1].get("unit_class") is not None]
            if failed:
                raise HomeAssistantError("Counter statistics migration could not be verified; retry the action")

            # Remove only confirmed, obsolete count-unit issues. This also cleans up
            # repairs left behind if the user already fixed their metadata manually.
            for statistic_id, (_, meta) in after.items():
                if (candidates[statistic_id] == "recorder" and meta["source"] == "recorder"
                        and meta["unit_of_measurement"] is None and meta.get("unit_class") is None):
                    issue_id = f"units_changed_{statistic_id}"
                    issue = ir.async_get(hass).async_get_issue("sensor", issue_id)
                    if (issue is not None and issue.data is not None
                            and issue.data.get("metadata_unit") in _LEGACY_UNITS
                            and issue.data.get("state_unit") in (None, "")):
                        ir.async_delete_issue(hass, "sensor", issue_id)
            if pending:
                _LOGGER.info("Migrated %d Halthy counter statistics to unitless; historical values are unchanged", len(pending))
            return {"status": "migrated" if pending else "no_changes", "dry_run": False,
                    "statistic_ids": sorted(pending)}
        except TimeoutError as error:
            raise HomeAssistantError("Counter statistics migration timed out; retry the action later") from error


async def async_register_migration_service(hass: HomeAssistant) -> None:
    """Offer a UI action for previewing or retrying the automatic repair."""
    import voluptuous as vol

    from homeassistant.core import SupportsResponse
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.service import async_register_admin_service

    if hass.services.has_service(DOMAIN, SERVICE_MIGRATE_COUNTER_STATISTICS):
        return

    async def migrate(call: Any) -> dict[str, Any]:
        return await async_migrate_counter_statistics(
            hass, call.data.get("entry_id"), dry_run=call.data["dry_run"]
        )

    async_register_admin_service(
        hass, DOMAIN, SERVICE_MIGRATE_COUNTER_STATISTICS, migrate,
        schema=vol.Schema({vol.Optional("entry_id"): cv.string,
                           vol.Optional("dry_run", default=False): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_migrate_counter_statistics_on_setup(hass: HomeAssistant, entry: Any) -> None:
    """A recorder outage must not prevent sensors from loading or mark a repair done."""
    from homeassistant.core import CoreState
    from homeassistant.helpers.start import async_at_started

    async def migrate(_: HomeAssistant) -> None:
        try:
            await async_migrate_counter_statistics(hass, entry.entry_id)
        except Exception as error:
            _LOGGER.warning("Halthy counter unit migration deferred: %s. Retry halthy.%s", error,
                            SERVICE_MIGRATE_COUNTER_STATISTICS)

    # Recorder processes its write queue only after HA starts. Awaiting a queued
    # metadata update during config-entry setup would delay or deadlock startup.
    if hass.state is CoreState.running:
        await migrate(hass)
    else:
        entry.async_on_unload(async_at_started(hass, migrate))
