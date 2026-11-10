"""Library for working with CalDAV api."""

from typing import cast
from urllib.parse import quote

from caldav.calendarobjectresource import CalendarObjectResource, Event, Todo
from caldav.collection import Calendar
from caldav.davclient import DAVClient
from caldav.elements import ical
from caldav.elements.base import BaseElement
from caldav.lib.error import DAVError, NotFoundError
from caldav.lib.namespace import ns
from caldav.lib.url import URL

from homeassistant.core import HomeAssistant

# Privileges that allow changing calendar objects (rfc3744)
_WRITE_PRIVILEGES = {"write", "write-content", "all"}


class CurrentUserPrivilegeSet(BaseElement):
    """The DAV:current-user-privilege-set property (rfc3744 5.4)."""

    tag = ns("D", "current-user-privilege-set")


async def async_get_calendars(
    hass: HomeAssistant, client: DAVClient, component: str
) -> list[tuple[Calendar, str | None]]:
    """Get all calendars that support the specified component."""

    def _get_calendars() -> list[tuple[Calendar, str | None]]:
        calendars = cast(list[Calendar], client.get_principal().get_calendars())
        return [
            (calendar, cast(str | None, calendar.get_display_name()))
            for calendar in calendars
            if component in cast(list[str], calendar.get_supported_components())
        ]

    return await hass.async_add_executor_job(_get_calendars)


async def async_get_calendar_color(
    hass: HomeAssistant, calendar: Calendar
) -> str | None:
    """Get the color of a calendar, None if it has none."""
    color = cast(
        str | None,
        await hass.async_add_executor_job(calendar.get_property, ical.CalendarColor()),
    )
    # Apple Calendar, DAVx5 and SOGo append an alpha channel.
    if color and color.startswith("#") and len(color) == 9:
        return color[:7]
    return color


def calendar_is_writable(calendar: Calendar) -> bool:
    """Return whether the user may change objects in a calendar (blocking).

    Shared calendars can be read-only. When the server doesn't report the
    privileges, writing is allowed and the server has the last word.
    """
    try:
        response = calendar._query_properties([CurrentUserPrivilegeSet()], depth=0)  # noqa: SLF001
    except DAVError:
        return True
    tree = getattr(response, "tree", None)
    if tree is None:
        return True
    names = {el.tag.rsplit("}", 1)[-1] for el in tree.iter() if isinstance(el.tag, str)}
    if "privilege" not in names:
        return True
    return bool(names & _WRITE_PRIVILEGES)


def find_object(
    calendar: Calendar, uid: str, known_url: URL | str | None = None, *, todo: bool = False
) -> CalendarObjectResource:
    """Fetch the calendar object resource holding ``uid`` (blocking).

    Searching by UID is not supported everywhere: iCloud answers that REPORT with
    412 Precondition Failed. So first try the URL seen when the object was listed,
    then ``<UID>.ics`` (how iCloud, Apple clients and most servers name objects),
    then a UID search, and only then a scan of the whole calendar.
    """
    cls = Todo if todo else Event
    tried: set[str] = set()
    for url in (known_url, calendar.url.join(quote(uid.replace("/", "%2F")) + ".ics")):
        if not url or str(url) in tried:
            continue
        tried.add(str(url))
        try:
            obj = cls(client=calendar.client, url=url, parent=calendar).load()
        except (NotFoundError, DAVError):
            continue
        if _uid_of(obj) == uid:
            return obj
    try:
        return calendar.get_object_by_uid(uid, comp_class=cls)
    except (NotFoundError, DAVError):
        pass
    objects = calendar.todos(include_completed=True) if todo else calendar.events()
    for obj in objects:
        if _uid_of(obj) == uid:
            return obj
    raise NotFoundError(f"No calendar object with UID {uid}")


def _uid_of(obj: CalendarObjectResource) -> str | None:
    try:
        return str(obj.icalendar_component.get("UID"))
    except Exception:  # noqa: BLE001 - unparsable object
        return None


def get_attr_value(obj: CalendarObjectResource, attribute: str) -> str | None:
    """Return the value of the CalDav object attribute if defined."""
    if hasattr(obj, attribute):
        return getattr(obj, attribute).value
    return None
