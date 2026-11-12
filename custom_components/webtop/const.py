"""Constants for the Webtop (SmartSchool) integration."""

from __future__ import annotations

DOMAIN = "webtop"

# pywebtop's import name is `webtop`; the integration package is
# `custom_components.webtop`, so the two never collide.
LIBRARY_LOGGER = "webtop"

# Config-entry data keys.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
# Optional override for the "Data" field of the login request. Left empty, the
# integration computes it from the username (see auth.py).
CONF_DATA = "data"
DEFAULT_DATA = ""
# pywebtop's built-in default, which earlier versions of this integration
# stored. It is only valid for one username, so it is treated as "not set".
LEGACY_DEFAULT_DATA = "+Aabe7FAdVluG6Lu+0ibrA=="

# Options.
CONF_SCAN_INTERVAL = "scan_interval"  # minutes
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 720

REQUEST_TIMEOUT = 30.0

WEBTOP_PORTAL_URL = "https://webtop.smartschool.co.il"
MANUFACTURER = "SmartSchool"

# Events fired on the Home Assistant bus when something new shows up after
# the first successful poll of a running instance.
EVENT_NEW_HOMEWORK = f"{DOMAIN}_new_homework"
EVENT_NEW_MESSAGE = f"{DOMAIN}_new_message"

# Sensor attributes hold lists; keep them bounded so the recorder database
# does not balloon.
MAX_LIST_ATTR_ITEMS = 25
