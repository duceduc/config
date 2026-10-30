"""Workout boundary sensors follow calendar data without requiring map uploads."""

import asyncio
from copy import deepcopy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_statistics import BRIDGE
from test_paired_webhook import Request


class WorkoutSensorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester", app_username="Tester", display_name="Tester",
            owner_user_id="owner", statistics_enabled=False,
        )
        self.new_workout = {
            "workout_uuid": "74d459fe-20aa-4180-b19c-e198b3a712ac",
            "workout_type": "Walking",
            "workout_start": "2026-10-28T09:00:00Z",
            "workout_end": "2026-10-28T10:00:00Z",
        }
        self.ids = []
        for boundary in ("start", "end"):
            key = f"workout_route_{boundary}"
            uid = BRIDGE._unique_sensor_id("tester", "old_phone", key)
            self.ids.append(uid)
            self.runtime.sensors[uid] = BRIDGE.HalthySensorState(
                unique_id=uid, metric_key=key, name=f"Workout {boundary}",
                state=f"2026-10-09T{'09' if boundary == 'start' else '10'}:00:00Z",
                unit=None, icon="mdi:run-fast", username="Tester", device_id="old_phone",
                attributes={"measurement_timestamp": "2026-10-09T10:00:00Z"},
            )

    def test_calendar_updates_existing_ids_without_map_and_does_not_regress(self):
        BRIDGE._upsert_workout_record(self.runtime, self.new_workout)
        self.assertTrue(BRIDGE._upsert_latest_workout_sensors(None, self.runtime, "entry"))
        self.assertEqual(set(self.runtime.sensors), set(self.ids))
        for uid, boundary in zip(self.ids, ("start", "end")):
            sensor = self.runtime.sensors[uid]
            self.assertEqual(sensor.state, self.new_workout[f"workout_{boundary}"])
            self.assertEqual(sensor.device_id, "old_phone")
            self.assertEqual(sensor.attributes["workout_uuid"], self.new_workout["workout_uuid"])
        original = deepcopy(self.runtime.sensors)
        older = {**self.new_workout, "workout_uuid": "84d459fe-20aa-4180-b19c-e198b3a712ac",
                 "workout_start": "2026-10-20T09:00:00Z", "workout_end": "2026-10-20T10:00:00Z"}
        BRIDGE._upsert_workout_record(self.runtime, older)
        self.assertFalse(BRIDGE._upsert_latest_workout_sensors(None, self.runtime, "entry"))
        self.assertEqual(self.runtime.sensors, original)
        self.assertEqual(self.runtime.images, {})

    def test_corrected_same_workout_can_move_its_end_backwards(self):
        BRIDGE._upsert_workout_record(self.runtime, self.new_workout)
        BRIDGE._upsert_latest_workout_sensors(None, self.runtime, "entry")
        corrected = {**self.new_workout, "workout_end": "2026-10-28T09:55:00Z"}
        BRIDGE._upsert_workout_record(self.runtime, corrected)
        self.assertTrue(BRIDGE._upsert_latest_workout_sensors(None, self.runtime, "entry"))
        self.assertEqual(self.runtime.sensors[self.ids[1]].state, corrected["workout_end"])

    def test_older_calendar_does_not_replace_a_newer_direct_sensor(self):
        BRIDGE._upsert_workout_record(self.runtime, self.new_workout)
        for sensor in self.runtime.sensors.values():
            sensor.attributes["measurement_timestamp"] = "2026-10-29T10:00:00Z"
            sensor.attributes["workout_uuid"] = "newer-workout"
        self.assertFalse(BRIDGE._upsert_latest_workout_sensors(None, self.runtime, "entry"))

    def test_metadata_only_push_refreshes_workout_sensors(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                async def execute(function, *args):
                    return function(*args)

                hass = SimpleNamespace(
                    data={BRIDGE.DOMAIN: {"entries": {"entry": self.runtime}}},
                    config=SimpleNamespace(units=SimpleNamespace(temperature_unit="\u00b0C"),
                                           path=lambda relative: directory + "/" + relative),
                    async_add_executor_job=execute,
                )
                request = Request(hass, {"username": "tester", "device_id": "new_phone",
                                        "workouts": [self.new_workout]},
                                  user=SimpleNamespace(id="owner", is_active=True, local_only=False))
                request.read = AsyncMock(return_value=request.raw)
                registry = SimpleNamespace(async_get_entity_id=lambda *args: None)
                with patch.object(BRIDGE, "_schedule_store_save"), \
                     patch.object(BRIDGE, "_async_emit_activity_log_entries", new=AsyncMock()), \
                     patch.object(BRIDGE.er, "async_get", return_value=registry):
                    response = await BRIDGE.HalthyPushView().post(request)
                self.assertTrue(response["args"][0]["ok"])
                self.assertEqual(self.runtime.sensors[self.ids[1]].state, self.new_workout["workout_end"])
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
