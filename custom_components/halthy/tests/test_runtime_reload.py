"""Runtime persistence regression tests; synthetic workouts only."""
import copy
import types
import unittest
from unittest.mock import AsyncMock, patch
from test_statistics import BRIDGE


class RecordingStore:
    def __init__(self):
        self.saved = None
        self.pending = None

    async def async_save(self, payload):
        self.saved = copy.deepcopy(payload)
        self.pending = None

    def async_delay_save(self, callback, _delay):
        self.pending = callback


class RuntimeReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = BRIDGE.IntegrationRuntime(configured_username="tester", app_username="tester", display_name="Tester")
        self.store = RecordingStore()
        self.hass = types.SimpleNamespace(data={BRIDGE.DOMAIN: {
            "entries": {"entry": self.runtime}, "stored_entries": {"other": {"workouts": {"retained": {}}}},
            "store": self.store}}, config_entries=types.SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)))
        self.store_patch = patch.object(BRIDGE, "Store", RecordingStore)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def upload(self):
        BRIDGE._upsert_workout_record(self.runtime, {"workout_uuid": "synthetic-strength", "workout_type": "Strength Training",
            "workout_start": "2026-10-22T07:00:00Z", "workout_end": "2026-10-22T08:00:00Z", "workout_duration_s": 3600})
        BRIDGE._schedule_store_save(self.hass)

    async def test_unload_flushes_recent_non_route_workout_and_retains_other_profiles(self):
        self.upload()
        manager = types.SimpleNamespace(lock=__import__("asyncio").Lock(), suspend_entry=lambda _id: None)
        with patch.object(BRIDGE, "_async_ensure_webhook_manager", AsyncMock(return_value=manager)), \
             patch.object(BRIDGE, "_clear_force_upload_timer"), patch.object(BRIDGE, "_clear_daily_upload_reset_timer"):
            self.assertTrue(await BRIDGE.async_unload_entry(self.hass, types.SimpleNamespace(entry_id="entry")))
        self.assertIn("uuid:synthetic-strength", self.store.saved["entries"]["entry"]["workouts"])
        self.assertIn("other", self.store.saved["entries"])
        # A delayed save while the entry is unloaded must retain both profiles.
        payload = self.store.pending()
        self.assertEqual(payload["entries"], self.store.saved["entries"])
        restored = BRIDGE._workout_from_storage("uuid:synthetic-strength", payload["entries"]["entry"]["workouts"]["uuid:synthetic-strength"])
        self.assertEqual(restored.summary, "Strength Training")

    async def test_delayed_save_reads_latest_runtime_and_failed_unload_retains_it(self):
        BRIDGE._schedule_store_save(self.hass)
        self.upload()
        self.assertIn("uuid:synthetic-strength", self.store.pending()["entries"]["entry"]["workouts"])
        self.hass.config_entries.async_unload_platforms.return_value = False
        manager = types.SimpleNamespace(lock=__import__("asyncio").Lock(), suspend_entry=lambda _id: None)
        with patch.object(BRIDGE, "_async_ensure_webhook_manager", AsyncMock(return_value=manager)):
            self.assertFalse(await BRIDGE.async_unload_entry(self.hass, types.SimpleNamespace(entry_id="entry")))
        self.assertIs(self.hass.data[BRIDGE.DOMAIN]["entries"]["entry"], self.runtime)

    async def test_permanent_removal_only_removes_selected_entry(self):
        self.upload()
        await BRIDGE.async_remove_entry(self.hass, types.SimpleNamespace(entry_id="entry"))
        self.assertEqual(set(self.store.saved["entries"]), {"other"})

    async def test_flush_failure_does_not_unload_or_remove_runtime(self):
        self.upload()
        manager = types.SimpleNamespace(lock=__import__("asyncio").Lock())
        with patch.object(BRIDGE, "_async_ensure_webhook_manager", AsyncMock(return_value=manager)), \
             patch.object(self.store, "async_save", AsyncMock(side_effect=OSError("disk full"))):
            with self.assertRaises(OSError):
                await BRIDGE.async_unload_entry(self.hass, types.SimpleNamespace(entry_id="entry"))
        self.hass.config_entries.async_unload_platforms.assert_not_awaited()
        self.assertIs(self.hass.data[BRIDGE.DOMAIN]["entries"]["entry"], self.runtime)
