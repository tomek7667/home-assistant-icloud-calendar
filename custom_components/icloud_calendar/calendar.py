"""Calendar platform: iCloud (and other CalDAV) calendars you can edit from Home Assistant."""

import asyncio
from datetime import datetime
from functools import partial
import logging
from typing import Any, override

from caldav.calendarobjectresource import CalendarObjectResource
from caldav.lib.error import DAVError, NotFoundError
from caldav.lib.http_sync import requests as caldav_requests
from icalendar import Calendar as ICalendar

from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CalDavConfigEntry
from .api import async_get_calendar_color, async_get_calendars, calendar_is_writable, find_object
from .const import DOMAIN
from .coordinator import CalDavUpdateCoordinator
from .editing import EditError, EditResult, delete_event, get_master, update_event

_LOGGER = logging.getLogger(__name__)

# Number of days to look ahead for next event when configured by ConfigEntry
CONFIG_ENTRY_DEFAULT_DAYS = 7

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
    calendars = [
        (calendar, name)
        for calendar, name in await async_get_calendars(hass, entry.runtime_data, SUPPORTED_COMPONENT)
        if name
    ]
    # One privilege request per calendar; run them side by side
    writable_flags = await asyncio.gather(
        *(hass.async_add_executor_job(calendar_is_writable, calendar) for calendar, _ in calendars)
    )
    entities = []
    for (calendar, calendar_name), writable in zip(calendars, writable_flags, strict=True):
        entities.append(
            WebDavCalendarEntity(
                calendar_name,
                async_generate_entity_id(ENTITY_ID_FORMAT, calendar_name, hass=hass),
                CalDavUpdateCoordinator(
                    hass,
                    entry,
                    calendar=calendar,
                    calendar_name=calendar_name,
                    days=CONFIG_ENTRY_DEFAULT_DAYS,
                    include_all_day=True,
                    search=None,
                ),
                unique_id=f"{entry.entry_id}-{calendar.id}",
                writable=writable,
            )
        )
    async_add_entities(entities, True)


class WebDavCalendarEntity(CoordinatorEntity[CalDavUpdateCoordinator], CalendarEntity):
    """A calendar on a CalDAV server."""

    def __init__(
        self,
        name: str | None,
        entity_id: str,
        coordinator: CalDavUpdateCoordinator,
        unique_id: str | None = None,
        writable: bool = True,
    ) -> None:
        """Create the calendar entity."""
        super().__init__(coordinator)
        self.entity_id = entity_id
        self._event: CalendarEvent | None = None
        self._attr_name = name
        if unique_id is not None:
            self._attr_unique_id = unique_id
        # Read-only (e.g. shared) calendars get no editing buttons
        self._attr_supported_features = WRITE_FEATURES if writable else CalendarEntityFeature(0)

    @override
    async def async_prepare_to_add_to_hass(self) -> None:
        """Read the server color of a calendar that has no entity yet.

        The color only applies when an entity is first registered.
        """
        await super().async_prepare_to_add_to_hass()
        if self.unique_id is None or er.async_get(self.hass).async_get_entity_id(
            self.platform.domain, self.platform.platform_name, self.unique_id
        ):
            return
        try:
            self._attr_initial_color = await async_get_calendar_color(
                self.hass, self.coordinator.calendar
            )
        except (caldav_requests.exceptions.RequestException, DAVError) as err:
            _LOGGER.debug("Cannot read the color of %s: %s", self.name, err)

    @property
    @override
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self._event

    @override
    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        return await self.coordinator.async_get_events(hass, start_date, end_date)

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
        try:
            await self.hass.async_add_executor_job(
                partial(self.coordinator.calendar.add_event, **item_data),
            )
        except _NETWORK_ERRORS as err:
            raise HomeAssistantError(f"CalDAV save error: {err}") from err
        await self.coordinator.async_request_refresh()

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

    async def _async_edit(self, uid: str, change) -> None:
        """Fetch the object holding ``uid``, change it, and write it back."""
        calendar = self.coordinator.calendar

        def _edit() -> None:
            try:
                obj = find_object(calendar, uid, self.coordinator.urls.get(uid))
            except NotFoundError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="event_not_found", translation_placeholders={"uid": uid}
                ) from err
            ical: ICalendar = obj.icalendar_instance
            try:
                result: EditResult = change(ical)
            except EditError as err:
                raise HomeAssistantError(str(err)) from err
            # The ETag (or Schedule-Tag) is sent as a precondition, so a change
            # made elsewhere since the object was read isn't overwritten.
            if result.delete_resource:
                obj.delete(precondition="auto")
                self.coordinator.urls.pop(uid, None)
            else:
                ical.add_missing_timezones()
                obj.icalendar_instance = ical
                obj.save(increase_seqno=False, precondition="auto")
            self.coordinator.rules.pop(uid, None)  # the series' rule may have changed
            for new_cal in result.new_calendars:
                new_cal.add_missing_timezones()
                saved: CalendarObjectResource = calendar.save_event(ical=new_cal.to_ical().decode())
                if (master := get_master(new_cal)) is not None and saved.url:
                    self.coordinator.urls[str(master["UID"])] = saved.url

        try:
            await self.hass.async_add_executor_job(_edit)
        except HomeAssistantError:
            raise
        except _NETWORK_ERRORS as err:
            if "412" in str(err) or type(err).__name__ == "ETagMismatchError":
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="changed_elsewhere"
                ) from err
            raise HomeAssistantError(f"CalDAV save error: {err}") from err
        await self.coordinator.async_request_refresh()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Update event data."""
        self._event = self.coordinator.data
        super()._handle_coordinator_update()

    @override
    async def async_added_to_hass(self) -> None:
        """When entity is added to hass update state from existing coordinator data."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()
