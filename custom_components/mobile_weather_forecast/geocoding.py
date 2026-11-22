"""Optional reverse geocoding (coordinates -> place name) through Nominatim (OpenStreetMap)."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, NOMINATIM_MIN_INTERVAL, NOMINATIM_URL, REPO_URL

_NAME_KEYS = (
    "city",
    "town",
    "village",
    "municipality",
    "hamlet",
    "suburb",
    "city_district",
    "county",
)


def parse_nominatim(data: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a Nominatim reverse response into a small place dictionary."""
    if not isinstance(data, dict) or "error" in data:
        return None
    address = data.get("address") or {}
    name = next((address[k] for k in _NAME_KEYS if address.get(k)), None)
    if not name:
        return None
    country_code = address.get("country_code")
    return {
        "name": name,
        "region": address.get("state") or address.get("region"),
        "country": address.get("country"),
        "country_code": country_code.upper() if country_code else None,
        "source": "nominatim",
    }


async def async_reverse_geocode(
    hass: HomeAssistant,
    latitude: float,
    longitude: float,
    language: str,
    version: str | None = None,
) -> dict[str, Any] | None:
    """Look up the place name of a position.

    The position is rounded to about 1 km before being sent, requests are spaced by
    at least one second across all entries (Nominatim usage policy), and the
    caller caches the result.
    """
    store = hass.data.setdefault(DOMAIN, {})
    lock: asyncio.Lock = store.setdefault("geocode_lock", asyncio.Lock())

    async with lock:
        wait = store.get("geocode_last", 0.0) + NOMINATIM_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            session = async_get_clientsession(hass)
            headers = {
                "User-Agent": f"HomeAssistant-MobileWeatherForecast/{version or 'dev'} (+{REPO_URL})"
            }
            params = {
                "format": "jsonv2",
                "lat": f"{latitude:.2f}",
                "lon": f"{longitude:.2f}",
                "zoom": 10,
                "addressdetails": 1,
                "accept-language": language,
            }
            async with asyncio.timeout(15):
                response = await session.get(NOMINATIM_URL, params=params, headers=headers)
                response.raise_for_status()
                data = await response.json()
        finally:
            store["geocode_last"] = time.monotonic()

    return parse_nominatim(data)
