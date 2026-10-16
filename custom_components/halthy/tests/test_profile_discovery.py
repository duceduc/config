"""Read-only owned profile discovery; local HA stubs, not live middleware."""
import types
import unittest
from unittest.mock import patch
from test_statistics import BRIDGE


class ProfileDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, username="tester", owner="owner", name="Tester"):
        return BRIDGE.IntegrationRuntime(configured_username=username, app_username=username,
                                         display_name=name, owner_user_id=owner)

    async def discover(self, entries, user="owner"):
        request = types.SimpleNamespace(
            app={BRIDGE.KEY_HASS: types.SimpleNamespace(data={BRIDGE.DOMAIN: {"entries": entries}})},
            get=lambda key: types.SimpleNamespace(id=user) if user is not None else None,
        )
        return await BRIDGE.HalthyConnectionsView().get(request)

    def test_endpoint_requires_authentication(self):
        self.assertTrue(BRIDGE.HalthyConnectionsView.requires_auth)
        self.assertEqual(BRIDGE.HalthyConnectionsView.url, "/api/halthy/connections")

    async def test_discovery_is_read_only_and_excludes_other_or_ownerless_profiles(self):
        owned, other, unowned = self.runtime(), self.runtime("other", "other"), self.runtime("unowned", None)
        with patch.object(BRIDGE, "_schedule_store_save") as save:
            reply = await self.discover({"entry": owned, "secret": other, "unclaimed": unowned})
        self.assertEqual(reply["args"][0], {"ok": True, "version": 1, "account_id": "owner",
            "profiles": [{"entry_id": "entry", "username": "tester", "display_name": "Tester"}]})
        self.assertIsNone(unowned.owner_user_id)
        self.assertEqual(owned.sensors, {})
        save.assert_not_called()

    async def test_duplicate_username_is_omitted_even_when_other_entry_is_hidden(self):
        reply = await self.discover({"entry": self.runtime(), "hidden": self.runtime(owner="other")})
        self.assertEqual(reply["args"][0]["profiles"], [])

    async def test_missing_user_is_denied_before_discovery(self):
        reply = await self.discover({"entry": self.runtime()}, None)
        self.assertEqual(reply["kwargs"]["status"], 403)

    async def test_no_integration_returns_explicit_unavailable(self):
        reply = await self.discover({})
        self.assertEqual(reply["kwargs"]["status"], 503)

    async def test_no_owned_profiles_returns_empty_not_another_accounts_data(self):
        reply = await self.discover({"entry": self.runtime(owner="other")})
        self.assertEqual(reply["args"][0]["account_id"], "owner")
        self.assertEqual(reply["args"][0]["profiles"], [])

    async def test_malformed_or_oversized_owned_profile_fails_closed(self):
        for name in ("", "bad\nname", "x" * 257):
            reply = await self.discover({"entry": self.runtime(name=name)})
            self.assertEqual(reply["kwargs"]["status"], 409)

    async def test_bounded_profile_count(self):
        reply = await self.discover({str(i): self.runtime("user_" + str(i)) for i in range(129)})
        self.assertEqual(reply["kwargs"]["status"], 409)

    def test_discovery_registration_is_idempotent(self):
        views = []
        hass = types.SimpleNamespace(http=types.SimpleNamespace(register_view=views.append))
        state = {}
        BRIDGE._ensure_http_views_registered(hass, state)
        BRIDGE._ensure_http_views_registered(hass, state)
        self.assertEqual(sum(isinstance(view, BRIDGE.HalthyConnectionsView) for view in views), 1)
