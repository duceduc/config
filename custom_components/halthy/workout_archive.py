"""Workout image archive persistence and retrieval helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, unquote
from uuid import uuid4

from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    MAX_WORKOUT_ARCHIVE_RETENTION,
    MIN_WORKOUT_ARCHIVE_RETENTION,
    WORKOUT_ARCHIVE_IMAGE_ENDPOINT_PATH,
)
from .naming import normalize_metric_key as _normalize_metric_key, sanitize_identifier
from .timestamps import parse_measurement_timestamp as _parse_measurement_timestamp

_LOGGER = logging.getLogger(__name__)


def _first_nonempty_attribute_value(
    attributes: dict[str, Any],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        raw_value = attributes.get(key)
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if value:
            return value
    return None

WORKOUT_IMAGE_METRIC_KEY = "workout"
WORKOUT_ARCHIVE_TIMESTAMP_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "measurement_timestamp",
    "measured_at",
    "recorded_at",
    "observed_at",
    "sample_timestamp",
    "timestamp",
    "last_pushed",
    "updated_at",
    "workout_end",
    "workout_start",
)
WORKOUT_ARCHIVE_ID_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "workout_uuid",
    "workout_id",
    "uuid",
)
WORKOUT_ARCHIVE_WORKOUT_TYPE_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "workout_type",
    "workout_activity_type",
    "type",
)
WORKOUT_IMAGE_CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/heic": "heic",
    "image/heif": "heif",
}
WORKOUT_ARCHIVE_FOLDER_DOMAIN_CANDIDATES: tuple[str, ...] = (
    DOMAIN,
    "halthy_bridge",
    "health2ha",
    "health2ha_bridge",
)
WORKOUT_ARCHIVE_IMAGE_SUFFIXES: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
)
WORKOUT_ARCHIVE_METADATA_KEYS: tuple[str, ...] = (
    "title",
    "name",
    "workout_uuid",
    "workout_id",
    "uuid",
    "workout_type",
    "workout_activity_type",
    "activity_type",
    "workout_kind",
    "type",
    "measurement_timestamp",
    "measured_at",
    "recorded_at",
    "observed_at",
    "sample_timestamp",
    "timestamp",
    "last_pushed",
    "updated_at",
    "workout_start",
    "workout_end",
    "end",
    "end_time",
    "start",
    "start_time",
    "workout_distance_m",
    "distance_m",
    "distance",
    "workout_duration_s",
    "duration_s",
    "duration_seconds",
    "duration",
    "workout_active_energy_kcal",
    "active_energy_kcal",
    "energy_kcal",
    "workout_avg_heart_rate_bpm",
    "avg_heart_rate_bpm",
    "lowest_heart_rate_bpm",
    "min_heart_rate_bpm",
    "highest_heart_rate_bpm",
    "max_heart_rate_bpm",
    "avg_speed_mps",
    "workout_avg_speed_mps",
    "average_speed_mps",
    "workout_average_speed_mps",
    "highest_speed_mps",
    "max_speed_mps",
    "lowest_speed_mps",
    "min_speed_mps",
    "cadence_spm",
    "avg_cadence_spm",
    "power_w",
    "avg_power_w",
    "respiratory_rate_brpm",
    "avg_respiratory_rate_brpm",
    "workout_elevation_gain_m",
    "elevation_gain_m",
    "highest_altitude_m",
    "max_altitude_m",
    "lowest_altitude_m",
    "min_altitude_m",
    "total_flights_climbed",
    "workout_total_flights_climbed",
    "flights_climbed",
    "workout_activity_type_raw",
    "point_count",
    "rendered_point_count",
    "detailed_map",
    "map_style",
    "dark_map",
    "weather_condition",
    "weather_condition_raw",
    "weather_temperature_c",
    "weather_humidity_percent",
    "workout_zone_groups",
    "heart_rate_zones",
    "heart_rate_zone_total_duration_s",
    "cycling_power_zones",
    "cycling_power_zone_total_duration_s",
)
WORKOUT_ARCHIVE_DEFAULT_LIST_LIMIT = 240
WORKOUT_ARCHIVE_MAX_LIST_LIMIT = 1000

def _workout_archive_timestamp(attributes: dict[str, Any]) -> datetime:
    for key in WORKOUT_ARCHIVE_TIMESTAMP_ATTRIBUTE_KEYS:
        raw_value = attributes.get(key)
        parsed = None
        if isinstance(raw_value, str):
            parsed = _parse_measurement_timestamp(raw_value.strip())
        elif isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            parsed = _parse_measurement_timestamp(str(raw_value))
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def _image_extension_from_content_type(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    return WORKOUT_IMAGE_CONTENT_TYPE_EXTENSIONS.get(normalized, "jpg")


def _validated_image_content_type(content_type: str, image_bytes: bytes) -> str | None:
    """Return a supported canonical MIME type when its signature matches."""

    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return "image/jpeg" if image_bytes.startswith(b"\xff\xd8\xff") else None
    if normalized == "image/png":
        return "image/png" if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") else None
    return None


def _workout_archive_fingerprint(
    metric_key: str,
    attributes: dict[str, Any],
    workout_timestamp: datetime,
) -> str:
    raw_workout_id = _first_nonempty_attribute_value(attributes, WORKOUT_ARCHIVE_ID_ATTRIBUTE_KEYS)
    workout_start = _first_nonempty_attribute_value(attributes, ("workout_start",))
    workout_end = _first_nonempty_attribute_value(attributes, ("workout_end",))
    workout_type = _first_nonempty_attribute_value(attributes, WORKOUT_ARCHIVE_WORKOUT_TYPE_ATTRIBUTE_KEYS)
    archive_timestamp = _first_nonempty_attribute_value(
        attributes,
        WORKOUT_ARCHIVE_TIMESTAMP_ATTRIBUTE_KEYS,
    )
    if raw_workout_id is not None:
        sanitized_workout_id = sanitize_identifier(raw_workout_id)[:64]
        if sanitized_workout_id:
            return f"uuid_{sanitized_workout_id}"

    signature_source = "|".join(
        part
        for part in (
            _normalize_metric_key(metric_key),
            workout_start,
            workout_end,
            workout_type,
            archive_timestamp,
        )
        if part
    )
    if not signature_source:
        signature_source = f"{_normalize_metric_key(metric_key)}|{uuid4()}"
    digest = hashlib.sha1(signature_source.encode(), usedforsecurity=False).hexdigest()[:16]
    return f"sig_{digest}"


def _workout_archive_file_name(
    metric_key: str,
    attributes: dict[str, Any],
    content_type: str,
) -> tuple[str, str, datetime, str]:
    workout_timestamp = _workout_archive_timestamp(attributes).astimezone(timezone.utc)
    timestamp_fragment = workout_timestamp.strftime("%Y%m%dT%H%M%SZ")
    workout_fingerprint = _workout_archive_fingerprint(metric_key, attributes, workout_timestamp)
    extension = _image_extension_from_content_type(content_type)
    file_name = f"{timestamp_fragment}_{workout_fingerprint}.{extension}"
    return file_name, workout_fingerprint, workout_timestamp, extension


def _workout_archive_metadata_path(image_path: Path) -> Path:
    return image_path.with_suffix(".json")


def _workout_archive_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return value
    if isinstance(value, list):
        cleaned_list = [
            cleaned
            for item in value
            if (cleaned := _workout_archive_metadata_value(item)) is not None
        ]
        return cleaned_list
    if isinstance(value, dict):
        cleaned_dict: dict[str, Any] = {}
        for key, item in value.items():
            cleaned = _workout_archive_metadata_value(item)
            if cleaned is not None:
                cleaned_dict[str(key)] = cleaned
        return cleaned_dict
    return None


def _workout_archive_metadata_from_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in WORKOUT_ARCHIVE_METADATA_KEYS:
        if key not in attributes:
            continue
        value = _workout_archive_metadata_value(attributes[key])
        if value is None or value == "" or value == [] or value == {}:
            continue
        metadata[key] = value
    return metadata


def _store_workout_archive_metadata(
    archive_dir: Path,
    file_name: str,
    attributes: dict[str, Any],
) -> None:
    metadata = _workout_archive_metadata_from_attributes(attributes)
    metadata_path = _workout_archive_metadata_path(archive_dir / file_name)
    if not metadata:
        try:
            metadata_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.debug("Failed to remove empty workout archive metadata: %s", metadata_path)
        return

    temp_path = metadata_path.with_name(f".{metadata_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, sort_keys=True)
    temp_path.replace(metadata_path)


def _read_workout_archive_metadata(image_path: Path) -> dict[str, Any]:
    metadata_path = _workout_archive_metadata_path(image_path)
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            metadata[str(key)] = value
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metadata[str(key)] = value
            continue
        if isinstance(value, (list, dict)):
            cleaned = _workout_archive_metadata_value(value)
            if cleaned is not None:
                metadata[str(key)] = cleaned
    return metadata


def _workout_archive_record_identity(
    file_path: Path,
    metadata: dict[str, Any],
) -> str:
    raw_workout_id = _first_nonempty_attribute_value(
        metadata,
        WORKOUT_ARCHIVE_ID_ATTRIBUTE_KEYS,
    )
    if raw_workout_id is not None:
        normalized_workout_id = sanitize_identifier(raw_workout_id)
        if normalized_workout_id:
            return f"uuid:{normalized_workout_id}"

    file_stem_parts = file_path.stem.split("_", 1)
    if len(file_stem_parts) == 2:
        fingerprint = file_stem_parts[1].strip().lower()
        if fingerprint.startswith(("uuid_", "sig_")):
            return f"fingerprint:{fingerprint}"
    return ""


def _store_workout_archive_file(
    archive_dir: Path,
    file_name: str,
    image_bytes: bytes,
    workout_fingerprint: str,
) -> int:
    archive_dir.mkdir(parents=True, exist_ok=True)
    target_path = archive_dir / file_name
    replaced_count = 0
    for existing in archive_dir.iterdir():
        if (
            not existing.is_file()
            or existing.name == file_name
            or existing.suffix.lower() not in WORKOUT_ARCHIVE_IMAGE_SUFFIXES
        ):
            continue
        existing_fingerprint = existing.stem.split("_", 1)[1] if "_" in existing.stem else ""
        same_fingerprint = existing_fingerprint == workout_fingerprint
        legacy_uuid_variant = (
            workout_fingerprint.startswith("uuid_")
            and existing_fingerprint.startswith(f"{workout_fingerprint}_")
        )
        if not same_fingerprint and not legacy_uuid_variant:
            continue
        try:
            existing.unlink()
            try:
                _workout_archive_metadata_path(existing).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                _LOGGER.debug("Failed to remove older workout archive metadata: %s", existing)
            replaced_count += 1
        except OSError:
            _LOGGER.debug("Failed to remove older workout archive file: %s", existing)

    temp_path = target_path.with_name(f".{target_path.name}.tmp")
    with temp_path.open("wb") as handle:
        handle.write(image_bytes)
    temp_path.replace(target_path)
    return replaced_count


def _prune_workout_archive_files(archive_dir: Path, retention_limit: int) -> int:
    """Remove the oldest archived workout images after a successful write."""

    retention_limit = max(
        MIN_WORKOUT_ARCHIVE_RETENTION,
        min(MAX_WORKOUT_ARCHIVE_RETENTION, int(retention_limit)),
    )
    image_paths = sorted(
        (
            path
            for path in archive_dir.iterdir()
            if path.is_file() and path.suffix.lower() in WORKOUT_ARCHIVE_IMAGE_SUFFIXES
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed_count = 0
    for image_path in image_paths[retention_limit:]:
        try:
            image_path.unlink()
            removed_count += 1
        except OSError:
            _LOGGER.debug("Failed to prune workout archive file: %s", image_path)
            continue
        try:
            _workout_archive_metadata_path(image_path).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.debug("Failed to prune workout archive metadata: %s", image_path)
    return removed_count


def migrate_workout_archive_username(
    media_root: Path,
    old_username: str,
    new_username: str,
) -> int:
    """Move archived workout files when a configured username changes."""

    old_username = sanitize_identifier(old_username)
    new_username = sanitize_identifier(new_username)
    if not old_username or not new_username or old_username == new_username:
        return 0

    migrated_count = 0
    for archive_domain in WORKOUT_ARCHIVE_FOLDER_DOMAIN_CANDIDATES:
        source_dir = media_root / archive_domain / "workouts" / old_username
        target_dir = media_root / archive_domain / "workouts" / new_username
        if not source_dir.exists() or not source_dir.is_dir() or source_dir.is_symlink():
            continue

        image_paths = [
            path
            for path in source_dir.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and _is_supported_workout_archive_file(path)
        ]
        for source_image in image_paths:
            try:
                relative_path = source_image.relative_to(source_dir)
            except ValueError:
                continue
            target_image = target_dir / relative_path
            target_image.parent.mkdir(parents=True, exist_ok=True)

            source_metadata = _workout_archive_metadata_path(source_image)
            target_metadata = _workout_archive_metadata_path(target_image)
            try:
                keep_source = (
                    not target_image.exists()
                    or source_image.stat().st_mtime_ns >= target_image.stat().st_mtime_ns
                )
                if keep_source:
                    source_image.replace(target_image)
                    if source_metadata.exists() and not source_metadata.is_symlink():
                        source_metadata.replace(target_metadata)
                    migrated_count += 1
                else:
                    source_image.unlink()
                    if source_metadata.exists() and not source_metadata.is_symlink():
                        source_metadata.unlink()
            except OSError as err:
                _LOGGER.warning(
                    "Failed to migrate workout archive file from '%s' to '%s': %s",
                    old_username,
                    new_username,
                    err,
                )

        directories = sorted(
            (path for path in source_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in (*directories, source_dir):
            try:
                directory.rmdir()
            except OSError:
                pass

    return migrated_count


async def _async_archive_workout_image(
    hass: HomeAssistant,
    username: str,
    metric_key: str,
    content_type: str,
    image_bytes: bytes,
    attributes: dict[str, Any],
    retention_limit: int,
) -> dict[str, Any]:
    if _normalize_metric_key(metric_key) != WORKOUT_IMAGE_METRIC_KEY:
        return attributes

    file_name, workout_fingerprint, workout_timestamp, _extension = _workout_archive_file_name(
        metric_key=metric_key,
        attributes=attributes,
        content_type=content_type,
    )
    sanitized_username = sanitize_identifier(username)
    relative_path = f"{DOMAIN}/workouts/{sanitized_username}/{file_name}"
    archive_dir = Path(hass.config.path("media")) / DOMAIN / "workouts" / sanitized_username

    try:
        replaced_count = await hass.async_add_executor_job(
            _store_workout_archive_file,
            archive_dir,
            file_name,
            image_bytes,
            workout_fingerprint,
        )
        await hass.async_add_executor_job(
            _store_workout_archive_metadata,
            archive_dir,
            file_name,
            attributes,
        )
        pruned_count = await hass.async_add_executor_job(
            _prune_workout_archive_files,
            archive_dir,
            retention_limit,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Failed to archive workout image for '%s' (%s): %s",
            username,
            metric_key,
            err,
        )
        return attributes

    archived_attributes = dict(attributes)
    archived_attributes["archive_file_name"] = file_name
    archived_attributes["archive_relative_path"] = relative_path
    archived_attributes["archive_local_url"] = f"/media/local/{relative_path}"
    archived_attributes["archive_media_source_id"] = (
        f"media-source://media_source/local/{relative_path}"
    )
    archived_attributes["archive_replaced_file_count"] = replaced_count
    archived_attributes["archive_pruned_file_count"] = pruned_count
    archived_attributes["archive_workout_fingerprint"] = workout_fingerprint
    archived_attributes["archive_workout_timestamp"] = (
        workout_timestamp.isoformat().replace("+00:00", "Z")
    )
    return archived_attributes


def _coerce_workout_archive_limit(raw_limit: Any) -> int:
    if isinstance(raw_limit, str):
        raw_limit = raw_limit.strip()
        if not raw_limit:
            return WORKOUT_ARCHIVE_DEFAULT_LIST_LIMIT
    if not isinstance(raw_limit, (int, str)):
        return WORKOUT_ARCHIVE_DEFAULT_LIST_LIMIT
    try:
        value = int(raw_limit)
    except (TypeError, ValueError):
        return WORKOUT_ARCHIVE_DEFAULT_LIST_LIMIT
    if value < 1:
        return 1
    if value > WORKOUT_ARCHIVE_MAX_LIST_LIMIT:
        return WORKOUT_ARCHIVE_MAX_LIST_LIMIT
    return value


def _safe_utc_datetime(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def _workout_archive_timestamp_from_file_name(file_name: str) -> datetime | None:
    source = file_name.strip()
    if not source:
        return None

    compact_match = re.search(r"(\d{8})T(\d{6})Z?", source, flags=re.IGNORECASE)
    if compact_match:
        date_token = compact_match.group(1)
        time_token = compact_match.group(2)
        parsed = _safe_utc_datetime(
            int(date_token[0:4]),
            int(date_token[4:6]),
            int(date_token[6:8]),
            int(time_token[0:2]),
            int(time_token[2:4]),
            int(time_token[4:6]),
        )
        if parsed is not None:
            return parsed

    separated_match = re.search(
        r"(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})[T _-]?(\d{2})[:._-]?(\d{2})(?:[:._-]?(\d{2}))?",
        source,
    )
    if separated_match:
        parsed = _safe_utc_datetime(
            int(separated_match.group(1)),
            int(separated_match.group(2)),
            int(separated_match.group(3)),
            int(separated_match.group(4)),
            int(separated_match.group(5)),
            int(separated_match.group(6) or "0"),
        )
        if parsed is not None:
            return parsed

    date_only_match = re.search(r"(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})", source)
    if date_only_match:
        return _safe_utc_datetime(
            int(date_only_match.group(1)),
            int(date_only_match.group(2)),
            int(date_only_match.group(3)),
        )

    return None


def _is_supported_workout_archive_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in WORKOUT_ARCHIVE_IMAGE_SUFFIXES


def _collect_workout_archive_records(
    media_root: Path,
    username: str,
    limit: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_relative_paths: set[str] = set()

    for archive_domain in WORKOUT_ARCHIVE_FOLDER_DOMAIN_CANDIDATES:
        archive_dir = media_root / archive_domain / "workouts" / username
        if not archive_dir.exists() or not archive_dir.is_dir():
            continue
        try:
            iterator = archive_dir.rglob("*")
        except OSError:
            continue

        for file_path in iterator:
            try:
                if not file_path.is_file() or not _is_supported_workout_archive_file(file_path):
                    continue
                relative_path = file_path.relative_to(media_root).as_posix()
            except (OSError, ValueError):
                continue
            if relative_path in seen_relative_paths:
                continue
            seen_relative_paths.add(relative_path)

            try:
                stat = file_path.stat()
                modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            except OSError:
                modified_at = datetime.now(timezone.utc)

            timestamp_from_name = _workout_archive_timestamp_from_file_name(file_path.name)
            workout_timestamp = timestamp_from_name or modified_at
            encoded_relative_path = quote(relative_path, safe="/")
            metadata = _read_workout_archive_metadata(file_path)
            workout_identity = _workout_archive_record_identity(file_path, metadata)
            title = _first_nonempty_attribute_value(
                metadata,
                (*WORKOUT_ARCHIVE_WORKOUT_TYPE_ATTRIBUTE_KEYS, "activity_type", "workout_kind", "title", "name"),
            )
            record: dict[str, Any] = {
                "file_name": file_path.name,
                "relative_path": relative_path,
                "local_url": f"/media/local/{encoded_relative_path}",
                "media_source_id": f"media-source://media_source/local/{encoded_relative_path}",
                "timestamp": workout_timestamp.isoformat().replace("+00:00", "Z"),
                "day_key": workout_timestamp.date().isoformat(),
                "modified_at": modified_at.isoformat().replace("+00:00", "Z"),
                "_sort_timestamp": workout_timestamp.timestamp(),
                "_sort_modified": modified_at.timestamp(),
                "_workout_identity": workout_identity,
            }
            for key, value in metadata.items():
                if key not in record:
                    record[key] = value
            if title is not None:
                record["title"] = str(title)
            records.append(record)

    records.sort(
        key=lambda item: (
            float(item.get("_sort_timestamp", 0.0)),
            float(item.get("_sort_modified", 0.0)),
            str(item.get("relative_path", "")),
        ),
        reverse=True,
    )
    deduplicated_records: list[dict[str, Any]] = []
    seen_workout_identities: set[str] = set()
    for item in records:
        workout_identity = str(item.pop("_workout_identity", "")).strip()
        if workout_identity:
            if workout_identity in seen_workout_identities:
                continue
            seen_workout_identities.add(workout_identity)
        item.pop("_sort_timestamp", None)
        item.pop("_sort_modified", None)
        deduplicated_records.append(item)
    return deduplicated_records[:limit]


async def _async_collect_workout_archive_records(
    hass: HomeAssistant,
    username: str,
    limit: int,
) -> list[dict[str, Any]]:
    media_root = Path(hass.config.path("media"))
    return await hass.async_add_executor_job(
        _collect_workout_archive_records,
        media_root,
        username,
        limit,
    )


def _workout_archive_image_url(
    username: str,
    relative_path: str,
    version: str | None = None,
) -> str:
    encoded_username = quote(username, safe="")
    encoded_path = quote(relative_path, safe="")
    url = (
        f"{WORKOUT_ARCHIVE_IMAGE_ENDPOINT_PATH}"
        f"?username={encoded_username}&path={encoded_path}"
    )
    normalized_version = str(version or "").strip()
    if normalized_version:
        url += f"&v={quote(normalized_version, safe='')}"
    return url


def _normalize_requested_relative_path(raw_path: str) -> str:
    decoded = unquote(raw_path.strip())
    if not decoded:
        return ""
    normalized = decoded.replace("\\", "/").lstrip("/")
    path_obj = Path(normalized)
    if path_obj.is_absolute():
        return ""
    if any(part in {"", ".", ".."} for part in path_obj.parts):
        return ""
    return path_obj.as_posix()


def _resolve_workout_archive_file_path(
    media_root: Path,
    username: str,
    requested_relative_path: str,
) -> Path | None:
    normalized_path = _normalize_requested_relative_path(requested_relative_path)
    if not normalized_path:
        return None

    username_prefix = f"workouts/{username}/"
    if "/workouts/" not in normalized_path or not normalized_path.endswith(tuple(WORKOUT_ARCHIVE_IMAGE_SUFFIXES)):
        return None

    for archive_domain in WORKOUT_ARCHIVE_FOLDER_DOMAIN_CANDIDATES:
        prefix = f"{archive_domain}/{username_prefix}"
        if not normalized_path.startswith(prefix):
            continue
        candidate_path = media_root / normalized_path
        allowed_root = media_root / archive_domain / "workouts" / username
        try:
            resolved_candidate = candidate_path.resolve()
            resolved_allowed_root = allowed_root.resolve()
            resolved_candidate.relative_to(resolved_allowed_root)
        except (OSError, ValueError):
            return None
        if not resolved_candidate.exists() or not resolved_candidate.is_file():
            return None
        if not _is_supported_workout_archive_file(resolved_candidate):
            return None
        return resolved_candidate

    return None
