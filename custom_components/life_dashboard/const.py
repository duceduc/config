"""Constants for the Life Dashboard integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "life_dashboard"

# Config entry data keys.
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_SECRET: Final = "secret"
CONF_URL_CHOICE: Final = "url_choice"
CONF_BASE_URL: Final = "base_url"
CONF_CLOUDHOOK_URL: Final = "cloudhook_url"
# The phone's platform, from the source of its health payloads: an iPhone has no Receive.
CONF_PLATFORM: Final = "platform"
PLATFORM_ANDROID: Final = "android"
PLATFORM_IPHONE: Final = "iphone"
# The lowercased entry id of a removed phone whose statistics a new pairing takes over,
# kept in the entry's data until the setup has done it (statistics.py).
CONF_TAKE_OVER: Final = "take_over"

# Values for CONF_URL_CHOICE. "url" is an address the user typed or accepted, kept in
# CONF_BASE_URL; "cloud" is a cloudhook. Internal and external are what entries from
# before 0.5.0 hold, and are only read to prefill the address on reconfigure.
URL_CHOICE_URL: Final = "url"
URL_CHOICE_CLOUD: Final = "cloud"
URL_CHOICE_INTERNAL: Final = "internal"
URL_CHOICE_EXTERNAL: Final = "external"

DEFAULT_NAME: Final = "Life Dashboard"

# Shown as the device manufacturer and model.
MANUFACTURER: Final = "owen282000"
MODEL: Final = "Life Dashboard Companion"

# Config entry options.
CONF_MIN_WORKOUT_MINUTES: Final = "min_workout_minutes"
DEFAULT_MIN_WORKOUT_MINUTES: Final = 0
