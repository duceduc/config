"""Calendar rollover for daily totals; never a HealthKit measurement or upload."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .naming import normalize_metric_key
from .timestamps import measurement_timestamp_value, parse_measurement_timestamp

# Explicit catalog/legacy compatibility, not a heuristic based on units or names.
_CUMULATIVE_IDENTIFIERS = (
    "HKQuantityTypeIdentifierActiveEnergyBurned",
    "HKQuantityTypeIdentifierAppleExerciseTime",
    "HKQuantityTypeIdentifierAppleMoveTime",
    "HKQuantityTypeIdentifierAppleStandTime",
    "HKQuantityTypeIdentifierBasalEnergyBurned",
    "HKQuantityTypeIdentifierDistanceCrossCountrySkiing",
    "HKQuantityTypeIdentifierDistanceCycling",
    "HKQuantityTypeIdentifierDistanceDownhillSnowSports",
    "HKQuantityTypeIdentifierDistancePaddleSports",
    "HKQuantityTypeIdentifierDistanceRowing",
    "HKQuantityTypeIdentifierDistanceSkatingSports",
    "HKQuantityTypeIdentifierDistanceSwimming",
    "HKQuantityTypeIdentifierDistanceWalkingRunning",
    "HKQuantityTypeIdentifierDistanceWheelchair",
    "HKQuantityTypeIdentifierFlightsClimbed",
    "HKQuantityTypeIdentifierNikeFuel",
    "HKQuantityTypeIdentifierPushCount",
    "HKQuantityTypeIdentifierStepCount",
    "HKQuantityTypeIdentifierSwimmingStrokeCount",
    "HKQuantityTypeIdentifierDietaryBiotin",
    "HKQuantityTypeIdentifierDietaryCaffeine",
    "HKQuantityTypeIdentifierDietaryCalcium",
    "HKQuantityTypeIdentifierDietaryCarbohydrates",
    "HKQuantityTypeIdentifierDietaryChloride",
    "HKQuantityTypeIdentifierDietaryCholesterol",
    "HKQuantityTypeIdentifierDietaryChromium",
    "HKQuantityTypeIdentifierDietaryCopper",
    "HKQuantityTypeIdentifierDietaryEnergyConsumed",
    "HKQuantityTypeIdentifierDietaryFatMonounsaturated",
    "HKQuantityTypeIdentifierDietaryFatPolyunsaturated",
    "HKQuantityTypeIdentifierDietaryFatSaturated",
    "HKQuantityTypeIdentifierDietaryFatTotal",
    "HKQuantityTypeIdentifierDietaryFiber",
    "HKQuantityTypeIdentifierDietaryFolate",
    "HKQuantityTypeIdentifierDietaryIodine",
    "HKQuantityTypeIdentifierDietaryIron",
    "HKQuantityTypeIdentifierDietaryMagnesium",
    "HKQuantityTypeIdentifierDietaryManganese",
    "HKQuantityTypeIdentifierDietaryMolybdenum",
    "HKQuantityTypeIdentifierDietaryNiacin",
    "HKQuantityTypeIdentifierDietaryPantothenicAcid",
    "HKQuantityTypeIdentifierDietaryPhosphorus",
    "HKQuantityTypeIdentifierDietaryPotassium",
    "HKQuantityTypeIdentifierDietaryProtein",
    "HKQuantityTypeIdentifierDietaryRiboflavin",
    "HKQuantityTypeIdentifierDietarySelenium",
    "HKQuantityTypeIdentifierDietarySodium",
    "HKQuantityTypeIdentifierDietarySugar",
    "HKQuantityTypeIdentifierDietaryThiamin",
    "HKQuantityTypeIdentifierDietaryVitaminA",
    "HKQuantityTypeIdentifierDietaryVitaminB12",
    "HKQuantityTypeIdentifierDietaryVitaminB6",
    "HKQuantityTypeIdentifierDietaryVitaminC",
    "HKQuantityTypeIdentifierDietaryVitaminD",
    "HKQuantityTypeIdentifierDietaryVitaminE",
    "HKQuantityTypeIdentifierDietaryVitaminK",
    "HKQuantityTypeIdentifierDietaryWater",
    "HKQuantityTypeIdentifierDietaryZinc",
    "HKQuantityTypeIdentifierInsulinDelivery",
    "HKQuantityTypeIdentifierNumberOfAlcoholicBeverages",
    "HKQuantityTypeIdentifierNumberOfTimesFallen",
    "HKQuantityTypeIdentifierTimeInDaylight",
    "HKQuantityTypeIdentifierInhalerUsage",
)
_CATEGORY_NAMES = (
    "AppleStandHour", "EnvironmentalAudioExposureEvent", "HeadphoneAudioExposureEvent",
    "HighHeartRateEvent", "HypertensionEvent", "IrregularHeartRhythmEvent", "LowCardioFitnessEvent",
    "LowHeartRateEvent", "MindfulSession", "AppleWalkingSteadinessEvent", "HandwashingEvent",
    "ToothbrushingEvent", "BleedingAfterPregnancy", "BleedingDuringPregnancy", "CervicalMucusQuality",
    "Contraceptive", "InfrequentMenstrualCycles", "IntermenstrualBleeding", "IrregularMenstrualCycles",
    "Lactation", "MenstrualFlow", "OvulationTestResult", "PersistentIntermenstrualBleeding", "Pregnancy",
    "PregnancyTestResult", "ProgesteroneTestResult", "ProlongedMenstrualPeriods", "SexualActivity",
    "SleepApneaEvent", "AbdominalCramps", "Acne", "AppetiteChanges", "BladderIncontinence", "Bloating",
    "BreastPain", "ChestTightnessOrPain", "Chills", "Constipation", "Coughing", "Diarrhea", "Dizziness",
    "DrySkin", "Fainting", "Fatigue", "Fever", "GeneralizedBodyAche", "HairLoss", "Headache", "Heartburn",
    "HotFlashes", "LossOfSmell", "LossOfTaste", "LowerBackPain", "MemoryLapse", "MoodChanges", "Nausea",
    "NightSweats", "PelvicPain", "RapidPoundingOrFlutteringHeartbeat", "RunnyNose", "ShortnessOfBreath",
    "SinusCongestion", "SkippedHeartbeat", "SleepChanges", "SoreThroat", "VaginalDryness", "Vomiting", "Wheezing",
)
DAILY_COUNTER_KEYS = frozenset(normalize_metric_key(key) for key in _CUMULATIVE_IDENTIFIERS) | frozenset(
    normalize_metric_key("HKCategoryTypeIdentifier" + key) for key in _CATEGORY_NAMES
) | frozenset({
    "steps", "flights", "walking_distance", "cycling_distance", "active_energy", "basal_energy",
    "exercise_time", "move_time", "stand_time", "time_in_daylight",
    "workout_count", "workout_duration", "workout_duration_minutes",
    "workout_distance", "workout_active_energy", "screen_time",
    "longest_activity_session", "pickups_without_app_use",
})
_DAILY_SEMANTICS = frozenset({
    "cumulative_total", "workout_count", "workout_duration_minutes", "workout_distance_m",
    "workout_active_energy_kcal", "category_event_count", "stood_hours_count",
})
_SERVER_ATTRIBUTES = frozenset({
    "daily_reporting_date", "daily_period_start", "daily_reset", "daily_reset_at",
    "daily_source_fresh", "last_source_pushed", "source_measurement_timestamp",
})
_SLEEP_SESSION_KEYS = frozenset({
    "sleep_duration", "sleep_in_bed", "sleep_awake", "sleep_core", "sleep_deep",
    "sleep_rem", "sleep_score", "sleep_change",
})


def reset_kind(metric_key: str, attributes: dict[str, Any]) -> str | None:
    key = normalize_metric_key(metric_key)
    if key == "first_pickup_time":
        return "unknown"
    semantics = attributes.get("healthkit_daily_semantics")
    if key in _SLEEP_SESSION_KEYS or (isinstance(semantics, str) and semantics.startswith("sleep_")):
        return None
    return "zero" if key in DAILY_COUNTER_KEYS or (isinstance(semantics, str) and semantics in _DAILY_SEMANTICS) else None


def reporting_zone(attributes: dict[str, Any], fallback: str) -> tzinfo:
    name = attributes.get("reporting_time_zone", fallback)
    if not isinstance(name, str):
        raise ValueError("invalid_reporting_time_zone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Foundation also permits fixed-offset identifiers such as GMT+0530.
        fixed = re.fullmatch(r"(?:GMT|UTC)([+-])(\d{2}):?(\d{2})(?::?(\d{2}))?", name)
        if fixed:
            sign, hours, minutes, seconds = fixed.groups()
            hours, minutes, seconds = int(hours), int(minutes), int(seconds or 0)
            if hours < 24 and minutes < 60 and seconds < 60:
                offset = timedelta(hours=hours, minutes=minutes, seconds=seconds)
                return timezone(offset if sign == "+" else -offset)
        if "reporting_time_zone" in attributes:
            raise ValueError("invalid_reporting_time_zone") from None
        return timezone.utc


def _date_value(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def _start(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def source_day(attributes: dict[str, Any], zone: tzinfo, *, stored: bool = False) -> date | None:
    if stored:
        recorded = _date_value(attributes.get("daily_reporting_date"))
        if recorded is not None:
            return recorded
    raw_start = attributes.get("healthkit_daily_start", attributes.get("day_start"))
    raw_end = attributes.get("healthkit_daily_end", attributes.get("day_end"))
    if raw_start is not None:
        start = parse_measurement_timestamp(raw_start) if isinstance(raw_start, str) else None
        end = parse_measurement_timestamp(raw_end) if isinstance(raw_end, str) else None
        if start is None or (raw_end is not None and end is None):
            raise ValueError("invalid_daily_period")
        day = start.astimezone(zone).date()
        if end is not None and (end <= start or end > _start(day + timedelta(days=1), zone)):
            raise ValueError("invalid_daily_period")
        return day
    if raw_end is not None:
        raise ValueError("invalid_daily_period")
    measured = parse_measurement_timestamp(measurement_timestamp_value(attributes))
    return measured.astimezone(zone).date() if measured is not None else None


def prepare_daily_attributes(metric_key: str, attributes: dict[str, Any], now: datetime,
                             fallback_zone: str) -> tuple[dict[str, Any], bool]:
    """Return server-owned freshness metadata and whether this may update live state.

    Old-day/historical values still go to statistics; they never resurrect today's
    counter. Legacy payloads with no period/time use arrival day, but are not
    falsely labelled a fresh source measurement.
    """
    if reset_kind(metric_key, attributes) is None:
        return attributes, True
    zone = reporting_zone(attributes, fallback_zone)
    day = source_day(attributes, zone)
    today = now.astimezone(zone).date()
    if day is not None and day > today:
        raise ValueError("future_daily_period")
    current = day in (None, today) and attributes.get("historical_daily_snapshot") is not True
    clean = {key: value for key, value in attributes.items() if key not in _SERVER_ATTRIBUTES}
    if not current:
        return clean, False
    measured = parse_measurement_timestamp(measurement_timestamp_value(clean))
    clean.update({
        "reporting_time_zone": str(zone),
        "daily_reporting_date": today.isoformat(),
        "daily_period_start": _start(today, zone).isoformat(),
        "daily_reset": False,
        "daily_source_fresh": measured is not None,
    })
    if measured is not None:
        clean["source_measurement_timestamp"] = measured.isoformat()
    return clean, True


def rollover_daily_sensors(sensors: dict[str, Any], now: datetime, fallback_zone: str) -> list[str]:
    """Reset in-place, retaining IDs and original source/receipt timestamps."""
    changed = []
    for unique_id, sensor in list(sensors.items()):
        kind = reset_kind(sensor.metric_key, sensor.attributes)
        if kind is None:
            continue
        try:
            zone = reporting_zone(sensor.attributes, fallback_zone)
            day = source_day(sensor.attributes, zone, stored=True)
        except ValueError:
            # A malformed legacy period is not allowed to keep an old value forever.
            zone = reporting_zone({}, fallback_zone)
            day = sensor.updated_at.astimezone(zone).date()
        today = now.astimezone(zone).date()
        if day is None:
            day = sensor.updated_at.astimezone(zone).date()
        if day == today:
            continue
        attributes = dict(sensor.attributes)
        original_measurement = attributes.get("source_measurement_timestamp") or measurement_timestamp_value(attributes)
        attributes.update({
            "reporting_time_zone": str(zone),
            "daily_reporting_date": today.isoformat(),
            "daily_period_start": _start(today, zone).isoformat(),
            "daily_reset": True,
            "daily_reset_at": now.isoformat(),
            "daily_source_fresh": False,
            "last_source_pushed": attributes.get("last_source_pushed", sensor.updated_at.isoformat()),
        })
        if original_measurement is not None:
            attributes["source_measurement_timestamp"] = original_measurement
        # A local calendar reset is not a newly measured HealthKit zero.
        attributes.pop("measurement_timestamp", None)
        sensors[unique_id] = replace(sensor, state=0 if kind == "zero" else "unknown",
                                     attributes=attributes, updated_at=now)
        changed.append(unique_id)
    return changed

