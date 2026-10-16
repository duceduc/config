"""Daily rollover policy and real bridge writes with synthetic HA dependencies."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_statistics import BRIDGE

DAILY = sys.modules[BRIDGE.prepare_daily_attributes.__module__]


def at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


NOW = at("2026-10-15T06:00:00Z")
YESTERDAY = at("2026-10-14T18:00:00Z")


def sensor(key, state=42, attributes=None):
    return BRIDGE.HalthySensorState(unique_id="unchanged_" + key, metric_key=key, name=key,
        state=state, unit="count", icon=None, attributes=attributes or {}, username="tester",
        device_id="phone", updated_at=YESTERDAY)


def period(day="2026-10-15", measured="2026-10-15T06:00:00Z", **extra):
    return {"healthkit_daily_start": day + "T00:00:00+02:00",
            "healthkit_daily_end": day + "T07:00:00+02:00",
            "reporting_time_zone": "Europe/Berlin", "measurement_timestamp": measured, **extra}


class DailyResetPolicyTests(unittest.TestCase):
    def test_entire_explicit_catalog_resets_without_renaming(self):
        keys = DAILY.DAILY_COUNTER_KEYS
        self.assertGreater(len(keys), 130)
        sensors = {key: sensor(key) for key in keys}
        self.assertEqual(set(DAILY.rollover_daily_sensors(sensors, NOW, "Europe/Berlin")), set(keys))
        for key, value in sensors.items():
            self.assertEqual(value.state, 0, key)
            self.assertEqual(value.unique_id, "unchanged_" + key)
            self.assertEqual(value.metric_key, key)
            self.assertIs(value.attributes["daily_source_fresh"], False)
        for key in ("handwash_in_g_event", "sleep_apnea_event", "sleep_change_s", "distance_row_in_g",
                    "in_haler_usage", "in_sul_in_delivery", "screen_time", "pickups_without_app_use"):
            self.assertIn(key, keys)

    def test_midnight_is_not_an_upload_or_new_source_measurement(self):
        old = sensor("steps", attributes=period("2026-10-14", "2026-10-14T18:00:00Z"))
        sensors = {old.unique_id: old}
        DAILY.rollover_daily_sensors(sensors, NOW, "UTC")
        reset = sensors[old.unique_id]
        self.assertEqual(reset.attributes["source_measurement_timestamp"], "2026-10-14T18:00:00Z")
        self.assertEqual(reset.attributes["last_source_pushed"], YESTERDAY.isoformat())
        self.assertNotIn("measurement_timestamp", reset.attributes)
        self.assertEqual(reset.attributes["daily_period_start"], "2026-10-15T00:00:00+02:00")
        self.assertEqual(DAILY.rollover_daily_sensors(sensors, NOW, "UTC"), [])

    def test_first_pickup_clears_to_unknown_but_last_sessions_and_measurements_survive(self):
        preserved = ["sleep_duration", "sleep_in_bed", "sleep_core", "sleep_score", "sleep_change",
                     "fall_asleep_time", "go_to_bed_time", "wake_up_time", "heart_rate", "body_mass",
                     "six_minute_walk_test_distance", "workout_start", "workout_end", "last_full_sync"]
        sensors = {key: sensor(key) for key in preserved + ["first_pickup_time"]}
        self.assertEqual(DAILY.rollover_daily_sensors(sensors, NOW, "Europe/Berlin"), ["first_pickup_time"])
        self.assertEqual(sensors["first_pickup_time"].state, "unknown")
        for key in preserved:
            self.assertEqual(sensors[key].state, 42)

    def test_explicit_new_daily_semantics_supported_but_sleep_semantics_protected(self):
        for semantics in ("category_event_count", "stood_hours_count", "cumulative_total", "workout_count"):
            self.assertEqual(DAILY.reset_kind("future_counter", {"healthkit_daily_semantics": semantics}), "zero")
        self.assertIsNone(DAILY.reset_kind("future_sleep", {"healthkit_daily_semantics": "sleep_asleep_hours"}))
        self.assertIsNone(DAILY.reset_kind("heart_rate", {"healthkit_daily_semantics": []}))

    def test_reporting_zone_and_legacy_ha_fallback(self):
        instant = at("2026-10-14T22:00:00Z")
        values = {"source": sensor("steps", attributes={"reporting_time_zone": "Europe/Berlin"}),
                  "legacy": sensor("steps"), "west": sensor("steps", attributes={"reporting_time_zone": "America/New_York"})}
        self.assertEqual(DAILY.rollover_daily_sensors(values, instant, "Europe/Berlin"), ["source", "legacy"])
        self.assertEqual(values["west"].state, 42)

    def test_foundation_fixed_offset_zones_survive_storage_round_trip(self):
        for zone in ("GMT+0530", "UTC+05:30", "GMT-0330"):
            attrs, live = DAILY.prepare_daily_attributes("steps", {"reporting_time_zone": zone}, NOW, "UTC")
            self.assertTrue(live)
            self.assertEqual(DAILY.reporting_zone(attrs, "UTC").utcoffset(NOW),
                             DAILY.reporting_zone({"reporting_time_zone": zone}, "UTC").utcoffset(NOW))

    def test_calendar_midnight_on_short_and_long_dst_days(self):
        zone = ZoneInfo("Europe/Berlin")
        for start, end, hours in (("2026-03-29T00:00:00+01:00", "2026-03-30T00:00:00+02:00", 23),
                                  ("2026-10-25T00:00:00+02:00", "2026-10-26T00:00:00+01:00", 25)):
            with self.subTest(start=start):
                self.assertEqual((at(end) - at(start)).total_seconds(), hours * 3600)
                attrs = {"day_start": start, "day_end": end, "reporting_time_zone": "Europe/Berlin"}
                value = sensor("screen_time", attributes=attrs)
                sensors = {"digital": value}
                before = at(end).timestamp() - 1
                self.assertEqual(DAILY.rollover_daily_sensors(sensors, datetime.fromtimestamp(before, timezone.utc), "UTC"), [])
                self.assertEqual(DAILY.rollover_daily_sensors(sensors, at(end), "UTC"), ["digital"])
                self.assertEqual(sensors["digital"].attributes["daily_period_start"], at(end).astimezone(zone).isoformat())

    def test_current_values_are_totals_not_additions_and_cannot_forge_reset_provenance(self):
        for value in (0, 10, 4):
            attrs, live = DAILY.prepare_daily_attributes("steps", period(daily_reset=True, daily_source_fresh=False,
                daily_reporting_date="1990-01-01", last_source_pushed="forged"), NOW, "UTC")
            self.assertTrue(live)
            self.assertIs(attrs["daily_reset"], False)
            self.assertIs(attrs["daily_source_fresh"], True)
            self.assertEqual(attrs["daily_reporting_date"], "2026-10-15")
            self.assertNotIn("last_source_pushed", attrs)

    def test_old_day_and_history_never_replace_current_day(self):
        for attrs in (period("2026-10-14"), period(historical_daily_snapshot=True),
                      {"measurement_timestamp": "2026-10-14T10:00:00Z"}):
            self.assertFalse(DAILY.prepare_daily_attributes("steps", attrs, NOW, "Europe/Berlin")[1])
        self.assertTrue(DAILY.prepare_daily_attributes("heart_rate", period("2026-10-14"), NOW, "Europe/Berlin")[1])

    def test_invalid_periods_rejected_and_corrupt_old_storage_does_not_block_rollover(self):
        invalid = [{"reporting_time_zone": "invalid"}, {"reporting_time_zone": []},
                   {"day_start": "bad"}, {"day_end": "2026-10-15T00:00:00Z"},
                   period(healthkit_daily_end="2026-10-17T00:00:00Z"), period("2026-10-16")]
        for attrs in invalid:
            with self.subTest(attrs=attrs), self.assertRaises(ValueError):
                DAILY.prepare_daily_attributes("steps", attrs, NOW, "UTC")
        sensors = {"steps": sensor("steps", attributes={"day_start": "broken"})}
        self.assertEqual(DAILY.rollover_daily_sensors(sensors, NOW, "UTC"), ["steps"])

    def test_saved_restart_state_keeps_reset_idempotent_and_offline_days_catch_up(self):
        sensors = {"steps": sensor("steps", attributes=period("2026-10-14"))}
        DAILY.rollover_daily_sensors(sensors, NOW, "UTC")
        saved = BRIDGE._sensor_to_storage(sensors["steps"])
        restored = {"steps": BRIDGE._sensor_from_storage("steps", saved)}
        self.assertEqual(DAILY.rollover_daily_sensors(restored, NOW, "UTC"), [])
        self.assertEqual(DAILY.rollover_daily_sensors(restored, at("2026-10-18T06:00:00Z"), "UTC"), ["steps"])
        self.assertEqual(restored["steps"].attributes["daily_reporting_date"], "2026-10-18")


class DailyResetBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = BRIDGE.IntegrationRuntime(configured_username="tester", app_username="tester",
            display_name="Tester", owner_user_id="owner", statistics_enabled=False,
            daily_upload_count=5, daily_upload_count_day="2026-10-14")
        async def execute(function, *args):
            return function(*args)
        self.hass = types.SimpleNamespace(data={BRIDGE.DOMAIN: {"entries": {"entry": self.runtime}}},
            config=types.SimpleNamespace(time_zone="Europe/Berlin", units=types.SimpleNamespace(temperature_unit="C"),
                path=lambda relative: str(Path(self.directory.name) / relative)), async_add_executor_job=execute)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz or timezone.utc)
        for name, replacement in (("datetime", Clock), ("_schedule_store_save", None),
                                  ("_async_emit_activity_log_entries", AsyncMock()),
                                  ("_current_local_day_key", lambda hass: "2026-10-15")):
            context = patch.object(BRIDGE, name, replacement) if replacement is not None else patch.object(BRIDGE, name)
            context.start()
            self.addCleanup(context.stop)
        registry = types.SimpleNamespace(async_get_entity_id=lambda *args: None)
        context = patch.object(BRIDGE.er, "async_get", return_value=registry)
        context.start()
        self.addCleanup(context.stop)

    async def push(self, state, attrs, key="steps"):
        payload = {"username": "tester", "device_id": "phone", "sensors": [
            {"key": key, "state": state, "unit": "count", "attributes": attrs}]}
        body = json.dumps(payload).encode()
        request = types.SimpleNamespace(app={BRIDGE.KEY_HASS: self.hass}, content_length=len(body),
            read=AsyncMock(return_value=body), get=lambda _: types.SimpleNamespace(id="owner"))
        return await BRIDGE.HalthyPushView().post(request)

    async def test_timer_resets_health_and_diagnostic_without_completing_sync(self):
        self.runtime.sensors["unchanged_steps"] = sensor("steps")
        self.runtime.sensors["sync"] = sensor("last_full_sync", "old-sync")
        await BRIDGE._async_reset_daily_upload_counter_for_entry(self.hass, "entry")
        self.assertEqual(self.runtime.sensors["unchanged_steps"].state, 0)
        self.assertEqual(self.runtime.daily_upload_count, 0)
        self.assertEqual(self.runtime.sensors["sync"].state, "old-sync")

    async def test_timer_checks_every_minute_boundary_and_cancels_previous_subscription(self):
        cancel = unittest.mock.Mock()
        self.hass.data[BRIDGE.DOMAIN]["daily_upload_reset_timers"] = {"entry": cancel}
        with patch.object(BRIDGE, "async_track_time_change", return_value=lambda: None) as track:
            BRIDGE._reschedule_daily_upload_reset(self.hass, "entry")
        cancel.assert_called_once()
        self.assertEqual(track.call_args.kwargs, {"second": 0})

    async def test_old_queued_write_after_midnight_cannot_resurrect_daily_value(self):
        self.runtime.sensors["unchanged_steps"] = sensor("steps", 9000, period("2026-10-14", "2026-10-14T18:00:00Z"))
        result = await self.push(9999, period("2026-10-14", "2026-10-14T20:00:00Z"))
        self.assertTrue(result["args"][0]["ok"])
        self.assertEqual(result["args"][0]["ignored_older"], 1)
        self.assertEqual(self.runtime.sensors["unchanged_steps"].state, 0)
        self.assertEqual(self.runtime.daily_upload_count, 5)
        await self.push(10, period())
        self.assertEqual(self.runtime.sensors["unchanged_steps"].state, 10)
        await self.push(4, period(measured="2026-10-15T06:01:00Z"))
        self.assertEqual(self.runtime.sensors["unchanged_steps"].state, 4)
        self.assertIs(self.runtime.sensors["unchanged_steps"].attributes["daily_reset"], False)

    async def test_historical_daily_values_still_reach_statistics_import(self):
        self.runtime.statistics_enabled = True
        with patch.object(BRIDGE, "_prepare_statistics_imports_for_runtime", return_value=([], {})) as prepare:
            result = await self.push(7000, period("2026-10-14", "2026-10-14T18:00:00Z", historical_daily_snapshot=True))
        self.assertTrue(result["args"][0]["ok"])
        self.assertNotIn("steps", [value.metric_key for value in self.runtime.sensors.values()])
        self.assertGreater(len(prepare.call_args.args[1]), 0)

    async def test_invalid_daily_payload_fails_before_any_state_mutation(self):
        self.runtime.sensors["unchanged_steps"] = sensor("steps", 9000)
        response = await self.push(1, period(reporting_time_zone="not-a-zone"))
        self.assertEqual(response["kwargs"]["status"], 400)
        self.assertEqual(self.runtime.sensors["unchanged_steps"].state, 9000)
