"""iCloud Calendar (CalDAV): calendars and reminders you can edit from Home Assistant."""

from functools import partial

from caldav.davclient import DAVClient
from caldav.lib.error import AuthorizationError, DAVError
from caldav.lib.http_sync import requests as caldav_requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from .const import DOMAIN, TIMEOUT

# Connections kept per host. All calendars of an account share one session and
# are refreshed together; with urllib3's default of 10, accounts with more
# calendars logged "Connection pool is full" and reconnected every time.
POOL_SIZE = 32

type CalDavConfigEntry = ConfigEntry[DAVClient]


PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.TODO]


async def async_setup_entry(hass: HomeAssistant, entry: CalDavConfigEntry) -> None:
    """Set up CalDAV from a config entry."""
    client = await hass.async_add_executor_job(
        partial(
            DAVClient,
            entry.data[CONF_URL],
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            ssl_verify_cert=entry.data[CONF_VERIFY_SSL],
            timeout=TIMEOUT,
        )
    )
    try:
        await hass.async_add_executor_job(client.get_principal)
    except AuthorizationError as err:
        if err.reason == "Unauthorized":
            raise ConfigEntryAuthFailed("Credentials error from CalDAV server") from err
        # AuthorizationError can be raised if the url is incorrect or
        # on some other unexpected server response.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unexpected_response",
        ) from err
    except caldav_requests.exceptions.Timeout as err:
        raise ConfigEntryNotReady("Timeout connecting to CalDAV server") from err
    except caldav_requests.exceptions.ConnectionError as err:
        raise ConfigEntryNotReady("Connection error from CalDAV server") from err
    except DAVError as err:
        raise ConfigEntryNotReady("CalDAV client error") from err

    _widen_pool(client)
    entry.runtime_data = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


def _widen_pool(client: DAVClient) -> None:
    """Give the client's HTTP session room for one connection per calendar."""
    session = getattr(client, "session", None)
    if caldav_requests.__name__ != "requests" or not hasattr(session, "mount"):
        return  # niquests multiplexes, and other sessions are left alone
    for prefix in ("https://", "http://"):
        session.mount(prefix, caldav_requests.adapters.HTTPAdapter(pool_maxsize=POOL_SIZE))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unload a config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
