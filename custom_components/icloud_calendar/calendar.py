"""Calendar platform: iCloud (and other CalDAV) calendars you can edit from Home Assistant."""

from collections.abc import Callable
from datetime import datetime
from functools import partial
import logging
from typing import Any, override

from caldav.calendarobjectresource import CalendarObjectResource
from caldav.lib.error import AuthorizationError, DAVError, NotFoundError
from caldav.lib.http_sync import requests as caldav_requests
from icalendar import Calendar as ICalendar

from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CalDavConfigEntry
from .api import find_object
from .const import DOMAIN
from .coordinator import AccountCoordinator
from .editing import EditError, EditResult, delete_event, get_master, update_event
from .store import CalendarInfo, Throttled, calendar_color

_LOGGER = logging.getLogger(__name__)

# Only allow VCALENDARs that support this component type
SUPPORTED_COMPONENT = "VEVENT"

WRITE_FEATURES = (
    CalendarEntityFeature.CREATE_EVENT
    | CalendarEntityFeature.UPDATE_EVENT
    | CalendarEntityFeature.DELETE_EVENT
)
THIS_AND_FUTURE = "THISANDFUTURE"

_NETWORK_ERRORS = (
    caldav_requests.exceptions.ConnectionError,
    caldav_requests.exceptions.Timeout,
    DAVError,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalDavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the calendar platform for a config entry."""
    data = entry.runtime_data
    async_add_entities(
        WebDavCalendarEntity(
            info,
            async_generate_entity_id(ENTITY_ID_FORMAT, info.name, hass=hass),
            data.coordinator,
            unique_id=f"{entry.entry_id}-{info.calendar.id}",
        )
        for info in data.store.calendars.values()
        if SUPPORTED_COMPONENT in info.components and info.name
    )


def _throttled_error(err: Throttled) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="throttled",
        translation_placeholders={"minutes": str(err.minutes)},
    )


class WebDavCalendarEntity(CoordinatorEntity[AccountCoordinator], CalendarEntity):
    """A calendar on a CalDAV server."""

    def __init__(
        self,
        info: CalendarInfo,
        entity_id: str,
        coordinator: AccountCoordinator,
        unique_id: str,
    ) -> None:
        """Create the calendar entity."""
        super().__init__(coordinator)
        self.entity_id = entity_id
        self._key = info.key
        self._calendar = info.calendar
        self._attr_name = info.name
        self._attr_unique_id = unique_id
        # Read-only (e.g. shared) calendars get no editing buttons
        self._attr_supported_features = WRITE_FEATURES if info.writable else CalendarEntityFeature(0)

    @override
    async def async_prepare_to_add_to_hass(self) -> None:
        """Read the server color of a calendar that has no entity yet.

        The color only applies when an entity is first registered.
        """
        await super().async_prepare_to_add_to_hass()
        if er.async_get(self.hass).async_get_entity_id(
            self.platform.domain, self.platform.platform_name, self.unique_id or ""
        ):
            return
        if self.coordinator.store.throttle.left() <= 0:
            self._attr_initial_color = await self.hass.async_add_executor_job(
                calendar_color, self.coordinator.store.client, self._calendar
            )

    @property
    @override
    def available(self) -> bool:
        """Return whether the calendar could be read."""
        return super().available and self._key not in self.coordinator.unreadable

    @property
    @override
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return (self.coordinator.data or {}).get(self._key)

    @override
    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        try:
            return await hass.async_add_executor_job(
                self.coordinator.events, self._key, start_date, end_date
            )
        except Throttled as err:
            raise _throttled_error(err) from err
        except _NETWORK_ERRORS as err:
            raise HomeAssistantError(f"CalDAV error: {err}") from err

    @override
    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a new event in the calendar."""
        item_data: dict[str, Any] = {
            "summary": kwargs["summary"],
            "dtstart": kwargs["dtstart"],
            "dtend": kwargs["dtend"],
        }
        if description := kwargs.get("description"):
            item_data["description"] = description
        if location := kwargs.get("location"):
            item_data["location"] = location
        if rrule := kwargs.get("rrule"):
            item_data["rrule"] = rrule
        store = self.coordinator.store

        def _create() -> None:
            store.call(partial(self._calendar.add_event, **item_data))

        await self._async_write(_create)

    @override
    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update an event, one occurrence of a series, or an occurrence and all after it."""
        await self._async_edit(
            uid,
            partial(
                update_event,
                changes=event,
                recurrence_id=recurrence_id,
                this_and_future=recurrence_range == THIS_AND_FUTURE,
            ),
        )

    @override
    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete an event, one occurrence of a series, or an occurrence and all after it."""
        await self._async_edit(
            uid,
            partial(
                delete_event,
                recurrence_id=recurrence_id,
                this_and_future=recurrence_range == THIS_AND_FUTURE,
            ),
        )

    async def _async_edit(self, uid: str, change: Callable[[ICalendar], EditResult]) -> None:
        """Fetch the object holding ``uid``, change it, and write it back."""
        calendar = self._calendar
        store = self.coordinator.store
        urls = store.urls[self._key]

        def _edit() -> None:
            try:
                obj = store.call(find_object, calendar, uid, urls.get(uid))
            except NotFoundError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="event_not_found",
                    translation_placeholders={"uid": uid},
                ) from err
            ical: ICalendar = obj.icalendar_instance
            try:
                result = change(ical)
            except EditError as err:
                raise HomeAssistantError(str(err)) from err
            # The ETag (or Schedule-Tag) is sent as a precondition, so a change
            # made elsewhere since the object was read isn't overwritten.
            if result.delete_resource:
                store.call(obj.delete, precondition="auto")
                urls.pop(uid, None)
            else:
                ical.add_missing_timezones()
                obj.icalendar_instance = ical
                store.call(obj.save, increase_seqno=False, precondition="auto")
            for new_cal in result.new_calendars:
                new_cal.add_missing_timezones()
                saved: CalendarObjectResource = store.call(
                    calendar.save_event, ical=new_cal.to_ical().decode()
                )
                if (master := get_master(new_cal)) is not None and saved.url:
                    urls[str(master["UID"])] = str(saved.url)

        await self._async_write(_edit)

    async def _async_write(self, write: Callable[[], None]) -> None:
        """Run a change on the server, then show it."""
        store = self.coordinator.store
        try:
            await self.hass.async_add_executor_job(write)
        except HomeAssistantError:
            raise
        except Throttled as err:
            raise _throttled_error(err) from err
        except AuthorizationError as err:
            raise HomeAssistantError(f"CalDAV authorization error: {err}") from err
        except _NETWORK_ERRORS as err:
            if "412" in str(err) or type(err).__name__ == "ETagMismatchError":
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="changed_elsewhere"
                ) from err
            raise HomeAssistantError(f"CalDAV save error: {err}") from err
        finally:
            # Even a failed write may have changed something
            store.invalidate(self._key)
        await self.coordinator.async_request_refresh()
