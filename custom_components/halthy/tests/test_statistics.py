"""Unit tests for statistics import helpers."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
import unittest


def _install_homeassistant_stubs() -> None:
    if "voluptuous" not in sys.modules:
        voluptuous_module = types.ModuleType("voluptuous")
        voluptuous_module.Schema = lambda schema, *args, **kwargs: schema
        voluptuous_module.Optional = lambda key, *args, **kwargs: key
        voluptuous_module.Required = lambda key, *args, **kwargs: key
        sys.modules["voluptuous"] = voluptuous_module

    if "aiohttp" not in sys.modules:
        aiohttp_module = types.ModuleType("aiohttp")
        web_module = types.SimpleNamespace(
            Request=type("Request", (), {}),
            Response=type("Response", (), {}),
            json_response=lambda *args, **kwargs: {"args": args, "kwargs": kwargs},
        )
        aiohttp_module.web = web_module
        sys.modules["aiohttp"] = aiohttp_module

    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")

    const_module = sys.modules.setdefault(
        "homeassistant.const",
        types.ModuleType("homeassistant.const"),
    )
    if not hasattr(const_module, "Platform"):
        const_module.Platform = type("Platform", (), {"SENSOR": "sensor"})
    if not hasattr(const_module, "UnitOfTemperature"):
        const_module.UnitOfTemperature = type(
            "UnitOfTemperature",
            (),
            {"CELSIUS": "°C", "FAHRENHEIT": "°F"},
        )

    if "homeassistant.components" not in sys.modules:
        sys.modules["homeassistant.components"] = types.ModuleType("homeassistant.components")

    http_module = sys.modules.setdefault(
        "homeassistant.components.http",
        types.ModuleType("homeassistant.components.http"),
    )
    if not hasattr(http_module, "KEY_HASS"):
        http_module.KEY_HASS = "hass"
    if not hasattr(http_module, "HomeAssistantView"):
        http_module.HomeAssistantView = type("HomeAssistantView", (), {})

    recorder_statistics_module = sys.modules.setdefault(
        "homeassistant.components.recorder.statistics",
        types.ModuleType("homeassistant.components.recorder.statistics"),
    )
    if not hasattr(recorder_statistics_module, "async_add_external_statistics"):

        async def _default_add_external_statistics(_hass, _metadata, _rows):
            return None

        recorder_statistics_module.async_add_external_statistics = _default_add_external_statistics

    recorder_models_module = sys.modules.setdefault(
        "homeassistant.components.recorder.models.statistics",
        types.ModuleType("homeassistant.components.recorder.models.statistics"),
    )
    if not hasattr(recorder_models_module, "StatisticMeanType"):
        recorder_models_module.StatisticMeanType = type(
            "StatisticMeanType",
            (),
            {"ARITHMETIC": 1},
        )
    if not hasattr(recorder_models_module, "StatisticMetaData"):

        class _StatisticMetaData:
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        recorder_models_module.StatisticMetaData = _StatisticMetaData
    if not hasattr(recorder_models_module, "StatisticData"):

        class _StatisticData:
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        recorder_models_module.StatisticData = _StatisticData

    if "homeassistant.helpers" not in sys.modules:
        sys.modules["homeassistant.helpers"] = types.ModuleType("homeassistant.helpers")

    config_validation_module = sys.modules.setdefault(
        "homeassistant.helpers.config_validation",
        types.ModuleType("homeassistant.helpers.config_validation"),
    )
    if not hasattr(config_validation_module, "string"):
        config_validation_module.string = str
    if not hasattr(config_validation_module, "config_entry_only_config_schema"):
        config_validation_module.config_entry_only_config_schema = lambda domain: {domain: {}}

    entity_registry_module = sys.modules.setdefault(
        "homeassistant.helpers.entity_registry",
        types.ModuleType("homeassistant.helpers.entity_registry"),
    )
    if not hasattr(entity_registry_module, "async_get"):
        entity_registry_module.async_get = lambda _hass: None
    if not hasattr(entity_registry_module, "async_entries_for_config_entry"):
        entity_registry_module.async_entries_for_config_entry = lambda _registry, _entry_id: []

    dispatcher_module = sys.modules.setdefault(
        "homeassistant.helpers.dispatcher",
        types.ModuleType("homeassistant.helpers.dispatcher"),
    )
    if not hasattr(dispatcher_module, "async_dispatcher_send"):
        dispatcher_module.async_dispatcher_send = lambda *_args, **_kwargs: None

    event_module = sys.modules.setdefault(
        "homeassistant.helpers.event",
        types.ModuleType("homeassistant.helpers.event"),
    )
    if not hasattr(event_module, "async_track_time_change"):
        event_module.async_track_time_change = lambda *_args, **_kwargs: (lambda: None)
    if not hasattr(event_module, "async_track_time_interval"):
        event_module.async_track_time_interval = lambda *_args, **_kwargs: (lambda: None)

    storage_module = sys.modules.setdefault(
        "homeassistant.helpers.storage",
        types.ModuleType("homeassistant.helpers.storage"),
    )
    if not hasattr(storage_module, "Store"):

        class _Store:
            def __init__(self, *_args, **_kwargs):
                return

            async def async_load(self):
                return None

            def async_delay_save(self, *_args, **_kwargs):
                return

        storage_module.Store = _Store

    config_entries_module = sys.modules.setdefault(
        "homeassistant.config_entries",
        types.ModuleType("homeassistant.config_entries"),
    )
    if not hasattr(config_entries_module, "ConfigEntry"):
        config_entries_module.ConfigEntry = type("ConfigEntry", (), {})

    core_module = sys.modules.setdefault(
        "homeassistant.core",
        types.ModuleType("homeassistant.core"),
    )
    if not hasattr(core_module, "HomeAssistant"):
        core_module.HomeAssistant = type("HomeAssistant", (), {})
    if not hasattr(core_module, "ServiceCall"):
        core_module.ServiceCall = type("ServiceCall", (), {"data": {}})
    if not hasattr(core_module, "callback"):
        core_module.callback = lambda func: func

    if "homeassistant.util" not in sys.modules:
        sys.modules["homeassistant.util"] = types.ModuleType("homeassistant.util")
    dt_module = sys.modules.setdefault(
        "homeassistant.util.dt",
        types.ModuleType("homeassistant.util.dt"),
    )
    if not hasattr(dt_module, "now"):
        dt_module.now = lambda: datetime.now(timezone.utc)


def _load_integration_module():
    _install_homeassistant_stubs()

    package_name = "halthy_testpkg_stats"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = []  # type: ignore[attr-defined]
        sys.modules[package_name] = package

    base_path = pathlib.Path(__file__).resolve().parents[1]
    for module_base in (
        "const",
        "naming",
        "units",
        "runtime",
        "timestamps",
        "statistics",
        "workout_archive",
    ):
        module_name = f"{package_name}.{module_base}"
        if module_name in sys.modules:
            continue
        module_path = base_path / f"{module_base}.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        if module_base == "runtime":
            original_dataclass = dataclasses.dataclass

            def _runtime_compat_dataclass(*args, **kwargs):
                kwargs.pop("slots", None)
                return original_dataclass(*args, **kwargs)

            dataclasses.dataclass = _runtime_compat_dataclass  # type: ignore[assignment]
            try:
                spec.loader.exec_module(module)
            finally:
                dataclasses.dataclass = original_dataclass  # type: ignore[assignment]
        else:
            spec.loader.exec_module(module)

    module_name = f"{package_name}.bridge"
    if module_name in sys.modules:
        return (
            sys.modules[module_name],
            sys.modules[f"{package_name}.workout_archive"],
            sys.modules[f"{package_name}.statistics"],
            sys.modules[f"{package_name}.timestamps"],
        )

    module_path = base_path / "__init__.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    # Python 3.9 does not support dataclass(slots=True), so ignore this kwarg while loading.
    original_dataclass = dataclasses.dataclass

    def _compat_dataclass(*args, **kwargs):
        kwargs.pop("slots", None)
        return original_dataclass(*args, **kwargs)

    dataclasses.dataclass = _compat_dataclass  # type: ignore[assignment]
    try:
        spec.loader.exec_module(module)
    finally:
        dataclasses.dataclass = original_dataclass  # type: ignore[assignment]
    return (
        module,
        sys.modules[f"{package_name}.workout_archive"],
        sys.modules[f"{package_name}.statistics"],
        sys.modules[f"{package_name}.timestamps"],
    )


BRIDGE, ARCHIVE, STATISTICS, TIMESTAMPS = _load_integration_module()


class StatisticsHelpersTests(unittest.TestCase):
    def test_workout_archive_timestamp_from_file_name_formats(self) -> None:
        self.assertEqual(
            ARCHIVE._workout_archive_timestamp_from_file_name("20260323T123436Z_uuid_test.jpg"),
            datetime(2026, 3, 23, 12, 34, 36, tzinfo=timezone.utc),
        )
        self.assertEqual(
            ARCHIVE._workout_archive_timestamp_from_file_name("workout_2026-04-01_10-11-12_map.png"),
            datetime(2026, 4, 1, 10, 11, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(
            ARCHIVE._workout_archive_timestamp_from_file_name("route_20260402.jpg"),
            datetime(2026, 4, 2, 0, 0, 0, tzinfo=timezone.utc),
        )

    def test_workout_calendar_record_normalizes_title_and_times(self) -> None:
        record = BRIDGE._workout_record_from_attributes(
            {
                "workout_uuid": "ABCD-1234",
                "workout_type": "HKWorkoutActivityTypeOutdoorCycling",
                "workout_start": "2026-04-01T10:00:00Z",
                "workout_end": "2026-04-01T11:15:00Z",
                "workout_distance_m": 25000,
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.record_id, "uuid:abcd-1234")
        self.assertEqual(record.uid, "ABCD-1234")
        self.assertEqual(record.summary, "Outdoor Cycling")
        self.assertEqual(record.start, datetime(2026, 4, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(record.end, datetime(2026, 4, 1, 11, 15, tzinfo=timezone.utc))

    def test_workout_calendar_record_uses_timestamp_and_duration_fallback(self) -> None:
        record = BRIDGE._workout_record_from_attributes(
            {
                "workout_type": "walking",
                "measurement_timestamp": "2026-04-01T11:00:00Z",
                "workout_duration_s": 1800,
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.summary, "Walking")
        self.assertEqual(record.start, datetime(2026, 4, 1, 10, 30, tzinfo=timezone.utc))
        self.assertEqual(record.end, datetime(2026, 4, 1, 11, tzinfo=timezone.utc))

    def test_workout_calendar_upsert_deduplicates_by_uuid(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester",
            app_username="tester",
            display_name="Tester",
        )
        original = {
            "workout_uuid": "same-workout",
            "workout_type": "walking",
            "workout_start": "2026-04-01T10:00:00Z",
            "workout_end": "2026-04-01T10:30:00Z",
            "avg_heart_rate_bpm": 123,
        }

        self.assertEqual(BRIDGE._upsert_workout_record(runtime, original), "created")
        self.assertEqual(BRIDGE._upsert_workout_record(runtime, original), "duplicate")
        self.assertEqual(
            BRIDGE._upsert_workout_record(runtime, {**original, "workout_type": "hiking"}),
            "updated",
        )
        self.assertEqual(len(runtime.workouts), 1)
        updated = next(iter(runtime.workouts.values()))
        self.assertEqual(updated.summary, "Hiking")
        self.assertEqual(updated.metadata["avg_heart_rate_bpm"], 123)

    def test_workout_calendar_upsert_migrates_fallback_identity_to_uuid(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester",
            app_username="tester",
            display_name="Tester",
        )
        workout = {
            "workout_type": "walking",
            "workout_start": "2026-04-01T10:00:00Z",
            "workout_end": "2026-04-01T10:30:00Z",
        }
        self.assertEqual(BRIDGE._upsert_workout_record(runtime, workout), "created")

        self.assertEqual(
            BRIDGE._upsert_workout_record(
                runtime,
                {**workout, "workout_uuid": "new-stable-id"},
            ),
            "updated",
        )
        self.assertEqual(set(runtime.workouts), {"uuid:new-stable-id"})

    def test_workout_calendar_storage_round_trip(self) -> None:
        record = BRIDGE._workout_record_from_attributes(
            {
                "workout_uuid": "round-trip",
                "workout_type": "running",
                "workout_start": "2026-04-02T06:00:00Z",
                "workout_end": "2026-04-02T06:45:00Z",
                "workout_active_energy_kcal": 420,
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        restored = BRIDGE._workout_from_storage(
            record.record_id,
            BRIDGE._workout_to_storage(record),
        )
        self.assertEqual(restored, record)

    def test_collect_workout_archive_records_includes_legacy_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = pathlib.Path(temp_dir)
            modern_dir = media_root / "halthy" / "workouts" / "tester"
            legacy_dir = media_root / "halthy_bridge" / "workouts" / "tester"
            modern_dir.mkdir(parents=True, exist_ok=True)
            legacy_dir.mkdir(parents=True, exist_ok=True)

            modern_file = modern_dir / "20260401T101112Z_uuid_modern.jpg"
            legacy_file = legacy_dir / "20260323T123436Z_uuid_legacy.png"
            modern_file.write_bytes(b"modern")
            legacy_file.write_bytes(b"legacy")
            os.utime(modern_file, (1711966272, 1711966272))
            os.utime(legacy_file, (1710765276, 1710765276))

            records = ARCHIVE._collect_workout_archive_records(media_root, "tester", limit=10)

            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["file_name"], "20260401T101112Z_uuid_modern.jpg")
            self.assertEqual(records[0]["day_key"], "2026-04-01")
            self.assertEqual(records[1]["file_name"], "20260323T123436Z_uuid_legacy.png")
            self.assertEqual(records[1]["day_key"], "2026-03-23")
            self.assertTrue(records[0]["local_url"].startswith("/media/local/"))
            self.assertIn("/halthy/workouts/tester/", records[0]["local_url"])

    def test_collect_workout_archive_records_prefers_newest_copy_of_same_workout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = pathlib.Path(temp_dir)
            modern_dir = media_root / "halthy" / "workouts" / "tester"
            legacy_dir = media_root / "halthy_bridge" / "workouts" / "tester"
            modern_dir.mkdir(parents=True, exist_ok=True)
            legacy_dir.mkdir(parents=True, exist_ok=True)

            file_name = "20260401T101112Z_uuid_same_workout.jpg"
            modern_file = modern_dir / file_name
            legacy_file = legacy_dir / file_name
            modern_file.write_bytes(b"new-layout")
            legacy_file.write_bytes(b"old-layout")
            os.utime(legacy_file, (1710765276, 1710765276))
            os.utime(modern_file, (1711966272, 1711966272))

            records = ARCHIVE._collect_workout_archive_records(media_root, "tester", limit=10)

            self.assertEqual(len(records), 1)
            self.assertIn("/halthy/workouts/tester/", records[0]["local_url"])

    def test_workout_archive_image_url_contains_no_access_token(self) -> None:
        url = ARCHIVE._workout_archive_image_url(
            "tester_1",
            "halthy/workouts/tester_1/20260401T101112Z_uuid.jpg",
            "2026-04-01T10:11:13Z",
        )
        self.assertIn("/api/halthy/workout_image?", url)
        self.assertIn("username=tester_1", url)
        self.assertNotIn("token=", url)
        self.assertIn("v=2026-04-01T10%3A11%3A13Z", url)

    def test_image_content_type_requires_matching_signature(self) -> None:
        self.assertEqual(
            ARCHIVE._validated_image_content_type("image/jpeg", b"\xff\xd8\xffpayload"),
            "image/jpeg",
        )
        self.assertEqual(
            ARCHIVE._validated_image_content_type("image/png", b"\x89PNG\r\n\x1a\npayload"),
            "image/png",
        )
        self.assertIsNone(ARCHIVE._validated_image_content_type("image/jpeg", b"not-an-image"))
        self.assertIsNone(ARCHIVE._validated_image_content_type("image/svg+xml", b"<svg/>"))

    def test_workout_archive_prunes_oldest_images_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_dir = pathlib.Path(temporary_directory)
            image_paths: list[pathlib.Path] = []
            for index in range(27):
                image_path = archive_dir / f"workout_{index:02d}.jpg"
                image_path.write_bytes(b"\xff\xd8\xffpayload")
                ARCHIVE._workout_archive_metadata_path(image_path).write_text("{}")
                os.utime(image_path, (index + 1, index + 1))
                image_paths.append(image_path)

            removed = ARCHIVE._prune_workout_archive_files(archive_dir, 25)

            self.assertEqual(removed, 2)
            self.assertFalse(image_paths[0].exists())
            self.assertFalse(image_paths[1].exists())
            self.assertFalse(ARCHIVE._workout_archive_metadata_path(image_paths[0]).exists())
            self.assertEqual(len(list(archive_dir.glob("*.jpg"))), 25)
            self.assertEqual(len(list(archive_dir.glob("*.json"))), 25)

    def test_workout_archive_retention_is_bounded(self) -> None:
        self.assertEqual(BRIDGE._coerce_workout_archive_retention("invalid"), 250)
        self.assertEqual(BRIDGE._coerce_workout_archive_retention(1), 25)
        self.assertEqual(BRIDGE._coerce_workout_archive_retention(9000), 2000)

    def test_runtime_workout_retention_keeps_newest_records(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester",
            app_username="tester",
            display_name="Tester",
        )
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(27):
            result = BRIDGE._upsert_workout_record(
                runtime,
                {
                    "workout_uuid": f"workout-{index}",
                    "workout_type": "walking",
                    "workout_start": (start + timedelta(days=index)).isoformat(),
                    "workout_end": (start + timedelta(days=index, minutes=30)).isoformat(),
                },
            )
            self.assertEqual(result, "created")

        removed = BRIDGE._prune_runtime_workouts(runtime, 25)

        self.assertEqual(removed, 2)
        self.assertEqual(len(runtime.workouts), 25)
        self.assertNotIn("uuid:workout-0", runtime.workouts)
        self.assertNotIn("uuid:workout-1", runtime.workouts)
        self.assertIn("uuid:workout-26", runtime.workouts)

    def test_workout_archive_username_migration_moves_newest_file_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = pathlib.Path(temp_dir)
            source_dir = media_root / "halthy" / "workouts" / "old_user"
            target_dir = media_root / "halthy" / "workouts" / "new_user"
            source_dir.mkdir(parents=True)
            target_dir.mkdir(parents=True)
            file_name = "20260401T101112Z_uuid_same.jpg"
            source_image = source_dir / file_name
            target_image = target_dir / file_name
            source_image.write_bytes(b"newer")
            target_image.write_bytes(b"older")
            ARCHIVE._workout_archive_metadata_path(source_image).write_text(
                '{"workout_type":"cycling"}',
                encoding="utf-8",
            )
            os.utime(target_image, (1, 1))
            os.utime(source_image, (2, 2))

            migrated = ARCHIVE.migrate_workout_archive_username(
                media_root,
                "old_user",
                "new_user",
            )

            self.assertEqual(migrated, 1)
            self.assertFalse(source_image.exists())
            self.assertEqual(target_image.read_bytes(), b"newer")
            self.assertEqual(
                ARCHIVE._read_workout_archive_metadata(target_image)["workout_type"],
                "cycling",
            )

    def test_archive_attributes_follow_username_migration(self) -> None:
        migrated = BRIDGE._rewrite_archive_username_attributes(
            {
                "archive_relative_path": "halthy/workouts/old_user/map.jpg",
                "archive_local_url": "/media/local/halthy/workouts/old_user/map.jpg",
                "archive_media_source_id": (
                    "media-source://media_source/local/halthy/workouts/old_user/map.jpg"
                ),
            },
            "old_user",
            "new_user",
        )

        self.assertEqual(
            migrated["archive_relative_path"],
            "halthy/workouts/new_user/map.jpg",
        )
        self.assertNotIn("old_user", migrated["archive_local_url"])
        self.assertNotIn("old_user", migrated["archive_media_source_id"])

    def test_workout_archive_file_name_uses_timestamp_and_workout_uuid(self) -> None:
        file_name, workout_fingerprint, workout_timestamp, extension = (
            ARCHIVE._workout_archive_file_name(
                metric_key="workout",
                attributes={
                    "measurement_timestamp": "2026-03-28T18:15:12Z",
                    "workout_uuid": "ABCD-1234",
                },
                content_type="image/jpeg",
            )
        )
        expected_fingerprint = "uuid_abcd_1234"

        self.assertEqual(workout_fingerprint, expected_fingerprint)
        self.assertEqual(extension, "jpg")
        self.assertEqual(
            workout_timestamp,
            datetime(2026, 3, 28, 18, 15, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(file_name, f"20260328T181512Z_{expected_fingerprint}.jpg")

    def test_store_workout_archive_file_replaces_files_for_same_workout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_dir = pathlib.Path(temp_dir)
            old_same_workout_jpg = archive_dir / "20260328T181512Z_uuid_abcd_1234_abc123def4.jpg"
            old_same_workout_png = archive_dir / "20260328T181513Z_uuid_abcd_1234_abc123def4.png"
            old_same_workout_stable_key = archive_dir / "20260328T181519Z_uuid_abcd_1234.jpg"
            old_same_workout_metadata = archive_dir / "20260328T181519Z_uuid_abcd_1234.json"
            other_workout_file = archive_dir / "20260328T181514Z_uuid_other_5678.jpg"
            old_same_workout_jpg.write_bytes(b"old_jpg")
            old_same_workout_png.write_bytes(b"old_png")
            old_same_workout_stable_key.write_bytes(b"old_stable")
            old_same_workout_metadata.write_text("{}", encoding="utf-8")
            other_workout_file.write_bytes(b"other")

            replaced_count = ARCHIVE._store_workout_archive_file(
                archive_dir=archive_dir,
                file_name="20260328T181520Z_uuid_abcd_1234.jpg",
                image_bytes=b"new_image",
                workout_fingerprint="uuid_abcd_1234",
            )

            self.assertEqual(replaced_count, 3)
            self.assertFalse(old_same_workout_jpg.exists())
            self.assertFalse(old_same_workout_png.exists())
            self.assertFalse(old_same_workout_stable_key.exists())
            self.assertFalse(old_same_workout_metadata.exists())
            self.assertTrue(other_workout_file.exists())
            self.assertEqual(
                (archive_dir / "20260328T181520Z_uuid_abcd_1234.jpg").read_bytes(),
                b"new_image",
            )

    def test_workout_archive_metadata_is_written_and_collected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = pathlib.Path(temp_dir)
            archive_dir = media_root / "halthy" / "workouts" / "tester"
            archive_dir.mkdir(parents=True, exist_ok=True)
            file_name = "20260401T101112Z_uuid_abcd_1234.jpg"
            (archive_dir / file_name).write_bytes(b"image")

            ARCHIVE._store_workout_archive_metadata(
                archive_dir,
                file_name,
                {
                    "workout_type": "Walking",
                    "workout_distance_m": 1234.5,
                    "workout_duration_s": 1800,
                    "avg_speed_mps": 0.69,
                    "highest_speed_mps": 1.4,
                    "lowest_speed_mps": 0.3,
                    "avg_heart_rate_bpm": 122,
                    "lowest_heart_rate_bpm": 92,
                    "highest_heart_rate_bpm": 151,
                    "cadence_spm": 104,
                    "power_w": 180,
                    "respiratory_rate_brpm": 18,
                    "workout_elevation_gain_m": 42,
                    "highest_altitude_m": 120,
                    "lowest_altitude_m": 78,
                    "total_flights_climbed": 5,
                    "weather_temperature_c": 12.4,
                    "weather_humidity_percent": 71,
                    "ignored_route_payload": [{"lat": 1}],
                },
            )

            records = ARCHIVE._collect_workout_archive_records(media_root, "tester", limit=10)

            self.assertEqual(records[0]["title"], "Walking")
            self.assertEqual(records[0]["workout_distance_m"], 1234.5)
            self.assertEqual(records[0]["workout_duration_s"], 1800)
            self.assertEqual(records[0]["avg_speed_mps"], 0.69)
            self.assertEqual(records[0]["highest_speed_mps"], 1.4)
            self.assertEqual(records[0]["lowest_speed_mps"], 0.3)
            self.assertEqual(records[0]["avg_heart_rate_bpm"], 122)
            self.assertEqual(records[0]["lowest_heart_rate_bpm"], 92)
            self.assertEqual(records[0]["highest_heart_rate_bpm"], 151)
            self.assertEqual(records[0]["cadence_spm"], 104)
            self.assertEqual(records[0]["power_w"], 180)
            self.assertEqual(records[0]["respiratory_rate_brpm"], 18)
            self.assertEqual(records[0]["workout_elevation_gain_m"], 42)
            self.assertEqual(records[0]["highest_altitude_m"], 120)
            self.assertEqual(records[0]["lowest_altitude_m"], 78)
            self.assertEqual(records[0]["total_flights_climbed"], 5)
            self.assertEqual(records[0]["weather_temperature_c"], 12.4)
            self.assertEqual(records[0]["weather_humidity_percent"], 71)
            self.assertNotIn("ignored_route_payload", records[0])

    def test_workout_archive_file_name_uses_timestamp_fallback_key(self) -> None:
        file_name, workout_fingerprint, workout_timestamp, extension = (
            ARCHIVE._workout_archive_file_name(
                metric_key="workout",
                attributes={
                    "timestamp": "2026-04-04T07:01:02Z",
                    "workout_uuid": "ABCD-1234",
                },
                content_type="image/jpeg",
            )
        )
        expected_fingerprint = "uuid_abcd_1234"

        self.assertEqual(workout_fingerprint, expected_fingerprint)
        self.assertEqual(
            workout_timestamp,
            datetime(2026, 4, 4, 7, 1, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(extension, "jpg")
        self.assertEqual(file_name, f"20260404T070102Z_{expected_fingerprint}.jpg")

    def test_numeric_state_value_accepts_numeric_strings_with_units(self) -> None:
        self.assertEqual(STATISTICS.numeric_state_value("72"), 72.0)
        self.assertEqual(STATISTICS.numeric_state_value("72,5"), 72.5)
        self.assertEqual(STATISTICS.numeric_state_value("72 bpm"), 72.0)
        self.assertEqual(STATISTICS.numeric_state_value("36.5 °C"), 36.5)
        self.assertIsNone(STATISTICS.numeric_state_value("not-a-number"))

    def test_measurement_timestamp_value_uses_fallback_keys(self) -> None:
        attrs = {"timestamp": "2026-04-02T10:11:12Z"}
        self.assertEqual(TIMESTAMPS.measurement_timestamp_value(attrs), "2026-04-02T10:11:12Z")

    def test_prepare_statistics_uses_top_of_hour_buckets(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester",
            app_username="tester",
            display_name="Tester",
        )
        statistic_id = STATISTICS.statistics_id_for_metric("tester", "heart_rate")
        candidates = [
            {
                "statistic_id": statistic_id,
                "name": "Heart rate",
                "unit": "bpm",
                "start": datetime(2026, 3, 1, 10, 6, tzinfo=timezone.utc),
                "value": 61.0,
            },
            {
                "statistic_id": statistic_id,
                "name": "Heart rate",
                "unit": "bpm",
                "start": datetime(2026, 3, 1, 10, 42, tzinfo=timezone.utc),
                "value": 67.0,
            },
        ]

        batches, _cursor_updates = STATISTICS.prepare_statistics_imports_for_runtime(
            runtime, candidates
        )
        self.assertEqual(len(batches), 1)
        _metadata, rows = batches[0]
        self.assertEqual(len(rows), 1)
        start = getattr(rows[0], "start")
        self.assertEqual(start.minute, 0)
        self.assertEqual(start.second, 0)
        self.assertEqual(start.microsecond, 0)

    def test_statistics_candidates_disabled_per_runtime(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester",
            app_username="Tester",
            display_name="Tester",
            statistics_enabled=False,
        )

        candidates = STATISTICS.statistics_candidates_from_sensor(
            runtime=runtime,
            metric_key="heart_rate",
            metric_name="Heart rate",
            state=72,
            unit="bpm",
            attributes={"measurement_timestamp": "2026-06-19T10:15:00Z"},
        )

        self.assertEqual(candidates, [])

    def test_statistics_candidate_name_includes_username(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester_1",
            app_username="Tester 1",
            display_name="Tester One",
        )

        candidates = STATISTICS.statistics_candidates_from_sensor(
            runtime=runtime,
            metric_key="heart_rate",
            metric_name="Heart rate",
            state=72,
            unit="bpm",
            attributes={"measurement_timestamp": "2026-06-19T10:15:00Z"},
        )

        self.assertEqual(candidates[0]["name"], "Heart rate (Tester 1)")
        self.assertEqual(candidates[0]["statistic_id"], "halthy:tester_1_heart_rate")

    def test_statistics_metadata_includes_unit_class(self) -> None:
        statistic_id = STATISTICS.statistics_id_for_metric("tester", "heart_rate")
        metadata = STATISTICS.statistics_metadata(statistic_id, "Heart rate", "bpm")
        self.assertIsNotNone(metadata)
        self.assertTrue(hasattr(metadata, "unit_class"))
        self.assertIsNone(getattr(metadata, "unit_class"))

    def test_prepare_statistics_does_not_advance_cursor(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester",
            app_username="tester",
            display_name="Tester",
        )
        statistic_id = STATISTICS.statistics_id_for_metric("tester", "heart_rate")
        previous_cursor = "2026-03-01T10:00:00+00:00"
        runtime.statistics_cursors[statistic_id] = previous_cursor

        candidates = [
            {
                "statistic_id": statistic_id,
                "name": "Heart rate",
                "unit": "bpm",
                "start": datetime(2026, 3, 1, 10, 6, tzinfo=timezone.utc),
                "value": 61.0,
            }
        ]
        batches, cursor_updates = STATISTICS.prepare_statistics_imports_for_runtime(runtime, candidates)

        self.assertEqual(runtime.statistics_cursors.get(statistic_id), previous_cursor)
        self.assertEqual(len(batches), 1)
        self.assertIn(statistic_id, cursor_updates)
        self.assertEqual(
            cursor_updates[statistic_id].latest_imported_at,
            datetime(2026, 3, 1, 10, 6, tzinfo=timezone.utc),
        )

    def test_commit_statistics_updates_only_successful_series(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester",
            app_username="tester",
            display_name="Tester",
        )
        successful_statistic_id = STATISTICS.statistics_id_for_metric("tester", "heart_rate")
        failed_statistic_id = STATISTICS.statistics_id_for_metric("tester", "oxygen_saturation")
        legacy_id = "tester_heart_rate"
        runtime.statistics_cursors[legacy_id] = "2026-03-01T09:30:00+00:00"
        runtime.statistics_cursors[failed_statistic_id] = "2026-03-01T09:00:00+00:00"

        cursor_updates = {
            successful_statistic_id: BRIDGE.StatisticsCursorUpdate(
                latest_imported_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
                legacy_statistic_ids=(legacy_id,),
            ),
            failed_statistic_id: BRIDGE.StatisticsCursorUpdate(
                latest_imported_at=datetime(2026, 3, 1, 10, 5, tzinfo=timezone.utc),
                legacy_statistic_ids=(),
            ),
        }
        STATISTICS.commit_statistics_cursor_updates(
            runtime=runtime,
            cursor_updates=cursor_updates,
            successful_statistic_ids={successful_statistic_id},
        )

        self.assertEqual(
            runtime.statistics_cursors.get(successful_statistic_id),
            "2026-03-01T10:00:00+00:00",
        )
        self.assertNotIn(legacy_id, runtime.statistics_cursors)
        self.assertEqual(
            runtime.statistics_cursors.get(failed_statistic_id),
            "2026-03-01T09:00:00+00:00",
        )

    def test_commit_statistics_never_rolls_cursor_back(self) -> None:
        runtime = BRIDGE.IntegrationRuntime(
            configured_username="tester",
            app_username="tester",
            display_name="Tester",
        )
        statistic_id = STATISTICS.statistics_id_for_metric("tester", "heart_rate")
        runtime.statistics_cursors[statistic_id] = "2026-03-01T12:00:00+00:00"

        cursor_updates = {
            statistic_id: BRIDGE.StatisticsCursorUpdate(
                latest_imported_at=datetime(2026, 3, 1, 11, 0, tzinfo=timezone.utc),
                legacy_statistic_ids=(),
            )
        }
        STATISTICS.commit_statistics_cursor_updates(
            runtime=runtime,
            cursor_updates=cursor_updates,
            successful_statistic_ids={statistic_id},
        )

        self.assertEqual(
            runtime.statistics_cursors.get(statistic_id),
            "2026-03-01T12:00:00+00:00",
        )

    def test_statistics_batch_import_reports_successful_series(self) -> None:
        statistic_id_ok = STATISTICS.statistics_id_for_metric("tester", "heart_rate")
        statistic_id_fail = STATISTICS.statistics_id_for_metric("tester", "oxygen_saturation")

        metadata_ok = STATISTICS.statistics_metadata(statistic_id_ok, "Heart rate", "bpm")
        metadata_fail = STATISTICS.statistics_metadata(statistic_id_fail, "Blood oxygen", "%")
        row_ok = STATISTICS.statistics_data(
            start=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
            state=61.0,
        )
        row_fail = STATISTICS.statistics_data(
            start=datetime(2026, 3, 1, 10, 5, tzinfo=timezone.utc),
            state=97.0,
        )
        self.assertIsNotNone(metadata_ok)
        self.assertIsNotNone(metadata_fail)
        self.assertIsNotNone(row_ok)
        self.assertIsNotNone(row_fail)

        original_importer = BRIDGE.async_add_external_statistics

        async def _fake_importer(_hass, metadata, _rows):
            if getattr(metadata, "statistic_id", "") == statistic_id_fail:
                raise RuntimeError("simulated failure")
            return None

        BRIDGE.async_add_external_statistics = _fake_importer
        try:
            imported_samples, successful_ids = asyncio.run(
                BRIDGE._async_import_statistics_batches(
                    hass=object(),
                    batches=[
                        (metadata_ok, [row_ok]),
                        (metadata_fail, [row_fail]),
                    ],
                )
            )
        finally:
            BRIDGE.async_add_external_statistics = original_importer

        self.assertEqual(imported_samples, 1)
        self.assertIn(statistic_id_ok, successful_ids)
        self.assertNotIn(statistic_id_fail, successful_ids)


if __name__ == "__main__":
    unittest.main()
