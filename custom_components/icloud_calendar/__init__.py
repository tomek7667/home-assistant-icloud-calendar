"""iCloud Calendar (CalDAV): calendars and reminders you can edit from Home Assistant."""

from dataclasses import dataclass
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
from homeassistant.helpers.storage import Store
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from .const import DOMAIN, TIMEOUT
from .coordinator import AccountCoordinator
from .store import AccountStore, Throttle, Throttled, discover

# Connections kept per host. All calendars of an account share one session;
# with urllib3's default of 10, accounts with more calendars logged
# "Connection pool is full" and reconnected every time.
POOL_SIZE = 32

PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.TODO]


@dataclass
class ICloudCalendarData:
    """What a loaded config entry keeps."""

    client: DAVClient
    store: AccountStore
    coordinator: AccountCoordinator


type CalDavConfigEntry = ConfigEntry[ICloudCalendarData]


async def async_setup_entry(hass: HomeAssistant, entry: CalDavConfigEntry) -> None:
    """Set up CalDAV from a config entry."""
    # The wait the server asked for outlives reloads and setup retries
    throttle: Throttle = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, Throttle())
    if (left := throttle.left()) > 0:
        raise ConfigEntryNotReady(f"The server asked to wait {left / 60:.0f} more minutes")
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
    _widen_pool(client)
    saved = _listing_store(hass, entry)
    known = await saved.async_load() or {}
    try:
        store = await hass.async_add_executor_job(discover, client, throttle, known)
    except Throttled as err:
        raise ConfigEntryNotReady(f"The server asked to wait {err.minutes} minutes") from err
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
        raise ConfigEntryNotReady(f"CalDAV client error: {err}") from err

    # Remember the calendars, so one the server can't describe next time isn't lost
    await saved.async_save(store.listing())

    coordinator = AccountCoordinator(hass, entry, store)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = ICloudCalendarData(client, store, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


def _listing_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict]:
    return Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.calendars")


def _widen_pool(client: DAVClient) -> None:
    """Give the client's HTTP session room for one connection per calendar."""
    session = getattr(client, "session", None)
    if caldav_requests.__name__ != "requests" or not hasattr(session, "mount"):
        return  # niquests multiplexes, and other sessions are left alone
    for prefix in ("https://", "http://"):
        session.mount(prefix, caldav_requests.adapters.HTTPAdapter(pool_maxsize=POOL_SIZE))


async def async_unload_entry(hass: HomeAssistant, entry: CalDavConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await hass.async_add_executor_job(entry.runtime_data.client.close)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the saved calendar listing of a removed entry."""
    await _listing_store(hass, entry).async_remove()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
