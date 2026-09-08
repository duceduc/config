"""Unit tests for naming helpers."""

from __future__ import annotations

import unittest
import importlib.util
import pathlib
import sys
import types


def _load_naming_module():
    package_name = "halthy_testpkg"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = []  # type: ignore[attr-defined]
        sys.modules[package_name] = package

    module_name = f"{package_name}.naming"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = pathlib.Path(__file__).resolve().parents[1] / "naming.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


NAMING = _load_naming_module()


class NamingHelpersTests(unittest.TestCase):
    def test_body_mass_display_name_preserves_metric_identity(self) -> None:
        for key in ("body_mass", "bodyMass", "HKQuantityTypeIdentifierBodyMass"):
            self.assertEqual(NAMING.friendly_metric_name(key, "Body mass"), "Weight")
            self.assertEqual(NAMING.normalize_metric_key(key), "body_mass")
        self.assertEqual(NAMING.friendly_metric_name("lean_body_mass", None), "Lean body mass")

    def test_normalize_healthkit_identifier(self) -> None:
        key = NAMING.normalize_metric_key("HKQuantityTypeIdentifierAppleSleepingWristTemperature")
        self.assertEqual(key, "sleeping_wrist_temperature")

    def test_normalize_alias_fix(self) -> None:
        key = NAMING.normalize_metric_key("sleep_in_g_wrist_temperature")
        self.assertEqual(key, "sleeping_wrist_temperature")

    def test_friendly_name_sentence_case(self) -> None:
        name = NAMING.friendly_metric_name("walking_hr_avg", None)
        self.assertEqual(name, "Walking heart rate average")

    def test_friendly_name_expands_vo2(self) -> None:
        name = NAMING.friendly_metric_name("vo2_max", None)
        self.assertEqual(name, "Maximal oxygen uptake")

    def test_friendly_name_expands_bp(self) -> None:
        name = NAMING.friendly_metric_name("bp_systolic", None)
        self.assertEqual(name, "Blood pressure systolic")

    def test_friendly_name_oxygen_override(self) -> None:
        name = NAMING.friendly_metric_name("oxygen_saturation", None)
        self.assertEqual(name, "Blood oxygen")

    def test_friendly_name_workout_route_start_override(self) -> None:
        name = NAMING.friendly_metric_name("workout_route_start", None)
        self.assertEqual(name, "Workout start")

    def test_selection_management_includes_regular_metric(self) -> None:
        self.assertTrue(NAMING.is_selection_managed_metric("steps"))

    def test_selection_management_includes_workout_route_map(self) -> None:
        self.assertFalse(NAMING.is_selection_managed_metric("workout_route_map"))

    def test_workout_route_map_aliases_to_workout(self) -> None:
        key = NAMING.normalize_metric_key("workout_route_map")
        self.assertEqual(key, "workout")

    def test_selection_management_excludes_last_update(self) -> None:
        self.assertFalse(NAMING.is_selection_managed_metric("last_update"))

    def test_selection_management_excludes_last_full_sync(self) -> None:
        self.assertFalse(NAMING.is_selection_managed_metric("last_full_sync"))

    def test_selection_management_excludes_daily_upload_count(self) -> None:
        self.assertFalse(NAMING.is_selection_managed_metric("daily_upload_count"))

    def test_selection_management_excludes_workout_start(self) -> None:
        self.assertFalse(NAMING.is_selection_managed_metric("workout_start"))

    def test_selection_management_excludes_workout_end(self) -> None:
        self.assertFalse(NAMING.is_selection_managed_metric("workout_end"))


if __name__ == "__main__":
    unittest.main()
