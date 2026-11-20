"""Workout details: calories, distance, pace, heart rate and steps of one workout.

An exercise record says when a workout was and what kind it was, nothing more. What
happened during it is in the other records: calories, distance and steps over a stretch
of time, heart rate one sample at a time. So the integration keeps those records for a
while (the pool), and a workout's details are read from the ones that overlap it, every
time the sensors are published. That way it does not matter in which order they come:
a phone that syncs every 15 minutes sends the heart rate of a run before the run itself,
and a watch can upload everything hours later with the original timestamps.

The pool, as stored in the ledger:

    add: series -> key -> [source, start, end, amount, received]
    heart_rate: group -> [source, received, [[start, end, count, sum, min, max], ...]]

Times are whole seconds since the epoch, which keeps the file small. A record is stored
under its uuid, so one that arrives again or is edited replaces itself, and one named in
`deleted_records` goes. Heart rate is stored per record (the samples of one record arrive
together, see docs/webhook.md), folded into a row per minute. Buckets have no uuid; they
are stored under their window and the payload's `sequence`, so a payload that arrives
twice replaces its own bucket and a later payload with more samples for the window adds to
it, the way the app's docs say buckets combine. A bucket marked `complete` replaces every
earlier one for its window.

Nothing is kept longer than it can matter: records that overlap a kept workout (yesterday
and today, see workouts.py), and records received in the last few hours, which may still
turn out to belong to a workout that arrives later.

Calories, distance and steps are never added up across apps: a phone and a watch often
both record the same walk. The workout's own app counts; when it wrote none, the one app
with the most. Heart rate is an average, so every app's samples go in.

Nothing here imports from Home Assistant.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Final

from .payload import is_own_record, parse_instant
from .workout_types import segment_type

#: The accumulated series a workout reads, with the field each raw record holds.
ADDITIVE_SERIES: Final[dict[str, str]] = {
    "active_calories": "calories",
    "total_calories": "calories",
    "distance": "meters",
    "steps": "count",
}
HEART_RATE: Final = "heart_rate"

#: How long a record that overlaps no kept workout stays, waiting for one to arrive.
PENDING_SECONDS: Final = 6 * 3600

#: The kinds a pace per kilometre and a cadence make sense for.
PACE_KINDS: Final = frozenset({"running", "running_treadmill", "walking", "hiking"})
#: The kinds a speed in km/h makes sense for.
SPEED_KINDS: Final = frozenset({"biking", "biking_stationary"})

#: Health Connect's ExerciseSegment constants for a pause (39) and a rest (44), which are not sets.
NOT_A_SET: Final = frozenset({"39", "44"})
#: At most this many exercises are listed for one workout.
MAX_EXERCISES: Final = 20

#: Every attribute the details can add, for a sensor that has to replace them.
DETAIL_KEYS: Final[tuple[str, ...]] = (
    "active_calories",
    "total_calories",
    "distance_km",
    "pace_min_per_km",
    "pace",
    "speed_kmh",
    "heart_rate_avg",
    "heart_rate_min",
    "heart_rate_max",
    "steps",
    "cadence_spm",
    "laps",
    "sets",
    "repetitions",
    "exercises",
)

_BUCKET: Final = "b:"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _epoch(moment: datetime) -> int:
    return math.floor(moment.timestamp())


def _source(record: dict[str, Any]) -> str | None:
    source = record.get("source")
    return source if isinstance(source, str) and source else None


def _bucket_source(bucket: dict[str, Any]) -> str | None:
    """A bucket's apps as one name; a mix of apps is a source of its own."""
    sources = bucket.get("sources")
    if not isinstance(sources, list):
        return None
    named = sorted({s for s in sources if isinstance(s, str) and s})
    return "+".join(named) or None


def _sequence(data: dict[str, Any]) -> str:
    sequence = data.get("sequence")
    if isinstance(sequence, int) and not isinstance(sequence, bool):
        return str(sequence)
    return "-"


def _is_bucketed(records: list[Any]) -> bool:
    return bool(records) and isinstance(records[0], dict) and "bucket_start" in records[0]


def _span(record: dict[str, Any], start: str, end: str) -> tuple[int, int] | None:
    try:
        lo = _epoch(parse_instant(record.get(start)))
        hi = _epoch(parse_instant(record.get(end)))
    except ValueError:
        return None
    return (lo, hi) if hi >= lo else None


# --- Taking a payload in ---------------------------------------------------------------


def apply_records(pool: dict[str, Any], data: dict[str, Any], *, now: datetime) -> bool:
    """Fold the records of a payload a workout can read into the pool.

    Deletions first, as for the workouts: a deleted uuid can come back as a record, and
    the app never sends one as both in the same payload. Returns whether anything changed.
    """
    received = _epoch(now)
    add: dict[str, dict[str, list]] = pool.setdefault("add", {})
    hr: dict[str, list] = pool.setdefault(HEART_RATE, {})
    changed = False

    deleted = data.get("deleted_records")
    if isinstance(deleted, list):
        for entry in deleted:
            if not isinstance(entry, dict) or not isinstance(entry.get("uuid"), str):
                continue
            kind, uuid = entry.get("type"), entry["uuid"]
            # A heart rate record is kept whole under its own uuid, samples and all.
            known = hr if kind == HEART_RATE else add.get(kind) if kind in ADDITIVE_SERIES else None
            if known is not None and known.pop(uuid, None) is not None:
                changed = True

    sequence = _sequence(data)
    for series, value_field in ADDITIVE_SERIES.items():
        records = data.get(series)
        if not isinstance(records, list) or not records:
            continue
        known = add.setdefault(series, {})
        if _is_bucketed(records):
            changed |= _apply_additive_buckets(known, records, sequence, received)
        else:
            changed |= _apply_additive_records(known, records, value_field, received)

    records = data.get(HEART_RATE)
    if isinstance(records, list) and records:
        if _is_bucketed(records):
            changed |= _apply_heart_rate_buckets(hr, records, sequence, received)
        else:
            changed |= _apply_heart_rate_samples(hr, records, received)

    for series in [series for series, known in add.items() if not known]:
        del add[series]
    return changed


def _store_row(known: dict[str, list], key: str, row: list) -> bool:
    """Store a row, keeping the moment it was first received when nothing else changed,
    so a repeat changes nothing at all."""
    previous = known.get(key)
    if previous is not None and previous[:-1] == row[:-1]:
        return False
    known[key] = row
    return True


def _apply_additive_records(
    known: dict[str, list], records: list[Any], value_field: str, received: int
) -> bool:
    changed = False
    for record in records:
        if not isinstance(record, dict) or is_own_record(record):
            continue
        span = _span(record, "start_time", "end_time")
        amount = _number(record.get(value_field))
        if span is None or amount is None:
            continue
        source = _source(record)
        uuid = record.get("uuid")
        key = uuid if isinstance(uuid, str) and uuid else f"{span[0]}:{span[1]}:{amount}:{source}"
        changed |= _store_row(known, key, [source, span[0], span[1], amount, received])
    return changed


def _apply_additive_buckets(
    known: dict[str, list], buckets: list[Any], sequence: str, received: int
) -> bool:
    changed = False
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        span = _span(bucket, "bucket_start", "bucket_end")
        total = _number(bucket.get("total"))
        if total is None:
            total = _number(bucket.get("sum"))
        if span is None or total is None:
            continue
        window = f"{_BUCKET}{span[0]}:"
        key = f"{window}{sequence}"
        if bucket.get("complete") is True:
            # Holds the whole window: what came before for it is in here already.
            for stale in [k for k in known if k.startswith(window) and k != key]:
                del known[stale]
                changed = True
        row = [_bucket_source(bucket), span[0], span[1], total, received]
        changed |= _store_row(known, key, row)
    return changed


def _apply_heart_rate_buckets(
    hr: dict[str, list], buckets: list[Any], sequence: str, received: int
) -> bool:
    changed = False
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        span = _span(bucket, "bucket_start", "bucket_end")
        mean = _number(bucket.get("avg"))
        if span is None or mean is None:
            continue
        count = _number(bucket.get("sample_count"))
        count = count if count is not None and count > 0 else 1.0
        low = _number(bucket.get("min"))
        high = _number(bucket.get("max"))
        row = [
            span[0],
            span[1],
            count,
            mean * count,
            low if low is not None else mean,
            high if high is not None else mean,
        ]
        group = [_bucket_source(bucket), received, [row]]
        key = f"{_BUCKET}{span[0]}:{sequence}"
        changed |= _store_group(hr, key, group, merge=False)
    return changed


def _apply_heart_rate_samples(hr: dict[str, list], samples: list[Any], received: int) -> bool:
    """Samples, grouped by the record they belong to, one row per minute of it."""
    records: dict[str, tuple[str | None, dict[int, float]]] = {}
    for sample in samples:
        if not isinstance(sample, dict) or is_own_record(sample):
            continue
        try:
            moment = parse_instant(sample.get("time"))
        except ValueError:
            continue
        bpm = _number(sample.get("bpm"))
        if bpm is None:
            continue
        millis = math.floor(moment.timestamp() * 1000)
        source = _source(sample)
        uuid = sample.get("uuid")
        record = uuid.split("#", 1)[0] if isinstance(uuid, str) and uuid else f"t:{millis}:{source}"
        # Keyed by the moment, so a sample that is in the payload twice counts once.
        records.setdefault(record, (source, {}))[1][millis] = bpm

    changed = False
    for record, (source, readings) in records.items():
        minutes: dict[int, list[float]] = {}
        for millis in sorted(readings):
            second = millis // 1000
            bpm = readings[millis]
            row = minutes.get(second // 60)
            if row is None:
                minutes[second // 60] = [second, second, 1, bpm, bpm, bpm]
            else:
                row[1] = second
                row[2] += 1
                row[3] += bpm
                row[4] = min(row[4], bpm)
                row[5] = max(row[5], bpm)
        rows = [minutes[minute] for minute in sorted(minutes)]
        changed |= _store_group(hr, record, [source, received, rows], merge=True)
    return changed


def _store_group(hr: dict[str, list], key: str, group: list, *, merge: bool) -> bool:
    """Store the rows of one heart rate record.

    A record arrives whole, so a record that comes again replaces what was kept of it
    for the stretch its samples cover. What was kept outside that stretch stays: a record
    that arrived in parts adds up, and a part that comes again replaces only itself.
    """
    previous = hr.get(key)
    if previous is not None and merge:
        new_rows = group[2]
        new_lo, new_hi = min(r[0] for r in new_rows), max(r[1] for r in new_rows)
        outside = [r for r in previous[2] if r[1] < new_lo or r[0] > new_hi]
        if outside:
            group = [group[0], group[1], sorted(outside + new_rows)]
    if previous is not None and previous[0] == group[0] and previous[2] == group[2]:
        return False
    hr[key] = group
    return True


def _numbers(values: Any, length: int) -> bool:
    return (
        isinstance(values, list)
        and len(values) == length
        and all(_number(value) is not None for value in values)
    )


def clean_pool(data: Any) -> dict[str, Any]:
    """A stored pool, without the rows that cannot be read: one bad row in the file must
    not make every later payload fail."""
    if not isinstance(data, dict) or not data:
        return {}
    add: dict[str, dict[str, list]] = {}
    stored_add = data.get("add")
    for series, rows in (stored_add if isinstance(stored_add, dict) else {}).items():
        if series not in ADDITIVE_SERIES or not isinstance(rows, dict):
            continue
        kept = {
            key: list(row)
            for key, row in rows.items()
            if isinstance(row, list)
            and len(row) == 5
            and (row[0] is None or isinstance(row[0], str))
            and _numbers(row[1:], 4)
        }
        if kept:
            add[series] = kept
    heart_rate: dict[str, list] = {}
    stored_hr = data.get(HEART_RATE)
    for key, group in (stored_hr if isinstance(stored_hr, dict) else {}).items():
        if (
            isinstance(group, list)
            and len(group) == 3
            and (group[0] is None or isinstance(group[0], str))
            and _number(group[1]) is not None
            and isinstance(group[2], list)
            and group[2]
            and all(_numbers(row, 6) for row in group[2])
        ):
            heart_rate[key] = [group[0], group[1], [list(row) for row in group[2]]]
    return {"add": add, HEART_RATE: heart_rate}


# --- Letting go -------------------------------------------------------------------------


def _overlaps(lo: float, hi: float, windows: Iterable[tuple[float, float]]) -> bool:
    for start, end in windows:
        if hi > lo:
            if lo < end and hi > start:
                return True
        elif start <= lo <= end:
            return True
    return False


def prune_pool(
    pool: dict[str, Any],
    windows: list[tuple[float, float]],
    *,
    now: datetime,
    cutoff: datetime,
) -> bool:
    """Keep what overlaps a kept workout, and what came in lately and is still recent.

    `windows` are the kept workouts as (start, end) in epoch seconds; `cutoff` the start
    of the oldest day a workout is kept for. A heart rate record kept only for a workout
    keeps only its minutes during workouts. Returns whether anything was dropped.
    """
    recent = _epoch(now) - PENDING_SECONDS
    oldest = _epoch(cutoff)
    changed = False

    add = pool.get("add", {})
    for series in list(add):
        known = add[series]
        for key in list(known):
            source, lo, hi, _amount, received = known[key]
            if _overlaps(lo, hi, windows) or (received >= recent and hi > oldest):
                continue
            del known[key]
            changed = True
        if not known:
            del add[series]

    hr = pool.get(HEART_RATE, {})
    for key in list(hr):
        source, received, rows = hr[key]
        if received >= recent and max(row[1] for row in rows) > oldest:
            continue
        kept = [row for row in rows if _overlaps(row[0], row[1], windows)]
        if not kept:
            del hr[key]
            changed = True
        elif len(kept) != len(rows):
            hr[key] = [source, received, kept]
            changed = True
    return changed


def windows_of(workouts: Iterable[Any]) -> list[tuple[float, float]]:
    """The (start, end) of workouts, in epoch seconds."""
    return [(workout.start.timestamp(), workout.end.timestamp()) for workout in workouts]


# --- Reading a workout's details --------------------------------------------------------


def _fraction(lo: float, hi: float, start: float, end: float) -> float:
    """How much of a record falls inside a workout; a moment is in or out."""
    if hi <= lo:
        return 1.0 if start <= lo <= end else 0.0
    return max(0.0, min(hi, end) - max(lo, start)) / (hi - lo)


def _additive(
    known: dict[str, list], start: float, end: float, own_source: str | None
) -> float | None:
    """A series' amount during a workout, from one app only.

    Per app, its records when it sent records, else its buckets: a series switched to
    buckets meanwhile would otherwise count twice.
    """
    per_source: dict[str | None, list[float | None]] = {}
    for key, (source, lo, hi, amount, _received) in known.items():
        fraction = _fraction(lo, hi, start, end)
        if fraction <= 0:
            continue
        slot = per_source.setdefault(source, [None, None])
        index = 1 if key.startswith(_BUCKET) else 0
        slot[index] = (slot[index] or 0.0) + amount * fraction
    if not per_source:
        return None
    totals = {
        source: raw if raw is not None else bucketed
        for source, (raw, bucketed) in per_source.items()
    }
    if own_source in totals:
        return totals[own_source]
    return max(value for value in totals.values() if value is not None)


def _heart_rate(hr: dict[str, list], start: float, end: float) -> tuple[float, float, float] | None:
    """Average, minimum and maximum, weighted by the samples inside the workout."""
    weight = total = 0.0
    low = high = None
    for _source, _received, rows in hr.values():
        for lo, hi, count, summed, row_min, row_max in rows:
            fraction = _fraction(lo, hi, start, end)
            if fraction <= 0 or count <= 0:
                continue
            weight += count * fraction
            total += summed * fraction
            low = row_min if low is None else min(low, row_min)
            high = row_max if high is None else max(high, row_max)
    if weight <= 0 or low is None or high is None:
        return None
    return total / weight, low, high


def _pace(minutes_per_km: float) -> str:
    seconds = round(minutes_per_km * 60)
    return f"{seconds // 60}:{seconds % 60:02d}"


def workout_details(pool: dict[str, Any] | None, workout: Any) -> dict[str, Any]:
    """The attributes a workout's records justify; a key without data is left out."""
    start, end = workout.start.timestamp(), workout.end.timestamp()
    add = (pool or {}).get("add", {})
    hr = (pool or {}).get(HEART_RATE, {})
    kind = workout.kind
    details: dict[str, Any] = {}

    amounts = {
        series: _additive(add.get(series, {}), start, end, workout.source)
        for series in ADDITIVE_SERIES
    }
    for series in ("active_calories", "total_calories"):
        if amounts[series] is not None:
            details[series] = round(amounts[series])

    meters = amounts["distance"]
    if meters is None and workout.lap_meters:
        meters = workout.lap_meters
    if meters is not None:
        details["distance_km"] = round(meters / 1000, 2)
    minutes = workout.minutes
    if meters and meters > 0 and minutes > 0:
        km = meters / 1000
        if kind in PACE_KINDS:
            details["pace_min_per_km"] = round(minutes / km, 2)
            details["pace"] = _pace(minutes / km)
        elif kind in SPEED_KINDS:
            details["speed_kmh"] = round(km / (minutes / 60), 1)

    if (heart_rate := _heart_rate(hr, start, end)) is not None:
        average, low, high = heart_rate
        details["heart_rate_avg"] = round(average)
        details["heart_rate_min"] = round(low)
        details["heart_rate_max"] = round(high)

    steps = amounts["steps"]
    if steps is not None:
        details["steps"] = round(steps)
        if kind in PACE_KINDS and minutes > 0:
            details["cadence_spm"] = round(steps / minutes)

    if workout.laps:
        details["laps"] = workout.laps
    if workout.sets:
        details["sets"] = workout.sets
    if workout.repetitions:
        details["repetitions"] = workout.repetitions
    if workout.exercises:
        details["exercises"] = [
            {
                "type": segment_type(code),
                "type_code": code,
                "sets": sets,
                "repetitions": repetitions,
            }
            for code, sets, repetitions in workout.exercises
        ]
    return details


# --- What an exercise record carries inside it -----------------------------------------


def summarize_exercise(record: dict[str, Any]) -> dict[str, Any]:
    """Laps and sets of an exercise record, as the few numbers a workout keeps of them.

    laps and lap_meters (the sum of the laps that have a length); sets, the segments that
    are not a rest or a pause, with their repetitions, in total and per exercise type.
    """
    summary: dict[str, Any] = {}
    laps = record.get("laps")
    if isinstance(laps, list):
        valid = [lap for lap in laps if isinstance(lap, dict)]
        if valid:
            summary["laps"] = len(valid)
            meters = [m for lap in valid if (m := _number(lap.get("meters"))) is not None]
            if meters:
                summary["lap_meters"] = sum(meters)

    segments = record.get("segments")
    if isinstance(segments, list):
        exercises: dict[str, list[int]] = {}
        sets = repetitions = 0
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            code = segment.get("type")
            code = str(code) if isinstance(code, (str, int)) and not isinstance(code, bool) else ""
            if code in NOT_A_SET:
                continue
            reps = segment.get("repetitions")
            reps = reps if isinstance(reps, int) and not isinstance(reps, bool) and reps > 0 else 0
            sets += 1
            repetitions += reps
            entry = exercises.setdefault(code, [0, 0])
            entry[0] += 1
            entry[1] += reps
        if sets:
            summary["sets"] = sets
            if repetitions:
                summary["repetitions"] = repetitions
            listed = [[code, n, reps] for code, (n, reps) in exercises.items() if code]
            if listed:
                summary["exercises"] = listed[:MAX_EXERCISES]
    return summary
