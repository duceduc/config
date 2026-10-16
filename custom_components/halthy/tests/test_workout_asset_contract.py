"""Actual bridge acknowledgment semantics for isolated workout/image adapters.

No HTTP server or real archive is used; only local HA stubs and in-memory state.
"""
import base64
import asyncio
import json
import types
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_statistics import BRIDGE


class WorkoutAssetContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester", app_username="tester", display_name="Tester",
            owner_user_id="owner", statistics_enabled=False,
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        async def execute(function, *args):
            return function(*args)

        self.hass = types.SimpleNamespace(data={BRIDGE.DOMAIN: {"entries": {"entry": self.runtime}}},
            config=types.SimpleNamespace(units=types.SimpleNamespace(temperature_unit="\u00b0C"),
                path=lambda relative: directory.name + "/" + relative), async_add_executor_job=execute)
        self.workout = {
            "workout_uuid": "74d459fe-20aa-4180-b19c-e198b3a712ac", "workout_type": "Walking",
            "workout_start": "2026-10-06T08:00:00.000Z", "workout_end": "2026-10-06T10:00:00.000Z",
            "workout_duration_s": 3600, "workout_distance_m": 1000, "workout_active_energy_kcal": 200,
        }

    def image(self, style="map-v1"):
        return {
            "key": "workout", "name": "Workout", "content_type": "image/jpeg",
            "image_base64": base64.b64encode(b"\xff\xd8\xff\x00").decode(),
            "attributes": {**self.workout, "render_style": style, "measurement_timestamp": self.workout["workout_end"]},
        }

    async def push(self, workouts=None, image=None, archive_only=False, writer=None, owner="owner", extra=None, archive_endpoint=False):
        payload = {"username": "tester", "device_id": "phone", "prune_unselected_metrics": False,
                   "workouts": workouts or [], "images": [image] if image else []}
        if archive_only:
            payload["archive_only"] = True
        if extra:
            payload.update(extra)
        raw = json.dumps(payload).encode()
        request = types.SimpleNamespace(
            app={BRIDGE.KEY_HASS: self.hass}, content_length=len(raw), read=AsyncMock(return_value=raw),
            get=lambda key: types.SimpleNamespace(id=owner),
        )

        async def archive(**kwargs):
            return kwargs["attributes"]

        registry = types.SimpleNamespace(async_get_entity_id=lambda *args: None)
        with patch.object(BRIDGE, "_schedule_store_save"), \
             patch.object(BRIDGE, "_async_archive_workout_image", new=writer or archive), \
             patch.object(BRIDGE, "_async_emit_activity_log_entries", new=AsyncMock()), \
             patch.object(BRIDGE.er, "async_get", return_value=registry):
            view = BRIDGE.HalthyWorkoutArchiveWriteView() if archive_endpoint else BRIDGE.HalthyPushView()
            response = await view.post(request)
        return response["args"][0]

    def assert_counts(self, reply, explicit, images):
        self.assertIs(reply["ok"], True)
        self.assertEqual(reply["accepted_sensor_unique_ids"], [])
        self.assertEqual(len(reply["accepted_image_unique_ids"]), images)
        self.assertEqual(reply["workouts_created"] + reply["workouts_updated"] + reply["duplicates"], explicit + images)
        self.assertEqual(reply["accepted"] + reply["duplicates"], explicit + 2 * images)
        self.assertEqual(reply["ignored_older"], 0)

    async def test_image_only_acknowledges_image_and_implicit_calendar_record(self):
        reply = await self.push(image=self.image())
        self.assert_counts(reply, explicit=0, images=1)
        self.assertEqual(reply["accepted"], 2)
        self.assertEqual(reply["workouts_created"], 1)
        self.assertEqual(len(self.runtime.workouts), 1)

    async def test_image_retry_still_accepts_image_with_duplicate_calendar_record(self):
        await self.push(image=self.image())
        reply = await self.push(image=self.image())
        self.assert_counts(reply, explicit=0, images=1)
        self.assertEqual(reply["accepted"], 1)
        self.assertEqual(reply["duplicates"], 1)

    async def test_explicit_and_image_workout_upserts_count_separately(self):
        reply = await self.push(workouts=[self.workout], image=self.image())
        self.assert_counts(reply, explicit=1, images=1)
        self.assertEqual(len(self.runtime.workouts), 1)
        self.assertEqual(next(iter(self.runtime.images.values())).attributes["render_style"], "map-v1")

    async def test_calendar_only_retry_returns_duplicates_not_fake_updated_count(self):
        await self.push(workouts=[self.workout])
        reply = await self.push(workouts=[self.workout])
        self.assert_counts(reply, explicit=1, images=0)
        self.assertEqual(reply["accepted"], 0)
        self.assertEqual(reply["duplicates"], 1)

    async def test_corrected_calendar_metadata_is_updated(self):
        await self.push(workouts=[self.workout])
        reply = await self.push(workouts=[{**self.workout, "workout_distance_m": 1200}])
        self.assert_counts(reply, explicit=1, images=0)
        self.assertEqual(reply["workouts_updated"], 1)

    async def test_changed_render_style_confirms_image_even_with_duplicate_calendar(self):
        await self.push(image=self.image())
        reply = await self.push(image=self.image("map-v2"))
        self.assert_counts(reply, explicit=0, images=1)
        self.assertEqual(reply["workouts_updated"], 0)
        self.assertEqual(reply["duplicates"], 1)
        self.assertEqual(reply["accepted"], 1)
        self.assertEqual(next(iter(self.runtime.images.values())).attributes["render_style"], "map-v2")

    async def test_archive_only_leaves_calendar_current_map_and_upload_state_unchanged(self):
        await self.push(image=self.image())
        before_images = dict(self.runtime.images)
        before_workouts = dict(self.runtime.workouts)
        reply = await self.push(image=self.image("old-map"), archive_only=True, archive_endpoint=True)
        self.assertTrue(BRIDGE.HalthyWorkoutArchiveWriteView.requires_auth)
        self.assertEqual(reply["archived_workout_uuids"], [self.workout["workout_uuid"]])
        self.assertEqual(reply["accepted"], 1)
        self.assertEqual(reply["accepted_image_unique_ids"], [])
        self.assertEqual(reply["workouts_created"] + reply["workouts_updated"], 0)
        self.assertEqual(self.runtime.images, before_images)
        self.assertEqual(self.runtime.workouts, before_workouts)

    async def test_archive_only_writes_real_file_and_metadata_before_ack(self):
        from pathlib import Path
        reply = await self.push(image=self.image(), archive_only=True, writer=BRIDGE._async_archive_workout_image)
        root = Path(self.hass.config.path("media")) / "halthy" / "workouts" / "tester"
        self.assertTrue(reply["archive_only"])
        files = list(root.glob("*.jpg"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"\xff\xd8\xff\x00")
        metadata = json.loads(files[0].with_suffix(".json").read_text())
        self.assertEqual(metadata["workout_uuid"], self.workout["workout_uuid"])
        self.assertEqual(self.runtime.images, {})
        self.assertEqual(self.runtime.workouts, {})

    async def test_archive_disk_failure_has_no_success_ack_or_current_map_effect(self):
        async def fail(**kwargs):
            self.assertIs(kwargs["require_success"], True)
            raise OSError("simulated private path")
        reply = await self.push(image=self.image(), archive_only=True, writer=fail)
        self.assertEqual(reply, {"error": "archive_write_failed"})
        self.assertEqual(self.runtime.images, {})
        self.assertEqual(self.runtime.workouts, {})

    async def test_real_archive_image_metadata_and_prune_failures_never_ack(self):
        execute = self.hass.async_add_executor_job
        for name in ("_store_workout_archive_file", "_store_workout_archive_metadata", "_prune_workout_archive_files"):
            async def failing(function, *args):
                if function.__name__ == name:
                    raise OSError("simulated disk failure")
                return await execute(function, *args)
            self.hass.async_add_executor_job = failing
            reply = await self.push(image=self.image(), archive_only=True, writer=BRIDGE._async_archive_workout_image)
            self.assertEqual(reply, {"error": "archive_write_failed"})
        self.hass.async_add_executor_job = execute
        reply = await self.push(image=self.image(), archive_only=True, writer=BRIDGE._async_archive_workout_image)
        self.assertEqual(reply["accepted"], 1)
        self.assertEqual(self.runtime.images, {})
        self.assertEqual(self.runtime.workouts, {})

    async def test_failed_replacement_write_keeps_existing_archive_alias(self):
        from pathlib import Path
        archive = BRIDGE._async_archive_workout_image.__globals__
        root = Path(self.hass.config.path("media"))
        root.mkdir(parents=True)
        old = root / "20261006T100000Z_uuid_test.jpg"
        old.write_bytes(b"old")
        with patch.object(Path, "open", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                archive["_store_workout_archive_file"](root, "20261006T110000Z_uuid_test.jpg", b"new", "uuid_test")
        self.assertEqual(old.read_bytes(), b"old")

    async def test_archive_endpoint_rejects_non_archive_payload(self):
        reply = await self.push(image=self.image(), archive_endpoint=True)
        self.assertEqual(reply, {"error": "invalid_archive_mode"})
        self.assertEqual(self.runtime.images, {})

    async def test_archive_scope_rejects_foreign_unowned_and_ambiguous_profiles(self):
        reply = await self.push(image=self.image(), archive_only=True, owner="foreign")
        self.assertEqual(reply["error"], "username_not_owned_by_user")
        self.runtime.owner_user_id = None
        reply = await self.push(image=self.image(), archive_only=True)
        self.assertEqual(reply["error"], "archive_scope_mismatch")
        self.runtime.owner_user_id = "owner"
        self.hass.data[BRIDGE.DOMAIN]["entries"]["duplicate"] = self.runtime
        reply = await self.push(image=self.image(), archive_only=True)
        self.assertEqual(reply["error"], "archive_scope_mismatch")

    async def test_archive_rejects_mixed_or_malformed_requests_before_disk_write(self):
        writer = AsyncMock()
        for extra in ({"archive_only": 1}, {"sensors": []}, {"workouts": [self.workout]},
                      {"prune_unselected_metrics": True}, {"device_id": ""}):
            reply = await self.push(image=self.image(), archive_only=True, extra=extra, writer=writer)
            self.assertIn("error", reply)
        image = self.image()
        image["attributes"]["workout_uuid"] = "not-a-uuid"
        reply = await self.push(image=image, archive_only=True, writer=writer)
        self.assertEqual(reply["error"], "invalid_archive_workout")
        writer.assert_not_awaited()

    async def test_cancelled_archive_request_joins_disk_work_before_releasing_runtime(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def write(**kwargs):
            started.set()
            await release.wait()
            finished.set()
            return kwargs["attributes"]
        task = asyncio.create_task(self.push(image=self.image(), archive_only=True, writer=write))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(BRIDGE._runtime_lock(self.runtime).locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.assertFalse(BRIDGE._runtime_lock(self.runtime).locked())
