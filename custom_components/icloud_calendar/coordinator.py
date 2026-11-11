"""The account coordinator: one refresh for all calendars of a config entry."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, override

from caldav.lib.error import AuthorizationError, DAVError
from caldav.lib.http_sync import requests as caldav_requests

from homeassistant.components.calendar import (
    CalendarEvent,
    CalendarEventStatus,
    extract_offset,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONFIG_ENTRY_DEFAULT_DAYS, UPDATE_INTERVAL
from .events import Occurrence, expand
from .store import AccountStore, Throttled

if TYPE_CHECKING:
    from . import CalDavConfigEntry

_LOGGER = logging.getLogger(__name__)

OFFSET = "!!"


class AccountCoordinator(DataUpdateCoordinator[dict[str, CalendarEvent | None]]):
    """Finds the next event of every calendar of an account.

    A refresh reads the change tags of all calendars with one request and fetches
    only the calendars that changed, so it is cheap enough to run every few minutes.
    """

    def __init__(
        self, hass: HomeAssistant, entry: CalDavConfigEntry, store: AccountStore
    ) -> None:
        """Set up the coordinator for one account."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="iCloud Calendar",
            update_interval=UPDATE_INTERVAL,
        )
        self.store = store
        self.days = CONFIG_ENTRY_DEFAULT_DAYS
        # Calendars the last refresh couldn't read and has nothing to show for
        self.unreadable: set[str] = set()

    @override
    async def _async_update_data(self) -> dict[str, CalendarEvent | None]:
        """Return the next event per calendar key."""
        try:
            return await self.hass.async_add_executor_job(self._next_events)
        except Throttled as err:
            raise UpdateFailed(
                f"The server asked to wait {err.minutes} more minutes"
            ) from err
        except AuthorizationError as err:
            raise ConfigEntryAuthFailed("Credentials error from CalDAV server") from err
        except (
            caldav_requests.exceptions.ConnectionError,
            caldav_requests.exceptions.Timeout,
            DAVError,
        ) as err:
            raise UpdateFailed(f"CalDAV error: {err}") from err

    def _next_events(self) -> dict[str, CalendarEvent | None]:
        """Find the next event of each calendar (blocking)."""
        try:
            self.store.refresh_ctags(30)  # skipped right after setup listed the calendars
        except Throttled:
            pass  # the cached events still say what's next
        except AuthorizationError:
            raise
        except DAVError as err:
            # Read the calendars one by one below; those that work still update
            _LOGGER.debug("Cannot check the calendars for changes: %s", err)
        start = dt_util.start_of_local_day()
        end = start + timedelta(days=self.days)
        previous = self.data or {}
        result: dict[str, CalendarEvent | None] = {}
        failed: dict[str, Exception] = {}
        keys = [
            key
            for key, info in self.store.calendars.items()
            if "VEVENT" in info.components and info.name
        ]
        for key in keys:
            try:
                objects = self.store.objects(key, start, end)
            except Throttled:
                if key not in previous:
                    raise
                result[key] = previous[key]  # nothing cached yet; keep what was shown
                continue
            except AuthorizationError:
                raise
            except (caldav_requests.exceptions.RequestException, DAVError) as err:
                # One calendar the server fails to read mustn't hide the others
                failed[key] = err
                if key in previous:
                    result[key] = previous[key]
                continue
            result[key] = next_event(expand(objects, start, end))
        if failed and len(failed) == len(keys):
            raise next(iter(failed.values()))
        if failed:
            _LOGGER.warning(
                "Could not read %s: %s",
                ", ".join(str(self.store.calendars[key].name) for key in failed),
                next(iter(failed.values())),
            )
        # Calendars with nothing to show are unavailable until they can be read
        self.unreadable = {key for key in failed if key not in result}
        return result

    def events(self, key: str, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Return the events of a calendar in a time range (blocking)."""
        occurrences = expand(self.store.objects(key, start, end), start, end)
        events = [to_calendar_event(occurrence) for occurrence in occurrences]
        # Sort by timestamp, as datetimes in the same time zone are compared by
        # wall time, which puts events in the wrong order when clocks fall back
        events.sort(key=lambda event: event.start_datetime_local.timestamp())
        return events


def next_event(occurrences: list[Occurrence]) -> CalendarEvent | None:
    """Return the first occurrence that isn't over yet."""
    now = dt_util.now()
    for occurrence in sorted(occurrences, key=lambda o: to_datetime(o.start).timestamp()):
        if now >= to_datetime(occurrence.end):
            continue
        summary, _ = extract_offset(occurrence.summary, OFFSET)
        return to_calendar_event(occurrence, summary)
    return None


def to_calendar_event(occurrence: Occurrence, summary: str | None = None) -> CalendarEvent:
    """Return an occurrence as a Home Assistant calendar event."""
    return CalendarEvent(
        summary=occurrence.summary if summary is None else summary,
        start=to_local(occurrence.start),
        end=to_local(occurrence.end),
        location=occurrence.location,
        description=occurrence.description,
        uid=occurrence.uid,
        recurrence_id=occurrence.recurrence_id,
        rrule=occurrence.rrule,
        status=_status(occurrence.status),
    )


def _status(value: str | None) -> CalendarEventStatus | None:
    """Return a supported rfc5545 STATUS; anything else is dropped."""
    if value is None:
        return None
    try:
        return CalendarEventStatus(value.lower())
    except ValueError:
        _LOGGER.debug("Ignoring unsupported event status %s", value)
        return None


def to_local(value: datetime | date) -> datetime | date:
    """Return a datetime in local time (floating times taken as local); dates stay."""
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    return value


def to_datetime(value: datetime | date) -> datetime:
    """Return a datetime, midnight local time for a date."""
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    return datetime.combine(value, time.min).replace(tzinfo=dt_util.get_default_time_zone())
