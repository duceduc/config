"""Workouts: the exercise sessions of the last two days, and the sensors read from them.

The exercise records of a sync are what the Last workout sensor shows, newest first, like
every other latest-value sensor. Exercise today and Exercise yesterday are sums over a
day, and a sync only carries what is new, so they are read from the sessions the ledger
keeps (history.py): every session of today and yesterday, by uuid, so a record that
arrives again or is edited replaces itself.

A session that crosses midnight counts on both days for the part of it that fell on
each, so a run from 23:30 to 00:30 is half an hour on the one day and half an hour on
the other. The long-term statistic `exercise_minutes` is different on purpose: it books a
whole session on the day it ended, as it always did.

The shortest workout that counts is an option. It is applied when a sensor is read, never
when a session is stored, so changing it takes effect at once and loses nothing.

Every workout also carries its details, such as calories, distance and heart rate, read
from the other records of its time when a sensor is published (workout_details.py).

A session that is no longer of those two days stays as a past workout, for as long as the
ledger keeps days. The records its details are read from go as they always did, so it
takes the details along as they are when it leaves. One that arrives already older, as a
backfill sends them, first waits a few hours for its records, which can come in other
payloads. `kept_workouts` returns them all, for the action `life_dashboard.get_workouts`.

Nothing here imports from Home Assistant.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Final

from .payload import (
    KEY_EXERCISE_TODAY,
    KEY_EXERCISE_YESTERDAY,
    KEY_LAST_WORKOUT,
    SensorUpdate,
    is_own_record,
    parse_instant,
)
from .workout_details import (
    DETAIL_KEYS,
    PENDING_SECONDS,
    apply_records,
    summarize_exercise,
    workout_details,
)
from .workout_types import type_code, workout_type

#: How many days back a session is kept: yesterday and today.
KEPT_DAYS: Final = 2

#: How many past workouts wait for their records at once. A backfill sends its windows
#: oldest first, so by the time this many newer ones came, the oldest has had its records.
MAX_WAITING: Final = 50


@dataclass(frozen=True)
class Workout:
    """One exercise session."""

    #: The type exactly as the phone sent it, "56" or "running".
    code: str
    start: datetime
    end: datetime
    seconds: float
    #: The app that wrote the record to Health Connect, when the phone said so.
    source: str | None = None
    uuid: str | None = None
    #: What the recording app wrote inside the session: laps, and sets with repetitions.
    laps: int | None = None
    lap_meters: float | None = None
    sets: int | None = None
    repetitions: int | None = None
    #: Per exercise type of the sets: (type code, sets, repetitions).
    exercises: tuple[tuple[str, int, int], ...] = ()

    @property
    def kind(self) -> str:
        """The key it is shown as: running, walking, other."""
        return workout_type(self.code)

    @property
    def minutes(self) -> float:
        return self.seconds / 60


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def parse_workout(record: Any) -> Workout | None:
    """A workout from an exercise record, or None when it cannot be read.

    Needs an end and a length; a record without a length is as long as its span. The start
    is the one that is sent, or the end minus the length when it is missing or does not
    come before the end.
    """
    if not isinstance(record, dict) or is_own_record(record):
        return None
    try:
        end = parse_instant(record.get("end_time"))
    except ValueError:
        return None

    seconds = _finite_number(record.get("duration_seconds"))
    try:
        start = parse_instant(record.get("start_time"))
    except ValueError:
        start = None
    if start is not None and start >= end:
        start = None
    if record.get("duration_seconds") is None and start is not None:
        seconds = (end - start).total_seconds()
    if seconds is None or seconds <= 0:
        return None
    if start is None:
        start = end - timedelta(seconds=seconds)

    uuid = record.get("uuid")
    source = record.get("source")
    return Workout(
        code=type_code(record.get("type")),
        start=start,
        end=end,
        seconds=seconds,
        source=source if isinstance(source, str) and source else None,
        uuid=uuid if isinstance(uuid, str) and uuid else None,
        **_summary_fields(summarize_exercise(record)),
    )


def _summary_fields(summary: dict[str, Any]) -> dict[str, Any]:
    """The Workout fields of a summary, as summarize_exercise writes it or a row keeps it."""
    fields: dict[str, Any] = {}
    for name in ("laps", "sets", "repetitions"):
        value = summary.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            fields[name] = value
    meters = _finite_number(summary.get("lap_meters"))
    if meters is not None:
        fields["lap_meters"] = meters
    exercises = summary.get("exercises")
    if isinstance(exercises, list):
        fields["exercises"] = tuple(
            (str(code), int(sets), int(reps))
            for entry in exercises
            if isinstance(entry, (list, tuple)) and len(entry) == 3
            for code, sets, reps in [entry]
        )
    return fields


def _row(workout: Workout) -> dict[str, Any]:
    """What a kept session holds. The keys after `seconds` only when there is something."""
    row: dict[str, Any] = {
        "type": workout.code,
        "start": workout.start.isoformat(),
        "end": workout.end.isoformat(),
        "seconds": workout.seconds,
    }
    if workout.source:
        row["source"] = workout.source
    for name in ("laps", "lap_meters", "sets", "repetitions"):
        if (value := getattr(workout, name)) is not None:
            row[name] = value
    if workout.exercises:
        row["exercises"] = [list(entry) for entry in workout.exercises]
    return row


def identity(workout: Workout) -> str:
    """What a session is stored under: its uuid, or for a record without one the moment,
    the length and the type, which identify it for the case that matters, the same batch
    arriving again."""
    if workout.uuid:
        return workout.uuid
    return f"{workout.end.isoformat()}:{workout.seconds}:{workout.code}"


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def cutoff(now: datetime, tz: tzinfo) -> datetime:
    """The start of the oldest day that is kept."""
    today = now.astimezone(tz).date()
    return _local_midnight(today - timedelta(days=KEPT_DAYS - 1), tz)


def keep_sessions(
    sessions: dict[str, dict[str, Any]],
    data: dict[str, Any],
    *,
    now: datetime,
    tz: tzinfo,
    past: dict[str, dict[str, Any]] | None = None,
    pool: dict[str, Any] | None = None,
) -> bool:
    """Fold the exercise records of a payload into the stored sessions, by uuid.

    Deletions come first: the apps send a removed workout in `deleted_records`, and on
    iOS every edit is a deletion of the old uuid plus a record with a new one, so a kept
    session left in place would count next to its edited copy. A deleted uuid can come back
    later as a record, which is why the records are applied after the deletions.

    Sessions that ended before the start of yesterday are dropped, and never stored when
    a backfill brings them; an edit that moves a kept session to before yesterday drops it
    too. Returns whether anything changed.

    With `past`, the past workouts by the same uuid, such a session goes there instead of
    being lost. One that leaves the two days takes its details along as the records in
    `pool` say at that moment, the last one they can. One that arrives already older waits
    there for its records, and gets its details once it waited long enough
    (`_finish_waiting`). A deletion takes a past workout away as well, and a record that
    arrives again changes nothing, so a workout is in one of the two places and never in
    both. A past workout that is deleted and written again as it was, in one payload,
    stays as it is: its records are gone, and it would come back without its details.
    """
    records = data.get("exercise")
    changed = False
    oldest = cutoff(now, tz)
    removed: dict[str, dict[str, Any]] = {}

    deleted = data.get("deleted_records")
    if isinstance(deleted, list):
        for entry in deleted:
            if not isinstance(entry, dict) or entry.get("type") != "exercise":
                continue
            uuid = entry.get("uuid")
            if not isinstance(uuid, str):
                continue
            if sessions.pop(uuid, None) is not None:
                changed = True
            if past is not None and uuid in past:
                removed[uuid] = past.pop(uuid)
                changed = True

    if isinstance(records, list):
        for record in records:
            workout = parse_workout(record)
            if workout is None:
                continue
            row = _row(workout)
            key = identity(workout)
            if workout.end <= oldest:
                if sessions.pop(key, None) is not None:
                    changed = True
                if past is not None:
                    known = past.get(key, removed.get(key))
                    if known is not None and _bare(known) == row:
                        past[key] = known
                    else:
                        # New, or edited: its records get their few hours from now.
                        past[key] = {**row, "received": math.floor(now.timestamp())}
                        changed = True
                continue
            if past is not None and past.pop(key, None) is not None:
                changed = True
            if sessions.get(key) != row:
                sessions[key] = row
                changed = True

    for key, row in list(sessions.items()):
        try:
            workout = _from_row(row, key)
        except (KeyError, TypeError, ValueError):
            workout = None
        if workout is None or workout.end <= oldest:
            del sessions[key]
            changed = True
            if workout is not None and past is not None:
                past[key] = _finished(row, workout, pool)

    if past and _finish_waiting(past, pool, now=now):
        changed = True
    return changed


def _bare(row: dict[str, Any]) -> dict[str, Any]:
    """A past workout's row without what was added to the session it was kept from."""
    return {key: value for key, value in row.items() if key not in ("received", "details")}


def _finished(row: dict[str, Any], workout: Workout, pool: dict[str, Any] | None) -> dict[str, Any]:
    """A past workout's row once nothing more is waited for: the session, and its details
    as the records say now. Always with `details`, which is empty when there were none."""
    return {**_bare(row), "details": workout_details(pool, workout)}


def _finish_waiting(
    past: dict[str, dict[str, Any]], pool: dict[str, Any] | None, *, now: datetime
) -> bool:
    """Give the past workouts that waited long enough their details, and stop waiting.

    Long enough is PENDING_SECONDS, as long as a record waits for a workout. With more
    than MAX_WAITING waiting, the ones that waited longest go too, the oldest session
    first: every one of them holds its records in the pool. A row that cannot be read is
    dropped. Returns whether anything changed.
    """
    waiting = []
    changed = False
    for key, row in list(past.items()):
        if "received" not in row:
            continue
        try:
            waiting.append((row["received"], _from_row(row, key), key))
        except (KeyError, TypeError, ValueError):
            del past[key]
            changed = True
    waiting.sort(key=lambda item: (item[0], item[1].end, item[2]))
    due = math.floor(now.timestamp()) - PENDING_SECONDS
    surplus = len(waiting) - MAX_WAITING
    for index, (received, workout, key) in enumerate(waiting):
        if received > due and index >= surplus:
            break
        past[key] = _finished(past[key], workout, pool)
        changed = True
    return changed


def _from_row(row: dict[str, Any], key: str | None = None) -> Workout:
    if not isinstance(row, dict):
        raise TypeError(f"not a stored session: {row!r}")
    source = row.get("source")
    return Workout(
        code=row["type"],
        start=datetime.fromisoformat(row["start"]),
        end=datetime.fromisoformat(row["end"]),
        seconds=float(row["seconds"]),
        source=source if isinstance(source, str) and source else None,
        uuid=key,
        **_summary_fields(row),
    )


def stored_workouts(sessions: dict[str, dict[str, Any]]) -> list[Workout]:
    """The stored sessions, oldest first. A row that cannot be read is skipped."""
    workouts = []
    for key, row in sessions.items():
        try:
            workouts.append(_from_row(row, key))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(workouts, key=lambda workout: workout.start)


def waiting_workouts(past: dict[str, dict[str, Any]]) -> list[Workout]:
    """The past workouts that still wait for their records, which the pool keeps for them
    as it does for the stored sessions."""
    return stored_workouts({key: row for key, row in past.items() if "received" in row})


def clean_past(raw: Any) -> dict[str, dict[str, Any]]:
    """The past workouts as a ledger holds them, without the rows that cannot be read: one
    bad row in the file must not make every later payload fail."""
    past: dict[str, dict[str, Any]] = {}
    for key, row in raw.items() if isinstance(raw, dict) else ():
        try:
            _from_row(row, key)
        except (KeyError, TypeError, ValueError):
            continue
        # It waits since a moment, or it has its details; a row that says neither is no row.
        if "received" in row:
            readable = _finite_number(row["received"]) is not None
        else:
            readable = isinstance(row.get("details"), dict)
        if readable:
            past[key] = dict(row)
    return past


def prune_past(past: dict[str, dict[str, Any]], *, before: date, tz: tzinfo) -> bool:
    """Let the past workouts that ended before a local day go, as the ledger lets the
    sessions of its day sums go. Returns whether anything went."""
    boundary = _local_midnight(before, tz)
    dropped = False
    for key, row in list(past.items()):
        try:
            old = _from_row(row).end < boundary
        except (KeyError, TypeError, ValueError):
            old = True
        if old:
            del past[key]
            dropped = True
    return dropped


def _local(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).isoformat()


def _session_row(
    workout: Workout, tz: tzinfo, minutes: int, details: dict[str, Any]
) -> dict[str, Any]:
    """An entry of the `sessions` attribute: a workout, the minutes of it that count and
    its details."""
    return {
        "type": workout.kind,
        "start": _local(workout.start, tz),
        "end": _local(workout.end, tz),
        "minutes": minutes,
        **details,
    }


def day_sessions(
    workouts: Iterable[Workout],
    day: date,
    *,
    tz: tzinfo,
    min_minutes: float = 0,
    pool: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The sessions that have a part on a local day, each with the minutes that fell on it.

    A session shorter than `min_minutes` is left out; one that crosses midnight is listed
    on both days with its start and end whole and only its minutes split. With the pool of
    records, each also carries its details, of the whole session like its start and end.
    """
    day_start = _local_midnight(day, tz)
    day_end = _local_midnight(day + timedelta(days=1), tz)
    rows = []
    for workout in workouts:
        if workout.minutes < min_minutes:
            continue
        span = (workout.end - workout.start).total_seconds()
        overlap = (min(workout.end, day_end) - max(workout.start, day_start)).total_seconds()
        if overlap <= 0:
            continue
        rows.append(
            _session_row(
                workout,
                tz,
                # The length it was sent with, shared out by the time that fell on the day.
                round(workout.minutes * min(overlap / span, 1)),
                workout_details(pool, workout),
            )
        )
    return sorted(rows, key=lambda row: row["start"])


def kept_workouts(
    sessions: dict[str, dict[str, Any]],
    past: dict[str, dict[str, Any]],
    *,
    tz: tzinfo,
    pool: dict[str, Any] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    """The kept workouts as data, each in the format of an entry of the `sessions` attribute.

    The sessions of yesterday and today and the past ones as one list, oldest first. Each
    is in it once and whole, so one that crosses midnight has all its minutes, and
    whatever its length: the shortest workout is an option of the sensors, and whoever
    reads the list picks. Every workout that ended on a local day in the window, both ends
    included: the day the statistic `exercise_minutes` books it on. A past workout has the
    details it left the two days with; a session of those days, and a past one that still
    waits for its records, reads them from the pool.
    """
    listed = []
    for key, row in {**past, **sessions}.items():
        try:
            workout = _from_row(row, key)
        except (KeyError, TypeError, ValueError):
            continue
        day = workout.end.astimezone(tz).date()
        if (start is not None and day < start) or (end is not None and day > end):
            continue
        # A copy, so whoever gets the list cannot change what the ledger keeps.
        details = (
            copy.deepcopy(row["details"]) if "details" in row else workout_details(pool, workout)
        )
        entry = _session_row(workout, tz, round(workout.minutes), details)
        listed.append((workout.start, workout.end, key, entry))
    return [entry for *_, entry in sorted(listed, key=lambda item: item[:3])]


def day_minutes(
    workouts: Iterable[Workout], day: date, *, tz: tzinfo, min_minutes: float = 0
) -> int:
    """The minutes of a local day that had a workout going, rounded once.

    Time covered by more than one session counts once: the same run written by the phone
    and by the watch, or by two apps, is one run. Summing the sessions would count it twice,
    and rounding each one first would turn ten sessions of 40 seconds into 10 minutes.
    """
    day_start = _local_midnight(day, tz)
    day_end = _local_midnight(day + timedelta(days=1), tz)
    spans = sorted(
        (max(workout.start, day_start), min(workout.end, day_end))
        for workout in workouts
        if workout.minutes >= min_minutes and workout.end > day_start and workout.start < day_end
    )
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in spans:
        if current_end is None or start > current_end:
            if current_start is not None and current_end is not None:
                total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        elif end > current_end:
            current_end = end
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds()
    return round(total / 60)


def day_updates(
    workouts: Iterable[Workout],
    *,
    now: datetime,
    tz: tzinfo,
    min_minutes: float = 0,
    pool: dict[str, Any] | None = None,
) -> list[SensorUpdate]:
    """Exercise today and Exercise yesterday, as of `now`."""
    today = now.astimezone(tz).date()
    kept = list(workouts)
    updates = []
    for key, day in (
        (KEY_EXERCISE_TODAY, today),
        (KEY_EXERCISE_YESTERDAY, today - timedelta(days=1)),
    ):
        sessions = day_sessions(kept, day, tz=tz, min_minutes=min_minutes, pool=pool)
        updates.append(
            SensorUpdate(
                key=key,
                value=day_minutes(kept, day, tz=tz, min_minutes=min_minutes),
                measured_at=_local_midnight(day, tz),
                attributes={"date": day.isoformat(), "sessions": sessions},
            )
        )
    return updates


def newest_workout(records: Any, *, min_minutes: float = 0) -> Workout | None:
    """The session of a payload that ended last, of those long enough."""
    if not isinstance(records, list):
        return None
    best: Workout | None = None
    for record in records:
        workout = parse_workout(record)
        if workout is None or workout.minutes < min_minutes:
            continue
        if best is None or workout.end > best.end:
            best = workout
    return best


def payload_pool(data: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """The records of one payload alone, for a workout older than the ones kept."""
    pool: dict[str, Any] = {}
    apply_records(pool, data, now=now)
    return pool


def workout_update(
    workout: Workout, *, tz: tzinfo, pool: dict[str, Any] | None = None
) -> SensorUpdate:
    """Last workout, for one session, with the details its records justify."""
    attributes: dict[str, Any] = {
        "start": _local(workout.start, tz),
        "end": _local(workout.end, tz),
        "duration_minutes": round(workout.minutes),
        "type_code": workout.code,
    }
    if workout.source:
        attributes["source"] = workout.source
    if workout.uuid:
        attributes["uuid"] = workout.uuid
    attributes.update(workout_details(pool, workout))
    return SensorUpdate(
        key=KEY_LAST_WORKOUT, value=workout.kind, measured_at=workout.end, attributes=attributes
    )


def last_workout_update(
    records: Any, *, tz: tzinfo, min_minutes: float = 0, pool: dict[str, Any] | None = None
) -> SensorUpdate | None:
    """Last workout, from the newest session of a payload that is long enough."""
    best = newest_workout(records, min_minutes=min_minutes)
    return None if best is None else workout_update(best, tz=tz, pool=pool)


def refresh_last_workout(
    current: SensorUpdate | None, workouts: Iterable[Workout], pool: dict[str, Any] | None
) -> SensorUpdate | None:
    """Last workout again with the details as they are now, or None when nothing changed.

    Its records can come after the workout itself: a watch that uploads later, a sync that
    sent the run before the calories. The workout is found among the kept ones by its
    uuid, or, for a record without one, by its start, end and type.
    """
    if current is None:
        return None
    attributes = current.attributes
    match: Workout | None = None
    for workout in workouts:
        if attributes.get("uuid"):
            if workout.uuid == attributes["uuid"]:
                match = workout
                break
            continue
        try:
            start = parse_instant(attributes.get("start"))
            end = parse_instant(attributes.get("end"))
        except ValueError:
            return None
        if (workout.start, workout.end, workout.code) == (start, end, attributes.get("type_code")):
            match = workout
            break
    if match is None:
        return None
    refreshed = {key: value for key, value in attributes.items() if key not in DETAIL_KEYS}
    refreshed.update(workout_details(pool, match))
    if refreshed == attributes:
        return None
    return SensorUpdate(
        key=current.key,
        value=current.value,
        measured_at=current.measured_at,
        attributes=refreshed,
        last_reset=current.last_reset,
    )
