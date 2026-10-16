"""Synthetic pairing/lifecycle/bridge contracts, not live HA middleware tests."""
import asyncio
import base64
from copy import deepcopy
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from test_statistics import BRIDGE

MODULE = sys.modules[BRIDGE.HalthyWebhookManager.__module__]


class FakeStore:
    def __init__(self, payload=None):
        self.payload = deepcopy(payload)
        self.fail = False
        self.saved = 0
        self.entered = None
        self.release = None

    async def async_load(self):
        return deepcopy(self.payload)

    async def async_save(self, payload):
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.fail:
            raise OSError("secret exception must not reach client")
        self.payload = deepcopy(payload)
        self.saved += 1


class Request:
    def __init__(self, hass, payload, *, user=None, grant="grant", chunks=None, length=True):
        self.app = {BRIDGE.KEY_HASS: hass}
        self.raw = json.dumps(payload).encode()
        self.content_length = len(self.raw) if length else None
        self.content_type = "application/json"
        self.method = "POST"
        self.remote = "127.0.0.1"
        self.values = {MODULE.KEY_HASS_USER: user, MODULE.KEY_HASS_REFRESH_TOKEN_ID: grant}
        self.chunks = chunks if chunks is not None else [self.raw]
        self.content = self
        self.consumed = 0

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    def get(self, key, default=None):
        return self.values.get(key, default)


class PairedWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.time = 1000.0
        self.user = types.SimpleNamespace(id="owner", is_active=True, local_only=False)
        self.grant = types.SimpleNamespace(id="grant", user=self.user, expire_at=None)
        self.grants = {"grant": self.grant}
        self.runtime = BRIDGE.IntegrationRuntime(configured_username="tester", app_username="tester",
            display_name="Tester", owner_user_id="owner", statistics_enabled=False)
        self.hass = types.SimpleNamespace(data={BRIDGE.DOMAIN: {"entries": {"entry": self.runtime}}},
            auth=types.SimpleNamespace(async_get_refresh_token=self.grants.get,
                async_validate_refresh_token=lambda grant, remote: None))
        self.store = FakeStore()
        self.push = AsyncMock(return_value={"args": ({"ok": True, "accepted": 1},), "kwargs": {}})
        self.manager = MODULE.HalthyWebhookManager(self.hass, self.push, store=self.store, now=lambda: self.time)
        self.hass.data[BRIDGE.DOMAIN][MODULE.MANAGER_KEY] = self.manager
        await self.manager.load()
        self.registration = str(uuid4())
        self.secret = "a" * 64
        self.payload = {"version": 1, "action": "pair", "registration_id": self.registration,
            "entry_id": "entry", "username": "tester", "device_id": "phone", "webhook_id": self.secret}
        self.write = {"username": "tester", "device_id": "phone", "sensors": [
            {"key": "heart_rate", "state": 90, "unit": "count/min"}]}

    def request(self, payload, **kwargs):
        kwargs.setdefault("user", self.user)
        return Request(self.hass, payload, **kwargs)

    async def pair(self, payload=None, **kwargs):
        return await self.manager.control(self.request(payload or self.payload, **kwargs))

    async def upload(self, payload=None, request=None):
        return await self.manager.handle(self.hass, self.secret, request or self.request(payload or self.write))

    def assert_error(self, reply, status, code):
        self.assertEqual(reply["kwargs"].get("status"), status)
        self.assertEqual(reply["args"][0], {"error": code})

    def revoke_payload(self):
        return {key: value for key, value in {**self.payload, "action": "revoke"}.items() if key != "webhook_id"}

    def lookup_payload(self):
        return {key: value for key, value in {**self.payload, "action": "lookup"}.items()
                if key not in ("webhook_id", "registration_id")}

    async def test_lookup_is_exact_scope_read_only_and_never_returns_credentials(self):
        await self.pair()
        before = deepcopy(self.manager.records)
        response = await self.pair(self.lookup_payload())
        self.assertEqual(response["kwargs"]["headers"], {"Cache-Control": "no-store"})
        reply = response["args"][0]
        self.assertEqual(reply, {"ok": True, "version": 1, "account_id": "owner", "entry_id": "entry",
                                "username": "tester", "device_id": "phone", "pairings": [
                                    {"registration_id": self.registration, "expires_at": 1000 + MODULE.PAIRING_LIFETIME}]})
        self.assertNotIn(self.secret, json.dumps(reply))
        self.assertNotIn("grant_id", json.dumps(reply))
        self.assertEqual(self.manager.records, before)
        self.assertEqual(self.store.saved, 1)
        self.push.assert_not_called()
        other_device = (await self.pair({**self.lookup_payload(), "device_id": "other"}))["args"][0]
        self.assertEqual(other_device["pairings"], [])

    async def test_lookup_after_grant_renewal_finds_expired_pairing_for_explicit_removal(self):
        await self.pair()
        self.grants.pop("grant")
        self.grants["renewed"] = types.SimpleNamespace(id="renewed", user=self.user, expire_at=None)
        self.time += MODULE.PAIRING_LIFETIME + 1
        reply = (await self.pair(self.lookup_payload(), grant="renewed"))["args"][0]
        self.assertEqual(reply["pairings"][0]["registration_id"], self.registration)
        self.assertFalse(self.manager.records[self.registration]["revoked"])
        self.assertEqual(self.store.saved, 1)
        await self.pair(self.revoke_payload(), grant="renewed")
        reply = (await self.pair(self.lookup_payload(), grant="renewed"))["args"][0]
        self.assertEqual(reply["pairings"], [])

    async def test_lookup_rejects_unauthorized_foreign_and_ambiguous_profiles(self):
        await self.pair()
        for request in ({"user": None}, {"grant": None}, {"grant": "unknown"}):
            reply = await self.pair(self.lookup_payload(), **request)
            self.assertEqual(reply["kwargs"]["status"], 403)
            self.assertNotIn(self.registration, json.dumps(reply))
        self.grants["foreign"] = types.SimpleNamespace(id="foreign", user=types.SimpleNamespace(
            id="foreign", is_active=True, local_only=False), expire_at=None)
        reply = await self.pair(self.lookup_payload(), user=self.grants["foreign"].user, grant="foreign")
        self.assert_error(reply, 403, "profile_not_owned_or_ambiguous")
        self.hass.data[BRIDGE.DOMAIN]["entries"]["second"] = self.runtime
        self.assert_error(await self.pair(self.lookup_payload()), 403, "profile_not_owned_or_ambiguous")
        self.assertEqual(self.store.saved, 1)

    async def test_lookup_strict_schema_and_no_secret_endpoint_access(self):
        await self.pair()
        for extra in ({"registration_id": self.registration}, {"webhook_id": self.secret},
                      {"version": True}, {"device_id": ""}, {"entry_id": "wrong"}):
            reply = await self.pair({**self.lookup_payload(), **extra})
            self.assertIn(reply["kwargs"]["status"], (400, 403))
            self.assertNotIn(self.secret, json.dumps(reply))
        self.assert_error(await self.upload(self.lookup_payload()), 403, "write_scope_mismatch")
        self.assertEqual(self.store.saved, 1)

    async def test_lookup_storage_unavailable_or_duplicate_state_fails_closed(self):
        await self.pair()
        duplicate = str(uuid4())
        self.manager.records[duplicate] = {**self.manager.records[self.registration], "webhook_id": "b" * 64}
        self.assert_error(await self.pair(self.lookup_payload()), 503, "pairing_storage_unavailable")
        self.manager.available = False
        self.assert_error(await self.pair(self.lookup_payload()), 503, "pairing_storage_unavailable")
        self.assertEqual(self.store.saved, 1)

    async def test_lookup_survives_storage_reload_without_registering_or_mutating_hooks(self):
        await self.pair()
        restored = MODULE.HalthyWebhookManager(self.hass, self.push, store=self.store, now=lambda: self.time)
        await restored.load()
        reply = (await restored.control(self.request(self.lookup_payload())))["args"][0]
        self.assertEqual(reply["pairings"][0]["registration_id"], self.registration)
        self.assertEqual(restored.registered, set())
        self.assertEqual(self.store.saved, 1)

    async def test_authenticated_registration_and_post_only_scope(self):
        self.assertTrue(MODULE.HalthyWebhookPairingView.requires_auth)
        reply = await self.pair()
        self.assertTrue(reply["args"][0]["ok"])
        self.assertEqual(self.store.saved, 1)
        self.assertNotIn(self.secret, json.dumps(reply))

    async def test_archive_only_hook_uses_real_archive_handler_without_current_map_effects(self):
        await self.pair()
        image = {"key": "workout", "name": "Workout", "content_type": "image/jpeg",
            "image_base64": base64.b64encode(b"\xff\xd8\xff\x00").decode(), "attributes": {
                "workout_uuid": str(uuid4()), "workout_type": "Walking", "workout_start": "2026-10-06T08:00:00Z",
                "workout_end": "2026-10-06T10:00:00Z", "workout_duration_s": 3600}}
        payload = {"username": "tester", "device_id": "phone", "archive_only": True,
            "prune_unselected_metrics": False, "workouts": [], "images": [image]}
        self.manager.push = BRIDGE.HalthyPushView().post
        writer = AsyncMock(return_value=image["attributes"])
        with patch.object(BRIDGE, "_async_archive_workout_image", new=writer):
            reply = await self.upload(payload)
        self.assertTrue(reply["args"][0]["archive_only"])
        writer.assert_awaited_once()
        self.assertIs(writer.call_args.kwargs["require_success"], True)
        self.assertEqual(self.runtime.images, {})
        self.assertEqual(self.runtime.workouts, {})
        handler, options = self.hass.data["test_webhooks"][self.secret]
        self.assertEqual(handler, self.manager.handle)
        self.assertEqual(options, {"local_only": False, "allowed_methods": ("POST",)})
        self.assertEqual(self.manager.records[self.registration]["grant_id"], "grant")

    async def test_pair_retry_does_not_extend_lifetime_or_save_again(self):
        first = await self.pair()
        self.time += 60
        second = await self.pair()
        self.assertEqual(first, second)
        self.assertEqual(self.store.saved, 1)

    async def test_missing_authenticated_user_or_grant_cannot_pair(self):
        for changes in ({"user": None}, {"grant": None}, {"grant": "unknown"}):
            reply = await self.pair(**changes)
            self.assertEqual(reply["kwargs"]["status"], 403)
        self.assertEqual(self.store.saved, 0)

    async def test_ownerless_foreign_or_ambiguous_profile_cannot_pair(self):
        for owner in (None, "someone_else"):
            self.runtime.owner_user_id = owner
            self.assert_error(await self.pair(), 403, "profile_not_owned_or_ambiguous")
        self.runtime.owner_user_id = "owner"
        self.hass.data[BRIDGE.DOMAIN]["entries"]["other"] = self.runtime
        self.assert_error(await self.pair(), 403, "profile_not_owned_or_ambiguous")
        self.assertEqual(self.store.saved, 0)

    async def test_pairing_requires_exact_profile_entry_and_username(self):
        for values in ({"entry_id": "wrong"}, {"username": "Tester"}, {"username": "tester "}):
            reply = await self.pair({**self.payload, **values})
            self.assertIn(reply["kwargs"]["status"], (400, 403))
        self.assertEqual(self.manager.records, {})

    async def test_strict_schema_and_entropy_format(self):
        for values in ({"extra": True}, {"version": True}, {"webhook_id": "short"},
                       {"registration_id": "not-uuid"}, {"device_id": "a\nb"}, {"device_id": "x" * 257}):
            self.assert_error(await self.pair({**self.payload, **values}), 400, "invalid_pairing_request")
        self.assertEqual(self.store.saved, 0)

    async def test_same_registration_cannot_change_secret_or_grant_or_device(self):
        await self.pair()
        self.grants["second"] = types.SimpleNamespace(id="second", user=self.user, expire_at=None)
        for values, request_changes in (({"webhook_id": "b" * 64}, {}),
            ({"device_id": "other"}, {}), ({}, {"grant": "second"})):
            self.assert_error(await self.pair({**self.payload, **values}, **request_changes), 409, "registration_conflict")
        self.assertEqual(self.store.saved, 1)

    async def test_rotation_revokes_old_hook_and_delayed_retry_cannot_resurrect_it(self):
        await self.pair()
        second = {**self.payload, "registration_id": str(uuid4()), "webhook_id": "b" * 64}
        await self.pair(second)
        self.assertNotIn(self.secret, self.hass.data["test_webhooks"])
        self.assertIn("b" * 64, self.hass.data["test_webhooks"])
        self.assert_error(await self.pair(), 409, "registration_retired")
        self.assert_error(await self.upload(), 403, "pairing_not_active")

    async def test_secret_cannot_be_reused_for_another_registration(self):
        await self.pair()
        self.assert_error(await self.pair({**self.payload, "registration_id": str(uuid4()), "device_id": "other"}),
                          409, "registration_conflict")

    async def test_registry_collision_is_not_saved_or_unregistered(self):
        foreign = (object(), {})
        self.hass.data["test_webhooks"] = {self.secret: foreign}
        self.assert_error(await self.pair(), 409, "registration_conflict")
        self.assertEqual(self.store.saved, 0)
        self.assertIs(self.hass.data["test_webhooks"][self.secret], foreign)

    async def test_failed_pair_save_leaves_no_capability_or_record(self):
        self.store.fail = True
        reply = await MODULE.HalthyWebhookPairingView().post(self.request(self.payload))
        self.assert_error(reply, 503, "pairing_save_failed")
        self.assertEqual(self.manager.records, {})
        self.assertNotIn(self.secret, self.hass.data["test_webhooks"])

    async def test_failed_rotation_save_keeps_old_pairing(self):
        await self.pair()
        self.store.fail = True
        request = self.request({**self.payload, "registration_id": str(uuid4()), "webhook_id": "b" * 64})
        self.assert_error(await MODULE.HalthyWebhookPairingView().post(request), 503, "pairing_save_failed")
        self.assertEqual(self.manager.registered, {self.secret})
        self.assertFalse(self.manager.records[self.registration]["revoked"])

    async def test_revoke_is_durable_idempotent_and_blocks_repair(self):
        await self.pair()
        first = await self.pair(self.revoke_payload())
        second = await self.pair(self.revoke_payload())
        self.assertEqual(first, second)
        self.assertEqual(self.store.saved, 2)
        self.assertTrue(self.store.payload["pairings"][self.registration]["revoked"])
        self.assertEqual(self.manager.registered, set())
        self.assert_error(await self.pair(), 409, "registration_retired")

    async def test_unknown_revoke_is_not_falsely_acknowledged(self):
        self.assert_error(await self.pair(self.revoke_payload()), 404, "registration_not_found")

    async def test_failed_revoke_does_not_acknowledge_or_disable_existing_hook(self):
        await self.pair()
        self.store.fail = True
        self.assert_error(await MODULE.HalthyWebhookPairingView().post(self.request(self.revoke_payload())),
                          503, "pairing_save_failed")
        self.assertEqual(self.manager.registered, {self.secret})
        self.assertFalse(self.manager.records[self.registration]["revoked"])

    async def test_write_delegates_original_bytes_and_server_resolved_user(self):
        await self.pair()
        request = self.request(self.write, user=types.SimpleNamespace(id="intruder"))
        reply = await self.upload(request=request)
        self.assertEqual(reply, self.push.return_value)
        adapted = self.push.call_args.args[0]
        self.assertEqual(await adapted.read(), request.raw)
        self.assertIs(adapted.get("hass_user"), self.user)

    async def test_restricted_controls_and_routing_do_not_reach_push(self):
        await self.pair()
        for values in ({"username": "someone_else"}, {"device_id": "other"},
            {"connection_test": True}, {"prune_unselected_metrics": True},
            {"prune_unselected_metrics": 0}, {"command": {}}, {"action": "import"}):
            self.assert_error(await self.upload({**self.write, **values}), 403, "write_scope_mismatch")
        self.push.assert_not_called()

    async def test_current_sync_completion_metadata_is_allowed_without_pruning(self):
        await self.pair()
        reply = await self.upload({**self.write, "sync_completed": True})
        self.assertEqual(reply, self.push.return_value)
        body = json.loads(await self.push.call_args.args[0].read())
        self.assertIs(body["sync_completed"], True)
        self.assertIsNot(body.get("prune_unselected_metrics"), True)

    async def test_grant_revocation_disabled_user_expiry_and_provider_revocation_block_writes(self):
        await self.pair()
        self.grants.clear()
        self.assert_error(await self.upload(), 403, "grant_not_valid")
        self.grants["grant"] = self.grant
        self.user.is_active = False
        self.assert_error(await self.upload(), 403, "grant_not_valid")
        self.user.is_active = True
        self.grant.expire_at = self.time
        self.assert_error(await self.upload(), 403, "grant_not_valid")
        self.grant.expire_at = None
        self.hass.auth.async_validate_refresh_token = lambda *args: (_ for _ in ()).throw(ValueError("revoked"))
        self.assert_error(await self.upload(), 403, "grant_not_valid")
        self.push.assert_not_called()

    async def test_local_only_ha_user_is_not_exposed_remotely(self):
        await self.pair()
        self.user.local_only = True
        request = self.request(self.write)
        request.remote = "1.2.3.4"
        self.assert_error(await self.upload(request=request), 403, "grant_not_valid")
        self.push.assert_not_called()

    async def test_expired_capability_needs_new_authenticated_pairing(self):
        await self.pair()
        self.time += MODULE.PAIRING_LIFETIME
        self.assert_error(await self.upload(), 403, "pairing_not_active")
        self.assert_error(await self.pair(), 409, "registration_retired")
        self.push.assert_not_called()

    async def test_changed_owner_username_deleted_or_duplicate_entry_blocks_writes(self):
        await self.pair()
        self.runtime.owner_user_id = "other"
        self.assert_error(await self.upload(), 403, "profile_not_owned_or_ambiguous")
        self.runtime.owner_user_id = "owner"
        self.runtime.configured_username = "renamed"
        self.assert_error(await self.upload(), 403, "profile_not_owned_or_ambiguous")
        self.runtime.configured_username = "tester"
        self.hass.data[BRIDGE.DOMAIN]["entries"]["second"] = self.runtime
        self.assert_error(await self.upload(), 403, "profile_not_owned_or_ambiguous")
        self.hass.data[BRIDGE.DOMAIN]["entries"].clear()
        self.assert_error(await self.upload(), 403, "profile_not_owned_or_ambiguous")
        self.push.assert_not_called()

    async def test_suspend_reload_restores_only_current_owned_runtime(self):
        await self.pair()
        async with self.manager.lock:
            self.manager.suspend_entry("entry")
        self.assert_error(await self.upload(), 403, "pairing_not_active")
        fresh = MODULE.HalthyWebhookManager(self.hass, self.push, store=self.store, now=lambda: self.time)
        await fresh.load()
        fresh.restore_entry("entry")
        self.assertEqual(fresh.registered, {self.secret})
        fresh.suspend_entry("entry")
        self.runtime.owner_user_id = "other"
        fresh.restore_entry("entry")
        self.assertEqual(fresh.registered, set())

    async def test_invalid_storage_fails_closed_without_resetting_or_legacy_mutation(self):
        await self.pair()
        snapshot = deepcopy(self.store.payload)
        malformed = [[], {"version": 2, "pairings": {}}, {"version": True, "pairings": {}},
                     {"version": 1, "pairings": {"bad": {}}}]
        for mutate in (lambda r: r.update(expires_at=float("nan")), lambda r: r.update(revoked=0),
                       lambda r: r.update(webhook_id="short"), lambda r: r.update(extra="secret"),
                       lambda r: r.update(expires_at=10 ** 1000), lambda r: r.update(device_id="\ud800")):
            payload = deepcopy(snapshot)
            mutate(payload["pairings"][self.registration])
            malformed.append(payload)
        for payload in malformed:
            store = FakeStore(payload)
            fresh = MODULE.HalthyWebhookManager(self.hass, self.push, store=store)
            await fresh.load()
            self.assertFalse(fresh.available)
            self.assert_error(await fresh.control(self.request(self.payload)), 503, "pairing_storage_unavailable")
            self.assertEqual(store.saved, 0)
            self.assertEqual(self.runtime.sensors, {})

    async def test_pairing_limit_includes_retired_records(self):
        await self.pair()
        await self.pair(self.revoke_payload())
        with patch.object(MODULE, "MAX_PAIRINGS", 1):
            self.assert_error(await self.pair({**self.payload, "registration_id": str(uuid4()), "webhook_id": "b" * 64}),
                              409, "pairing_limit_reached")

    async def test_invalid_method_content_type_and_chunked_bounds(self):
        await self.pair()
        request = self.request(self.write)
        request.method = "GET"
        self.assert_error(await self.upload(request=request), 405, "method_not_allowed")
        request.method = "POST"
        request.content_type = "text/plain"
        self.assert_error(await self.upload(request=request), 415, "json_required")
        request = self.request(self.write, chunks=[b"x" * 4096] * 3, length=False)
        with patch.object(MODULE, "MAX_WRITE_BYTES", 4096):
            self.assert_error(await self.upload(request=request), 413, "payload_too_large")
        self.assertEqual(request.consumed, 2)
        self.push.assert_not_called()

    async def test_invalid_json_and_object_are_not_written(self):
        await self.pair()
        for chunks in ([b"{"], [b"\xff"], [b"[]"]):
            reply = await self.upload(request=self.request(self.write, chunks=chunks, length=False))
            self.assertEqual(reply["kwargs"]["status"], 400)
        self.push.assert_not_called()

    async def test_push_exception_returns_explicit_failure_not_empty_http_200(self):
        await self.pair()
        self.push.side_effect = ValueError(self.secret)
        self.assert_error(await self.upload(), 503, "webhook_write_failed")

    async def test_pairing_cancellation_joins_save_and_publishes_for_retry(self):
        self.store.entered, self.store.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.pair())
        await self.store.entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.manager.lock.locked())
        self.store.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.manager.lock.locked())
        self.assertEqual(self.manager.records, self.store.payload["pairings"])
        self.assertTrue((await self.pair())["args"][0]["ok"])
        self.assertEqual(self.store.saved, 1)

    async def test_cancelled_write_holds_gate_until_write_finishes_then_revoke_runs(self):
        await self.pair()
        entered, release = asyncio.Event(), asyncio.Event()
        async def write(request):
            entered.set()
            await release.wait()
            return self.push.return_value
        self.push.side_effect = write
        task = asyncio.create_task(self.upload())
        await entered.wait()
        task.cancel()
        revoke = asyncio.create_task(self.pair(self.revoke_payload()))
        await asyncio.sleep(0)
        self.assertTrue(self.manager.lock.locked())
        self.assertFalse(revoke.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue((await revoke)["args"][0]["revoked"])
        self.assert_error(await self.upload(), 403, "pairing_not_active")

    async def test_write_revalidates_pairing_after_waiting_for_gate(self):
        await self.pair()
        async with self.manager.lock:
            task = asyncio.create_task(self.upload())
            await asyncio.sleep(0)
            self.manager.suspend_entry("entry")
        self.assert_error(await task, 403, "pairing_not_active")
        self.push.assert_not_called()

    async def test_real_bridge_validation_ack_duplicate_and_latest_timestamp_are_reused(self):
        await self.pair()
        self.manager.push = BRIDGE.HalthyPushView().post
        async def execute(function, *args):
            return function(*args)
        self.hass.async_add_executor_job = execute
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.hass.config = types.SimpleNamespace(units=types.SimpleNamespace(temperature_unit="\u00b0C"),
            path=lambda relative: directory.name + "/" + relative)
        registry = types.SimpleNamespace(async_get_entity_id=lambda *args: None)
        payload = deepcopy(self.write)
        payload["sensors"][0]["attributes"] = {"measurement_timestamp": "2026-10-07T08:00:00Z"}
        with patch.object(BRIDGE, "_schedule_store_save"), \
             patch.object(BRIDGE, "_async_emit_activity_log_entries", new=AsyncMock()), \
             patch.object(BRIDGE.er, "async_get", return_value=registry):
            first = (await self.upload(payload))["args"][0]
            self.assertTrue(first["ok"])
            self.assertGreaterEqual(first["accepted"], 1)
            duplicate = (await self.upload(payload))["args"][0]
            self.assertGreaterEqual(duplicate["duplicates"], 1)
            payload["sensors"][0]["attributes"]["measurement_timestamp"] = "2026-10-06T08:00:00Z"
            payload["sensors"][0]["state"] = 80
            older = (await self.upload(payload))["args"][0]
            self.assertGreaterEqual(older["ignored_older"], 1)
            self.assertEqual(next(sensor.state for sensor in self.runtime.sensors.values() if sensor.metric_key == "heart_rate"), 90)
            invalid = await self.upload({**self.write, "sensors": "not-a-list"})
            self.assert_error(invalid, 400, "missing_sensors")

    async def test_profile_unload_joins_in_flight_write(self):
        await self.pair()
        entered, release = asyncio.Event(), asyncio.Event()
        async def write(request):
            entered.set()
            await release.wait()
            return self.push.return_value
        self.push.side_effect = write
        self.hass.config_entries = types.SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
        task = asyncio.create_task(self.upload())
        await entered.wait()
        entry = types.SimpleNamespace(entry_id="entry")
        with patch.object(BRIDGE, "_schedule_store_save"):
            unload = asyncio.create_task(BRIDGE.async_unload_entry(self.hass, entry))
            await asyncio.sleep(0)
            self.hass.config_entries.async_unload_platforms.assert_not_called()
            release.set()
            await task
            self.assertTrue(await unload)
        self.assertEqual(self.manager.registered, set())
        self.assertEqual(self.hass.data[BRIDGE.DOMAIN]["entries"], {})

    async def test_revocation_cancellation_is_durable_before_gate_release(self):
        await self.pair()
        self.store.entered, self.store.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.pair(self.revoke_payload()))
        await self.store.entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.manager.lock.locked())
        self.store.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.manager.records[self.registration]["revoked"])
        self.assertEqual(self.manager.records, self.store.payload["pairings"])
        self.assertEqual(self.manager.registered, set())

    async def test_new_authenticated_grant_can_revoke_but_not_rebind_old_pairing(self):
        await self.pair()
        self.grants["second"] = types.SimpleNamespace(id="second", user=self.user, expire_at=None)
        self.assert_error(await self.pair(grant="second"), 409, "registration_conflict")
        reply = await self.pair(self.revoke_payload(), grant="second")
        self.assertTrue(reply["args"][0]["revoked"])
        self.assertEqual(reply["args"][0]["account_id"], "owner")

    async def test_failed_unload_preserves_runtime_and_hook(self):
        await self.pair()
        self.hass.config_entries = types.SimpleNamespace(async_unload_platforms=AsyncMock(return_value=False))
        self.assertFalse(await BRIDGE.async_unload_entry(self.hass, types.SimpleNamespace(entry_id="entry")))
        self.assertIs(self.hass.data[BRIDGE.DOMAIN]["entries"]["entry"], self.runtime)
        self.assertEqual(self.manager.registered, {self.secret})

    async def test_empty_or_failed_storage_load_does_not_break_legacy_runtime(self):
        domain = self.hass.data[BRIDGE.DOMAIN]
        del domain[MODULE.MANAGER_KEY]
        with patch.object(MODULE, "Store", return_value=FakeStore()):
            first = await BRIDGE._async_ensure_webhook_manager(self.hass, domain)
            second = await BRIDGE._async_ensure_webhook_manager(self.hass, domain)
        self.assertIs(first, second)
        self.assertTrue(first.available)
        self.assertIs(domain["entries"]["entry"], self.runtime)
        domain.pop(MODULE.MANAGER_KEY)
        broken = FakeStore()
        broken.async_load = AsyncMock(side_effect=OSError("not logged"))
        with patch.object(MODULE, "Store", return_value=broken):
            failed = await BRIDGE._async_ensure_webhook_manager(self.hass, domain)
        self.assertFalse(failed.available)
        self.assertIs(domain["entries"]["entry"], self.runtime)

    async def test_duplicate_loaded_secret_or_active_device_fails_closed(self):
        await self.pair()
        for changes in ({"device_id": "other"}, {"webhook_id": "b" * 64}):
            payload = deepcopy(self.store.payload)
            payload["pairings"][str(uuid4())] = {**payload["pairings"][self.registration], **changes}
            fresh = MODULE.HalthyWebhookManager(self.hass, self.push, store=FakeStore(payload))
            await fresh.load()
            self.assertFalse(fresh.available)
            self.assertEqual(fresh.records, {})

    async def test_control_payload_is_bounded_before_auth_or_storage_work(self):
        request = self.request(self.payload, chunks=[b"a" * 4096, b"a"], length=False)
        self.assert_error(await self.manager.control(request), 413, "payload_too_large")
        self.assertEqual(self.manager.records, {})
        self.assertEqual(self.store.saved, 0)

    async def test_missing_manager_returns_explicit_failure(self):
        self.hass.data[BRIDGE.DOMAIN].pop(MODULE.MANAGER_KEY)
        self.assert_error(await MODULE.HalthyWebhookPairingView().post(self.request(self.payload)),
                          503, "pairing_storage_unavailable")

    async def test_body_timeout_returns_explicit_failure_without_delegate(self):
        await self.pair()
        original = asyncio.wait_for
        async def immediate_timeout(work, timeout):
            return await original(work, timeout=0)
        with patch.object(MODULE.asyncio, "wait_for", new=immediate_timeout):
            self.assert_error(await self.upload(), 408, "request_timeout")
        self.push.assert_not_called()
