"""Unit tests for unit and temperature helpers."""

from __future__ import annotations

import unittest
import importlib.util
import pathlib
import sys
import types


class _UnitOfTemperature:
    CELSIUS = "°C"
    FAHRENHEIT = "°F"


def _load_units_dependencies():
    if "homeassistant" not in sys.modules:
        homeassistant = types.ModuleType("homeassistant")
        sys.modules["homeassistant"] = homeassistant
    if "homeassistant.const" not in sys.modules:
        const_mod = types.ModuleType("homeassistant.const")

        class _Platform:
            SENSOR = "sensor"

        const_mod.Platform = _Platform
        const_mod.UnitOfTemperature = _UnitOfTemperature
        sys.modules["homeassistant.const"] = const_mod

    package_name = "halthy_testpkg"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = []  # type: ignore[attr-defined]
        sys.modules[package_name] = package

    base_path = pathlib.Path(__file__).resolve().parents[1]
    for module_base in ("const", "naming", "units"):
        module_name = f"{package_name}.{module_base}"
        if module_name in sys.modules:
            continue
        module_path = base_path / f"{module_base}.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

    return sys.modules[f"{package_name}.const"], sys.modules[f"{package_name}.units"]


CONST, UNITS = _load_units_dependencies()


class UnitHelpersTests(unittest.TestCase):
    def test_canonical_temperature_unit(self) -> None:
        self.assertEqual(UNITS.canonical_unit("sleeping_wrist_temperature", "degC"), "°C")

    def test_temperature_conversion_uses_hass_system_when_selected(self) -> None:
        state, unit = UNITS.resolve_temperature_state(
            metric_key="sleeping_wrist_temperature",
            state=36.5,
            unit="°C",
            preference=CONST.TEMPERATURE_UNIT_SYSTEM,
            hass_temperature_unit=_UnitOfTemperature.FAHRENHEIT,
        )
        self.assertEqual(unit, _UnitOfTemperature.FAHRENHEIT)
        self.assertEqual(state, 97.7)

    def test_temperature_conversion_respects_explicit_celsius_preference(self) -> None:
        state, unit = UNITS.resolve_temperature_state(
            metric_key="sleeping_wrist_temperature",
            state=98.6,
            unit="°F",
            preference=CONST.TEMPERATURE_UNIT_CELSIUS,
            hass_temperature_unit=_UnitOfTemperature.FAHRENHEIT,
        )
        self.assertEqual(unit, _UnitOfTemperature.CELSIUS)
        self.assertEqual(state, 37.0)

    def test_temperature_conversion_respects_explicit_fahrenheit_preference(self) -> None:
        state, unit = UNITS.resolve_temperature_state(
            metric_key="sleeping_wrist_temperature",
            state=36.5,
            unit="°C",
            preference=CONST.TEMPERATURE_UNIT_FAHRENHEIT,
            hass_temperature_unit=_UnitOfTemperature.CELSIUS,
        )
        self.assertEqual(unit, _UnitOfTemperature.FAHRENHEIT)
        self.assertEqual(state, 97.7)

    def test_non_temperature_metric_passthrough(self) -> None:
        state, unit = UNITS.resolve_temperature_state(
            metric_key="heart_rate",
            state=63,
            unit="bpm",
            preference=CONST.TEMPERATURE_UNIT_SYSTEM,
            hass_temperature_unit=_UnitOfTemperature.CELSIUS,
        )
        self.assertEqual(state, 63)
        self.assertEqual(unit, "bpm")

    def test_duration_metric_detection(self) -> None:
        self.assertTrue(UNITS.is_duration_metric("exercise_time", "min"))
        self.assertTrue(UNITS.is_duration_metric("stand_time", "min"))
        self.assertTrue(UNITS.is_duration_metric("apple_exercise_time", "min"))
        self.assertTrue(UNITS.is_duration_metric("apple_stand_time", "min"))
        self.assertTrue(UNITS.is_duration_metric("sleep_duration", "h"))
        self.assertFalse(UNITS.is_duration_metric("heart_rate", "bpm"))

    def test_duration_precision_suggestion(self) -> None:
        self.assertEqual(UNITS.duration_suggested_display_precision("sleep_duration", "h"), 2)
        self.assertEqual(UNITS.duration_suggested_display_precision("exercise_time", "min"), 1)
        self.assertIsNone(UNITS.duration_suggested_display_precision("heart_rate", "bpm"))

    def test_timestamp_metric_detection(self) -> None:
        self.assertTrue(UNITS.is_timestamp_metric("last_update"))
        self.assertTrue(UNITS.is_timestamp_metric("last_full_sync"))
        self.assertTrue(UNITS.is_timestamp_metric("workout_route_start"))
        self.assertTrue(UNITS.is_timestamp_metric("workout_route_end"))
        self.assertFalse(UNITS.is_timestamp_metric("workout_duration"))


if __name__ == "__main__":
    unittest.main()
