"""Diagnostics support for iCloud Calendar (CalDAV)."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import CalDavConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_URL, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CalDavConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (from memory, no server requests)."""
    payload: dict[str, Any] = {
        "now": dt_util.now().isoformat(),
        "timezone": str(dt_util.get_default_time_zone()),
        "system_timezone": str(dt_util.naive_now().astimezone().tzinfo),
        "data": async_redact_data(entry.data, TO_REDACT),
    }
    if entry.state is ConfigEntryState.LOADED:
        store = entry.runtime_data.store
        payload["throttled_seconds"] = round(store.throttle.left())
        payload["calendars"] = [
            {
                "has_name": bool(info.name),
                "supported_components": sorted(info.components),
                "writable": info.writable,
                "has_change_tag": info.ctag is not None,
                "cached": store.is_cached(key),
            }
            for key, info in store.calendars.items()
        ]
    return payload
