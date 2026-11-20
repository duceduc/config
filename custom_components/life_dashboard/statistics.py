"""Writing the ledger into Home Assistant's long-term statistics.

The thin layer between history.py, which decides what the rows say, and the recorder,
which stores them. One external statistic per data type per entry, fed from every
payload: a backfill fills the past, a normal sync extends the present.

External ids (`life_dashboard:<entry>_<key>`) rather than the sensors' own, because the
recorder compiles a sensor's statistics from its states itself, and two writers to one
running sum disagree at the seam. The sensors stay what they are, now; these are the
history.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .apps import app_unique_id
from .const import DOMAIN
from .history import (
    HistoryChange,
    HourBucket,
    Ledger,
    app_key,
    apply_payload,
    closing_rows,
    closing_start,
    day_rows,
    earliest_day,
    hour_rows,
    keep_from,
    prune,
    seed_taken_over,
)
from .payload import SENSOR_SPECS
from .sleep import NIGHT_STATISTICS, night_rows

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# 1.2 added the workouts, 1.3 what a prune carries (kept_from and carried, written only
# once something was pruned), 1.4 the records of the workouts' details (activity, and
# source, laps and sets in a workout), 1.5 the sleep stages (sleep, written once there are
# nights). Minor versions, because each change only adds keys: an older ledger reads as it
# is, and a downgrade to an earlier release still reads a newer one.
STORAGE_MINOR_VERSION = 5
SAVE_DELAY_SECONDS = 5

# Units and names for the day sums, which have no sensor to borrow from.
# The sleep stages are means rather than sums: one row per wake date (sleep.py).
# One app's screen time per day, for the apps whose sensor is on (`history.app_key`).
APP_PREFIX = "app_"

_DAY_UNITS: dict[str, str] = {
    "steps": "steps",
    "distance": "km",
    "active_calories": "kcal",
    "total_calories": "kcal",
    "sleep_minutes": "h",
    "exercise_minutes": "h",
    "mindfulness_minutes": "h",
    "hydration_total": "L",
    "nutrition_calories": "kcal",
    "nutrition_protein": "g",
    "nutrition_carbs": "g",
    "nutrition_fat": "g",
    "screen_time": "h",
    "sleep_deep": "h",
    "sleep_light": "h",
    "sleep_rem": "h",
    "sleep_awake": "h",
    "sleep_asleep": "h",
}

# The day sums kept in a larger unit than the ledger's, which holds what the phone sends:
# key -> (the ledger's unit, the factor to the statistic's). A graph cannot convert an
# external statistic, so 4.2 km and 7.5 h are stored as such. Up to 0.12 these were in
# m and min; `async_migrate_units` brings such a statistic over once.
_SCALED: dict[str, tuple[str, float]] = {
    "distance": ("m", 1 / 1000),
    "sleep_minutes": ("min", 1 / 60),
    "exercise_minutes": ("min", 1 / 60),
    "mindfulness_minutes": ("min", 1 / 60),
    "screen_time": ("min", 1 / 60),
}

# Names for the keys whose own name would say the old unit.
_NAMES: dict[str, str] = {
    "sleep_minutes": "sleep",
    "exercise_minutes": "exercise",
    "mindfulness_minutes": "mindfulness",
    "nutrition_calories": "calories eaten",
    "nutrition_protein": "protein eaten",
    "nutrition_carbs": "carbs eaten",
    "nutrition_fat": "fat eaten",
}


# The recorder's unit class per unit, so a statistics graph can show a statistic in another
# unit of its kind: distance in km or mi, sleep in hours, energy in kJ. Only units with an
# obvious class; steps, bpm and the like have none.
_UNIT_CLASSES: dict[str, str] = {
    "m": "distance",
    "km": "distance",
    "kcal": "energy",
    "min": "duration",
    "h": "duration",
    "L": "volume",
    "kg": "mass",
    "g": "mass",
}


def statistic_id(entry: ConfigEntry, key: str) -> str:
    """The recorder accepts [0-9a-z_] only, and an entry id is an uppercase ULID."""
    return f"{DOMAIN}:{entry.entry_id.lower()}_{key}"


class _LedgerStore(Store[dict[str, Any]]):
    """The ledger's file, which brings a ledger of an earlier minor version up to date."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version == STORAGE_VERSION and old_minor_version < STORAGE_MINOR_VERSION:
            # Nothing of the days, sessions and hours changes; the workouts start empty,
            # because the sessions kept for the statistics hold no type and no start. A
            # ledger that was never pruned has nothing carried, which is what 1.3 says too.
            # The records for the details start empty as well: the workouts kept so far
            # read without details until their records come again. So do the nights: the
            # sessions kept so far hold no stages, and Send history brings them in.
            return {
                **old_data,
                "workouts": old_data.get("workouts") or {},
                "activity": old_data.get("activity") or {},
                "sleep": old_data.get("sleep") or {},
            }
        raise NotImplementedError


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return _LedgerStore(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.{entry_id}.history",
        minor_version=STORAGE_MINOR_VERSION,
    )


async def async_remove_store(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the ledger with the entry. The statistics it wrote stay in the recorder."""
    await _store(hass, entry.entry_id).async_remove()


def _scale(key: str) -> tuple[str, float] | None:
    """The ledger's unit of a scaled day sum and its factor to the statistic's unit."""
    if key.startswith(APP_PREFIX):
        return ("min", 1 / 60)
    return _SCALED.get(key)


def _factor(key: str) -> float:
    return scale[1] if (scale := _scale(key)) else 1.0


def _scaled(key: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Day rows from the ledger's unit into the statistic's."""
    if (factor := _factor(key)) == 1.0:
        return rows
    return [
        row | {name: row[name] * factor for name in ("state", "sum") if row.get(name) is not None}
        for row in rows
    ]


def _unit(key: str) -> str | None:
    if key.startswith(APP_PREFIX):
        return "h"
    if key in _DAY_UNITS:
        return _DAY_UNITS[key]
    spec = SENSOR_SPECS.get(key)
    return spec.unit if spec else None


def _name(entry: ConfigEntry, key: str) -> str:
    return f"{entry.title} {_NAMES.get(key, key.replace('_', ' '))}"


def _split(sid: str) -> tuple[str, str] | None:
    """An id of ours into the lowercased entry id and the key. A ULID has no underscore."""
    prefix = f"{DOMAIN}:"
    if not sid.startswith(prefix) or "_" not in sid[len(prefix) :]:
        return None
    owner, key = sid[len(prefix) :].split("_", 1)
    return owner, key


async def async_orphaned_histories(hass: HomeAssistant) -> dict[str, tuple[str, int]]:
    """The statistics of phones that were removed: lowercased entry id -> (name, count).

    Removing a phone deletes its ledger but leaves its statistics in the recorder, under
    ids tied to an entry that is gone (P2-30). The name is the phone's name as it was,
    read back from a statistic's name.
    """
    if "recorder" not in hass.config.components:
        return {}
    from homeassistant.components.recorder.statistics import async_list_statistic_ids

    current = {entry.entry_id.lower() for entry in hass.config_entries.async_entries(DOMAIN)}
    found: dict[str, tuple[str, int]] = {}
    for item in await async_list_statistic_ids(hass):
        parts = _split(item["statistic_id"])
        if item.get("source") != DOMAIN or parts is None or parts[0] in current:
            continue
        owner, key = parts
        name, count = found.get(owner, ("", 0))
        if not name:
            label = item.get("name") or ""
            name = label.removesuffix(" " + key.replace("_", " ")) or owner
        found[owner] = (name, count + 1)
    return found


class HistoryWriter:
    """Keeps the ledger for one entry and imports what a payload changed."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store = _store(hass, entry.entry_id)
        self.ledger = Ledger()
        # The day of the last prune, so the whole ledger is walked once a day at most.
        self._pruned_on: date | None = None
        # Statistic key to package, for the apps whose sensor is on (see `_apps_on`).
        self._app_packages: dict[str, str] = {}
        # One fill of old history at a time: each reads the rows the one before wrote.
        self._old_lock = asyncio.Lock()

    async def async_load(self) -> None:
        self.ledger = Ledger.from_dict(await self._store.async_load())

    async def async_flush(self) -> None:
        await self._store.async_save(self.ledger.to_dict())

    @property
    def available(self) -> bool:
        return "recorder" in self._hass.config.components

    @callback
    def async_apply(self, data: dict[str, Any]) -> HistoryChange:
        """Fold a payload into the ledger, import the rows it changed, and prune it."""
        tz = dt_util.get_default_time_zone()
        change = apply_payload(self.ledger, data, tz=tz, apps=self._apps_on())
        if not change.is_empty:
            self._import(change, data, tz)
        if (change.old_days or change.old_hours) and self.available:
            self._entry.async_create_task(
                self._hass,
                self._async_import_old(change.old_days, change.old_hours, tz),
                f"{DOMAIN} old history",
            )

        pruned = self._prune(tz)

        if pruned or not change.is_empty or change.workouts or change.nights:
            self._store.async_delay_save(self.ledger.to_dict, SAVE_DELAY_SECONDS)
        return change

    def _prune(self, tz: tzinfo) -> bool:
        """Let the oldest days go, once a day, and say whether anything went.

        After the rows are written, so a payload's own days are in the recorder before
        anything is let go. Those rows stay there; the ledger only forgets what it would
        need to write them again. Anything older that comes in later the same day is let go
        the next day, which is safe: it was never carried, so it counts once.
        """
        today = dt_util.now(tz).date()
        if today == self._pruned_on:
            return False
        self._pruned_on = today
        before = keep_from(self.ledger, today=today, tz=tz)
        return before is not None and prune(self.ledger, before=before, tz=tz)

    def _import(self, change: HistoryChange, data: dict[str, Any], tz: tzinfo) -> None:
        """Write the statistics rows a payload changed."""
        if not self.available:
            _LOGGER.debug(
                "Recorder not loaded; history for %s kept in the ledger only", self._entry.title
            )
            return

        # Imported inside the function: recorder is an after_dependency and may be absent.
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import async_add_external_statistics

        for key, since in change.day_keys.items():
            rows = sorted(
                day_rows(self.ledger, key, since, tz=tz)
                + closing_rows(self.ledger, key, since, tz=tz),
                key=lambda row: row["start"],
            )
            rows = _scaled(key, rows)
            if rows:
                async_add_external_statistics(
                    self._hass,
                    self._metadata(key, sum_kind=True, mean_type=StatisticMeanType.NONE),
                    rows,
                )

        for key, since in change.hour_keys.items():
            rows = hour_rows(self.ledger, key, since)
            if rows:
                async_add_external_statistics(
                    self._hass,
                    self._metadata(key, sum_kind=False, mean_type=StatisticMeanType.ARITHMETIC),
                    rows,
                )

        for key, rows in night_rows(change.night_rows, tz=tz).items():
            rows = self._take_away_rows(key, rows)
            if rows:
                async_add_external_statistics(
                    self._hass,
                    self._metadata(key, sum_kind=False, mean_type=StatisticMeanType.ARITHMETIC),
                    rows,
                )

        if data.get("backfill") is True and not data.get("daily_totals"):
            _LOGGER.warning(
                "Backfill window from %s carries no daily_totals; its step, distance and"
                " calorie history stays empty (the Android app sends them from 1.17.0,"
                " the iOS app from 1.4.0)",
                earliest_day(change),
            )

    def _take_away_rows(self, key: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove the rows of dates that lost a value, and return the rows left to write.

        Home Assistant can delete part of a statistic from 2026.11 on, and the signature of
        the recorder's method is what says so. Before that a row cannot be deleted, so it is
        left in the list and written with a mean of nothing, which a graph leaves out.
        """
        from homeassistant.components.recorder import get_instance

        if all(row["mean"] is not None for row in rows):
            return rows
        recorder = get_instance(self._hass)
        if "start_time" not in inspect.signature(recorder.async_clear_statistics).parameters:
            return rows
        sid = statistic_id(self._entry, key)
        for row in rows:
            if row["mean"] is None:
                recorder.async_clear_statistics(
                    [sid], start_time=row["start"], end_time=row["start"] + timedelta(hours=1)
                )
        return [row for row in rows if row["mean"] is not None]

    @property
    def needs_unit_migration(self) -> bool:
        """Whether this phone's statistics may still be in m and min, or lack the closing rows
        a period's start is read from (both before 1.0.0)."""
        return self.available and self.ledger.units < 3

    async def async_migrate_units(self) -> None:
        """Bring this phone's day sums up to 1.0.0, once: km and h instead of m and min, and
        a closing row for every past day (`history.closing_rows`).

        The recorder converts a statistic's unit only for those it compiles itself, so the
        rows are written again in the new unit: an import replaces a row with the same start,
        and takes the new unit into the metadata. Run before the webhook is up, so no row in
        the new unit can land next to old ones, and after Home Assistant has started: the
        recorder works its queue only from then on.
        """
        if not self.needs_unit_migration:
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            async_list_statistic_ids,
            statistics_during_period,
        )

        recorder = get_instance(self._hass)
        await recorder.async_db_ready
        tz = dt_util.get_default_time_zone()
        owner = self._entry.entry_id.lower()
        moved = []
        for item in await async_list_statistic_ids(self._hass):
            parts = _split(item["statistic_id"])
            if item.get("source") != DOMAIN or parts is None or parts[0] != owner:
                continue
            key = parts[1]
            if not item.get("has_sum"):
                continue
            unit = item.get("statistics_unit_of_measurement")
            scale = _scale(key)
            convert = scale is not None and unit == scale[0]
            sid = item["statistic_id"]
            found = await recorder.async_add_executor_job(
                statistics_during_period,
                self._hass,
                datetime.fromtimestamp(0, UTC),
                None,
                {sid},
                "hour",
                None,
                {"state", "sum"},
            )
            rows = [
                {"start": datetime.fromtimestamp(row["start"], UTC)}
                | {name: row[name] for name in ("state", "sum") if row.get(name) is not None}
                for row in found.get(sid, [])
            ]
            if convert:
                rows = _scaled(key, rows)
            closing = _missing_closing_rows(rows, tz=tz)
            if not convert and not closing:
                continue
            metadata = self._metadata(key, sum_kind=True, mean_type=StatisticMeanType.NONE)
            if not convert:
                # The unit as the statistic has it, for a key this release no longer knows.
                metadata["unit_of_measurement"] = unit
                metadata["unit_class"] = _UNIT_CLASSES.get(unit or "")
            # Converted, every row goes again; otherwise only the closing rows are new.
            written = sorted(rows + closing, key=lambda row: row["start"]) if convert else closing
            async_add_external_statistics(self._hass, metadata, written)
            moved.append(key)
        if moved:
            await _async_settled(recorder)
            _LOGGER.info(
                "Statistics of %s brought up to 1.0.0: %s", self._entry.title, ", ".join(moved)
            )
        self.ledger.units = 3
        await self.async_flush()

    async def async_take_over(self, owner: str) -> bool:
        """Move a removed phone's statistics to this entry, and carry on their sums (P2-30).

        The recorder renames only statistics it compiles itself, so the rows are copied
        under this entry's ids, checked, and only then is the old copy cleared. A check that
        fails leaves the old statistics where they were.

        The ledger was deleted with the old entry; `seed_taken_over` starts it again on the
        last copied day, so the totals continue and that day can still change. Anything
        older that arrives later is added only where the copy has no row (P2-28), so a
        backfill counts nothing twice. Tried once: after a failure this entry writes its own
        rows, and a later copy would land on top of them. Returns whether it was done.
        """
        if not self.available:
            _LOGGER.warning("Recorder not loaded; history of a removed phone not taken over yet")
            return False
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            async_list_statistic_ids,
            statistics_during_period,
        )

        recorder = get_instance(self._hass)
        await recorder.async_db_ready
        await _async_settled(recorder)
        tz = dt_util.get_default_time_zone()
        taken: dict[str, int] = {}
        sums: dict[str, list[tuple[datetime, float, float]]] = {}
        hours: dict[str, list[tuple[datetime, float, float, float]]] = {}
        for item in await async_list_statistic_ids(self._hass):
            parts = _split(item["statistic_id"])
            if item.get("source") != DOMAIN or parts is None or parts[0] != owner:
                continue
            old_sid, key = item["statistic_id"], parts[1]
            found = await recorder.async_add_executor_job(
                statistics_during_period,
                self._hass,
                datetime.fromtimestamp(0, UTC),
                None,
                {old_sid},
                "hour",
                None,
                {"state", "sum", "mean", "min", "max"},
            )
            old_rows = found.get(old_sid, [])
            if not old_rows:
                continue
            has_sum = bool(item.get("has_sum"))
            fields = ("state", "sum") if has_sum else ("mean", "min", "max")
            rows = [
                {"start": datetime.fromtimestamp(row["start"], UTC)}
                | {name: row[name] for name in fields if row.get(name) is not None}
                for row in old_rows
            ]
            if key in NIGHT_STATISTICS:
                # A row whose mean was taken away (`_take_away_rows`) is no row to copy.
                rows = [row for row in rows if row.get("mean") is not None]
            metadata = self._metadata(
                key,
                sum_kind=has_sum,
                mean_type=StatisticMeanType.NONE if has_sum else StatisticMeanType.ARITHMETIC,
            )
            copied_unit = item.get("statistics_unit_of_measurement")
            scale = _scale(key)
            if has_sum and scale is not None and copied_unit == scale[0]:
                # From a phone on 0.12 or older, in m or min: copied in km or h.
                rows = _scaled(key, rows)
            else:
                # The unit as the old statistic has it, for a key this release no longer knows.
                metadata["unit_of_measurement"] = copied_unit
                metadata["unit_class"] = _UNIT_CLASSES.get(copied_unit or "")
            async_add_external_statistics(self._hass, metadata, rows)
            taken[old_sid] = len(rows)
            if has_sum:
                # Back in the ledger's unit, which is what the phone sends.
                back = 1 / _factor(key) if metadata["unit_of_measurement"] == _unit(key) else 1.0
                sums[key] = [
                    (row["start"], (row.get("state") or 0.0) * back, row["sum"] * back)
                    for row in rows
                    if row.get("sum") is not None
                ]
            elif key not in NIGHT_STATISTICS:
                # The nights stand on their own rows; only measured hours are carried on.
                hours[key] = [
                    (
                        row["start"],
                        row["mean"],
                        row.get("min", row["mean"]),
                        row.get("max", row["mean"]),
                    )
                    for row in rows
                    if row.get("mean") is not None
                ]

        if not taken:
            return True
        await _async_settled(recorder)
        for old_sid, count in taken.items():
            new_sid = statistic_id(self._entry, _split(old_sid)[1])  # type: ignore[index]
            found = await recorder.async_add_executor_job(
                statistics_during_period,
                self._hass,
                datetime.fromtimestamp(0, UTC),
                None,
                {new_sid},
                "hour",
                None,
                {"mean", "sum"},
            )
            if len(found.get(new_sid, [])) != count:
                _LOGGER.warning(
                    "Copying %s gave %d rows instead of %d; the old statistics are kept",
                    old_sid,
                    len(found.get(new_sid, [])),
                    count,
                )
                return False

        recorder.async_clear_statistics(list(taken))
        seed_taken_over(self.ledger, sums=sums, hours=hours, today=dt_util.now(tz).date(), tz=tz)
        await self.async_flush()
        _LOGGER.info(
            "Took over %d statistics of a removed phone for %s", len(taken), self._entry.title
        )
        return True

    def _statistic_name(self, key: str) -> str:
        """An app's statistic is named after the app, as its sensor is."""
        package = self._app_packages.get(key)
        if package is None:
            return _name(self._entry, key)
        roster = getattr(getattr(self._entry, "runtime_data", None), "apps", None)
        label = roster.labels.get(package, package) if roster is not None else package
        return f"{self._entry.title} {label} screen time"

    async def _async_import_old(
        self,
        old_days: dict[str, dict[str, float]],
        old_hours: dict[str, dict[str, HourBucket]],
        tz: tzinfo,
    ) -> None:
        """Write the days and hours from before what the ledger keeps (P2-28).

        The ledger no longer holds those, so it cannot tell a day it counted from one it
        never saw. The recorder can: a day with a row there was counted. A day without one
        is new, and every later row's running sum moves up by its amount, which the
        recorder does itself (adjust_statistics) before the new rows are added. The amount
        joins what the ledger carries, so the rows it writes later agree.

        What this cannot see: a second session on an old day that already has a row, or the
        rest of an hour whose row exists. Both are left out rather than counted twice.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            statistics_during_period,
        )

        recorder = get_instance(self._hass)
        async with self._old_lock:
            # The rows earlier payloads queued must be in before they are read back.
            await _async_settled(recorder)
            added = 0
            for key, amounts in old_days.items():
                sid = statistic_id(self._entry, key)
                found = await recorder.async_add_executor_job(
                    statistics_during_period,
                    self._hass,
                    datetime.fromtimestamp(0, UTC),
                    None,
                    {sid},
                    "hour",
                    None,
                    {"state", "sum"},
                )
                existing = {row["start"]: row.get("sum") or 0.0 for row in found.get(sid, [])}
                states = {row["start"]: row.get("state") for row in found.get(sid, [])}
                new = {
                    start: amount
                    for iso, amount in amounts.items()
                    if (start := _day_start(date.fromisoformat(iso), tz)).timestamp()
                    not in existing
                }
                if not new:
                    continue

                unit = _unit(key)
                # The statistics are in the statistic's unit, the ledger in the phone's.
                factor = _factor(key)
                for start, amount in new.items():
                    if any(ts > start.timestamp() for ts in existing):
                        recorder.async_adjust_statistics(
                            sid, start + timedelta(hours=1), amount * factor, unit or ""
                        )
                rows = []
                for start in sorted(new):
                    before = [ts for ts in existing if ts < start.timestamp()]
                    base = existing[max(before)] if before else 0.0
                    total = base + factor * sum(a for s, a in new.items() if s <= start)
                    state = new[start] * factor
                    rows.append({"start": start, "state": state, "sum": total})
                    # The evening rows a period's start is read from (history.closing_rows):
                    # the one before the new day, with the day before's amount as its state,
                    # and the new day's own evening when a later day already has a row there.
                    day = start.astimezone(tz).date()
                    eve = closing_start(day - timedelta(days=1), tz)
                    eve_midnight = _day_start(day - timedelta(days=1), tz)
                    # None before the very first day: a period from there starts at 0.
                    first = not before and start == min(new)
                    if eve.timestamp() not in existing and not first:
                        eve_state = (
                            new[eve_midnight] * factor
                            if eve_midnight in new
                            else states.get(eve_midnight.timestamp())
                        )
                        rows.append({"start": eve, "state": eve_state, "sum": total - state})
                    own = closing_start(day, tz)
                    if own.timestamp() in existing:
                        rows.append({"start": own, "state": state, "sum": total})
                rows.sort(key=lambda row: row["start"])
                async_add_external_statistics(
                    self._hass,
                    self._metadata(key, sum_kind=True, mean_type=StatisticMeanType.NONE),
                    rows,
                )
                # Nothing awaited since the adjustment was queued, so a payload cannot slip
                # in between and write a row that misses the amount.
                self.ledger.carried[key] = self.ledger.carried.get(key, 0.0) + sum(new.values())
                added += len(new)

            for key, buckets in old_hours.items():
                sid = statistic_id(self._entry, key)
                hours = {datetime.fromisoformat(iso): bucket for iso, bucket in buckets.items()}
                starts = sorted(hours)
                found = await recorder.async_add_executor_job(
                    statistics_during_period,
                    self._hass,
                    starts[0],
                    starts[-1] + timedelta(hours=1),
                    {sid},
                    "hour",
                    None,
                    {"mean"},
                )
                taken = {row["start"] for row in found.get(sid, [])}
                rows = [
                    {
                        "start": start,
                        "mean": hours[start].mean,
                        "min": hours[start].minimum,
                        "max": hours[start].maximum,
                    }
                    for start in starts
                    if start.timestamp() not in taken
                ]
                if rows:
                    async_add_external_statistics(
                        self._hass,
                        self._metadata(key, sum_kind=False, mean_type=StatisticMeanType.ARITHMETIC),
                        rows,
                    )
                    added += len(rows)

            if added:
                _LOGGER.info(
                    "Added %d days and hours from before the ledger to the history of %s",
                    added,
                    self._entry.title,
                )
                self._store.async_delay_save(self.ledger.to_dict, SAVE_DELAY_SECONDS)

    def _apps_on(self) -> dict[str, str]:
        """The packages whose screen time sensor is on, each with its statistic key.

        Their sensors only show the newest day; the statistic keeps every day. An app whose
        sensor is off gets none, so the statistics hold only what the user chose to see.
        """
        from homeassistant.helpers import entity_registry as er

        prefix = app_unique_id(self._entry.entry_id, "")
        packages = {
            item.unique_id.removeprefix(prefix)
            for item in er.async_entries_for_config_entry(
                er.async_get(self._hass), self._entry.entry_id
            )
            if item.unique_id.startswith(prefix) and item.disabled_by is None
        }
        self._app_packages = {app_key(package): package for package in packages}
        return {package: app_key(package) for package in packages}

    def _metadata(self, key: str, *, sum_kind: bool, mean_type: Any) -> dict[str, Any]:
        return {
            "statistic_id": statistic_id(self._entry, key),
            "source": DOMAIN,
            "name": self._statistic_name(key),
            "unit_of_measurement": _unit(key),
            "unit_class": _UNIT_CLASSES.get(_unit(key) or ""),
            "has_sum": sum_kind,
            "mean_type": mean_type,
        }


async def _async_settled(recorder: Any) -> None:
    """Wait until the recorder ran and committed every task queued before this one.

    Not `async_block_till_done`: that returns at once when the queue is empty, which it
    also is while the recorder still runs the last task it took, so a read straight after
    could miss rows an import was writing. A synchronize task is queued behind them all.
    """
    from homeassistant.components.recorder.tasks import SynchronizeTask

    future: asyncio.Future[None] = recorder.hass.loop.create_future()
    recorder.queue_task(SynchronizeTask(future))
    await future


def _missing_closing_rows(rows: list[dict[str, Any]], *, tz: tzinfo) -> list[dict[str, Any]]:
    """The evening rows a day sum written before 1.0.0 lacks (`history.closing_rows`): one
    at 23:00 before every day with a row at midnight, with the total of that evening and
    the evening's own amount as its state, except before the very first day."""
    taken = {row["start"].timestamp() for row in rows}
    states = {row["start"].timestamp(): row.get("state") for row in rows}
    closing: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        start = row["start"].astimezone(tz)
        if start.hour != 0 or row.get("sum") is None:
            continue
        state = row.get("state") or 0.0
        before = row["sum"] - state
        if index == 0 and not before:
            continue
        day = start.date()
        eve = closing_start(day - timedelta(days=1), tz)
        if eve.timestamp() in taken:
            continue
        eve_state = states.get(_day_start(day - timedelta(days=1), tz).timestamp())
        closing.append({"start": eve, "state": eve_state, "sum": before})
        taken.add(eve.timestamp())
    return closing


def _day_start(day: date, tz: tzinfo) -> datetime:
    """Where a day's row starts: local midnight on the hour, as day_rows writes it."""
    return datetime.combine(day, time.min, tzinfo=tz).replace(minute=0, second=0, microsecond=0)
