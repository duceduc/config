"""Sleep estimates can be replaced by recorded data without changing the value."""

import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_statistics import BRIDGE
from test_paired_webhook import Request


class SleepProvenanceTests(unittest.TestCase):
    def test_same_measurement_updates_provenance_and_then_deduplicates(self):
        async def exercise():
            async def execute(function, *args):
                return function(*args)

            runtime = BRIDGE.IntegrationRuntime(
                configured_username="tester", app_username="tester", display_name="Tester",
                owner_user_id="owner", statistics_enabled=False,
            )
            hass = SimpleNamespace(
                data={BRIDGE.DOMAIN: {"entries": {"entry": runtime}}},
                config=SimpleNamespace(units=SimpleNamespace(temperature_unit="\u00b0C"),
                                       path=lambda relative: directory + "/" + relative),
                async_add_executor_job=execute,
            )
            user = SimpleNamespace(id="owner", is_active=True, local_only=False)
            registry = SimpleNamespace(async_get_entity_id=lambda *args: None)
            for estimated in (True, False, False):
                attributes = {
                    "measurement_timestamp": "2026-10-29T05:00:00Z",
                    "estimated": estimated,
                    "value_source": "sleep_session_estimate" if estimated else "healthkit_in_bed",
                    "in_bed_start": "2026-10-28T21:00:00Z",
                    "in_bed_end": "2026-10-29T05:00:00Z",
                }
                payload = {"username": "tester", "device_id": "phone", "sensors": [
                    {"key": "go_to_bed_time", "state": attributes["in_bed_start"], "attributes": attributes},
                    {"key": "sleep_in_bed", "state": 8, "unit": "h", "attributes": attributes},
                ]}
                request = Request(hass, payload, user=user)
                request.read = AsyncMock(return_value=request.raw)
                with patch.object(BRIDGE, "_schedule_store_save"), \
                     patch.object(BRIDGE, "_async_emit_activity_log_entries", new=AsyncMock()), \
                     patch.object(BRIDGE.er, "async_get", return_value=registry):
                    response = await BRIDGE.HalthyPushView().post(request)
                self.assertTrue(response["args"][0]["ok"])
                sensors = [sensor for sensor in runtime.sensors.values()
                           if sensor.metric_key in ("go_to_bed_time", "sleep_in_bed")]
                self.assertEqual(len(sensors), 2)
                for sensor in sensors:
                    self.assertEqual(sensor.attributes["estimated"], estimated)
                    self.assertEqual(sensor.attributes["value_source"], attributes["value_source"])
            self.assertEqual(response["args"][0]["duplicates"], 2)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(exercise())
