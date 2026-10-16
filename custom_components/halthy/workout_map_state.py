"""Authenticated, read-only current-map revisions for conditional recovery.

This does not authorize replacement or deletion. Its image digest is not a
durable journal mutation token. A future writer must issue/check a persisted
state revision (including empty state) under the runtime lock.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from uuid import UUID

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.const import KEY_HASS_USER

from .const import DOMAIN
from .naming import normalize_metric_key
from .paired_webhook import PairingError, _body, _text
from .runtime import runtime_lock
from .timestamps import parse_measurement_timestamp
from .workout_archive import WORKOUT_IMAGE_METRIC_KEY, _validated_image_content_type
from .workout_map_journal import validate_json_tree


class WorkoutMapStateError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkoutMapStateError("invalid_map_state_request", 400)
        result[key] = value
    return result


def _owned_runtime(hass: Any, owner: str, scope: dict[str, Any]) -> Any:
    entries = hass.data.get(DOMAIN, {}).get("entries", {})
    runtime = entries.get(scope["entry_id"])
    if (runtime is None or runtime.owner_user_id != owner
            or runtime.configured_username != scope["username"]
            or sum(item.configured_username == scope["username"] for item in entries.values()) != 1):
        raise WorkoutMapStateError("profile_not_owned_or_ambiguous", 403)
    return runtime


def current_workout_map_state(entry_id: str, runtime: Any) -> dict[str, Any] | None:
    """Caller holds runtime_lock. Never treats unusable/ambiguous state as empty."""
    from . import MAX_ATTRIBUTES_BYTES, MAX_IMAGE_BYTES, _workout_record_from_attributes

    if runtime.map_journal_error:
        raise WorkoutMapStateError("current_map_state_unavailable", 503)
    try:
        images = [(key, image) for key, image in runtime.images.items()
                  if normalize_metric_key(image.metric_key) == WORKOUT_IMAGE_METRIC_KEY]
    except (AttributeError, TypeError, ValueError):
        raise WorkoutMapStateError("current_map_state_unavailable", 503) from None
    if not images:
        return None
    if len(images) != 1:
        raise WorkoutMapStateError("current_map_state_unavailable", 503)
    image_key, image = images[0]
    try:
        attrs = image.attributes
        validate_json_tree(attrs)
        raw_uuid = attrs.get("workout_uuid")
        raw_end = attrs.get("workout_end")
        if (not isinstance(raw_uuid, str) or str(UUID(raw_uuid)) != raw_uuid.lower()
                or not isinstance(raw_end, str) or not _text(image.device_id)
                or image_key != image.unique_id or not _text(image.unique_id) or not _text(image.name)
                or not isinstance(image.image_bytes, bytes) or not 0 < len(image.image_bytes) <= MAX_IMAGE_BYTES
                or _validated_image_content_type(image.content_type, image.image_bytes) != image.content_type
                or not isinstance(image.updated_at, datetime) or image.updated_at.tzinfo is None):
            raise ValueError
        end = parse_measurement_timestamp(raw_end)
        workout = _workout_record_from_attributes(attrs)
        if (end is None or workout is None or workout.end != end
                or str(UUID(workout.uid)) != raw_uuid.lower()):
            raise ValueError
        attr_json = json.dumps(attrs, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(attr_json.encode()) > MAX_ATTRIBUTES_BYTES:
            raise ValueError
        material = {
            "contract": "halthy_workout_map_state_v1", "entry_id": entry_id,
            "owner_id": runtime.owner_user_id, "username": runtime.configured_username,
            "image_id": image.unique_id, "source_device_id": image.device_id,
            "name": image.name, "content_type": image.content_type,
            "image_sha256": hashlib.sha256(image.image_bytes).hexdigest(),
            "attributes": attrs, "updated_at": image.updated_at.astimezone(timezone.utc).isoformat(),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        return {"revision": hashlib.sha256(encoded).hexdigest(), "workout_uuid": raw_uuid.lower(),
                "workout_end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_device_id": image.device_id}
    except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise WorkoutMapStateError("current_map_state_unavailable", 503) from None


class HalthyWorkoutMapStateView(HomeAssistantView):
    url = "/api/halthy/workout_map/state"
    name = "api:halthy:workout_map_state"
    requires_auth = True

    @staticmethod
    def _reply(payload: dict[str, Any], status: int = 200) -> Any:
        return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})

    async def post(self, request: Any) -> Any:
        try:
            owner = getattr(request.get(KEY_HASS_USER), "id", None)
            if not _text(owner):
                raise WorkoutMapStateError("missing_user_context", 403)
            raw, _ = await _body(request, 4096)
            scope = json.loads(raw, object_pairs_hook=_json_object)
            if (not isinstance(scope, dict) or set(scope) != {"version", "entry_id", "username", "device_id"}
                    or type(scope["version"]) is not int or scope["version"] != 1
                    or not all(_text(scope[key]) for key in ("entry_id", "username", "device_id"))
                    or re.fullmatch(r"[a-z0-9_]+", scope["username"]) is None):
                raise WorkoutMapStateError("invalid_map_state_request", 400)
            hass = request.app[KEY_HASS]
            runtime = _owned_runtime(hass, owner, scope)
            async with runtime_lock(runtime):
                if _owned_runtime(hass, owner, scope) is not runtime:
                    raise WorkoutMapStateError("profile_not_owned_or_ambiguous", 403)
                current = current_workout_map_state(scope["entry_id"], runtime)
            return self._reply({"ok": True, "version": 1, "account_id": owner, "entry_id": scope["entry_id"],
                                "username": scope["username"], "device_id": scope["device_id"], "current": current})
        except (WorkoutMapStateError, PairingError) as error:
            return self._reply({"error": error.code}, error.status)
        except (ValueError, UnicodeError, RecursionError):
            return self._reply({"error": "invalid_map_state_request"}, 400)
