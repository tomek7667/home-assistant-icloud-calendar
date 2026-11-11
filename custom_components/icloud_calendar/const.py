"""Constants for iCloud Calendar (CalDAV)."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "icloud_calendar"
TIMEOUT: Final = 30
DEFAULT_URL: Final = "https://caldav.icloud.com/"
# Where app-specific passwords are made
APPLE_ACCOUNT_URL: Final = "https://account.apple.com"
# The built-in integration whose settings can be copied
CALDAV_DOMAIN: Final = "caldav"
CONF_SOURCE_ENTRY: Final = "source_entry"

# Days to look ahead for the next event of a calendar
CONFIG_ENTRY_DEFAULT_DAYS: Final = 7
# How often the account is checked for changes. A check is one PROPFIND for all
# calendars; only calendars that changed are fetched again.
UPDATE_INTERVAL: Final = timedelta(minutes=5)
# Seconds a check of the calendars' change tags is trusted when a dashboard asks
# for events, so opening dashboards doesn't send a request each time
CTAG_MAX_AGE: Final = 60.0
# Seconds events of a calendar without a change tag are reused (some shared
# iCloud calendars have none)
NO_CTAG_MAX_AGE: Final = 300.0
# Seconds to wait after a 503/429 that doesn't say how long (iCloud asks ~30 min)
DEFAULT_BACKOFF: Final = 15 * 60
# Range fetched around the requested one, so moving a week or month in the
# calendar panel is served from the cache
WINDOW_PAST: Final = timedelta(days=35)
WINDOW_FUTURE: Final = timedelta(days=100)
# Ranges longer than this (browsing far back or ahead) are fetched but not cached
MAX_WINDOW: Final = timedelta(days=400)
