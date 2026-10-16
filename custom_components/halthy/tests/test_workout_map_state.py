"""Synthetic read-only map revisions; not real HA middleware or repair writes."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_statistics import BRIDGE
from test_paired_webhook import Request

MODULE = sys.modules[BRIDGE.HalthyWorkoutMapStateView.__module__]


class ObservedLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        await self.lock.acquire()

    async def __aexit__(self, *_):
        self.lock.release()


class WorkoutMapStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = types.SimpleNamespace(id="owner")
        self.runtime = BRIDGE.IntegrationRuntime(configured_username="tester", app_username="tester",
            display_name="Tester", owner_user_id="owner", statistics_enabled=False)
        self.hass = types.SimpleNamespace(data={BRIDGE.DOMAIN: {"entries": {"entry": self.runtime}}})
        self.payload = {"version": 1, "entry_id": "entry", "username": "tester", "device_id": "phone"}
        self.view = BRIDGE.HalthyWorkoutMapStateView()
        self.image = BRIDGE.HalthyImageState(unique_id="map", metric_key="workout", name="Workout",
            image_bytes=b"\xff\xd8\xff\x00", content_type="image/jpeg", username="tester", device_id="phone",
            updated_at=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc), attributes={
                "workout_uuid": "74d459fe-20aa-4180-b19c-e198b3a712ac", "workout_type": "Walking",
                "workout_start": "2026-10-09T08:00:00.000Z", "workout_end": "2026-10-09T10:00:00.000Z",
                "workout_duration_s": 3600, "workout_distance_m": 1000,
                "route_points": [{"latitude": 1, "longitude": 2}], "private_note": "not exposed",
            })

    def request(self, payload=None, user=None, **kwargs):
        return Request(self.hass, self.payload if payload is None else payload,
                       user=self.user if user is None else user, **kwargs)

    async def state(self, payload=None, request=None):
        return await self.view.post(request or self.request(payload))

    def assert_error(self, reply, code, status):
        self.assertEqual(reply["args"][0], {"error": code})
        self.assertEqual(reply["kwargs"]["status"], status)
        self.assertEqual(reply["kwargs"]["headers"], {"Cache-Control": "no-store"})

    async def test_empty_owned_profile_is_explicit_and_does_not_write_or_claim(self):
        before = BRIDGE._serialize_entries({"entry": self.runtime})
        with patch.object(BRIDGE, "_schedule_store_save") as save, \
             patch.object(BRIDGE, "_async_archive_workout_image", new=AsyncMock()) as archive:
            reply = await self.state()
        self.assertEqual(reply["args"][0], {"ok": True, "version": 1, "account_id": "owner",
            "entry_id": "entry", "username": "tester", "device_id": "phone", "current": None})
        self.assertTrue(self.view.requires_auth)
        self.assertEqual(reply["kwargs"]["headers"], {"Cache-Control": "no-store"})
        self.assertEqual(BRIDGE._serialize_entries({"entry": self.runtime}), before)
        save.assert_not_called()
        archive.assert_not_awaited()

    async def test_snapshot_contains_only_revision_identity_end_and_actual_source_device(self):
        self.image.device_id = "another_phone"
        self.runtime.images["map"] = self.image
        before = BRIDGE._serialize_entries({"entry": self.runtime})
        reply = (await self.state())["args"][0]
        self.assertEqual(set(reply), {"ok", "version", "account_id", "entry_id", "username", "device_id", "current"})
        self.assertEqual(set(reply["current"]), {"revision", "workout_uuid", "workout_end", "source_device_id"})
        self.assertRegex(reply["current"]["revision"], "^[0-9a-f]{64}$")
        self.assertEqual(reply["current"]["workout_uuid"], self.image.attributes["workout_uuid"])
        self.assertEqual(reply["current"]["workout_end"], "2026-10-09T10:00:00Z")
        self.assertEqual(reply["current"]["source_device_id"], "another_phone")
        self.assertEqual(reply["device_id"], "phone")
        for value in ("route_points", "latitude", "longitude", "not exposed", "image_base64", "attributes", "owner_id"):
            self.assertNotIn(value, json.dumps(reply))
        self.assertEqual(BRIDGE._serialize_entries({"entry": self.runtime}), before)

    async def test_revision_is_stable_across_reads_dictionary_order_and_storage_reload(self):
        self.runtime.images["map"] = self.image
        first = (await self.state())["args"][0]["current"]
        self.assertEqual((await self.state())["args"][0]["current"], first)
        self.image.attributes = dict(reversed(list(self.image.attributes.items())))
        self.assertEqual((await self.state())["args"][0]["current"], first)
        stored = BRIDGE._image_to_storage(self.image)
        self.runtime.images["map"] = BRIDGE._image_from_storage("map", stored)
        self.assertEqual((await self.state())["args"][0]["current"], first)

    async def test_every_image_content_metadata_identity_source_and_write_revision_changes_proof(self):
        self.runtime.images["map"] = self.image
        first = (await self.state())["args"][0]["current"]["revision"]
        for kind in ("bytes", "content-type", "name", "metadata", "end", "uuid", "device", "updated-at", "image-id"):
            image = deepcopy(self.image)
            if kind == "bytes": image.image_bytes += b"\x01"
            elif kind == "content-type": image.image_bytes = b"\x89PNG\r\n\x1a\n"; image.content_type = "image/png"
            elif kind == "name": image.name = "Changed name"
            elif kind == "metadata": image.attributes["workout_distance_m"] = 1200
            elif kind == "end": image.attributes["workout_end"] = "2026-10-09T10:05:00Z"
            elif kind == "uuid": image.attributes["workout_uuid"] = "34d459fe-20aa-4180-b19c-e198b3a712ac"
            elif kind == "device": image.device_id = "other_phone"
            elif kind == "updated-at": image.updated_at += timedelta(microseconds=1)
            else: image.unique_id = "another_map"
            self.runtime.images = {image.unique_id: image}
            revision = (await self.state())["args"][0]["current"]["revision"]
            self.assertNotEqual(revision, first, kind)

    async def test_revision_is_bound_to_account_profile_and_entry_not_only_image_bytes(self):
        self.runtime.images["map"] = self.image
        first = (await self.state())["args"][0]["current"]["revision"]
        self.runtime.owner_user_id = "other_owner"
        self.user.id = "other_owner"
        self.assertNotEqual((await self.state())["args"][0]["current"]["revision"], first)
        self.runtime.owner_user_id = "owner"; self.user.id = "owner"
        self.runtime.configured_username = "other_profile"
        self.assertNotEqual((await self.state({**self.payload, "username": "other_profile"}))["args"][0]["current"]["revision"], first)
        self.runtime.configured_username = "tester"
        self.hass.data[BRIDGE.DOMAIN]["entries"] = {"other_entry": self.runtime}
        self.assertNotEqual((await self.state({**self.payload, "entry_id": "other_entry"}))["args"][0]["current"]["revision"], first)

    async def test_missing_foreign_unowned_renamed_and_ambiguous_context_fails_closed(self):
        request = self.request(); request.values = {}
        self.assert_error(await self.state(request=request), "missing_user_context", 403)
        foreign = self.request(user=types.SimpleNamespace(id="foreign"))
        self.assert_error(await self.state(request=foreign), "profile_not_owned_or_ambiguous", 403)
        for payload in ({**self.payload, "entry_id": "missing"}, {**self.payload, "username": "renamed"}):
            self.assert_error(await self.state(payload), "profile_not_owned_or_ambiguous", 403)
        self.runtime.owner_user_id = None
        self.assert_error(await self.state(), "profile_not_owned_or_ambiguous", 403)
        self.assertIsNone(self.runtime.owner_user_id)
        self.runtime.owner_user_id = "owner"
        self.hass.data[BRIDGE.DOMAIN]["entries"]["duplicate"] = deepcopy(self.runtime)
        self.assert_error(await self.state(), "profile_not_owned_or_ambiguous", 403)

    async def test_multiple_current_images_are_unavailable_not_guessed_or_reported_empty(self):
        self.runtime.images = {"map": self.image, "other_map": deepcopy(self.image)}
        self.runtime.images["other_map"].unique_id = "other_map"
        self.assert_error(await self.state(), "current_map_state_unavailable", 503)

    async def test_unusable_saved_map_is_not_empty_success_and_never_echoes_private_values(self):
        for kind in ("uuid", "end", "backwards-end", "bytes", "mime", "oversized-image", "oversized-attributes",
                     "nan", "source", "image-key", "naive-date", "attributes"):
            image = deepcopy(self.image)
            if kind == "uuid": image.attributes["workout_uuid"] = "private corrupt uuid"
            elif kind == "end": image.attributes["workout_end"] = "private invalid time"
            elif kind == "backwards-end": image.attributes["workout_end"] = "2026-10-09T07:00:00Z"
            elif kind == "bytes": image.image_bytes = b"private invalid image"
            elif kind == "mime": image.content_type = "image/png"
            elif kind == "oversized-image": image.image_bytes += b"\x00" * BRIDGE.MAX_IMAGE_BYTES
            elif kind == "oversized-attributes": image.attributes["private"] = "x" * BRIDGE.MAX_ATTRIBUTES_BYTES
            elif kind == "nan": image.attributes["private"] = float("nan")
            elif kind == "source": image.device_id = "private\ninvalid device"
            elif kind == "image-key": image.unique_id = "different_dictionary_key"
            elif kind == "naive-date": image.updated_at = image.updated_at.replace(tzinfo=None)
            else: image.attributes = None
            self.runtime.images = {"map": image}
            reply = await self.state()
            self.assert_error(reply, "current_map_state_unavailable", 503)
            self.assertNotIn("private", json.dumps(reply))

    async def test_strict_schema_rejects_wrong_types_extra_fields_and_duplicate_json_keys(self):
        for payload in ({}, {**self.payload, "version": True}, {**self.payload, "version": 1.0},
                        {**self.payload, "version": 2}, {**self.payload, "action": "replace"},
                        {**self.payload, "account_id": "owner"}, {**self.payload, "device_id": ""},
                        {**self.payload, "device_id": " phone "}, {**self.payload, "device_id": ["phone"]},
                        {**self.payload, "username": "Tester"}, {**self.payload, "entry_id": "x" * 257}):
            self.assert_error(await self.state(payload), "invalid_map_state_request", 400)
        request = self.request()
        request.chunks = [b'{"version":1,"entry_id":"entry","username":"tester","device_id":"wrong","device_id":"phone"}']
        request.content_length = len(request.chunks[0])
        self.assert_error(await self.state(request=request), "invalid_map_state_request", 400)

    async def test_method_content_type_and_streaming_body_are_bounded(self):
        request = self.request(); request.method = "GET"
        self.assert_error(await self.state(request=request), "method_not_allowed", 405)
        request = self.request(); request.content_type = "text/plain"
        self.assert_error(await self.state(request=request), "json_required", 415)
        request = self.request(); request.content_length = 4097
        self.assert_error(await self.state(request=request), "payload_too_large", 413)
        self.assertEqual(request.consumed, 0)
        request = self.request(length=False, chunks=[b"x" * 4097, b"do not consume"])
        self.assert_error(await self.state(request=request), "payload_too_large", 413)
        self.assertEqual(request.consumed, 1)

    async def test_profile_is_revalidated_after_waiting_for_runtime_gate(self):
        for kind in ("owner", "rename", "replacement", "ambiguous", "removed"):
            self.setUp()
            self.runtime.images["map"] = self.image
            lock = ObservedLock(); self.runtime.lock = lock
            await lock.lock.acquire()
            task = asyncio.create_task(self.state())
            await asyncio.wait_for(lock.entered.wait(), 2)
            if kind == "owner": self.runtime.owner_user_id = "foreign"
            elif kind == "rename": self.runtime.configured_username = "renamed"
            elif kind == "replacement":
                self.hass.data[BRIDGE.DOMAIN]["entries"]["entry"] = BRIDGE.IntegrationRuntime(
                    configured_username="tester", app_username="tester", display_name="Tester", owner_user_id="owner")
            elif kind == "ambiguous": self.hass.data[BRIDGE.DOMAIN]["entries"]["duplicate"] = self.runtime
            else: self.hass.data[BRIDGE.DOMAIN]["entries"].pop("entry")
            lock.lock.release()
            self.assert_error(await task, "profile_not_owned_or_ambiguous", 403)

    async def test_cancellation_releases_wait_without_mutation_and_next_read_can_succeed(self):
        self.runtime.images["map"] = self.image
        before = BRIDGE._serialize_entries({"entry": self.runtime})
        lock = ObservedLock(); self.runtime.lock = lock
        await lock.lock.acquire()
        task = asyncio.create_task(self.state())
        await asyncio.wait_for(lock.entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        lock.lock.release()
        self.assertEqual(BRIDGE._serialize_entries({"entry": self.runtime}), before)
        self.assertTrue((await self.state())["args"][0]["ok"])

    async def test_view_registration_is_idempotent(self):
        registered = []
        self.hass.http = types.SimpleNamespace(register_view=registered.append)
        domain = self.hass.data[BRIDGE.DOMAIN]
        BRIDGE._ensure_http_views_registered(self.hass, domain)
        BRIDGE._ensure_http_views_registered(self.hass, domain)
        self.assertEqual(sum(type(view) is BRIDGE.HalthyWorkoutMapStateView for view in registered), 1)
        self.assertTrue(domain["workout_map_state_view_registered"])

    async def test_deep_json_request_is_a_fixed_error_not_an_unhandled_exception(self):
        request = self.request()
        request.chunks = [b'{"nested":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}']
        request.content_length = len(request.chunks[0])
        self.assert_error(await self.state(request=request), "invalid_map_state_request", 400)

    async def test_deep_saved_metadata_is_unavailable_not_empty_success(self):
        nested = None
        for _ in range(1500): nested = [nested]
        self.image.attributes["private_nested"] = nested
        self.runtime.images["map"] = self.image
        self.assert_error(await self.state(), "current_map_state_unavailable", 503)
