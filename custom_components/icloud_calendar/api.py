"""Library for working with CalDAV api."""

from urllib.parse import quote

from caldav.calendarobjectresource import CalendarObjectResource, Event, Todo
from caldav.collection import Calendar
from caldav.lib.error import DAVError, NotFoundError
from caldav.lib.url import URL


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
