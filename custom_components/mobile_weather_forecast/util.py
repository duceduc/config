"""Helper functions (coordinates, distances)."""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_coordinates(state: Any) -> tuple[float, float] | None:
    """Extract (lat, lon) from an entity state.

    Accepted formats:
    - latitude / longitude attributes (person, device_tracker)
    - location = [lat, lon] attribute ("geocoded location" sensor of the Companion app)
    """
    if state is None:
        return None
    attrs = state.attributes

    candidates: list[tuple[Any, Any]] = [
        (attrs.get("latitude"), attrs.get("longitude")),
        (attrs.get("Latitude"), attrs.get("Longitude")),
    ]
    location = attrs.get("location") or attrs.get("Location")
    if isinstance(location, (list, tuple)) and len(location) >= 2:
        candidates.append((location[0], location[1]))

    for lat_raw, lon_raw in candidates:
        lat = _to_float(lat_raw)
        lon = _to_float(lon_raw)
        if lat is None or lon is None:
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


def extract_locality(state: Any) -> str | None:
    """Return the locality name if the entity provides one."""
    if state is None:
        return None
    attrs = state.attributes
    return attrs.get("locality") or attrs.get("Locality")


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine distance in kilometers."""
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))
