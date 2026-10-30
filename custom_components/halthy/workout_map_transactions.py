"""Authenticated conditional map writes with joined IO and durable recovery.

Current-map state is independent of archive files and the delayed entity store.
No HealthKit deletion is inferred here: callers must supply source proof/intent.
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import re
import time
from threading import Lock
from typing import Any
from uuid import uuid5, NAMESPACE_URL

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.const import KEY_HASS_USER, KEY_HASS_REFRESH_TOKEN_ID
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, new_image_signal, update_image_signal
from .naming import normalize_metric_key
from .paired_webhook import PairingError, _body, _text, async_user_not_allowed_do_auth
from .runtime import runtime_lock
from .workout_map_state import WorkoutMapStateError, _owned_runtime, current_workout_map_state
from . import workout_map_journal as journal


class MapTransactionError(Exception):
    def __init__(self, code: str, status: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _scope(entry_id: str, runtime: Any) -> dict[str, str]:
    return {"entry_id": entry_id, "account_id": runtime.owner_user_id, "username": runtime.configured_username}


async def _io(hass: Any, function: Any, *args: Any) -> tuple[Any, bool]:
    """Join the worker itself, even if HA cancels its executor handle."""
    completed: Future[Any] = Future()
    guard = Lock()
    started = False
    abandoned = False
    cancelled = False

    @wraps(function)
    def run() -> None:
        nonlocal started
        with guard:
            if abandoned:
                return
            started = True
        try:
            value = function(*args)
        except BaseException as error:
            completed.set_exception(error)
        else:
            completed.set_result(value)

    def executor_finished(handle: Any) -> None:
        nonlocal abandoned, cancelled
        error = None if handle.cancelled() else handle.exception()
        with guard:
            if handle.cancelled():
                cancelled = True
            if not started and (handle.cancelled() or error is not None):
                # If the queued wrapper later starts, it must not perform IO.
                abandoned = True
                if handle.cancelled():
                    completed.cancel()
                else:
                    completed.set_exception(error)

    work = asyncio.ensure_future(hass.async_add_executor_job(run))
    work.add_done_callback(executor_finished)
    joined = asyncio.wrap_future(completed)
    while True:
        try:
            result = await asyncio.shield(joined)
            return result, cancelled
        except asyncio.CancelledError:
            if joined.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise


def _publish(runtime: Any, document: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    from . import _image_from_storage
    before = {key: value for key, value in runtime.images.items() if normalize_metric_key(value.metric_key) == "workout"}
    raw = document["current"]
    restored = None if raw is None else _image_from_storage(raw["unique_id"], raw)
    if raw is not None and restored is None:
        raise MapTransactionError("map_journal_unavailable")
    after = {} if restored is None else {restored.unique_id: restored}
    runtime.images = {key: value for key, value in runtime.images.items() if normalize_metric_key(value.metric_key) != "workout"}
    runtime.images.update(after)
    if document["slot_id"] is not None:
        runtime.workout_image_unique_id = document["slot_id"]
    runtime.map_journal_scope = deepcopy(document["scope"])
    runtime.map_journal_error = None
    return list(before.keys() - after.keys()), list(after.keys() - before.keys()), [
        key for key in after if key not in before or before[key] != after[key]]


def _notify(hass: Any, entry_id: str, changes: tuple[list[str], list[str], list[str]]) -> None:
    removed, added, updated = changes
    for key in removed:
        # A clear keeps the stable entity slot unavailable for later replacement.
        async_dispatcher_send(hass, update_image_signal(entry_id, key))
    for key in added:
        async_dispatcher_send(hass, new_image_signal(entry_id), key)
    for key in updated:
        async_dispatcher_send(hass, update_image_signal(entry_id, key))


async def async_restore_current_map(hass: Any, entry_id: str, runtime: Any, *, notify: bool = False) -> dict[str, Any] | None:
    """Caller holds runtime gate; also used before installing a fresh runtime."""
    scope = _scope(entry_id, runtime)
    if runtime.map_journal_scope is None and not _text(runtime.owner_user_id):
        return None
    if runtime.map_journal_scope is not None and runtime.map_journal_scope != scope:
        runtime.map_journal_error = "map_scope_changed"
        raise MapTransactionError("map_scope_changed")
    try:
        path = journal.journal_path(hass.config.path(".storage"), scope)
        document, cancelled = await _io(hass, journal.load_confirmed_snapshot, path, scope)
        if document is None:
            if runtime.map_journal_scope is not None:
                raise MapTransactionError("map_journal_missing")
            runtime.map_journal_error = None
            if cancelled:
                raise asyncio.CancelledError
            return None
        changes = _publish(runtime, document)
        if notify:
            _notify(hass, entry_id, changes)
        if cancelled:
            raise asyncio.CancelledError
        return document
    except journal.MapJournalError as error:
        runtime.map_journal_error = error.code
        raise MapTransactionError(error.code) from None
    except MapTransactionError as error:
        runtime.map_journal_error = error.code
        raise


def _baseline(entry_id: str, runtime: Any) -> dict[str, Any] | None:
    from . import _image_to_storage
    current = current_workout_map_state(entry_id, runtime)
    if current is None:
        return None
    image = next(value for value in runtime.images.values() if normalize_metric_key(value.metric_key) == "workout")
    result = _image_to_storage(image)
    result["image_base64"] = base64.b64encode(image.image_bytes).decode("ascii")
    result["metric_key"] = "workout"
    result["attributes"] = {key: deepcopy(value) for key, value in image.attributes.items() if not key.startswith("archive_")}
    result["attributes"]["workout_uuid"] = current["workout_uuid"]
    return result


def _replacement(scope: dict[str, str], runtime: Any, device_id: str, image: dict[str, Any], *, existing_id: str | None = None) -> dict[str, Any]:
    from . import _unique_image_id
    current = next((value for value in runtime.images.values() if normalize_metric_key(value.metric_key) == "workout"), None)
    unique_id = existing_id or (current.unique_id if current is not None else (
        runtime.workout_image_unique_id or _unique_image_id(scope["username"], device_id, "workout")))
    return {"unique_id": unique_id, "metric_key": "workout", "name": image["name"],
            "content_type": image["content_type"], "image_base64": image["image_base64"],
            "attributes": deepcopy(image["attributes"]), "username": runtime.app_username,
            "device_id": device_id, "updated_at": datetime.now(timezone.utc).isoformat()}


def _commit(path: Any, scope: dict[str, str], operation: str, device: str, expected: str,
            replacement: dict[str, Any] | None, raw: bytes) -> Any:
    return journal.commit_mutation(path, scope, operation_id=operation, device_id=device,
        expected_revision=expected, replacement=replacement, request_body=raw)


def _state(document: dict[str, Any]) -> dict[str, Any]:
    return {"revision": document["revision"], "generation": document["generation"],
            "current": journal._projection(document["current"])}


def _validate_image(image: Any) -> None:
    if (not isinstance(image, dict) or set(image) != {"key", "name", "content_type", "image_base64", "attributes"}
            or image["key"] != "workout" or not _text(image["name"])):
        raise MapTransactionError("invalid_map_transaction", 400)
    # Validate exact bytes/metadata through the shared self-contained contract.
    try:
        journal._image({"unique_id": "validation", "metric_key": "workout", "name": image["name"],
            "content_type": image["content_type"], "image_base64": image["image_base64"],
            "attributes": image["attributes"], "username": "validation", "device_id": "validation",
            "updated_at": "2026-01-01T00:00:00Z"}, {"entry_id": "validation", "account_id": "validation", "username": "validation"})
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise MapTransactionError("invalid_map_transaction", 400) from None


def _authorize(hass: Any, request: Any) -> str:
    user = request.get(KEY_HASS_USER)
    owner = getattr(user, "id", None)
    grant_id = request.get(KEY_HASS_REFRESH_TOKEN_ID)
    if not _text(owner) or not _text(grant_id):
        raise MapTransactionError("grant_not_valid", 403)
    grant = hass.auth.async_get_refresh_token(grant_id)
    if grant is None or grant.user.id != owner:
        raise MapTransactionError("grant_not_valid", 403)
    expiration = getattr(grant, "expire_at", None)
    if ((expiration is not None and expiration <= time.time())
            or async_user_not_allowed_do_auth(hass, grant.user, request)):
        raise MapTransactionError("grant_not_valid", 403)
    try:
        hass.auth.async_validate_refresh_token(grant, request.remote)
    except Exception:
        raise MapTransactionError("grant_not_valid", 403) from None
    return owner


class HalthyWorkoutMapTransactionView(HomeAssistantView):
    url = "/api/halthy/workout_map/transaction"
    name = "api:halthy:workout_map_transaction"
    requires_auth = True

    @staticmethod
    def _reply(payload: dict[str, Any], status: int = 200) -> Any:
        return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})

    async def post(self, request: Any) -> Any:
        try:
            hass = request.app[KEY_HASS]
            owner = _authorize(hass, request)
            raw, _ = await _body(request, 512 * 1024)
            payload = json.loads(raw, object_pairs_hook=journal._pairs)
            base = {"version", "action", "entry_id", "username", "device_id"}
            action = payload.get("action")
            extra = {"activate": {"baseline_revision"}, "state": set(),
                     "replace": {"operation_id", "expected_revision", "image"},
                     "clear": {"operation_id", "expected_revision"}}
            if (action not in extra or set(payload) != base | extra[action]
                    or type(payload["version"]) is not int or payload["version"] != 1
                    or not all(_text(payload[key]) for key in ("entry_id", "username", "device_id"))
                    or re.fullmatch(r"[a-z0-9_]+", payload["username"]) is None):
                raise MapTransactionError("invalid_map_transaction", 400)
            if action == "activate" and payload["baseline_revision"] is not None and not journal._hash(payload["baseline_revision"]):
                raise MapTransactionError("invalid_map_transaction", 400)
            if action in {"replace", "clear"}:
                if not journal._uuid(payload["operation_id"]) or not journal._hash(payload["expected_revision"]):
                    raise MapTransactionError("invalid_map_transaction", 400)
                if action == "replace":
                    _validate_image(payload["image"])
            runtime = _owned_runtime(hass, owner, payload)
            async with runtime_lock(runtime):
                if _authorize(hass, request) != owner or _owned_runtime(hass, owner, payload) is not runtime:
                    raise MapTransactionError("profile_not_owned_or_ambiguous", 403)
                if str(getattr(hass, "state", "RUNNING")) in {"STOPPING", "FINAL_WRITE", "STOPPED"}:
                    raise MapTransactionError("map_server_stopping")
                scope = _scope(payload["entry_id"], runtime)
                path = journal.journal_path(hass.config.path(".storage"), scope)
                document = await async_restore_current_map(hass, payload["entry_id"], runtime, notify=True)
                if action == "activate" and document is None:
                    current = current_workout_map_state(payload["entry_id"], runtime)
                    if payload["baseline_revision"] != (None if current is None else current["revision"]):
                        raise MapTransactionError("map_revision_conflict", 409)
                    try:
                        baseline = _baseline(payload["entry_id"], runtime)
                        slot_id = baseline["unique_id"] if baseline is not None else runtime.workout_image_unique_id
                        document, cancelled = await _io(hass, journal.initialize_journal, path, scope, baseline, slot_id)
                    except journal.MapJournalError as error:
                        runtime.map_journal_error = error.code
                        raise
                    if _owned_runtime(hass, owner, payload) is not runtime:
                        raise MapTransactionError("profile_not_owned_or_ambiguous", 403)
                    _notify(hass, payload["entry_id"], _publish(runtime, document))
                    if cancelled:
                        raise asyncio.CancelledError
                if document is None:
                    raise MapTransactionError("map_journal_not_active", 409)
                result = {"ok": True, "version": 1, "account_id": owner, "entry_id": payload["entry_id"],
                          "username": payload["username"], "device_id": payload["device_id"], "state": _state(document)}
                if action in {"replace", "clear"}:
                    replacement = None if action == "clear" else _replacement(scope, runtime, payload["device_id"], payload["image"], existing_id=document["slot_id"])
                    try:
                        (document, receipt, replay), cancelled = await _io(hass, _commit, path, scope,
                            payload["operation_id"], payload["device_id"], payload["expected_revision"], replacement, raw)
                    except journal.MapJournalError as error:
                        if error.code == "map_write_unconfirmed":
                            runtime.map_journal_error = error.code
                        if isinstance(error, journal.MapMutationRejection):
                            # Recheck access after joined IO before exposing the
                            # request-bound durable projection. No delivery ACK.
                            if _authorize(hass, request) != owner or _owned_runtime(hass, owner, payload) is not runtime:
                                raise MapTransactionError("profile_not_owned_or_ambiguous", 403)
                            if str(getattr(hass, "state", "RUNNING")) in {"STOPPING", "FINAL_WRITE", "STOPPED"}:
                                raise MapTransactionError("map_server_stopping")
                            return self._reply(error.proof, 409)
                        raise
                    if _owned_runtime(hass, owner, payload) is not runtime:
                        raise MapTransactionError("profile_not_owned_or_ambiguous", 403)
                    _notify(hass, payload["entry_id"], _publish(runtime, document))
                    result.update({"state": _state(document), "operation_id": payload["operation_id"],
                                   "receipt": receipt, "replayed": replay})
                    if cancelled:
                        raise asyncio.CancelledError
                from . import _schedule_store_save
                _schedule_store_save(hass)
                return self._reply(result)
        except (MapTransactionError, WorkoutMapStateError, PairingError) as error:
            return self._reply({"error": error.code}, error.status)
        except journal.MapJournalError as error:
            status = 409 if error.code in {"map_revision_conflict", "map_operation_conflict", "map_journal_full"} else 503
            return self._reply({"error": error.code}, status)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            return self._reply({"error": "invalid_map_transaction"}, 400)


async def async_legacy_map_write(hass: Any, entry_id: str, runtime: Any, device_id: str,
                                 image: dict[str, Any]) -> tuple[str, bool] | None:
    """Gated legacy advancement joins the journal once activated; no guesses."""
    document = await async_restore_current_map(hass, entry_id, runtime, notify=True)
    if document is None:
        return None
    scope = _scope(entry_id, runtime)
    attrs = {key: value for key, value in image["attributes"].items() if not key.startswith("archive_")}
    wire = {"key": "workout", "name": image["name"], "content_type": image["content_type"],
            "image_base64": base64.b64encode(image["image_bytes"]).decode("ascii"), "attributes": attrs}
    _validate_image(wire)
    previous = document["current"]
    if previous is None and document["generation"] > 0:
        raise MapTransactionError("conditional_map_write_required", 409)
    if previous is not None:
        equal = (previous["name"] == wire["name"] and previous["content_type"] == wire["content_type"]
                 and previous["image_base64"] == wire["image_base64"] and previous["attributes"] == attrs
                 and previous["device_id"] == device_id)
        if equal:
            return previous["unique_id"], False
        if journal._time(attrs["workout_end"]) <= journal._time(previous["attributes"]["workout_end"]):
            raise MapTransactionError("conditional_map_write_required", 409)
        if attrs["workout_uuid"] == previous["attributes"]["workout_uuid"]:
            raise MapTransactionError("conditional_map_write_required", 409)
    replacement = _replacement(scope, runtime, device_id, wire, existing_id=document["slot_id"])
    identity = json.dumps({"legacy": wire, "device_id": device_id, "expected_revision": document["revision"]},
                          sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    operation = str(uuid5(NAMESPACE_URL, "halthy_legacy_map:" + hashlib.sha256(identity).hexdigest()))
    path = journal.journal_path(hass.config.path(".storage"), scope)
    try:
        (document, _receipt, _replay), cancelled = await _io(hass, _commit, path, scope, operation,
                                                          device_id, document["revision"], replacement, identity)
    except journal.MapJournalError as error:
        runtime.map_journal_error = error.code
        raise MapTransactionError(error.code) from None
    _notify(hass, entry_id, _publish(runtime, document))
    if cancelled:
        raise asyncio.CancelledError
    return document["current"]["unique_id"], True
