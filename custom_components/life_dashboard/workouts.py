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

Nothing here imports from Home Assistant.
"""

from __future__ import annotations

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
from .workout_details import DETAIL_KEYS, apply_records, summarize_exercise, workout_details
from .workout_types import type_code, workout_type

#: How many days back a session is kept: yesterday and today.
KEPT_DAYS: Final = 2


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
    sessions: dict[str, dict[str, Any]], data: dict[str, Any], *, now: datetime, tz: tzinfo
) -> bool:
    """Fold the exercise records of a payload into the stored sessions, by uuid.

    Deletions come first: the apps send a removed workout in `deleted_records`, and on
    iOS every edit is a deletion of the old uuid plus a record with a new one, so a kept
    session left in place would count next to its edited copy. A deleted uuid can come back
    later as a record, which is why the records are applied after the deletions.

    Sessions that ended before the start of yesterday are dropped, and never stored when
    a backfill brings them; an edit that moves a kept session to before yesterday drops it
    too. Returns whether anything changed.
    """
    records = data.get("exercise")
    changed = False
    oldest = cutoff(now, tz)

    deleted = data.get("deleted_records")
    if isinstance(deleted, list):
        for entry in deleted:
            if not isinstance(entry, dict) or entry.get("type") != "exercise":
                continue
            uuid = entry.get("uuid")
            if isinstance(uuid, str) and sessions.pop(uuid, None) is not None:
                changed = True

    if isinstance(records, list):
        for record in records:
            workout = parse_workout(record)
            if workout is None:
                continue
            if workout.end <= oldest:
                if sessions.pop(identity(workout), None) is not None:
                    changed = True
                continue
            row = _row(workout)
            key = identity(workout)
            if sessions.get(key) != row:
                sessions[key] = row
                changed = True

    for key, row in list(sessions.items()):
        try:
            expired = _from_row(row).end <= oldest
        except (KeyError, TypeError, ValueError):
            expired = True
        if expired:
            del sessions[key]
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


def _local(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).isoformat()


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
            {
                "type": workout.kind,
                "start": _local(workout.start, tz),
                "end": _local(workout.end, tz),
                # The length it was sent with, shared out by the time that fell on the day.
                "minutes": round(workout.minutes * min(overlap / span, 1)),
                **workout_details(pool, workout),
            }
        )
    return sorted(rows, key=lambda row: row["start"])


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
