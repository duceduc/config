"""Which apps have a screen time sensor of their own, and what they last read.

A per-app sensor is created disabled, so most of them are never added to Home Assistant
and never restore a state of their own. What they need across a restart therefore lives
here instead, in one small store per entry: the label of every app that has a sensor,
so an entity rebuilt before the phone syncs keeps its name, and the newest day's table,
so a sensor the user has just enabled shows today's minutes as soon as the entry has
reloaded rather than at the next sync.

Two bounds keep the entity list from growing without end. An app needs a few minutes
over the days a payload carries before it gets a sensor, which leaves out what was only
opened once, and a phone gets a fixed number of app sensors at most. An app keeps its
sensor once it has one, also when it is no longer used: removing it would break the
automations and dashboards built on it.

A sensor the user deletes stays deleted. Its app is dismissed: the label goes, the slot
is free again, and the app gets no new sensor while the phone keeps sending it. The
store keeps a salted hash of a dismissed package rather than the package itself, since
all it has to answer is whether an app was dismissed, and the name of an app someone
chose to hide is exactly what should not stay behind in plain text. A hash of a package
name can still be checked against a guess, so this keeps the name out of sight, not
secret.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .payload import KEY_SCREEN_TIME_APPS, SensorUpdate, is_fallback_label, parse_instant

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 5

#: Minutes over the days of one payload (up to seven) before an app gets a sensor.
MIN_WEEK_MINUTES: Final = 5
#: App sensors per phone at most, disabled ones included.
MAX_APP_SENSORS: Final = 50


def app_unique_id(entry_id: str, package: str) -> str:
    """The unique id of an app's sensor: the package, so a renamed app keeps it."""
    return f"{entry_id}_screen_time_app_{package}"


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.apps")


async def async_remove_store(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the store with the entry: it names the apps on the phone."""
    await _store(hass, entry.entry_id).async_remove()


class AppRoster:
    """The apps of one phone that have a sensor, and the newest day's table."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._store = _store(hass, entry.entry_id)
        #: Package to label, for every app that has a sensor, in the order they came.
        self.labels: dict[str, str] = {}
        #: Salted hashes of the packages whose sensor the user deleted.
        self.dismissed: set[str] = set()
        #: The newest table, as the update it arrived in; None before the first one.
        self.latest: SensorUpdate | None = None
        self._cap_logged = False
        #: What the store holds, or is about to: a sync that changes none of it skips
        #: the write, which an SD card is grateful for.
        self._saved: dict[str, Any] | None = None

    async def async_load(self) -> None:
        self._read(await self._store.async_load() or {})
        self._saved = self._to_dict()

    def _read(self, stored: dict[str, Any]) -> None:
        labels = stored.get("labels")
        if isinstance(labels, dict):
            self.labels = {
                package: label
                for package, label in labels.items()
                if isinstance(package, str) and isinstance(label, str)
            }
        dismissed = stored.get("dismissed")
        if isinstance(dismissed, list):
            self.dismissed = {digest for digest in dismissed if isinstance(digest, str)}
        table = stored.get("table")
        if isinstance(table, dict) and isinstance(table.get("apps"), dict):
            try:
                measured_at = parse_instant(table.get("measured_at"))
            except ValueError:
                return
            self.latest = SensorUpdate(
                key=KEY_SCREEN_TIME_APPS,
                value=str(table.get("date")),
                measured_at=measured_at,
                attributes={"date": table.get("date"), "apps": table["apps"]},
            )

    async def async_flush(self) -> None:
        self._saved = self._to_dict()
        await self._store.async_save(self._saved)

    @callback
    def _async_save(self) -> None:
        """Write the store a moment from now, unless nothing in it changed."""
        snapshot = self._to_dict()
        if snapshot == self._saved:
            return
        self._saved = snapshot
        self._store.async_delay_save(self._to_dict, SAVE_DELAY_SECONDS)

    def _to_dict(self) -> dict[str, Any]:
        table = None
        if self.latest is not None:
            apps = self.latest.attributes.get("apps", {})
            table = {
                "date": self.latest.attributes.get("date"),
                "measured_at": self.latest.measured_at.isoformat(),
                # Only the apps with a sensor: the rest of the phone's apps are not
                # needed after the sync that judged them.
                "apps": {package: apps[package] for package in self.labels if package in apps},
            }
        return {
            "labels": dict(self.labels),
            "dismissed": sorted(self.dismissed),
            "table": table,
        }

    def _digest(self, package: str) -> str:
        return hashlib.sha256(f"{self._entry.entry_id}:{package}".encode()).hexdigest()

    @callback
    def async_dismiss(self, package: str) -> None:
        """Forget an app whose sensor the user deleted, and never make it one again."""
        self.labels.pop(package, None)
        self.dismissed.add(self._digest(package))
        self._async_save()

    @callback
    def async_track_registry(self, hass: HomeAssistant) -> Callable[[], None]:
        """Dismiss the apps whose sensor is deleted, now and while this entry is loaded.

        An app with a label but no registry entry lost its sensor while the entry was not
        loaded, for instance while Home Assistant was stopped. A removal event names the
        entity id only, and the registry entry is gone by then, so the entity ids of the
        app sensors are kept here, also through a rename of the entity id.
        """
        registry = er.async_get(hass)
        entry_id = self._entry.entry_id

        def package_of(registry_entry: er.RegistryEntry) -> str | None:
            if registry_entry.config_entry_id != entry_id:
                return None
            for package in self.labels:
                if registry_entry.unique_id == app_unique_id(entry_id, package):
                    return package
            return None

        by_entity_id: dict[str, str] = {}
        for registry_entry in er.async_entries_for_config_entry(registry, entry_id):
            if (package := package_of(registry_entry)) is not None:
                by_entity_id[registry_entry.entity_id] = package
        for package in set(self.labels) - set(by_entity_id.values()):
            self.async_dismiss(package)

        @callback
        def _changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
            data = event.data
            if data["action"] == "remove":
                if (package := by_entity_id.pop(data["entity_id"], None)) is not None:
                    self.async_dismiss(package)
                return
            if data["action"] == "update" and "old_entity_id" in data:
                by_entity_id.pop(data["old_entity_id"], None)
            elif data["action"] != "create":
                return
            registry_entry = registry.async_get(data["entity_id"])
            if registry_entry is not None and (package := package_of(registry_entry)):
                by_entity_id[registry_entry.entity_id] = package

        return hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _changed)

    @callback
    def async_take(self, update: SensorUpdate) -> list[str]:
        """Keep a newer table, and return the packages that get a sensor from it.

        The caller has already applied the ordering rule, so the table is not older than
        the one held. A label that changed is taken over for the next time the entity is
        built; the sensor shows the new one in its own attributes straight away.
        """
        apps: dict[str, dict[str, Any]] = update.attributes.get("apps", {})
        self.latest = update

        for package, row in apps.items():
            label = row.get("name")
            # An app uninstalled since comes under a made-up name; keep the real one.
            if package in self.labels and label and not is_fallback_label(package, label):
                self.labels[package] = label

        candidates = sorted(
            (
                (package, row)
                for package, row in apps.items()
                if package not in self.labels
                and row.get("week_minutes", 0) >= MIN_WEEK_MINUTES
                and self._digest(package) not in self.dismissed
            ),
            # The most used first, so a full roster keeps the apps that matter most.
            key=lambda item: (-item[1].get("week_minutes", 0), item[0]),
        )
        room = max(MAX_APP_SENSORS - len(self.labels), 0)
        added = [package for package, _ in candidates[:room]]
        for package in added:
            self.labels[package] = apps[package].get("name") or package

        if len(candidates) > room and not self._cap_logged:
            self._cap_logged = True
            _LOGGER.info(
                "%s has %s app screen time sensors, the most there can be; %s more apps get none",
                self._entry.title,
                MAX_APP_SENSORS,
                len(candidates) - room,
            )

        self._async_save()
        return added
