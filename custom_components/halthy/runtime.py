"""Shared runtime models for the Halthy integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .const import (
    DEFAULT_ACTIVITY_LOG_MODE,
    DEFAULT_STATISTICS_ENABLED,
    DEFAULT_TEMPERATURE_UNIT,
    DEFAULT_WORKOUT_ARCHIVE_RETENTION,
    DOMAIN,
)


@dataclass(slots=True)
class HalthySensorState:
    """In-memory state for one metric-backed sensor."""

    unique_id: str
    metric_key: str
    name: str
    state: str | float | int | bool
    unit: str | None
    icon: str | None
    attributes: dict[str, Any]
    username: str
    device_id: str
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class HalthyImageState:
    """In-memory state for one metric-backed image entity."""

    unique_id: str
    metric_key: str
    name: str
    content_type: str
    image_bytes: bytes
    attributes: dict[str, Any]
    username: str
    device_id: str
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class HalthyWorkoutRecord:
    """Persistent metadata for one HealthKit workout."""

    record_id: str
    uid: str
    summary: str
    start: datetime
    end: datetime
    metadata: dict[str, Any]


@dataclass(slots=True)
class IntegrationRuntime:
    """Runtime data shared between endpoints and entity platforms."""

    configured_username: str
    app_username: str
    display_name: str
    previous_configured_username: str | None = None
    owner_user_id: str | None = None
    temperature_unit_preference: str = DEFAULT_TEMPERATURE_UNIT
    activity_log_mode: str = DEFAULT_ACTIVITY_LOG_MODE
    statistics_enabled: bool = DEFAULT_STATISTICS_ENABLED
    workout_archive_retention: int = DEFAULT_WORKOUT_ARCHIVE_RETENTION
    sensors: dict[str, HalthySensorState] = field(default_factory=dict)
    images: dict[str, HalthyImageState] = field(default_factory=dict)
    workouts: dict[str, HalthyWorkoutRecord] = field(default_factory=dict)
    statistics_cursors: dict[str, str] = field(default_factory=dict)
    force_upload_interval_seconds: int = 0
    pending_force_upload_command: dict[str, Any] | None = None
    last_force_upload_ack_at: str | None = None
    last_force_upload_ack_status: str | None = None
    daily_upload_count: int = 0
    daily_upload_count_day: str = ""
    lock: asyncio.Lock | None = None


def runtime_lock(runtime: IntegrationRuntime) -> asyncio.Lock:
    """Create the runtime lock lazily inside the active event loop."""

    if runtime.lock is None:
        runtime.lock = asyncio.Lock()
    return runtime.lock


def runtime_device_identifiers(runtime: IntegrationRuntime) -> set[tuple[str, str]]:
    """Return current and transitional identifiers for one person's device."""

    usernames = {runtime.configured_username}
    if runtime.previous_configured_username:
        usernames.add(runtime.previous_configured_username)
    return {(DOMAIN, f"user:{username}") for username in usernames if username}


@dataclass(slots=True)
class StatisticsCursorUpdate:
    """Cursor update candidate for one statistics series."""

    latest_imported_at: datetime
    legacy_statistic_ids: tuple[str, ...] = ()
