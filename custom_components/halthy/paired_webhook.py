"""Opt-in, grant-bound, write-only webhook pairings.

Secret IDs are credentials. Never include IDs, request bodies or exception text
in logs. Retained revoked records prevent delayed pairing retries resurrecting a
rotated credential. Loading invalid storage fails closed instead of resetting it.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
import re
import time
from typing import Any

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.components.http import KEY_HASS, HomeAssistantView
try:
    from homeassistant.components.http.auth_util import async_user_not_allowed_do_auth
except ModuleNotFoundError as error:
    if error.name != "homeassistant.components.http.auth_util":
        raise
    # HA 2025.6 keeps the same authorization policy in the auth module.
    from homeassistant.components.http.auth import async_user_not_allowed_do_auth
from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID, KEY_HASS_USER
from homeassistant.helpers.storage import Store

from .const import DOMAIN

MAX_PAIRINGS = 256
MAX_CONTROL_BYTES = 4096
MAX_WRITE_BYTES = 512 * 1024
PAIRING_LIFETIME = 90 * 24 * 60 * 60
MANAGER_KEY = "webhook_pairings"


class PairingError(Exception):
    """Only fixed public error codes; never secret or user-supplied values."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        length = len(value.encode())
    except UnicodeEncodeError:
        return False
    return (0 < length <= 256
            and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _registration(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value) is not None


def _secret(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


async def _body(request: Any, limit: int) -> tuple[bytes, dict[str, Any]]:
    if request.method != "POST":
        raise PairingError("method_not_allowed", 405)
    if request.content_type != "application/json":
        raise PairingError("json_required", 415)
    if request.content_length is not None and request.content_length > limit:
        raise PairingError("payload_too_large", 413)
    body = bytearray()
    try:
        async def collect():
            async for chunk in request.content.iter_chunked(4096):
                if len(body) + len(chunk) > limit:
                    raise PairingError("payload_too_large", 413)
                body.extend(chunk)
        await asyncio.wait_for(collect(), timeout=10)
    except asyncio.TimeoutError:
        raise PairingError("request_timeout", 408) from None
    except web.HTTPRequestEntityTooLarge:
        raise PairingError("payload_too_large", 413) from None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise PairingError("invalid_json") from None
    if not isinstance(payload, dict):
        raise PairingError("invalid_payload")
    return bytes(body), payload


async def _join(work: Any) -> tuple[Any, bool]:
    """Do not release the gate while a cancelled client's save/write still runs."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


class _PushRequest:
    def __init__(self, hass: Any, body: bytes, user: Any) -> None:
        self.app = {KEY_HASS: hass}
        self.content_length = len(body)
        self._body = body
        self._user = user

    async def read(self) -> bytes:
        return self._body

    def get(self, key: Any, default: Any = None) -> Any:
        return self._user if key == KEY_HASS_USER or key == "hass_user" else default


class HalthyWebhookManager:
    """One joined gate for pairing, webhook writes and profile lifecycle changes."""

    def __init__(self, hass: Any, push: Any, *, store: Any = None, now: Any = time.time) -> None:
        self.hass = hass
        self.push = push
        self.store = store if store is not None else Store(hass, 1, f"{DOMAIN}_webhook_pairings")
        self.now = now
        self.lock = asyncio.Lock()
        self.records: dict[str, dict[str, Any]] = {}
        self.registered: set[str] = set()
        self.available = False

    async def load(self) -> None:
        payload = await self.store.async_load()
        if payload is None:
            self.available = True
            return
        if (not isinstance(payload, dict) or type(payload.get("version")) is not int or payload.get("version") != 1
                or set(payload) != {"version", "pairings"}
                or not isinstance(payload["pairings"], dict)
                or len(payload["pairings"]) > MAX_PAIRINGS):
            return
        records = payload["pairings"]
        secrets: set[str] = set()
        devices: set[tuple[str, str]] = set()
        for key, record in records.items():
            if not _registration(key) or not self._valid_record(record):
                return
            secret = record["webhook_id"]
            if secret in secrets:
                return
            secrets.add(secret)
            device = (record["entry_id"], record["device_id"])
            if not record["revoked"]:
                if device in devices:
                    return
                devices.add(device)
        self.records = deepcopy(records)
        self.available = True

    @staticmethod
    def _valid_record(record: Any) -> bool:
        if not isinstance(record, dict) or set(record) != {
            "entry_id", "username", "device_id", "owner_id", "grant_id", "webhook_id", "expires_at", "revoked"
        }:
            return False
        return (all(_text(record[key]) for key in ("entry_id", "username", "device_id", "owner_id", "grant_id"))
                and re.fullmatch(r"[a-z0-9_]+", record["username"]) is not None
                and _secret(record["webhook_id"])
                and type(record["revoked"]) is bool
                and type(record["expires_at"]) in (int, float)
                and 0 < record["expires_at"] <= 253402300799
                and math.isfinite(record["expires_at"]))

    def _profile(self, entry_id: str, username: str, owner_id: str) -> Any:
        entries = self.hass.data.get(DOMAIN, {}).get("entries", {})
        runtime = entries.get(entry_id)
        if (runtime is None or runtime.configured_username != username
                or runtime.owner_user_id != owner_id
                or sum(item.configured_username == username for item in entries.values()) != 1):
            raise PairingError("profile_not_owned_or_ambiguous", 403)
        return runtime

    def _grant_user(self, grant_id: str, owner_id: str, request: Any) -> Any:
        grant = self.hass.auth.async_get_refresh_token(grant_id)
        if grant is None or grant.user.id != owner_id:
            raise PairingError("grant_not_valid", 403)
        expiration = getattr(grant, "expire_at", None)
        if expiration is not None and expiration <= self.now():
            raise PairingError("grant_not_valid", 403)
        if async_user_not_allowed_do_auth(self.hass, grant.user, request):
            raise PairingError("grant_not_valid", 403)
        try:
            self.hass.auth.async_validate_refresh_token(grant, request.remote)
        except Exception:
            raise PairingError("grant_not_valid", 403) from None
        return grant.user

    def _register(self, secret: str) -> None:
        if secret in self.registered:
            return
        try:
            webhook.async_register(self.hass, DOMAIN, "Halthy write-only upload", secret,
                                   self.handle, local_only=False, allowed_methods=("POST",))
        except ValueError:
            raise PairingError("registration_conflict", 409) from None
        self.registered.add(secret)

    def _unregister(self, secret: str) -> None:
        if secret in self.registered:
            webhook.async_unregister(self.hass, secret)
            self.registered.remove(secret)

    def restore_entry(self, entry_id: str) -> None:
        """Caller holds lock and has installed the current runtime."""
        if not self.available:
            return
        for record in self.records.values():
            if record["entry_id"] != entry_id or record["revoked"] or record["expires_at"] <= self.now():
                continue
            try:
                self._profile(entry_id, record["username"], record["owner_id"])
                self._register(record["webhook_id"])
            except PairingError:
                continue

    def suspend_entry(self, entry_id: str) -> None:
        """Caller holds lock until runtime is removed; no pairing is deleted."""
        for record in self.records.values():
            if record["entry_id"] == entry_id:
                self._unregister(record["webhook_id"])

    async def control(self, request: Any) -> Any:
        try:
            _, payload = await _body(request, MAX_CONTROL_BYTES)
            async with self.lock:
                return await self._control_locked(request, payload)
        except PairingError as error:
            return web.json_response({"error": error.code}, status=error.status)

    async def _control_locked(self, request: Any, payload: dict[str, Any]) -> Any:
        if not self.available:
            raise PairingError("pairing_storage_unavailable", 503)
        action = payload.get("action")
        keys = {"version", "action", "registration_id", "entry_id", "username", "device_id"}
        if action == "pair":
            keys.add("webhook_id")
        elif action == "lookup":
            keys.remove("registration_id")
        if action not in ("pair", "revoke", "lookup") or set(payload) != keys or type(payload["version"]) is not int or payload["version"] != 1:
            raise PairingError("invalid_pairing_request")
        registration = payload.get("registration_id")
        if ((action != "lookup" and not _registration(registration))
                or not all(_text(payload[key]) for key in ("entry_id", "username", "device_id"))):
            raise PairingError("invalid_pairing_request")
        user = request.get(KEY_HASS_USER)
        owner = getattr(user, "id", None)
        grant_id = request.get(KEY_HASS_REFRESH_TOKEN_ID)
        if not _text(owner) or not _text(grant_id):
            raise PairingError("missing_authenticated_grant", 403)
        self._grant_user(grant_id, owner, request)
        self._profile(payload["entry_id"], payload["username"], owner)
        if action == "lookup":
            # Recovery is read-only and exact-scope. A renewed grant can discover
            # an old registration, but never obtains its secret or issuing grant.
            pairings = [{"registration_id": key, "expires_at": record["expires_at"]}
                        for key, record in sorted(self.records.items())
                        if not record["revoked"] and record["owner_id"] == owner
                        and all(record[field] == payload[field] for field in ("entry_id", "username", "device_id"))]
            if len(pairings) > 1:
                raise PairingError("pairing_storage_unavailable", 503)
            return web.json_response({"ok": True, "version": 1, "account_id": owner,
                                      "entry_id": payload["entry_id"], "username": payload["username"],
                                      "device_id": payload["device_id"], "pairings": pairings},
                                     headers={"Cache-Control": "no-store"})
        existing = self.records.get(registration)
        scope = {key: payload[key] for key in ("entry_id", "username", "device_id")}
        scope["owner_id"] = owner
        if existing is not None and any(existing[key] != value for key, value in scope.items()):
            raise PairingError("registration_conflict", 409)
        if action == "revoke":
            if existing is None:
                raise PairingError("registration_not_found", 404)
            if existing["revoked"]:
                return self._revoke_reply(registration, existing)
            updated = deepcopy(self.records)
            updated[registration]["revoked"] = True
            _, cancelled = await _join(self.store.async_save({"version": 1, "pairings": updated}))
            self.records = updated
            self._unregister(existing["webhook_id"])
            if cancelled:
                raise asyncio.CancelledError
            return self._revoke_reply(registration, existing)
        secret = payload["webhook_id"]
        if not _secret(secret):
            raise PairingError("invalid_pairing_request")
        if existing is not None:
            if existing["webhook_id"] != secret or existing["grant_id"] != grant_id:
                raise PairingError("registration_conflict", 409)
            if existing["revoked"] or existing["expires_at"] <= self.now():
                raise PairingError("registration_retired", 409)
            self._register(secret)
            return self._pair_reply(registration, existing)
        if len(self.records) >= MAX_PAIRINGS:
            raise PairingError("pairing_limit_reached", 409)
        if any(record["webhook_id"] == secret for record in self.records.values()):
            raise PairingError("registration_conflict", 409)
        record = {**scope, "grant_id": grant_id, "webhook_id": secret,
                  "expires_at": self.now() + PAIRING_LIFETIME, "revoked": False}
        updated = deepcopy(self.records)
        for previous in updated.values():
            if previous["entry_id"] == record["entry_id"] and previous["device_id"] == record["device_id"]:
                previous["revoked"] = True
        updated[registration] = record
        self._register(secret)
        try:
            _, cancelled = await _join(self.store.async_save({"version": 1, "pairings": updated}))
        except BaseException:
            self._unregister(secret)
            raise
        self.records = updated
        for previous in updated.values():
            if previous["revoked"]:
                self._unregister(previous["webhook_id"])
        if cancelled:
            raise asyncio.CancelledError
        return self._pair_reply(registration, record)

    @staticmethod
    def _revoke_reply(registration: str, record: dict[str, Any]) -> Any:
        return web.json_response({"ok": True, "version": 1, "registration_id": registration,
                                  "account_id": record["owner_id"], "entry_id": record["entry_id"],
                                  "username": record["username"], "device_id": record["device_id"], "revoked": True})

    @staticmethod
    def _pair_reply(registration: str, record: dict[str, Any]) -> Any:
        return web.json_response({"ok": True, "version": 1, "registration_id": registration,
                                  "account_id": record["owner_id"], "revoked": False,
                                  "entry_id": record["entry_id"], "username": record["username"],
                                  "device_id": record["device_id"], "expires_at": record["expires_at"]})

    async def handle(self, hass: Any, secret: str, request: Any) -> Any:
        try:
            body, payload = await _body(request, MAX_WRITE_BYTES)
            async with self.lock:
                record = next((item for item in self.records.values() if item["webhook_id"] == secret), None)
                if (not self.available or hass is not self.hass or secret not in self.registered
                        or record is None or record["revoked"] or record["expires_at"] <= self.now()):
                    raise PairingError("pairing_not_active", 403)
                self._profile(record["entry_id"], record["username"], record["owner_id"])
                user = self._grant_user(record["grant_id"], record["owner_id"], request)
                allowed = {"username", "device_id", "sensors", "images", "workouts", "selected_metric_keys", "prune_unselected_metrics", "archive_only", "sync_completed"}
                if (set(payload) - allowed or payload.get("username") != record["username"]
                        or payload.get("device_id") != record["device_id"]
                        or ("prune_unselected_metrics" in payload and payload["prune_unselected_metrics"] is not False)):
                    raise PairingError("write_scope_mismatch", 403)
                result, cancelled = await _join(self.push(_PushRequest(self.hass, body, user)))
                if cancelled:
                    raise asyncio.CancelledError
                return result
        except PairingError as error:
            return web.json_response({"error": error.code}, status=error.status)
        except Exception:
            # HA core otherwise converts handler exceptions to empty HTTP 200.
            return web.json_response({"error": "webhook_write_failed"}, status=503)


class HalthyWebhookPairingView(HomeAssistantView):
    url = "/api/halthy/webhook"
    name = "api:halthy:webhook"
    requires_auth = True

    async def post(self, request: Any) -> Any:
        manager = request.app[KEY_HASS].data.get(DOMAIN, {}).get(MANAGER_KEY)
        if manager is None:
            return web.json_response({"error": "pairing_storage_unavailable"}, status=503)
        try:
            return await manager.control(request)
        except Exception:
            return web.json_response({"error": "pairing_save_failed"}, status=503)
