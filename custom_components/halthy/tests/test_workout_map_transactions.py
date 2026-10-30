"""Synthetic authenticated transactions with real private temporary journals."""
import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from test_statistics import BRIDGE
from test_paired_webhook import Request
from test_workout_map_state import ObservedLock

MODULE = sys.modules[BRIDGE.HalthyWorkoutMapTransactionView.__module__]
JOURNAL = MODULE.journal


class WorkoutMapTransactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.user = types.SimpleNamespace(id="owner", is_active=True, local_only=False)
        self.grant = types.SimpleNamespace(id="grant", user=self.user, expire_at=None)
        self.grants = {"grant": self.grant}
        self.runtime = BRIDGE.IntegrationRuntime(configured_username="tester", app_username="tester",
            display_name="Tester", owner_user_id="owner", statistics_enabled=False)

        async def execute(function, *args):
            return await asyncio.to_thread(function, *args)

        self.hass = types.SimpleNamespace(state="RUNNING",
            data={BRIDGE.DOMAIN: {"entries": {"entry": self.runtime}}}, async_add_executor_job=execute,
            config=types.SimpleNamespace(units=types.SimpleNamespace(temperature_unit="\u00b0C"),
                path=lambda relative: self.directory.name + "/" + relative),
            auth=types.SimpleNamespace(async_get_refresh_token=self.grants.get,
                async_validate_refresh_token=lambda grant, remote: None))
        self.base = {"version": 1, "entry_id": "entry", "username": "tester", "device_id": "phone"}
        self.image = {"key": "workout", "name": "Workout", "content_type": "image/jpeg",
            "image_base64": base64.b64encode(b"\xff\xd8\xff\x00").decode(), "attributes": {
                "workout_uuid": "74d459fe-20aa-4180-b19c-e198b3a712ac", "workout_type": "Walking",
                "workout_start": "2026-10-09T09:00:00Z", "workout_end": "2026-10-09T10:00:00Z",
                "workout_duration_s": 3600, "render_style": "style", "route_points": [{"latitude": 1, "longitude": 2}]}}
        self.view = BRIDGE.HalthyWorkoutMapTransactionView()

    def request(self, payload, **kwargs):
        return Request(self.hass, payload, user=kwargs.pop("user", self.user), **kwargs)

    async def post(self, payload, request=None):
        with patch.object(BRIDGE, "_schedule_store_save"):
            return await self.view.post(request or self.request(payload))

    async def activate(self, baseline=None):
        reply = await self.post({**self.base, "action": "activate", "baseline_revision": baseline})
        self.assertTrue(reply["args"][0]["ok"])
        return reply["args"][0]

    async def state(self):
        return await self.post({**self.base, "action": "state"})

    def mutation(self, state, *, image=None, action="replace", operation=None, device="phone"):
        payload = {**self.base, "device_id": device, "action": action, "operation_id": operation or str(uuid4()),
                   "expected_revision": state["state"]["revision"]}
        if action == "replace":
            payload["image"] = deepcopy(self.image if image is None else image)
        return payload

    def path(self):
        return JOURNAL.journal_path(self.hass.config.path(".storage"),
            {"entry_id": "entry", "account_id": "owner", "username": "tester"})

    def assert_error(self, reply, status, code):
        body = reply["args"][0]
        if code in {"map_revision_conflict", "map_operation_conflict", "map_journal_full"} and set(body) != {"error"}:
            self.assertEqual(set(body), {"ok", "version", "error", "applied", "account_id", "entry_id", "username",
                "device_id", "operation_id", "request_sha256", "expected_revision", "state"})
            self.assertIs(body["ok"], False)
            self.assertIs(body["applied"], False)
            self.assertEqual(body["error"], code)
        else:
            self.assertEqual(body, {"error": code})
        self.assertEqual(reply["kwargs"]["status"], status)
        self.assertEqual(reply["kwargs"]["headers"], {"Cache-Control": "no-store"})

    async def rejected_mutation(self, code):
        initial = await self.activate()
        first_payload = self.mutation(initial)
        first = (await self.post(first_payload))["args"][0]
        payload = self.mutation(first, action="clear",
            operation=first_payload["operation_id"] if code == "map_operation_conflict" else None)
        return first_payload, first, payload

    async def test_collision_and_capacity_proofs_bind_exact_attempt_without_eviction_or_mutation(self):
        for code in ["map_operation_conflict", "map_journal_full"]:
            with self.subTest(code=code):
                original, first, payload = await self.rejected_mutation(code)
                request = self.request(payload)
                before = self.path().read_bytes()
                with patch.object(JOURNAL, "MAX_RECEIPTS", len(json.loads(before)["receipts"])), patch.object(JOURNAL, "MAX_RECEIPT_PAGES", 0):
                    reply = await self.post(payload, request)
                    self.assert_error(reply, 409, code)
                    replay = (await self.post(original))["args"][0]
                    self.assertTrue(replay["replayed"])
                    self.assertEqual(replay["receipt"], first["receipt"])
                proof = reply["args"][0]
                self.assertEqual(proof["state"], first["state"])
                self.assertEqual(proof["expected_revision"], payload["expected_revision"])
                self.assertEqual(proof["operation_id"], payload["operation_id"])
                material = {"scope": {"account_id": "owner", "entry_id": "entry", "username": "tester"},
                    "device_id": "phone", "operation_id": payload["operation_id"], "expected_revision": payload["expected_revision"],
                    "replacement": {"body_sha256": hashlib.sha256(b"".join(request.chunks)).hexdigest()}}
                self.assertEqual(proof["request_sha256"], hashlib.sha256(JOURNAL._encode(material)).hexdigest())
                self.assertEqual(self.path().read_bytes(), before)
                self.assertNotIn("image_base64", json.dumps(proof))
                self.assertNotIn("route_points", json.dumps(proof))

    async def test_collision_and_capacity_cannot_prove_non_delivery_without_disk_confirmation(self):
        for code in ["map_operation_conflict", "map_journal_full"]:
            with self.subTest(code=code):
                _, _, payload = await self.rejected_mutation(code)
                original = JOURNAL._confirm_file
                calls = 0
                def fail_confirmation(path):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError("synthetic confirmation failure")
                    return original(path)
                before = self.path().read_bytes()
                with patch.object(JOURNAL, "MAX_RECEIPTS", len(json.loads(before)["receipts"])), patch.object(JOURNAL, "MAX_RECEIPT_PAGES", 0), patch.object(JOURNAL, "_confirm_file", side_effect=fail_confirmation):
                    self.assert_error(await self.post(payload), 503, "map_write_unconfirmed")
                self.assertEqual(calls, 2)
                self.assertEqual(self.path().read_bytes(), before)
                self.runtime.map_journal_error = None

    async def test_collision_and_capacity_proofs_recheck_access_and_shutdown_after_io(self):
        for code in ["map_operation_conflict", "map_journal_full"]:
            for change in ["grant", "profile", "shutdown"]:
                with self.subTest(code=code, change=change):
                    _, _, payload = await self.rejected_mutation(code)
                    original = MODULE._commit
                    capacity = len(json.loads(self.path().read_bytes())["receipts"])
                    def change_during_commit(*args):
                        if change == "grant": self.grants.clear()
                        elif change == "profile": self.hass.data[BRIDGE.DOMAIN]["entries"].clear()
                        else: self.hass.state = "STOPPING"
                        return original(*args)
                    with patch.object(JOURNAL, "MAX_RECEIPTS", capacity), patch.object(JOURNAL, "MAX_RECEIPT_PAGES", 0), patch.object(MODULE, "_commit", side_effect=change_during_commit):
                        reply = await self.post(payload)
                    self.assertEqual(set(reply["args"][0]), {"error"})
                    self.assertEqual(reply["kwargs"]["status"], 503 if change == "shutdown" else 403)
                    self.grants["grant"] = self.grant
                    self.hass.data[BRIDGE.DOMAIN]["entries"]["entry"] = self.runtime
                    self.hass.state = "RUNNING"

    async def test_revision_rejection_is_exact_request_bound_and_durable_without_mutation(self):
        initial = await self.activate()
        written = (await self.post(self.mutation(initial)))["args"][0]
        payload = self.mutation(initial, action="clear")
        request = self.request(payload)
        before = self.path().read_bytes()
        reply = await self.post(payload, request)
        self.assert_error(reply, 409, "map_revision_conflict")
        proof = reply["args"][0]
        self.assertEqual(proof["state"], written["state"])
        self.assertEqual(proof["expected_revision"], initial["state"]["revision"])
        for key, value in {"account_id": "owner", "entry_id": "entry", "username": "tester", "device_id": "phone",
                           "operation_id": payload["operation_id"], "version": 1}.items():
            self.assertEqual(proof[key], value)
        material = {"scope": {"account_id": "owner", "entry_id": "entry", "username": "tester"},
            "device_id": "phone", "operation_id": payload["operation_id"], "expected_revision": payload["expected_revision"],
            "replacement": {"body_sha256": hashlib.sha256(b"".join(request.chunks)).hexdigest()}}
        self.assertEqual(proof["request_sha256"], hashlib.sha256(JOURNAL._encode(material)).hexdigest())
        self.assertEqual(self.path().read_bytes(), before)
        self.assertNotIn("image_base64", json.dumps(proof))
        self.assertNotIn("route_points", json.dumps(proof))

    async def test_exact_already_applied_operation_returns_delivery_receipt_not_rejection(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        applied = (await self.post(payload))["args"][0]
        later = (await self.post(self.mutation(applied, action="clear")))["args"][0]
        reply = await self.post(payload)
        self.assertEqual(reply["kwargs"]["status"], 200)
        body = reply["args"][0]
        self.assertEqual(body["state"], later["state"])
        self.assertEqual(body["receipt"], applied["receipt"])
        self.assertTrue(body["replayed"])
        self.assertNotIn("applied", body)

    async def test_unconfirmed_rejection_snapshot_has_no_proof(self):
        initial = await self.activate()
        await self.post(self.mutation(initial))
        original = JOURNAL._confirm_file
        calls = 0
        def fail_rejection_confirmation(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic private sync failure")
            return original(path)
        with patch.object(JOURNAL, "_confirm_file", side_effect=fail_rejection_confirmation):
            self.assert_error(await self.post(self.mutation(initial)), 503, "map_write_unconfirmed")
        self.assertEqual(calls, 2)

    async def test_revoked_grant_after_rejection_io_cannot_receive_projection(self):
        initial = await self.activate()
        await self.post(self.mutation(initial))
        original = MODULE._commit
        def revoke_during_commit(*args):
            self.grants.clear()
            return original(*args)
        with patch.object(MODULE, "_commit", side_effect=revoke_during_commit):
            reply = await self.post(self.mutation(initial))
        self.assertEqual(reply["kwargs"]["status"], 403)
        self.assertEqual(set(reply["args"][0]), {"error"})

    async def test_removed_profile_after_rejection_io_cannot_receive_projection(self):
        initial = await self.activate()
        await self.post(self.mutation(initial))
        original = MODULE._commit
        def remove_during_commit(*args):
            self.hass.data[BRIDGE.DOMAIN]["entries"].clear()
            return original(*args)
        with patch.object(MODULE, "_commit", side_effect=remove_during_commit):
            self.assert_error(await self.post(self.mutation(initial)), 403, "profile_not_owned_or_ambiguous")

    async def test_stopping_after_rejection_io_does_not_expose_snapshot(self):
        initial = await self.activate()
        await self.post(self.mutation(initial))
        original = MODULE._commit
        def stop_during_commit(*args):
            self.hass.state = "STOPPING"
            return original(*args)
        with patch.object(MODULE, "_commit", side_effect=stop_during_commit):
            self.assert_error(await self.post(self.mutation(initial)), 503, "map_server_stopping")

    async def push(self, image=None, extra=None):
        payload = {"username": "tester", "device_id": "phone", "prune_unselected_metrics": False,
                   "workouts": [], "images": [] if image is None else [image]}
        payload.update(extra or {})
        raw = json.dumps(payload).encode()
        request = types.SimpleNamespace(app={BRIDGE.KEY_HASS: self.hass}, content_length=len(raw),
            read=AsyncMock(return_value=raw), get=lambda key: self.user)

        async def archive(**kwargs):
            return kwargs["attributes"]

        with patch.object(BRIDGE, "_schedule_store_save"), \
             patch.object(BRIDGE, "_async_archive_workout_image", new=archive), \
             patch.object(BRIDGE, "_async_emit_activity_log_entries", new=AsyncMock()), \
             patch.object(BRIDGE.er, "async_get", return_value=types.SimpleNamespace(async_get_entity_id=lambda *args: None)):
            return await BRIDGE.HalthyPushView().post(request)

    async def test_explicit_activation_is_durable_scoped_private_and_idempotent(self):
        initial = await self.activate()
        self.assertEqual(set(initial), {"ok", "version", "account_id", "entry_id", "username", "device_id", "state"})
        self.assertEqual(set(initial["state"]), {"revision", "generation", "current"})
        self.assertIsNone(initial["state"]["current"])
        self.assertEqual(initial["state"]["generation"], 0)
        self.assertEqual((await self.activate())["state"], initial["state"])
        self.assertEqual((await self.state())["args"][0]["state"], initial["state"])
        self.assertTrue(self.view.requires_auth)
        stored = BRIDGE._serialize_entries({"entry": self.runtime})["entries"]["entry"]
        self.assertEqual(stored["map_journal_scope"], {"entry_id": "entry", "account_id": "owner", "username": "tester"})
        self.assertTrue(self.path().is_file())

    async def test_state_never_implicitly_activates_or_claims_profile(self):
        self.assert_error(await self.state(), 409, "map_journal_not_active")
        self.assertFalse(self.path().exists())
        self.runtime.owner_user_id = None
        self.assert_error(await self.state(), 403, "profile_not_owned_or_ambiguous")
        self.assertIsNone(self.runtime.owner_user_id)

    async def test_activation_requires_exact_current_snapshot_and_freezes_archive_independent_bytes(self):
        await self.push(self.image)
        image = next(iter(self.runtime.images.values()))
        image.attributes["archive_relative_path"] = "synthetic.jpg"
        image.updated_at = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
        digest = MODULE.current_workout_map_state("entry", self.runtime)["revision"]
        self.assert_error(await self.post({**self.base, "action": "activate", "baseline_revision": None}), 409, "map_revision_conflict")
        self.assertFalse(self.path().exists())
        reply = await self.activate(digest)
        stored = JOURNAL.load_snapshot(self.path(), self.runtime.map_journal_scope)
        self.assertEqual(stored["current"]["image_base64"], self.image["image_base64"])
        self.assertNotIn("archive_relative_path", stored["current"]["attributes"])
        self.assertEqual(reply["state"]["current"]["workout_uuid"], self.image["attributes"]["workout_uuid"])

    async def test_replace_publishes_only_after_journal_confirmation_and_exposes_no_routes(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        with patch.object(MODULE, "async_dispatcher_send") as signal:
            reply = (await self.post(payload))["args"][0]
        self.assertFalse(reply["replayed"])
        self.assertEqual(reply["operation_id"], payload["operation_id"])
        self.assertEqual(reply["receipt"]["revision"], reply["state"]["revision"])
        self.assertEqual(next(iter(self.runtime.images.values())).image_bytes, b"\xff\xd8\xff\x00")
        self.assertTrue(signal.called)
        for key in ("route_points", "latitude", "image_base64", "attributes", "grant"):
            self.assertNotIn(key, json.dumps(reply))

    async def test_same_workout_correction_and_older_fallback_are_explicit_revision_checked_writes(self):
        initial = await self.activate()
        first = (await self.post(self.mutation(initial)))["args"][0]
        corrected = deepcopy(self.image); corrected["attributes"]["workout_duration_s"] = 3700
        second = (await self.post(self.mutation(first, image=corrected)))["args"][0]
        self.assertEqual(next(iter(self.runtime.images.values())).attributes["workout_duration_s"], 3700)
        older = deepcopy(self.image)
        older["attributes"].update(workout_uuid=str(uuid4()), workout_start="2026-10-08T09:00:00Z", workout_end="2026-10-08T10:00:00Z")
        self.assert_error(await self.post(self.mutation(first, image=older)), 409, "map_revision_conflict")
        accepted = (await self.post(self.mutation(second, image=older)))["args"][0]
        self.assertEqual(accepted["state"]["current"]["workout_uuid"], older["attributes"]["workout_uuid"])

    async def test_exact_lost_reply_replay_after_new_clear_cannot_restore_obsolete_map(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        first = (await self.post(payload))["args"][0]
        cleared = (await self.post(self.mutation(first, action="clear")))["args"][0]
        replay = (await self.post(payload))["args"][0]
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["receipt"], first["receipt"])
        self.assertEqual(replay["state"], cleared["state"])
        self.assertEqual(self.runtime.images, {})

    async def test_clear_replacement_reuses_stable_slot_even_from_another_device(self):
        initial = await self.activate()
        first = (await self.post(self.mutation(initial)))["args"][0]
        slot = next(iter(self.runtime.images))
        with patch.object(MODULE, "async_dispatcher_send") as signal:
            cleared = (await self.post(self.mutation(first, action="clear")))["args"][0]
        self.assertEqual(self.runtime.images, {})
        signal.assert_called_with(self.hass, BRIDGE.update_image_signal("entry", slot))
        replaced = (await self.post(self.mutation(cleared, device="other_phone")))["args"][0]
        self.assertEqual(list(self.runtime.images), [slot])
        self.assertEqual(replaced["state"]["current"]["source_device_id"], "other_phone")

    async def test_receipt_rollover_restart_and_old_http_replay_preserve_latest_empty_map(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            state = await self.activate()
            original = self.mutation(state)
            first = (await self.post(original))["args"][0]
            state = first
            for index in range(5):
                state = (await self.post(self.mutation(state, action="replace" if index % 2 == 0 else "clear")))["args"][0]
            state = (await self.post(self.mutation(state, action="clear")))["args"][0]
            self.assertEqual(state["state"]["generation"], 7)
            document = JOURNAL.load_snapshot(self.path(), {"entry_id": "entry", "account_id": "owner", "username": "tester"})
            self.assertEqual(document["version"], 3)
            self.assertEqual(len(document["receipt_pages"]), 3)
            self.runtime = BRIDGE.IntegrationRuntime(configured_username="tester", app_username="tester",
                display_name="Tester", owner_user_id="owner", statistics_enabled=False)
            self.hass.data[BRIDGE.DOMAIN]["entries"]["entry"] = self.runtime
            self.assertEqual((await self.state())["args"][0]["state"], state["state"])
            replay = (await self.post(original))["args"][0]
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["receipt"], first["receipt"])
            self.assertEqual(replay["state"], state["state"])
            self.assertEqual(self.runtime.images, {})

    async def test_missing_archived_receipt_page_cannot_be_hidden_by_runtime_cache(self):
        with patch.object(JOURNAL, "MAX_RECEIPTS", 2):
            state = await self.activate()
            for _ in range(3):
                state = (await self.post(self.mutation(state)))["args"][0]
            path = self.path()
            descriptor = json.loads(path.read_bytes())["receipt_pages"][0]
            JOURNAL._page_path(path, descriptor["sha256"]).unlink()
            before = path.read_bytes()
            self.assert_error(await self.state(), 503, "map_journal_unavailable")
            self.assert_error(await self.post(self.mutation(state, action="clear")), 503, "map_journal_unavailable")
            self.assertEqual(path.read_bytes(), before)

    async def test_runtime_reload_recovers_committed_bytes_and_clear_without_delayed_store(self):
        initial = await self.activate()
        written = (await self.post(self.mutation(initial)))["args"][0]
        self.runtime = BRIDGE.IntegrationRuntime(configured_username="tester", app_username="tester",
            display_name="Tester", owner_user_id="owner", statistics_enabled=False)
        self.hass.data[BRIDGE.DOMAIN]["entries"]["entry"] = self.runtime
        async with BRIDGE._runtime_lock(self.runtime):
            restored = await MODULE.async_restore_current_map(self.hass, "entry", self.runtime)
        self.assertEqual(restored["revision"], written["state"]["revision"])
        self.assertEqual(next(iter(self.runtime.images.values())).image_bytes, b"\xff\xd8\xff\x00")
        cleared = (await self.post(self.mutation(written, action="clear")))["args"][0]
        self.runtime.images["stale_delayed_store_image"] = BRIDGE.HalthyImageState(unique_id="stale_delayed_store_image",
            metric_key="workout", name="Workout", image_bytes=b"\xff\xd8\xff\x01", content_type="image/jpeg",
            username="tester", device_id="phone", attributes=deepcopy(self.image["attributes"]))
        recovered = (await self.state())["args"][0]
        self.assertEqual(recovered["state"], cleared["state"])
        self.assertEqual(self.runtime.images, {})
        self.assertEqual(self.runtime.workout_image_unique_id, restored["slot_id"])

    async def test_first_replacement_reuses_registered_empty_entity_slot(self):
        self.runtime.workout_image_unique_id = "halthy_tester_old_phone_workout_existing"
        initial = await self.activate()
        await self.post(self.mutation(initial))
        self.assertEqual(set(self.runtime.images), {"halthy_tester_old_phone_workout_existing"})

    async def test_stale_changed_operation_and_other_device_replays_fail_without_mutation(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        first = (await self.post(payload))["args"][0]
        raw = self.path().read_bytes()
        self.assert_error(await self.post(self.mutation(initial)), 409, "map_revision_conflict")
        altered = deepcopy(payload); altered["image"]["attributes"]["workout_duration_s"] = 3800
        self.assert_error(await self.post(altered), 409, "map_operation_conflict")
        self.assert_error(await self.post({**payload, "device_id": "other"}), 409, "map_operation_conflict")
        self.assertEqual(self.path().read_bytes(), raw)
        self.assertEqual((await self.state())["args"][0]["state"], first["state"])

    async def test_invalid_foreign_revoked_inactive_and_local_only_context_cannot_write(self):
        payload = {**self.base, "action": "activate", "baseline_revision": None}
        for kwargs in ({"user": None}, {"grant": None}, {"grant": "unknown"}):
            self.assert_error(await self.post(payload, self.request(payload, **kwargs)), 403, "grant_not_valid")
        self.user.is_active = False
        self.assert_error(await self.post(payload), 403, "grant_not_valid")
        self.user.is_active = True; self.user.local_only = True
        request = self.request(payload); request.remote = "192.0.2.1"
        self.assert_error(await self.post(payload, request), 403, "grant_not_valid")
        self.user.local_only = False; self.runtime.owner_user_id = "other"
        self.assert_error(await self.post(payload), 403, "profile_not_owned_or_ambiguous")

    async def test_strict_schema_duplicate_keys_bad_images_and_oversized_body_fail_closed(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        for change in ({"version": True}, {"version": 2}, {"action": "unknown"}, {"device_id": " phone "},
                       {"username": "Tester"}, {"expected_revision": None}, {"operation_id": "invalid"}, {"extra": True}):
            self.assert_error(await self.post({**payload, **change}), 400, "invalid_map_transaction")
        for image in ({}, {**self.image, "content_type": "image/png"}, {**self.image, "image_base64": "%%%%"},
                      {**self.image, "attributes": {**self.image["attributes"], "archive_relative_path": "unconfirmed.jpg"}}):
            self.assert_error(await self.post({**payload, "image": image}), 400, "invalid_map_transaction")
        request = self.request(payload); request.chunks = [b'{"version":1,"version":1}']; request.content_length = len(request.chunks[0])
        self.assert_error(await self.post(payload, request), 400, "invalid_map_transaction")
        request = self.request(payload); request.content_length = 512 * 1024 + 1
        self.assert_error(await self.post(payload, request), 413, "payload_too_large")
        request = self.request(payload); request.content_type = "text/plain"
        self.assert_error(await self.post(payload, request), 415, "json_required")

    async def test_failure_before_replace_has_no_publication_and_retry_remains_exact(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        with patch.object(JOURNAL.os, "replace", side_effect=OSError("private failure")), \
             patch.object(MODULE, "async_dispatcher_send") as signal:
            self.assert_error(await self.post(payload), 503, "map_write_unconfirmed")
        self.assertEqual(self.runtime.images, {})
        signal.assert_not_called()
        self.assertTrue((await self.post(payload))["args"][0]["ok"])

    async def test_failure_after_replace_recovers_saved_receipt_without_rewriting_or_false_current(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        confirm = JOURNAL._confirm_file
        calls = 0

        def fail_commit_confirmation(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic post-replace uncertainty")
            return confirm(path)

        with patch.object(JOURNAL, "_confirm_file", side_effect=fail_commit_confirmation), \
             patch.object(MODULE, "async_dispatcher_send") as signal:
            self.assert_error(await self.post(payload), 503, "map_write_unconfirmed")
        self.assertEqual(self.runtime.images, {})
        signal.assert_not_called()
        replay = (await self.post(payload))["args"][0]
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["state"]["generation"], 1)

    async def test_cancelled_write_holds_gate_until_disk_finishes_then_restart_and_retry_recover(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        entered = asyncio.Event(); release = asyncio.Event()

        async def execute(function, *args):
            if getattr(function, "__wrapped__", function) is MODULE._commit:
                entered.set()
                await release.wait()
            return await asyncio.to_thread(function, *args)

        self.hass.async_add_executor_job = execute
        task = asyncio.create_task(self.post(payload))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        waiter_entered = asyncio.Event()

        async def waiter():
            async with BRIDGE._runtime_lock(self.runtime):
                waiter_entered.set()

        wait_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        self.assertFalse(waiter_entered.is_set())
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        await wait_task
        self.assertTrue((await self.post(payload))["args"][0]["replayed"])

    async def test_revoked_grant_is_rechecked_after_waiting_for_gate(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        lock = ObservedLock(); self.runtime.lock = lock
        await lock.lock.acquire()
        task = asyncio.create_task(self.post(payload))
        await asyncio.wait_for(lock.entered.wait(), 2)
        self.grants.clear()
        lock.lock.release()
        self.assert_error(await task, 403, "grant_not_valid")
        self.assertEqual(JOURNAL.load_snapshot(self.path(), self.runtime.map_journal_scope)["generation"], 0)

    async def test_cancelled_executor_handle_cannot_release_gate_while_worker_runs(self):
        entered = threading.Event(); release = threading.Event()
        handles = []

        def execute(function, *args):
            handle = asyncio.get_running_loop().run_in_executor(None, function, *args)
            handles.append(handle)
            return handle

        def write():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("synthetic worker not released")
            return "confirmed"

        self.hass.async_add_executor_job = execute
        task = asyncio.create_task(MODULE._io(self.hass, write))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            handles[0].cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertEqual(await task, ("confirmed", True))

    async def test_cancelled_submission_never_runs_late_queued_worker(self):
        submitted = asyncio.Event()
        wrappers = []; handles = []; writes = []

        def execute(function, *args):
            wrappers.append(lambda: function(*args))
            handle = asyncio.get_running_loop().create_future()
            handles.append(handle)
            submitted.set()
            return handle

        self.hass.async_add_executor_job = execute
        task = asyncio.create_task(MODULE._io(self.hass, lambda: writes.append("write")))
        await submitted.wait()
        handles[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await asyncio.to_thread(wrappers[0])
        self.assertEqual(writes, [])

    async def test_cancelled_executor_still_reports_uncertain_worker_failure_as_cancellation(self):
        entered = threading.Event(); release = threading.Event()
        handles = []

        def execute(function, *args):
            handle = asyncio.get_running_loop().run_in_executor(None, function, *args)
            handles.append(handle)
            return handle

        def write():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("synthetic worker not released")
            raise JOURNAL.MapJournalError("map_write_unconfirmed")

        self.hass.async_add_executor_job = execute
        task = asyncio.create_task(MODULE._io(self.hass, write))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            handles[0].cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_missing_corrupt_or_renamed_journal_fails_instead_of_adopting_empty_legacy_state(self):
        await self.activate()
        raw = self.path().read_bytes()
        self.path().unlink()
        self.assert_error(await self.state(), 503, "map_journal_missing")
        self.assert_error(await self.post({**self.base, "action": "activate", "baseline_revision": None}), 503, "map_journal_missing")
        self.path().write_bytes(b"corrupt")
        self.assert_error(await self.state(), 503, "map_journal_unavailable")
        self.path().write_bytes(raw)
        self.runtime.configured_username = "renamed"
        self.assert_error(await self.post({**self.base, "username": "renamed", "action": "state"}), 503, "map_scope_changed")

    async def test_server_shutdown_and_capacity_never_acknowledge_new_mutations(self):
        initial = await self.activate()
        self.hass.state = "STOPPING"
        self.assert_error(await self.post(self.mutation(initial)), 503, "map_server_stopping")
        self.hass.state = "RUNNING"
        with patch.object(JOURNAL, "MAX_RECEIPTS", 1), patch.object(JOURNAL, "MAX_RECEIPT_PAGES", 0):
            payload = self.mutation(initial)
            first = (await self.post(payload))["args"][0]
            self.assert_error(await self.post(self.mutation(first, action="clear")), 409, "map_journal_full")
            self.assertTrue((await self.post(payload))["args"][0]["replayed"])

    async def test_managed_legacy_newer_workout_changes_revision_and_blocks_delayed_conditional_write(self):
        initial = await self.activate()
        first = (await self.post(self.mutation(initial)))["args"][0]
        newer = deepcopy(self.image)
        newer["attributes"].update(workout_uuid=str(uuid4()), workout_start="2026-10-10T09:00:00Z", workout_end="2026-10-10T10:00:00Z")
        reply = await self.push(newer)
        self.assertTrue(reply["args"][0]["ok"])
        state = (await self.state())["args"][0]
        self.assertNotEqual(state["state"]["revision"], first["state"]["revision"])
        self.assertEqual(state["state"]["current"]["workout_uuid"], newer["attributes"]["workout_uuid"])
        self.assert_error(await self.post(self.mutation(first)), 409, "map_revision_conflict")

    async def test_managed_legacy_duplicate_is_confirmed_but_correction_older_map_or_pruning_is_not_guessed(self):
        initial = await self.activate()
        await self.post(self.mutation(initial))
        before = self.path().read_bytes()
        self.assertTrue((await self.push(self.image))["args"][0]["ok"])
        self.assertEqual(self.path().read_bytes(), before)
        corrected = deepcopy(self.image); corrected["attributes"]["workout_duration_s"] = 3700
        self.assertEqual((await self.push(corrected))["args"][0], {"error": "conditional_map_write_required"})
        self.assertEqual((await self.push(extra={"prune_unselected_metrics": True, "selected_metric_keys": ["heart_rate"]}))["args"][0],
                         {"error": "conditional_map_write_required"})
        self.assertEqual(self.path().read_bytes(), before)

    async def test_delayed_legacy_upload_cannot_resurrect_a_cleared_map(self):
        initial = await self.activate()
        first = (await self.post(self.mutation(initial)))["args"][0]
        await self.post(self.mutation(first, action="clear"))
        before = self.path().read_bytes()
        self.assertEqual((await self.push(self.image))["args"][0], {"error": "conditional_map_write_required"})
        self.assertEqual(self.runtime.images, {})
        self.assertEqual(self.path().read_bytes(), before)

    async def test_initial_activation_still_accepts_first_legacy_map_and_does_not_touch_calendar_or_sensors_for_conditional_writes(self):
        initial = await self.activate()
        legacy = (await self.push(self.image))["args"][0]
        self.assertTrue(legacy["ok"])
        self.assertEqual(len(self.runtime.images), 1)
        first = (await self.state())["args"][0]
        before_sensors = deepcopy(self.runtime.sensors)
        before_calendar = deepcopy(self.runtime.workouts)
        with patch.object(BRIDGE, "_async_archive_workout_image", new=AsyncMock()) as archive:
            await self.post(self.mutation(first, action="clear"))
        archive.assert_not_awaited()
        self.assertEqual(self.runtime.sensors, before_sensors)
        self.assertEqual(self.runtime.workouts, before_calendar)
        self.assertNotEqual(first["state"]["revision"], initial["state"]["revision"])

    async def test_actual_setup_entry_restores_journal_over_stale_delayed_store_before_platform_setup(self):
        initial = await self.activate()
        written = (await self.post(self.mutation(initial)))["args"][0]
        domain = self.hass.data[BRIDGE.DOMAIN]
        domain["stored_entries"] = {"entry": {"configured_username": "tester", "owner_user_id": "owner", "images": {}}}
        manager = types.SimpleNamespace(lock=asyncio.Lock(), suspend_entry=lambda _: None, restore_entry=lambda _: None)
        self.hass.http = types.SimpleNamespace(register_view=lambda _: None, register_static_path=lambda *args, **kwargs: None)
        self.hass.config_entries = types.SimpleNamespace(async_forward_entry_setups=AsyncMock())
        entry = types.SimpleNamespace(entry_id="entry", title="Tester", options={},
            data={BRIDGE.CONF_APP_USERNAME: "tester", BRIDGE.CONF_OWNER_USER_ID: "owner"},
            add_update_listener=lambda _: None, async_on_unload=lambda _: None)
        registry = types.SimpleNamespace(async_get_entity_id=lambda *args: None)
        with patch.object(BRIDGE, "_async_ensure_webhook_manager", new=AsyncMock(return_value=manager)), \
             patch.object(BRIDGE, "_async_collect_workout_archive_records", new=AsyncMock(return_value=[])), \
             patch.object(BRIDGE, "_current_local_day_key", return_value="2026-10-09"), \
             patch.object(BRIDGE, "_upsert_daily_upload_count_sensor"), \
             patch.object(BRIDGE, "_reschedule_force_upload_interval"), \
             patch.object(BRIDGE, "_reschedule_daily_upload_reset"), \
             patch.object(BRIDGE, "_schedule_store_save"), \
             patch.object(BRIDGE.er, "async_get", return_value=registry):
            self.assertTrue(await BRIDGE.async_setup_entry(self.hass, entry))
        restored = domain["entries"]["entry"]
        self.assertIsNot(restored, self.runtime)
        self.assertEqual(restored.integration_version, "test-installed-version")
        self.assertEqual(next(iter(restored.images.values())).image_bytes, b"\xff\xd8\xff\x00")
        self.assertEqual((await self.state())["args"][0]["state"], written["state"])
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once()

    async def test_unload_waits_for_map_writer_and_removes_runtime_only_after_commit(self):
        initial = await self.activate()
        payload = self.mutation(initial)
        entered = asyncio.Event(); release = asyncio.Event()

        async def execute(function, *args):
            if getattr(function, "__wrapped__", function) is MODULE._commit:
                entered.set(); await release.wait()
            return await asyncio.to_thread(function, *args)

        self.hass.async_add_executor_job = execute
        self.hass.config_entries = types.SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
        manager = types.SimpleNamespace(lock=asyncio.Lock(), suspend_entry=lambda entry: None)
        writer = asyncio.create_task(self.post(payload))
        await asyncio.wait_for(entered.wait(), 2)
        with patch.object(BRIDGE, "_async_ensure_webhook_manager", new=AsyncMock(return_value=manager)), \
             patch.object(BRIDGE, "_schedule_store_save"):
            unload = asyncio.create_task(BRIDGE.async_unload_entry(self.hass, types.SimpleNamespace(entry_id="entry")))
            await asyncio.sleep(0)
            self.hass.config_entries.async_unload_platforms.assert_not_awaited()
            self.assertIs(self.hass.data[BRIDGE.DOMAIN]["entries"]["entry"], self.runtime)
            release.set()
            self.assertTrue((await writer)["args"][0]["ok"])
            self.assertTrue(await unload)
        self.assertNotIn("entry", self.hass.data[BRIDGE.DOMAIN]["entries"])
        self.assertEqual(JOURNAL.load_snapshot(self.path(), self.runtime.map_journal_scope)["generation"], 1)

    async def test_view_registration_is_idempotent(self):
        registered = []
        self.hass.http = types.SimpleNamespace(register_view=registered.append)
        domain = self.hass.data[BRIDGE.DOMAIN]
        BRIDGE._ensure_http_views_registered(self.hass, domain)
        BRIDGE._ensure_http_views_registered(self.hass, domain)
        self.assertEqual(sum(type(view) is type(self.view) for view in registered), 1)
