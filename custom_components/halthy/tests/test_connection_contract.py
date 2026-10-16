"""Compatibility checks for the existing authenticated Halthy endpoints.

These use the local HA stubs; they do not exercise real HTTP authentication.
"""
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_statistics import BRIDGE


class ConnectionContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester", app_username="tester",
            display_name="Tester", owner_user_id="owner", statistics_enabled=False,
        )
        self.hass = types.SimpleNamespace(
            data={BRIDGE.DOMAIN: {"entries": {"entry": self.runtime}}}
        )

    async def push(self, payload, user_id="owner"):
        body = json.dumps(payload).encode()
        user = types.SimpleNamespace(id=user_id) if user_id is not None else None
        request = types.SimpleNamespace(
            app={BRIDGE.KEY_HASS: self.hass}, content_length=len(body),
            read=AsyncMock(return_value=body), get=lambda key: user,
        )
        return await BRIDGE.HalthyPushView().post(request)

    def assert_unchanged(self):
        self.assertEqual(self.runtime.sensors, {})
        self.assertEqual(self.runtime.images, {})
        self.assertEqual(self.runtime.owner_user_id, "owner")

    def test_all_existing_views_require_ha_authentication(self):
        for view in (BRIDGE.HalthyPushView, BRIDGE.HalthyCommandView,
                     BRIDGE.HalthyCommandAckView, BRIDGE.HalthyWorkoutArchiveView,
                     BRIDGE.HalthyWorkoutArchiveImageView):
            with self.subTest(view=view.__name__):
                self.assertIs(view.requires_auth, True)

    async def test_connection_probe_does_not_write_or_claim_unowned_profile(self):
        self.runtime.owner_user_id = None
        with patch.object(BRIDGE, "_schedule_store_save") as save:
            response = await self.push({
                "username": "tester", "device_id": "phone", "connection_test": True,
                "sensors": [{"key": "heart_rate", "state": 90, "unit": "bpm"}],
            })
        self.assertEqual(response["args"][0], {
            "ok": True, "connection_test": True, "entry_id": "entry", "sync_completion_supported": True,
            "sync_completion_receipts": True,
        })
        self.assertIsNone(self.runtime.owner_user_id)
        self.assertEqual(self.runtime.sensors, {})
        self.assertEqual(self.runtime.images, {})
        save.assert_not_called()

    async def test_profile_errors_remain_distinct_and_do_not_mutate_state(self):
        cases = (
            ("tester", None, 403, "missing_user_context"),
            ("tester", "other-user", 403, "username_not_owned_by_user"),
            ("unknown", "owner", 404, "username_not_configured"),
        )
        with patch.object(BRIDGE, "_schedule_store_save") as save:
            for username, user_id, status, error in cases:
                with self.subTest(error=error):
                    response = await self.push({
                        "username": username, "connection_test": True,
                    }, user_id=user_id)
                    self.assertEqual(response["kwargs"]["status"], status)
                    self.assertEqual(response["args"][0]["error"], error)
                    self.assert_unchanged()
        save.assert_not_called()

    async def test_probe_cannot_bypass_payload_limits(self):
        response = await self.push({
            "username": "tester", "connection_test": True,
            "sensors": [{"key": "heart_rate", "state": 90}]
                       * (BRIDGE.MAX_SENSORS_PER_PUSH + 1),
        })
        self.assertEqual(response["kwargs"]["status"], 413)
        self.assertEqual(response["args"][0]["error"], "too_many_sensors")
        self.assert_unchanged()

    async def test_pruning_requires_explicit_selection(self):
        response = await self.push({
            "username": "tester", "prune_unselected_metrics": True, "sensors": [],
        })
        self.assertEqual(response["kwargs"]["status"], 400)
        self.assertEqual(response["args"][0]["error"], "missing_selected_metric_keys")
        self.assert_unchanged()
