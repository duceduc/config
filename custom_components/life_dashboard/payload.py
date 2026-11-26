"""Signature verification and payload parsing for Life Dashboard.

This module deliberately imports nothing from Home Assistant, so the whole contract
with the phone can be unit tested on its own. The webhook handler feeds it a decoded
JSON object and gets back the sensor updates that object justifies.

The payload contract is documented in the companion app repository, docs/webhook.md
and docs/webhook-schema.json. The facts this module depends on:

- One sync is one POST per source; a backlog produces several POSTs, and a failed
  delivery is re-POSTed later from the app's outbox. So the same record arrives more
  than once and every update carries measured_at for the caller to order on.
- Records are optional everywhere. A payload only carries the arrays that had data.
- Nine dense series can arrive bucketed instead of raw, under the same key, with
  "bucket_start" as the reliable discriminator.
- The iOS app sends the same field names, daily_totals from 1.4.0 (none before), and
  its blood pressure records can lack "diastolic".

Since 0.7.0 the contract has a second half: the integration answers every accepted
POST with a JSON body, signed with a key derived from the same secret, so the phone can
receive measurements from Home Assistant. The request side of that (the "writeback"
block, the heartbeat) is read here; what goes into the answer is decided in
writeback_queue.py, and this module only signs and frames it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final

from .workout_types import WORKOUT_TYPES

SIGNATURE_HEADER: Final = "X-Signature"
SIGNATURE_PREFIX: Final = "sha256="

# The Android app's application id. Records it wrote to Health Connect itself come back
# with this as their source at the next sync; they are what Home Assistant sent it, so
# they feed neither a sensor nor the ledger. The app skips them too; this is the second
# layer, for an app older than the one that learned to.
APP_PACKAGE: Final = "com.owen282000.lifedashboard"

# The protocol version of the answer, and the label the answer key is derived with. The
# key is HMAC(secret, label) rather than the secret itself, so a captured request can
# never be replayed as an answer: the two directions verify against different keys.
RESPONSE_PROTOCOL: Final = 1
RESPONSE_KEY_LABEL: Final = b"life-dashboard-response-v1"

# The signed body: the answer and its signature together in the body, for a route that
# drops the header. Home Assistant Cloud hands the phone only the status, the body and
# the Content-Type of what a webhook answers. A request asks for it with
# writeback.answer, and the announcement carries the form's version.
ANSWER_IN_BODY: Final = "body"
SIGNED_BODY: Final = 1
SIGNED_BODY_KEY: Final = "life_dashboard_signed"

# Payload sources the app can send. healthkit_ios is the iOS companion app.
SOURCE_HEALTH_CONNECT: Final = "health_connect"
SOURCE_HEALTHKIT_IOS: Final = "healthkit_ios"
SOURCE_SCREEN_TIME: Final = "screen_time"

# The top app's state when the app filter left no app of today.
NO_TOP_APP: Final = "none"
HEALTH_SOURCES: Final = frozenset({SOURCE_HEALTH_CONNECT, SOURCE_HEALTHKIT_IOS})

# Every array key a health payload can carry, for counting records. From
# docs/webhook.md; the app omits an array entirely when it has no records.
HEALTH_ARRAYS: Final = frozenset(
    {
        "steps",
        "sleep",
        "heart_rate",
        "distance",
        "speed",
        "active_calories",
        "total_calories",
        "weight",
        "height",
        "blood_pressure",
        "blood_glucose",
        "oxygen_saturation",
        "body_temperature",
        "respiratory_rate",
        "resting_heart_rate",
        "exercise",
        "hydration",
        "nutrition",
        "mindfulness",
        "body_fat",
        "lean_body_mass",
        "bone_mass",
        "body_water_mass",
        "heart_rate_variability",
        "menstruation_period",
        "menstruation_flow",
        "basal_metabolic_rate",
        "vo2_max",
        "skin_temperature",
        "basal_body_temperature",
        "intermenstrual_bleeding",
        "ovulation_test",
        "cervical_mucus",
        "sexual_activity",
    }
)

# Sensor keys.
KEY_LAST_HEALTH_SYNC: Final = "last_health_sync"
KEY_LAST_SCREEN_TIME_SYNC: Final = "last_screen_time_sync"
_SYNC_KEYS: Final = frozenset({KEY_LAST_HEALTH_SYNC, KEY_LAST_SCREEN_TIME_SYNC})

# Not a sensor of its own: the newest day's minutes per app, which the per-app screen
# time sensors read their state from. One update for every app rather than one per app,
# so an app missing from a newer day reads 0 instead of keeping what it had, and the
# ordering rule keeps one entry instead of one per package.
KEY_SCREEN_TIME_APPS: Final = "screen_time_apps"

# The exercise sensors. Last workout comes from the exercise records of a payload; the
# two day sums come from the sessions the ledger keeps (workouts.py), so neither is made
# by parse_payload.
KEY_LAST_WORKOUT: Final = "last_workout"
KEY_EXERCISE_TODAY: Final = "exercise_today"
# What was eaten today, summed from the meals the ledger holds (nutrition.py).
KEY_CALORIES_EATEN_TODAY: Final = "calories_eaten_today"
KEY_PROTEIN_TODAY: Final = "protein_today"
KEY_CARBS_TODAY: Final = "carbs_today"
KEY_FAT_TODAY: Final = "fat_today"
KEY_EXERCISE_YESTERDAY: Final = "exercise_yesterday"

# The newest main night, read from the nights the ledger keeps (sleep.py).
KEY_LAST_SLEEP_ASLEEP: Final = "last_sleep_asleep"
KEY_LAST_SLEEP_DEEP: Final = "last_sleep_deep"
KEY_LAST_SLEEP_LIGHT: Final = "last_sleep_light"
KEY_LAST_SLEEP_REM: Final = "last_sleep_rem"
KEY_LAST_SLEEP_AWAKE: Final = "last_sleep_awake"

# State classes, as the string values Home Assistant's SensorStateClass uses.
STATE_CLASS_MEASUREMENT: Final = "measurement"
STATE_CLASS_TOTAL: Final = "total"


def signature_for(secret: str, body: bytes) -> str:
    """Build the X-Signature header value the app sends for this body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Check the signature over the raw body, in constant time.

    The app only sends the header when a secret is configured, so a missing header
    means the user did not paste the secret and the request must be refused.
    """
    if not header_value:
        return False
    return hmac.compare_digest(signature_for(secret, body), header_value)


def tile_hours(minutes: float) -> float:
    """Minutes as hours, rounded up at the fourth decimal: a tile cuts off the seconds, so
    386 minutes, stored as 6.43333 hours, would read 6h 25m."""
    return math.ceil(round(minutes / 60 * 10_000, 6)) / 10_000


def response_key(secret: str) -> bytes:
    """The key the answer is signed with: the 32 raw bytes of HMAC(secret, label)."""
    return hmac.new(secret.encode("utf-8"), RESPONSE_KEY_LABEL, hashlib.sha256).digest()


def response_signature_for(secret: str, body: bytes) -> str:
    """Build the X-Signature header value the integration puts on its answer."""
    digest = hmac.new(response_key(secret), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def format_instant(moment: datetime) -> str:
    """An instant as the app expects it: UTC, whole seconds, with a Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def response_body(
    *,
    version: str,
    in_reply_to: str,
    issued_at: datetime,
    configured: list[str],
    pending: list[dict[str, Any]] | None = None,
    more: bool | None = None,
) -> dict[str, Any]:
    """The answer to an accepted POST, as a JSON object.

    Every accepted POST gets one, also a test ping and a request from an app that will
    never read it: the announcement is how a newer app learns that this integration can
    send measurements. pending and more only appear when the caller decided the request
    asked for readings; a request without writeback.types gets neither key.
    """
    writeback: dict[str, Any] = {
        "in_reply_to": in_reply_to,
        "issued_at": format_instant(issued_at),
        "configured": list(configured),
    }
    if pending is not None:
        writeback["pending"] = pending
        writeback["more"] = bool(more)
    return {
        "life_dashboard": {
            "version": version,
            "writeback": RESPONSE_PROTOCOL,
            "signed_body": SIGNED_BODY,
        },
        "writeback": writeback,
    }


def writeback_request(data: dict[str, Any]) -> dict[str, Any] | None:
    """The writeback block of a request, if it carries one that counts.

    Only a health payload can ask for readings: the app puts the block in nothing
    else, and a screen time payload that carries one anyway is not answered with
    readings. This is the one place that rule lives.
    """
    if data.get("source") not in HEALTH_SOURCES:
        return None
    block = data.get("writeback")
    return block if isinstance(block, dict) else None


def is_heartbeat(data: dict[str, Any]) -> bool:
    """A POST the app sends only to collect readings: a writeback block and no records.

    With nothing to sync the app used to stay silent; with receiving on it posts the
    minimal body instead, so there is a response to carry the readings. It is not a
    sync, so it moves no sensor, not even the last-sync timestamp.
    """
    if data.get("test") is True or writeback_request(data) is None:
        return False
    if isinstance(data.get("daily_totals"), list):
        return False
    return not any(name in HEALTH_ARRAYS for name in data)


def wants_signed_body(data: dict[str, Any]) -> bool:
    """Whether the request asked for the answer's signature inside the body.

    Read from the writeback block of any payload, a test ping and a screen time payload
    included: it is about the form of the answer, not about readings, so the rule that
    only a health payload can ask for those does not apply.
    """
    block = data.get("writeback")
    return isinstance(block, dict) and block.get("answer") == ANSWER_IN_BODY


def signed_body(raw: bytes, signature: str) -> bytes:
    """The answer and its signature as one JSON body.

    The answer travels as text, byte for byte the JSON that was signed, so nothing on
    the way can reorder or respace it, and the phone checks the signature over that text
    before it parses it. json.dumps escapes everything outside ASCII, here and in the
    answer itself, so the body is plain ASCII.
    """
    envelope = {SIGNED_BODY_KEY: SIGNED_BODY, "answer": raw.decode("utf-8"), "signature": signature}
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def frame_answer(
    secret: str, answer: dict[str, Any], *, in_body: bool = False
) -> tuple[bytes, str]:
    """The answer as the exact bytes to send, and the X-Signature header for them.

    With in_body the bytes are the signed body around the answer. The header signs what
    is sent in both forms, so on a route that keeps it, it still covers the whole body.
    """
    raw = json.dumps(answer, separators=(",", ":"), allow_nan=False).encode("utf-8")
    signature = response_signature_for(secret, raw)
    if not in_body:
        return raw, signature
    body = signed_body(raw, signature)
    return body, response_signature_for(secret, body)


# Java's Instant.toString() and Swift's ISO8601DateFormatter both emit UTC with a Z,
# with zero to nine fractional digits. Python accepts at most six.
_FRACTION = re.compile(r"\.(\d{1,9})")


def parse_instant(value: Any) -> datetime:
    """Parse an ISO-8601 instant from the app into a timezone-aware datetime.

    Raises ValueError on anything that is not a usable timestamp, including a naive
    one, so a caller can skip the record.
    """
    if not isinstance(value, str):
        raise ValueError(f"not a timestamp: {value!r}")

    def _truncate(match: re.Match[str]) -> str:
        return "." + match.group(1)[:6]

    parsed = datetime.fromisoformat(_FRACTION.sub(_truncate, value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without a timezone: {value!r}")
    return parsed


def _parse_date(value: Any) -> date:
    """Parse a YYYY-MM-DD day from the app."""
    if not isinstance(value, str):
        raise ValueError(f"not a date: {value!r}")
    return date.fromisoformat(value)


def _number(value: Any) -> float | int:
    """Return value if it is a real number, else raise.

    bool is rejected on purpose: JSON true would otherwise become 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"not a number: {value!r}")
    return value


@dataclass(frozen=True)
class SensorUpdate:
    """One value for one sensor, with the moment it describes.

    measured_at is what the caller orders on: an update older than the value a
    sensor already holds is dropped, which makes re-delivered batches and backfill
    harmless without keeping a record of uuids.
    """

    key: str
    #: None only for a sensor that has no value for what it describes.
    value: float | int | str | datetime | None
    measured_at: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    last_reset: datetime | None = None


@dataclass(frozen=True)
class SensorSpec:
    """How one sensor presents itself in Home Assistant.

    device_class and state_class are the string values of Home Assistant's enums, so
    this module stays free of Home Assistant imports; sensor.py maps them back.
    """

    key: str
    unit: str | None = None
    device_class: str | None = None
    #: None on the sensors history.py writes a statistic for: the recorder's own would
    #: sit next to the correct one and, for a latest-value sensor, be wrong.
    state_class: str | None = None
    precision: int | None = None
    diagnostic: bool = False
    #: False for a sensor that would crowd the device page; the user can turn it on.
    enabled: bool = True
    #: The states of an enum sensor.
    options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _Latest:
    """A latest-value sensor: which array, which field, which time field."""

    key: str
    array: str
    value_field: str
    time_field: str = "time"
    to_int: bool = False


# The latest-value sensors, in the order they appear in the README table. Keys and
# units match the app's MQTT sensors so a user moving over sees the same names; only the
# last sleep differs, in hours here so a tile reads 7h 30m, and in minutes over MQTT.
_LATEST: Final[tuple[_Latest, ...]] = (
    _Latest("heart_rate", "heart_rate", "bpm", to_int=True),
    _Latest("resting_heart_rate", "resting_heart_rate", "bpm", to_int=True),
    _Latest("heart_rate_variability", "heart_rate_variability", "heart_rate_variability_millis"),
    _Latest("speed", "speed", "meters_per_second"),
    _Latest("sleep_duration", "sleep", "duration_seconds", time_field="session_end_time"),
    _Latest("weight", "weight", "kilograms"),
    _Latest("blood_pressure_systolic", "blood_pressure", "systolic"),
    _Latest("blood_pressure_diastolic", "blood_pressure", "diastolic"),
    _Latest("blood_glucose", "blood_glucose", "mmol_per_liter"),
    _Latest("oxygen_saturation", "oxygen_saturation", "percentage"),
    _Latest("body_temperature", "body_temperature", "celsius"),
    _Latest("skin_temperature_delta", "skin_temperature", "delta_celsius"),
    _Latest("basal_body_temperature", "basal_body_temperature", "celsius"),
    _Latest("respiratory_rate", "respiratory_rate", "rate"),
    _Latest("hydration", "hydration", "liters", time_field="end_time"),
    _Latest("body_fat", "body_fat", "percentage"),
    _Latest("lean_body_mass", "lean_body_mass", "kilograms"),
    _Latest("bone_mass", "bone_mass", "kilograms"),
    _Latest("body_water_mass", "body_water_mass", "kilograms"),
    _Latest("basal_metabolic_rate", "basal_metabolic_rate", "kilocalories_per_day"),
    _Latest("vo2_max", "vo2_max", "vo2_ml_per_min_per_kg"),
    _Latest("height", "height", "meters"),
)

# Series that can arrive bucketed and are averaged: the bucket's avg is the value.
# The accumulated ones (steps, distance, calories) are ignored when bucketed,
# because their day figures come from daily_totals instead.
_BUCKETED_MEASURED: Final = {
    "heart_rate": "heart_rate",
    "heart_rate_variability": "heart_rate_variability",
    "speed": "speed",
    "oxygen_saturation": "oxygen_saturation",
    "respiratory_rate": "respiratory_rate",
    "skin_temperature": "skin_temperature_delta",
}

# The four day totals, from daily_totals only.
_TODAY: Final[tuple[tuple[str, str], ...]] = (
    ("steps_today", "steps"),
    ("distance_today", "distance_meters"),
    ("active_calories_today", "active_calories"),
    ("total_calories_today", "total_calories"),
)

SENSOR_SPECS: Final[dict[str, SensorSpec]] = {
    spec.key: spec
    for spec in (
        # Latest value.
        SensorSpec("heart_rate", "bpm", precision=0),
        SensorSpec("resting_heart_rate", "bpm", precision=0),
        SensorSpec("heart_rate_variability", "ms", precision=1),
        # Running, cycling and treadmill apps write speed during a workout (app 1.29.0).
        SensorSpec("speed", "m/s", "speed", precision=2),
        # Hours, so a tile reads 6h 47m: the frontend only splits a duration from the unit up.
        # Three decimals because it rounds to the display precision and then down to the minute.
        SensorSpec("sleep_duration", "h", "duration", precision=3),
        SensorSpec("weight", "kg", "weight", precision=1),
        SensorSpec("blood_pressure_systolic", "mmHg", precision=0),
        SensorSpec("blood_pressure_diastolic", "mmHg", precision=0),
        SensorSpec("blood_glucose", "mmol/L", "blood_glucose_concentration", precision=2),
        SensorSpec("oxygen_saturation", "%", precision=1),
        SensorSpec("body_temperature", "°C", "temperature", precision=1),
        SensorSpec("skin_temperature_delta", "°C", "temperature", precision=2),
        SensorSpec("basal_body_temperature", "°C", "temperature", precision=1),
        SensorSpec("respiratory_rate", "breaths/min", precision=1),
        # No volume device class: Home Assistant only allows that with TOTAL, which
        # fits a tank rather than "how much the last drink was".
        SensorSpec("hydration", "L", precision=2),
        SensorSpec("body_fat", "%", precision=1),
        SensorSpec("lean_body_mass", "kg", "weight", precision=1),
        SensorSpec("bone_mass", "kg", "weight", precision=1),
        SensorSpec("body_water_mass", "kg", "weight", precision=1),
        SensorSpec("basal_metabolic_rate", "kcal/d", precision=0),
        SensorSpec("vo2_max", "mL/min/kg", precision=1),
        SensorSpec("height", "m", "distance", precision=2),
        # Day totals. TOTAL rather than TOTAL_INCREASING: Health Connect can revise a
        # day downward after deduplicating, and TOTAL_INCREASING reads that as a reset.
        SensorSpec("steps_today", "steps", precision=0),
        SensorSpec("distance_today", "km", "distance", precision=2),
        # No energy device class for calories: that would offer them to the Energy
        # dashboard, which is not what these are.
        SensorSpec("active_calories_today", "kcal", precision=0),
        SensorSpec("total_calories_today", "kcal", precision=0),
        # Exercise. The state of Last workout is the kind of workout, from a list, so it
        # is an enum; the day sums are minutes like the screen time ones.
        SensorSpec(KEY_LAST_WORKOUT, None, "enum", options=WORKOUT_TYPES),
        SensorSpec(KEY_EXERCISE_TODAY, "min", "duration", precision=0),
        SensorSpec(KEY_EXERCISE_YESTERDAY, "min", "duration", precision=0),
        # Nutrition, today's sums of the meals (nutrition.py). No device class: an energy
        # one would offer calories to the Energy dashboard, a weight one reads oddly for food.
        SensorSpec(KEY_CALORIES_EATEN_TODAY, "kcal", precision=0),
        SensorSpec(KEY_PROTEIN_TODAY, "g", precision=0),
        SensorSpec(KEY_CARBS_TODAY, "g", precision=0),
        SensorSpec(KEY_FAT_TODAY, "g", precision=0),
        # The newest main night (sleep.py), which the night sensors show.
        SensorSpec(KEY_LAST_SLEEP_ASLEEP, "h", "duration", STATE_CLASS_MEASUREMENT, precision=3),
        SensorSpec(
            KEY_LAST_SLEEP_DEEP,
            "h",
            "duration",
            STATE_CLASS_MEASUREMENT,
            precision=3,
            enabled=False,
        ),
        SensorSpec(
            KEY_LAST_SLEEP_LIGHT,
            "h",
            "duration",
            STATE_CLASS_MEASUREMENT,
            precision=3,
            enabled=False,
        ),
        SensorSpec(
            KEY_LAST_SLEEP_REM, "h", "duration", STATE_CLASS_MEASUREMENT, precision=3, enabled=False
        ),
        SensorSpec(
            KEY_LAST_SLEEP_AWAKE,
            "h",
            "duration",
            STATE_CLASS_MEASUREMENT,
            precision=3,
            enabled=False,
        ),
        # Screen time.
        SensorSpec("screen_time_today", "h", "duration", precision=3),
        SensorSpec("screen_time_yesterday", "h", "duration", precision=3),
        # A text sensor: no unit, no device class and no state class, or Home
        # Assistant refuses a non-numeric state.
        SensorSpec("screen_time_top_app"),
        # Diagnostics.
        SensorSpec(KEY_LAST_HEALTH_SYNC, None, "timestamp", diagnostic=True),
        SensorSpec(KEY_LAST_SCREEN_TIME_SYNC, None, "timestamp", diagnostic=True),
    )
}

# Decimals a state keeps beyond its sensor's display precision. An app can store a
# reading as a 32-bit float, which reads back as 126.199996948242 for 126.2 kg. Two
# decimals more than the display shows keep everything a scale or a watch measures,
# grams for a weight, and drop that tail.
_STATE_EXTRA_DECIMALS: Final = 2


def state_value(key: str, value: Any) -> Any:
    """The value a sensor's state carries.

    A float is rounded to its sensor's precision plus two decimals; an integer, a text
    or a timestamp is left as it is, and so is a key without a precision. Only the state:
    the statistics take the readings as they came, and an hour's mean has decimals of
    its own anyway.
    """
    spec = SENSOR_SPECS.get(key)
    if spec is None or spec.precision is None or not isinstance(value, float):
        return value
    return round(value, spec.precision + _STATE_EXTRA_DECIMALS)


def _midnight(day: date, tz: tzinfo) -> datetime:
    """The start of a local day, as an aware datetime."""
    return datetime.combine(day, time.min, tzinfo=tz)


def is_own_record(record: dict[str, Any]) -> bool:
    """Whether a record is one the app wrote to Health Connect on our behalf."""
    return record.get("source") == APP_PACKAGE


def _record_attributes(record: dict[str, Any]) -> dict[str, Any]:
    """Attributes shared by every latest-value sensor."""
    attributes: dict[str, Any] = {}
    for name in ("source", "uuid"):
        value = record.get(name)
        if isinstance(value, str) and value:
            attributes[name] = value
    return attributes


def _latest_from_records(
    records: list[Any], spec: _Latest
) -> tuple[dict[str, Any], datetime, float | int] | None:
    """Pick the newest record that actually carries a usable value.

    Filtering on the value field, not just the time field, is what makes iOS blood
    pressure work: a systolic sample without a matching diastolic one is sent with
    "systolic" only, and the diastolic sensor has to look further back.
    """
    best: tuple[dict[str, Any], datetime, float | int] | None = None
    for record in records:
        if not isinstance(record, dict) or is_own_record(record):
            continue
        try:
            measured_at = parse_instant(record.get(spec.time_field))
            value = _number(record.get(spec.value_field))
        except ValueError:
            continue
        if best is None or measured_at > best[1]:
            best = (record, measured_at, value)
    return best


def _bucketed_update(key: str, buckets: list[Any]) -> SensorUpdate | None:
    """Turn the newest closed bucket of an averaged series into an update."""
    best: tuple[datetime, dict[str, Any]] | None = None
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        try:
            bucket_end = parse_instant(bucket.get("bucket_end"))
            _number(bucket.get("avg"))
        except ValueError:
            continue
        if best is None or bucket_end > best[0]:
            best = (bucket_end, bucket)
    if best is None:
        return None

    bucket_end, bucket = best
    attributes: dict[str, Any] = {}
    for name in ("sample_count", "min", "max"):
        value = bucket.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            attributes[name] = value
    sources = bucket.get("sources")
    if isinstance(sources, list):
        named = [s for s in sources if isinstance(s, str) and s]
        if named:
            attributes["sources"] = ", ".join(named)
    return SensorUpdate(
        key=key,
        value=bucket["avg"],
        measured_at=bucket_end,
        attributes=attributes,
    )


def _is_bucketed(records: list[Any]) -> bool:
    """A bucketed series replaces its raw array under the same key."""
    return bool(records) and isinstance(records[0], dict) and "bucket_start" in records[0]


def _newest_totals(data: dict[str, Any]) -> tuple[date, dict[str, Any]] | None:
    """The newest day of a payload's daily_totals, with its entry, or None."""
    totals = data.get("daily_totals")
    if not isinstance(totals, list):
        return None
    newest: tuple[date, dict[str, Any]] | None = None
    for entry in totals:
        if not isinstance(entry, dict):
            continue
        try:
            day = _parse_date(entry.get("date"))
        except ValueError:
            continue
        if newest is None or day > newest[0]:
            newest = (day, entry)
    return newest


def newest_totals_day(data: dict[str, Any]) -> date | None:
    """The newest day a payload sends day totals for, or None."""
    newest = _newest_totals(data)
    return newest[0] if newest is not None else None


def day_total_zeros(
    known: Mapping[str, SensorUpdate], day: date, *, tz: tzinfo
) -> list[SensorUpdate]:
    """A 0 for every day total that still shows a day before `day`.

    Health Connect leaves a metric out of a day that has no data for it yet, so
    without this the sensor kept yesterday's active energy until the first record
    of the new day (app discussion #257). Only for sensors that exist: a metric the
    phone never sent gets no sensor from this.
    """
    midnight = _midnight(day, tz)
    return [
        SensorUpdate(
            key=key,
            value=0 if key == "steps_today" else 0.0,
            measured_at=midnight,
            attributes={"date": day.isoformat()},
            last_reset=midnight,
        )
        for key, _ in _TODAY
        if (current := known.get(key)) is not None and current.measured_at < midnight
    ]


def _parse_health(data: dict[str, Any], tz: tzinfo) -> list[SensorUpdate]:
    """Sensor updates from a health payload, Android or iOS."""
    updates: list[SensorUpdate] = []

    # Day totals. An iOS app before 1.4.0 sends no daily_totals, so these stay absent
    # for that iPhone, exactly as with its MQTT sensors.
    if (newest := _newest_totals(data)) is not None:
        day, entry = newest
        midnight = _midnight(day, tz)
        for key, field_name in _TODAY:
            try:
                value = _number(entry.get(field_name))
            except ValueError:
                continue
            updates.append(
                SensorUpdate(
                    key=key,
                    # Distance in km, as its statistic and the graphs have it.
                    value=int(value)
                    if key == "steps_today"
                    else float(value) / 1000
                    if key == "distance_today"
                    else float(value),
                    measured_at=midnight,
                    attributes={"date": day.isoformat()},
                    last_reset=midnight,
                )
            )

    # Latest values, raw or bucketed.
    for spec in _LATEST:
        records = data.get(spec.array)
        if not isinstance(records, list) or not records:
            continue
        if _is_bucketed(records):
            key = _BUCKETED_MEASURED.get(spec.array)
            # Accumulated series (steps, distance, calories) carry no average and
            # their day figures come from daily_totals, so a bucket adds nothing.
            if key is not None and (update := _bucketed_update(key, records)) is not None:
                updates.append(update)
            continue
        found = _latest_from_records(records, spec)
        if found is None:
            continue
        record, measured_at, value = found
        if spec.key == "sleep_duration":
            value = tile_hours(value / 60)
        updates.append(
            SensorUpdate(
                key=spec.key,
                value=int(value) if spec.to_int else float(value),
                measured_at=measured_at,
                attributes=_record_attributes(record),
            )
        )

    updates.append(_sync_update(data, KEY_LAST_HEALTH_SYNC, tz))
    return updates


def _parse_screen_time(data: dict[str, Any], tz: tzinfo) -> list[SensorUpdate]:
    """Sensor updates from a screen time payload.

    Every sync re-sends the last seven days recomputed, so the newest payload wins
    per date and the day entries are ordered on their own date, not on arrival.

    With an app filter on in the app (`app_filter`, app 1.23.0) the sensors follow it:
    the minutes are those of the apps that are sent, and the top app is the most used
    of those, or "none" when the filter left no app of today.
    """
    updates: list[SensorUpdate] = []
    days: dict[date, dict[str, Any]] = {}
    entries = data.get("screen_time")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                days[_parse_date(entry.get("date"))] = entry
            except ValueError:
                continue

    if days:
        today = max(days)
        wanted = (
            ("screen_time_today", today),
            ("screen_time_yesterday", today - timedelta(days=1)),
        )
        for key, day in wanted:
            entry = days.get(day)
            if entry is None:
                continue
            try:
                minutes = screen_time_minutes(entry)
            except ValueError:
                continue
            midnight = _midnight(day, tz)
            updates.append(
                SensorUpdate(
                    key=key,
                    # In hours, so a tile reads 2h 51m, as the last sleep does.
                    value=tile_hours(minutes),
                    measured_at=midnight,
                    attributes=_screen_time_attributes(entry, day),
                    last_reset=midnight if key == "screen_time_today" else None,
                )
            )

        if (top := _top_app(days[today])) is not None:
            name, package, minutes = top
            updates.append(
                SensorUpdate(
                    key="screen_time_top_app",
                    value=name,
                    measured_at=_midnight(today, tz),
                    attributes={
                        "package": package,
                        "minutes": minutes,
                        "date": today.isoformat(),
                    },
                )
            )
        elif data.get("app_filter") is not None:
            # The filter left no app of today. Without an update the sensor would keep
            # naming the last top app, which may be one the user has just left out.
            updates.append(
                SensorUpdate(
                    key="screen_time_top_app",
                    value=NO_TOP_APP,
                    measured_at=_midnight(today, tz),
                    attributes={"date": today.isoformat()},
                )
            )

        # Also with no app at all: every app that has a sensor then reads 0 for today.
        updates.append(
            SensorUpdate(
                key=KEY_SCREEN_TIME_APPS,
                value=today.isoformat(),
                measured_at=_midnight(today, tz),
                attributes={"date": today.isoformat(), "apps": app_table(days, today)},
            )
        )

    updates.append(_sync_update(data, KEY_LAST_SCREEN_TIME_SYNC, tz))
    return updates


# The package segments the Android app skips when it makes a name up for a package it
# cannot look up, from ScreenTimeManager.fallbackAppName (app 1.13.2 and later).
_GENERIC_PACKAGE_SEGMENTS: Final = frozenset(
    {"android", "app", "apps", "mobile", "client", "main", "release", "prod", "free", "pro", "lite"}
)


def fallback_name(package: str) -> str:
    """The name the app (1.13.2 and later) makes up for a package it cannot look up:
    the last segment that says something, so org.wakingup.android is "wakingup"."""
    # The first segment is the TLD-style prefix (com, org, io) and never a name.
    segments = [segment for segment in package.split(".") if segment.strip()][1:]
    return next(
        (s for s in reversed(segments) if s.lower() not in _GENERIC_PACKAGE_SEGMENTS), package
    )


def is_fallback_label(package: str, label: str) -> bool:
    """Whether a label is the stand-in the app sends for a package it cannot look up.

    That happens for an app uninstalled since: its days still come, under a name made
    from the package, such as "youtube" for com.google.android.youtube. Before 1.13.2
    the app used the last segment as it was. Both are recomputed here exactly, so a real
    label is never mistaken for one unless it is that very word.
    """
    return label in (fallback_name(package), package.rsplit(".", 1)[-1])


def app_table(days: dict[date, dict[str, Any]], today: date) -> dict[str, dict[str, Any]]:
    """Per package: its label, its minutes on today, and its minutes over every day sent.

    Every app of the window is in it, also one with no minutes today, so a new sensor
    can be judged on the week. The label is the one of the newest day the app appears
    on, so a renamed app takes its new name, unless that is only the stand-in for an app
    uninstalled since. A stand-in is always the one of 1.13.2 and later: the last segment
    an older app sends is often "android", which would name several apps alike. An app
    without a package cannot be told apart from the next one and is left out; one the
    app filter took out never arrives.
    """
    table: dict[str, dict[str, Any]] = {}
    for day in sorted(days):
        for app in _apps(days[day]):
            package = app.get("package")
            if not isinstance(package, str) or not package:
                continue
            row = table.setdefault(package, {"name": package, "minutes": 0, "week_minutes": 0})
            label = app["name"].strip()
            if label and is_fallback_label(package, label):
                label = fallback_name(package)
            if label and (row["name"] == package or not is_fallback_label(package, label)):
                row["name"] = label
            row["week_minutes"] += int(app["minutes"])
            if day == today:
                row["minutes"] += int(app["minutes"])
    return table


def app_list(apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every app of one day, most used first, as `package`, `name` and whole `minutes`.

    For a dashboard card that draws every app, not only the top five of `top_apps`. An
    app without a package or without a whole minute is left out, a package sent twice
    counts once with both minutes, and the stand-in name for an app uninstalled since
    becomes the same name its app sensor gets.
    """
    rows: dict[str, dict[str, Any]] = {}
    for app in apps:
        package = app.get("package")
        if not isinstance(package, str) or not package:
            continue
        row = rows.setdefault(package, {"package": package, "name": "", "minutes": 0.0})
        row["minutes"] += _number(app.get("minutes"))
        label = app["name"].strip()
        if label and is_fallback_label(package, label):
            label = fallback_name(package)
        if label and not row["name"]:
            row["name"] = label
    listed = []
    for row in rows.values():
        minutes = int(row["minutes"])
        if minutes > 0:
            name = row["name"] or fallback_name(row["package"])
            listed.append({"package": row["package"], "name": name, "minutes": minutes})
    listed.sort(key=lambda row: (-row["minutes"], row["name"].lower()))
    return listed


def _apps(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The usable app rows of a screen time day.

    Minutes that are not finite are no minutes: the app never sends them, but Python's
    JSON reader takes NaN and Infinity, and int() of either raises. The app table reads
    every day of the window, so one such row on any day would otherwise cost the whole
    payload rather than the today and yesterday sensors alone.
    """
    apps = entry.get("apps")
    if not isinstance(apps, list):
        return []
    usable = []
    for app in apps:
        if not isinstance(app, dict) or not isinstance(app.get("name"), str):
            continue
        try:
            minutes = _number(app.get("minutes"))
        except ValueError:
            continue
        if not math.isfinite(minutes):
            continue
        usable.append(app)
    return usable


def screen_time_minutes(entry: dict[str, Any]) -> float | int:
    """A screen time day's minutes: the apps that are sent, when the app filtered them.

    `total_screen_time_minutes` always counts every app; `filtered_screen_time_minutes`
    is there only when an app filter took some out, and the sensors follow the filter.
    Raises ValueError when the figure that applies is not a number.
    """
    if "filtered_screen_time_minutes" in entry:
        return _number(entry.get("filtered_screen_time_minutes"))
    return _number(entry.get("total_screen_time_minutes"))


def _screen_time_attributes(entry: dict[str, Any], day: date) -> dict[str, Any]:
    """Attributes for a screen time day sensor."""
    apps = _apps(entry)
    top = sorted(apps, key=lambda app: app["minutes"], reverse=True)[:5]
    attributes = {
        "date": day.isoformat(),
        "app_count": len(apps),
        "top_apps": ", ".join(f"{app['name']} ({int(app['minutes'])} min)" for app in top),
        "apps": app_list(apps),
    }
    if "filtered_screen_time_minutes" in entry:
        # The real total of every app, so a dashboard can show both.
        total = entry.get("total_screen_time_minutes")
        if not isinstance(total, bool) and isinstance(total, (int, float)):
            attributes["all_apps_minutes"] = int(total)
    return attributes


def _top_app(entry: dict[str, Any]) -> tuple[str, str, int] | None:
    """The most used app of a day, as (name, package, minutes)."""
    apps = _apps(entry)
    if not apps:
        return None
    app = max(apps, key=lambda app: app["minutes"])
    package = app.get("package")
    return (
        app["name"],
        package if isinstance(package, str) else "",
        int(app["minutes"]),
    )


def _sync_update(data: dict[str, Any], key: str, tz: tzinfo) -> SensorUpdate:
    """The diagnostic timestamp sensor for a payload.

    Test pings update this too. The user knows exactly when they pressed Test, so a
    moving timestamp is feedback rather than a surprise. A backfill chunk does not:
    parse_payload drops this update for it, so the backfill attribute stays false.
    """
    try:
        measured_at = parse_instant(data.get("timestamp"))
    except ValueError:
        measured_at = datetime.now(tz)

    attributes: dict[str, Any] = {}
    if isinstance(version := data.get("app_version"), str) and version:
        attributes["app_version"] = version
    if isinstance(source := data.get("source"), str) and source:
        attributes["source"] = source

    if key == KEY_LAST_HEALTH_SYNC:
        attributes["backfill"] = data.get("backfill") is True
        attributes["record_count"] = sum(
            len(value)
            for name, value in data.items()
            if name in HEALTH_ARRAYS and isinstance(value, list)
        )
    elif isinstance(device := data.get("device"), str) and device:
        attributes["device"] = device

    return SensorUpdate(key=key, value=measured_at, measured_at=measured_at, attributes=attributes)


def parse_payload(data: dict[str, Any], *, tz: tzinfo) -> list[SensorUpdate]:
    """Turn one decoded payload into the sensor updates it justifies.

    Never raises on a JSON object: a record it cannot read is skipped, because one
    unreadable record must not cost the user the rest of a sync.
    """
    source = data.get("source")

    # A test ping carries no data, only proof that URL and secret are right.
    if data.get("test") is True:
        key = KEY_LAST_SCREEN_TIME_SYNC if source == SOURCE_SCREEN_TIME else KEY_LAST_HEALTH_SYNC
        return [_sync_update(data, key, tz)]

    # A heartbeat only asks for readings; there was no sync.
    if is_heartbeat(data):
        return []

    if source == SOURCE_SCREEN_TIME:
        updates = _parse_screen_time(data, tz)
    else:
        updates = _parse_health(data, tz)

    # A backfill is one POST per chunk, hundreds of them a second or two apart for a
    # year. Each would move the last-sync timestamp and put a line in the logbook and a
    # row in the recorder, for a sync that only fills the past. So a backfill moves no
    # last-sync sensor; the next regular sync moves it again.
    if data.get("backfill") is True:
        updates = [update for update in updates if update.key not in _SYNC_KEYS]
    return updates
