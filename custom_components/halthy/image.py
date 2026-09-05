"""Image platform for Halthy."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .runtime import HalthyImageState, IntegrationRuntime, runtime_device_identifiers
from .const import (
    DOMAIN,
    MANUFACTURER,
    new_image_signal,
    remove_image_signal,
    update_image_signal,
)
from .naming import sanitize_identifier

_LOGGER = logging.getLogger(__name__)

WORKOUT_IMAGE_METRIC_KEY = "workout"
WORKOUT_STATE_TYPE_KEYS = (
    "workout_type",
    "workout_activity_type",
    "activity_type",
    "workout_kind",
    "type",
)
WORKOUT_ATTRIBUTE_SOURCES: dict[str, tuple[str, ...]] = {
    "Active energy (kcal)": (
        "workout_active_energy_kcal",
        "active_energy_kcal",
        "active_energy",
        "workout_active_energy",
    ),
    "Total flights climbed": (
        "total_flights_climbed",
        "workout_total_flights_climbed",
        "flights_climbed",
        "flights",
    ),
    "Highest altitude": (
        "highest_altitude_m",
        "max_altitude_m",
        "highest_altitude",
        "workout_highest_altitude_m",
    ),
    "Lowest altitude": (
        "lowest_altitude_m",
        "min_altitude_m",
        "lowest_altitude",
        "workout_lowest_altitude_m",
    ),
    "Lowest speed": (
        "lowest_speed_mps",
        "min_speed_mps",
        "lowest_speed",
        "workout_lowest_speed_mps",
    ),
    "Highest speed": (
        "highest_speed_mps",
        "max_speed_mps",
        "highest_speed",
        "workout_highest_speed_mps",
    ),
    "Avg speed": (
        "avg_speed_mps",
        "workout_avg_speed_mps",
        "average_speed_mps",
        "workout_average_speed_mps",
    ),
    "Lowest heart rate": (
        "lowest_heart_rate_bpm",
        "min_heart_rate_bpm",
        "lowest_heart_rate",
        "workout_lowest_heart_rate_bpm",
    ),
    "Highest heart rate": (
        "highest_heart_rate_bpm",
        "max_heart_rate_bpm",
        "highest_heart_rate",
        "workout_highest_heart_rate_bpm",
    ),
    "Avg heart rate": (
        "avg_heart_rate_bpm",
        "average_heart_rate_bpm",
        "workout_avg_heart_rate_bpm",
    ),
    "Cadence": (
        "cadence_spm",
        "avg_cadence_spm",
        "cadence",
    ),
    "Power": (
        "power_w",
        "avg_power_w",
        "power",
    ),
    "Respiratory rate": (
        "respiratory_rate_brpm",
        "respiratory_rate",
        "avg_respiratory_rate_brpm",
    ),
}
WORKOUT_METADATA_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "measurement_timestamp",
    "workout_start",
    "workout_end",
    "workout_uuid",
    "point_count",
    "rendered_point_count",
    "detailed_map",
    "archive_file_name",
    "archive_relative_path",
    "archive_local_url",
    "archive_media_source_id",
    "archive_replaced_file_count",
    "archive_workout_timestamp",
    "workout_distance_m",
    "distance_m",
    "workout_duration_s",
    "duration_s",
    "workout_active_energy_kcal",
    "active_energy_kcal",
    "total_flights_climbed",
    "workout_total_flights_climbed",
    "avg_speed_mps",
    "workout_avg_speed_mps",
    "highest_speed_mps",
    "lowest_speed_mps",
    "avg_heart_rate_bpm",
    "workout_avg_heart_rate_bpm",
    "lowest_heart_rate_bpm",
    "highest_heart_rate_bpm",
    "cadence_spm",
    "avg_cadence_spm",
    "power_w",
    "avg_power_w",
    "respiratory_rate_brpm",
    "avg_respiratory_rate_brpm",
    "workout_elevation_gain_m",
    "elevation_gain_m",
    "highest_altitude_m",
    "lowest_altitude_m",
    "weather_condition",
    "weather_condition_raw",
    "weather_temperature_c",
    "weather_humidity_percent",
    "weather_attribution_service",
    "weather_attribution_url",
    "workout_zone_groups",
    "heart_rate_zones",
    "heart_rate_zone_total_duration_s",
    "cycling_power_zones",
    "cycling_power_zone_total_duration_s",
)


def _is_workout_metric(metric_key: str) -> bool:
    return sanitize_identifier(metric_key) == WORKOUT_IMAGE_METRIC_KEY


def _first_present_attr(attrs: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = attrs.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric == float("inf") or numeric == float("-inf"):
            return None
        if numeric != numeric:
            return None
        return numeric
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        if not normalized:
            return None
        try:
            numeric = float(normalized)
        except ValueError:
            return None
        if numeric == float("inf") or numeric == float("-inf"):
            return None
        if numeric != numeric:
            return None
        return numeric
    return None


def _derive_from_route_points(attrs: dict[str, Any]) -> dict[str, float]:
    route_points = attrs.get("route_points")
    if not isinstance(route_points, list):
        return {}

    altitudes: list[float] = []
    speeds: list[float] = []
    for raw_point in route_points:
        if not isinstance(raw_point, dict):
            continue
        altitude = _coerce_float(raw_point.get("altitude"))
        if altitude is not None:
            altitudes.append(altitude)
        speed = _coerce_float(
            raw_point.get("speed_mps")
            or raw_point.get("speedMetersPerSecond")
            or raw_point.get("speed")
        )
        if speed is not None:
            speeds.append(speed)

    derived: dict[str, float] = {}
    if altitudes:
        derived["Highest altitude"] = max(altitudes)
        derived["Lowest altitude"] = min(altitudes)
    if speeds:
        derived["Highest speed"] = max(speeds)
        derived["Lowest speed"] = min(speeds)
    return derived


def _workout_type_from_attributes(attrs: dict[str, Any]) -> str:
    raw_type = _first_present_attr(attrs, WORKOUT_STATE_TYPE_KEYS)
    if raw_type is None:
        return "Workout"

    if isinstance(raw_type, str):
        cleaned = raw_type.strip()
        if not cleaned:
            return "Workout"
        cleaned = cleaned.replace("HKWorkoutActivityType", "")
        cleaned = cleaned.replace("_", " ").replace("-", " ").strip()
        if not cleaned:
            return "Workout"
        return " ".join(part.capitalize() for part in cleaned.split())

    return str(raw_type)


def _format_workout_attribute_value(label: str, value: Any) -> Any:
    if label == "Total flights climbed":
        numeric = _coerce_float(value)
        if numeric is not None:
            return int(round(numeric))
    numeric = _coerce_float(value)
    if numeric is not None:
        return round(numeric, 3)
    return value


def _workout_attributes(raw_attrs: dict[str, Any]) -> dict[str, Any]:
    attrs = dict(raw_attrs)
    derived = _derive_from_route_points(attrs)
    normalized: dict[str, Any] = {}
    for label, source_keys in WORKOUT_ATTRIBUTE_SOURCES.items():
        raw_value = _first_present_attr(attrs, source_keys)
        if raw_value is None:
            raw_value = derived.get(label)
        if raw_value is None:
            continue
        normalized[label] = _format_workout_attribute_value(label, raw_value)
    for key in WORKOUT_METADATA_ATTRIBUTE_KEYS:
        value = attrs.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        normalized[key] = value
    return normalized


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up bridge images from config entry."""
    domain_data = hass.data[DOMAIN]
    runtime: IntegrationRuntime = domain_data["entries"][entry.entry_id]
    entities: dict[str, HalthyImage] = {}

    # Clean up stale image entities that were left in the registry but are no longer
    # present in runtime storage (for example after unique-id or routing changes).
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.domain != "image":
            continue
        unique_id = registry_entry.unique_id or ""
        if not unique_id.startswith(f"{DOMAIN}_"):
            continue
        if unique_id in runtime.images:
            continue
        registry.async_remove(registry_entry.entity_id)

    @callback
    def async_add_image(unique_id: str) -> None:
        if unique_id in entities:
            return
        if unique_id not in runtime.images:
            return
        entity = HalthyImage(
            hass=hass,
            runtime=runtime,
            entry_id=entry.entry_id,
            unique_id=unique_id,
        )
        entities[unique_id] = entity
        async_add_entities([entity])

    for unique_id in sorted(runtime.images):
        async_add_image(unique_id)

    remove_listener = async_dispatcher_connect(hass, new_image_signal(entry.entry_id), async_add_image)
    entry.async_on_unload(remove_listener)


class HalthyImage(ImageEntity):
    """Represents a dynamically-created image pushed from the iOS app."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: IntegrationRuntime,
        entry_id: str,
        unique_id: str,
    ) -> None:
        # HA ImageEntity signature differs across versions.
        try:
            super().__init__(hass)
        except TypeError:
            super().__init__()
        self._runtime = runtime
        self._entry_id = entry_id
        self._image_unique_id = unique_id
        self._image_state = runtime.images[unique_id]
        self._workout_state = "Workout"

        self._attr_unique_id = unique_id
        self._attr_suggested_object_id = (
            f"{sanitize_identifier(self._runtime.configured_username)}_"
            f"{sanitize_identifier(self._image_state.metric_key)}"
        )
        self._apply_state(self._image_state)

    @callback
    def _apply_state(self, state: HalthyImageState) -> None:
        self._image_state = state
        self._attr_name = state.name
        self._attr_content_type = state.content_type
        if _is_workout_metric(state.metric_key):
            attrs = _workout_attributes(state.attributes)
            attrs["username"] = state.username
            attrs["device_id"] = state.device_id
            attrs["last_pushed"] = state.updated_at.isoformat()
            self._workout_state = _workout_type_from_attributes(state.attributes)
        else:
            attrs = dict(state.attributes)
            attrs.setdefault("metric_key", state.metric_key)
            attrs.setdefault("username", state.username)
            attrs.setdefault("device_id", state.device_id)
            attrs["last_pushed"] = state.updated_at.isoformat()
            self._workout_state = "Workout"
        self._attr_extra_state_attributes = attrs

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers=runtime_device_identifiers(self._runtime),
            manufacturer=MANUFACTURER,
            model="iOS App",
            name=self._runtime.display_name,
        )

    async def async_image(self) -> bytes | None:
        return self._image_state.image_bytes

    @property
    def state(self) -> str:
        if _is_workout_metric(self._image_state.metric_key):
            return self._workout_state
        return "idle"

    async def async_added_to_hass(self) -> None:
        """Register update callback."""
        await super().async_added_to_hass()
        await self._async_migrate_entity_id_if_needed()

        @callback
        def async_handle_image_update() -> None:
            state = self._runtime.images.get(self._image_unique_id)
            if state is None:
                return
            self._apply_state(state)
            self.async_write_ha_state()
            self.hass.async_create_task(self._async_migrate_entity_id_if_needed())

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                update_image_signal(self._entry_id, self._image_unique_id),
                async_handle_image_update,
            )
        )

        @callback
        def async_handle_image_remove() -> None:
            self.hass.async_create_task(self._async_remove_entity_if_present())

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                remove_image_signal(self._entry_id, self._image_unique_id),
                async_handle_image_remove,
            )
        )

    async def _async_migrate_entity_id_if_needed(self) -> None:
        if self.entity_id is None:
            return

        desired_entity_id = (
            f"image.{sanitize_identifier(self._runtime.configured_username)}_"
            f"{sanitize_identifier(self._image_state.metric_key)}"
        )
        if self.entity_id == desired_entity_id:
            return

        registry = er.async_get(self.hass)
        existing_target = registry.async_get(desired_entity_id)
        if existing_target is not None:
            # If canonical id is occupied by an orphan entry from this integration
            # (no matching runtime image), remove it and reclaim the stable id.
            is_same_entry = existing_target.config_entry_id == self._entry_id
            is_halthy_image = (
                existing_target.domain == "image"
                and (existing_target.unique_id or "").startswith(f"{DOMAIN}_")
            )
            is_orphan = (existing_target.unique_id or "") not in self._runtime.images
            if is_same_entry and is_halthy_image and is_orphan:
                registry.async_remove(desired_entity_id)
            else:
                return

        try:
            registry.async_update_entity(self.entity_id, new_entity_id=desired_entity_id)
        except ValueError:
            return

    async def _async_remove_entity_if_present(self) -> None:
        if self.entity_id is not None:
            registry = er.async_get(self.hass)
            if registry.async_get(self.entity_id) is not None:
                registry.async_remove(self.entity_id)
        try:
            await self.async_remove(force_remove=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to remove image entity '%s' for unique_id '%s': %s",
                self.entity_id,
                self._image_unique_id,
                err,
            )
            return
