"""Timestamp normalization shared by Halthy ingestion services."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

MEASUREMENT_TIMESTAMP_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "measurement_timestamp",
    "measured_at",
    "recorded_at",
    "observed_at",
    "sample_timestamp",
    "timestamp",
    "last_pushed",
    "updated_at",
)


def measurement_timestamp_value(attributes: dict[str, Any]) -> str | None:
    """Return the first usable timestamp attribute without changing its value."""

    for key in MEASUREMENT_TIMESTAMP_ATTRIBUTE_KEYS:
        raw_value = attributes.get(key)
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if value:
                return value
            continue
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            return str(raw_value)
    return None


def parse_measurement_timestamp(raw_value: str | None) -> datetime | None:
    """Parse Unix or ISO-8601 measurement timestamps as UTC."""

    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value:
        return None

    numeric = value.replace(",", ".")
    if re.fullmatch(r"[+-]?(\d+(\.\d+)?|\.\d+)", numeric):
        try:
            timestamp = float(numeric)
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, ValueError):
            return None

    normalized = value
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
