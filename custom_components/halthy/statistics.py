"""Home Assistant recorder-statistics preparation for Halthy metrics."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import inspect
import logging
import math
import re
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from homeassistant.components.recorder.models.statistics import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
except ImportError:
    try:
        from homeassistant.components.recorder.models import (
            StatisticData,
            StatisticMeanType,
            StatisticMetaData,
        )
    except ImportError:
        StatisticData = None  # type: ignore[assignment]
        StatisticMeanType = None  # type: ignore[assignment]
        StatisticMetaData = None  # type: ignore[assignment]

from .naming import sanitize_identifier
from .runtime import IntegrationRuntime, StatisticsCursorUpdate
from .timestamps import measurement_timestamp_value, parse_measurement_timestamp

_LOGGER = logging.getLogger(__name__)
STATISTICS_SOURCE = "halthy"

if StatisticMetaData is not None:
    try:
        _STATISTIC_METADATA_FIELDS = set(inspect.signature(StatisticMetaData).parameters.keys())
    except (TypeError, ValueError):
        _STATISTIC_METADATA_FIELDS = set()
else:
    _STATISTIC_METADATA_FIELDS = set()


def metadata_statistic_id(metadata: Any) -> str | None:
    """Read a statistic ID from either modern model objects or legacy mappings."""

    if isinstance(metadata, dict):
        raw = metadata.get("statistic_id")
        return str(raw).strip() if raw is not None else None
    raw = getattr(metadata, "statistic_id", None)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def statistics_id_for_metric(username: str, metric_key: str) -> str:
    return f"{STATISTICS_SOURCE}:{sanitize_identifier(username)}_{sanitize_identifier(metric_key)}"


def statistics_friendly_name(username: str, metric_name: str) -> str:
    resolved_username = username.strip() or "unknown"
    resolved_metric_name = metric_name.strip() or "Metric"
    return f"{resolved_metric_name} ({resolved_username})"


def statistics_source_for_id(statistic_id: str) -> str:
    if ":" in statistic_id:
        return statistic_id.split(":", 1)[0]
    return STATISTICS_SOURCE


def numeric_state_value(raw_state: Any) -> float | None:
    if isinstance(raw_state, bool):
        return None
    if isinstance(raw_state, (int, float)):
        value = float(raw_state)
        return value if math.isfinite(value) else None
    if isinstance(raw_state, str):
        normalized = raw_state.strip()
        if not normalized:
            return None

        numeric_candidate = normalized.replace(",", ".")
        direct_match = re.fullmatch(r"[+-]?(\d+(\.\d+)?|\.\d+)", numeric_candidate)
        if direct_match:
            try:
                value = float(numeric_candidate)
                return value if math.isfinite(value) else None
            except ValueError:
                return None

        with_unit = re.fullmatch(
            r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*[\w°%/.-][\w°%/\s.-]*",
            numeric_candidate,
        )
        if with_unit:
            try:
                value = float(with_unit.group(1))
                return value if math.isfinite(value) else None
            except ValueError:
                return None
    return None


def statistics_metadata(statistic_id: str, name: str, unit: str | None) -> Any:
    if StatisticMetaData is None:
        return None

    kwargs: dict[str, Any] = {
        "statistic_id": statistic_id,
        "source": statistics_source_for_id(statistic_id),
        "name": name,
        "has_sum": False,
        "unit_class": None,
        "unit_of_measurement": unit,
    }
    if StatisticMeanType is not None:
        kwargs["mean_type"] = StatisticMeanType.ARITHMETIC
    else:
        kwargs["has_mean"] = True
    try:
        return StatisticMetaData(**kwargs)
    except TypeError:
        fallback_kwargs: dict[str, Any] = {
            "has_mean": True,
            "has_sum": False,
            "name": name,
            "source": statistics_source_for_id(statistic_id),
            "statistic_id": statistic_id,
        }
        if "unit_class" in _STATISTIC_METADATA_FIELDS:
            fallback_kwargs["unit_class"] = None
        if unit and ("unit_of_measurement" in _STATISTIC_METADATA_FIELDS or not _STATISTIC_METADATA_FIELDS):
            fallback_kwargs["unit_of_measurement"] = unit
        if StatisticMeanType is not None and "mean_type" in _STATISTIC_METADATA_FIELDS:
            fallback_kwargs["mean_type"] = StatisticMeanType.ARITHMETIC
        try:
            return StatisticMetaData(**fallback_kwargs)
        except TypeError:
            return None


def statistics_data(
    start: datetime,
    state: float,
    mean: float | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
) -> Any:
    if StatisticData is None:
        return None

    resolved_mean = state if mean is None else mean
    resolved_min = state if min_value is None else min_value
    resolved_max = state if max_value is None else max_value
    kwargs: dict[str, Any] = {
        "start": start,
        "state": state,
        "mean": resolved_mean,
        "min": resolved_min,
        "max": resolved_max,
    }
    try:
        return StatisticData(**kwargs)
    except TypeError:
        try:
            return StatisticData(**kwargs)
        except TypeError:
            return None


def statistics_hour_bucket(measured_at: datetime) -> datetime:
    return measured_at.replace(minute=0, second=0, microsecond=0)


def _stored_hourly_samples(
    database_path: Path, candidates: list[dict[str, Any]]
) -> dict[str, dict[datetime, dict[str, float]]]:
    """Keep accepted samples so separate uploads can rebuild a complete hour."""

    database_path.parent.mkdir(parents=True, exist_ok=True)
    affected: dict[str, set[tuple[str, datetime]]] = {}
    # SQLite's transaction context commits/rolls back but does not close.
    with closing(sqlite3.connect(database_path, timeout=30)) as connection, connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            "statistic_id TEXT NOT NULL, unit TEXT NOT NULL, "
            "timestamp_us INTEGER NOT NULL, value REAL NOT NULL, "
            "PRIMARY KEY (statistic_id, unit, timestamp_us, value))"
        )
        for candidate in candidates:
            statistic_id = candidate["statistic_id"]
            unit = candidate["unit"] or ""
            measured_at = candidate["start"]
            timestamp_us = round(measured_at.timestamp() * 1_000_000)
            if candidate.get("replace_at_timestamp"):
                connection.execute(
                    "DELETE FROM samples WHERE statistic_id=? AND unit=? AND timestamp_us=?",
                    (statistic_id, unit, timestamp_us),
                )
            connection.execute(
                "INSERT OR IGNORE INTO samples VALUES (?, ?, ?, ?)",
                (statistic_id, unit, timestamp_us, float(candidate["value"])),
            )
            affected.setdefault(statistic_id, set()).add(
                (unit, statistics_hour_bucket(measured_at))
            )

        result: dict[str, dict[datetime, dict[str, float]]] = {}
        for statistic_id, hours in affected.items():
            for unit, hour_start in hours:
                start_us = round(hour_start.timestamp() * 1_000_000)
                end_us = start_us + 3_600_000_000
                aggregate = connection.execute(
                    "SELECT AVG(value), MIN(value), MAX(value) FROM samples "
                    "WHERE statistic_id=? AND unit=? AND timestamp_us>=? AND timestamp_us<?",
                    (statistic_id, unit, start_us, end_us),
                ).fetchone()
                latest = connection.execute(
                    "SELECT value FROM samples WHERE statistic_id=? AND unit=? "
                    "AND timestamp_us>=? AND timestamp_us<? "
                    "ORDER BY timestamp_us DESC, rowid DESC LIMIT 1",
                    (statistic_id, unit, start_us, end_us),
                ).fetchone()
                if aggregate is None or latest is None:
                    continue
                result.setdefault(statistic_id, {})[hour_start] = {
                    "state": latest[0],
                    "mean": aggregate[0],
                    "min": aggregate[1],
                    "max": aggregate[2],
                }
    return result


def statistics_candidates_from_sensor(
    runtime: IntegrationRuntime,
    metric_key: str,
    metric_name: str,
    state: str | float | int | bool,
    unit: str | None,
    attributes: dict[str, Any],
) -> list[dict[str, Any]]:
    if not runtime.statistics_enabled:
        return []

    measurement_raw = measurement_timestamp_value(attributes)
    measurement_at = parse_measurement_timestamp(measurement_raw)
    numeric_state = numeric_state_value(state)
    if measurement_at is None or numeric_state is None:
        _LOGGER.debug(
            "Skipping statistics candidate for metric '%s' (measurement=%r parsed=%r state=%r numeric=%r)",
            metric_key,
            measurement_raw,
            measurement_at,
            state,
            numeric_state,
        )
        return []

    statistic_id = statistics_id_for_metric(runtime.configured_username, metric_key)
    return [{
        "statistic_id": statistic_id,
        "name": statistics_friendly_name(runtime.app_username, metric_name),
        "unit": unit,
        "start": measurement_at,
        "value": numeric_state,
        "replace_at_timestamp": attributes.get("historical_daily_snapshot") is True,
    }]


def prepare_statistics_imports_for_runtime(
    runtime: IntegrationRuntime,
    candidates: list[dict[str, Any]],
    sample_store_path: Path | None = None,
) -> tuple[list[tuple[Any, list[Any]]], dict[str, StatisticsCursorUpdate]]:
    stored_hours = (
        _stored_hourly_samples(sample_store_path, candidates)
        if sample_store_path is not None and candidates else {}
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        statistic_id = candidate["statistic_id"]
        grouped.setdefault(statistic_id, []).append(candidate)

    imports: list[tuple[Any, list[Any]]] = []
    cursor_updates: dict[str, StatisticsCursorUpdate] = {}
    for statistic_id, rows in grouped.items():
        legacy_statistic_ids: list[str] = []
        if ":" in statistic_id:
            _, legacy_suffix = statistic_id.split(":", 1)
            if legacy_suffix:
                legacy_statistic_ids.append(legacy_suffix)
        else:
            legacy_statistic_ids.append(f"{STATISTICS_SOURCE}:{statistic_id}")

        cursor_raw = runtime.statistics_cursors.get(statistic_id)
        if cursor_raw is None:
            for legacy_statistic_id in legacy_statistic_ids:
                cursor_raw = runtime.statistics_cursors.get(legacy_statistic_id)
                if cursor_raw is not None:
                    break
        cursor_at = parse_measurement_timestamp(cursor_raw)
        rows.sort(key=lambda row: row["start"])

        latest_imported_at = cursor_at
        metadata = statistics_metadata(
            statistic_id=statistic_id,
            name=rows[-1]["name"],
            unit=rows[-1]["unit"],
        )
        if metadata is None:
            continue

        filtered_rows: list[dict[str, Any]] = []
        for row in rows:
            start = row["start"]
            # A high-water mark cannot prove older samples were imported:
            # backfills and retries can arrive after newer live readings.
            # Recorder upserts rows by statistic ID and hour.
            filtered_rows.append(row)
            if latest_imported_at is None or start > latest_imported_at:
                latest_imported_at = start

        if not filtered_rows:
            continue

        hourly_rows: dict[datetime, list[dict[str, Any]]] = {}
        for row in filtered_rows:
            hour_start = statistics_hour_bucket(row["start"])
            hourly_rows.setdefault(hour_start, []).append(row)

        import_rows: list[Any] = []
        for hour_start in sorted(hourly_rows):
            stored = stored_hours.get(statistic_id, {}).get(hour_start)
            if stored is None:
                bucket_rows = sorted(hourly_rows[hour_start], key=lambda item: item["start"])
                values = [float(item["value"]) for item in bucket_rows]
                if not values:
                    continue
                stored = {
                    "state": values[-1],
                    "mean": sum(values) / len(values),
                    "min": min(values),
                    "max": max(values),
                }
            statistic = statistics_data(
                start=hour_start,
                state=stored["state"],
                mean=stored["mean"],
                min_value=stored["min"],
                max_value=stored["max"],
            )
            if statistic is not None:
                import_rows.append(statistic)

        if not import_rows:
            continue

        if latest_imported_at is not None:
            cursor_updates[statistic_id] = StatisticsCursorUpdate(
                latest_imported_at=latest_imported_at,
                legacy_statistic_ids=tuple(legacy_statistic_ids),
            )
        imports.append((metadata, import_rows))

    return imports, cursor_updates


def commit_statistics_cursor_updates(
    runtime: IntegrationRuntime,
    cursor_updates: dict[str, StatisticsCursorUpdate],
    successful_statistic_ids: set[str],
) -> None:
    if not cursor_updates or not successful_statistic_ids:
        return

    for statistic_id, update in cursor_updates.items():
        if statistic_id not in successful_statistic_ids:
            continue
        existing_cursor = parse_measurement_timestamp(runtime.statistics_cursors.get(statistic_id))
        if existing_cursor is not None and existing_cursor >= update.latest_imported_at:
            continue
        runtime.statistics_cursors[statistic_id] = update.latest_imported_at.isoformat()
        for legacy_statistic_id in update.legacy_statistic_ids:
            runtime.statistics_cursors.pop(legacy_statistic_id, None)


async def import_statistics_batches(
    hass: Any,
    batches: list[tuple[Any, list[Any]]],
    importer: Callable[[Any, Any, list[Any]], Awaitable[Any] | Any] | None,
) -> tuple[int, set[str]]:
    if not batches or importer is None:
        return 0, set()

    imported_samples = 0
    successful_statistic_ids: set[str] = set()
    failed_batches: list[tuple[str, str]] = []
    for metadata, rows in batches:
        statistic_id = metadata_statistic_id(metadata) or "unknown"
        try:
            result = importer(hass, metadata, rows)
            if inspect.isawaitable(result):
                await result
            imported_samples += len(rows)
            successful_statistic_ids.add(statistic_id)
        except Exception as err:  # noqa: BLE001
            failed_batches.append((statistic_id, str(err)))

    if failed_batches:
        unique_failed = sorted({item[0] for item in failed_batches})
        sample_ids = ", ".join(unique_failed[:6])
        if len(unique_failed) > 6:
            sample_ids = f"{sample_ids}, ..."
        _LOGGER.warning(
            "Could not import Halthy statistics for %d batch(es) across %d metric(s): %s. Last error: %s",
            len(failed_batches),
            len(unique_failed),
            sample_ids,
            failed_batches[-1][1],
        )
    return imported_samples, successful_statistic_ids
