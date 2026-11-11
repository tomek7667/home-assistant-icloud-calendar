"""An iCloud (CalDAV) account: its calendars, a cache of their events, and throttling.

iCloud throttles accounts that ask too often: it answers 503 with a Retry-After of
half an hour, and may then refuse the password altogether. So the account is read
the way Apple's own clients read it. One PROPFIND returns the change tag (CTag) of
every calendar, and only calendars whose tag changed are fetched again. Repeating
events are expanded locally from the cached objects (see events.py).

Everything here blocks; call it from an executor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
import logging
import re
import threading
import time
from typing import Any, TypeVar

from caldav.collection import Calendar
from caldav.davclient import DAVClient
from caldav.lib.error import AuthorizationError, DAVError, RateLimitError
from caldav.lib.url import URL
from icalendar import Calendar as ICalendar

from homeassistant.util import dt as dt_util

from .const import (
    CTAG_MAX_AGE,
    DEFAULT_BACKOFF,
    MAX_WINDOW,
    NO_CTAG_MAX_AGE,
    WINDOW_FUTURE,
    WINDOW_PAST,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

_DAV = "{DAV:}"
_CALDAV = "{urn:ietf:params:xml:ns:caldav}"
_CS = "{http://calendarserver.org/ns/}"
_APPLE = "{http://apple.com/ns/ical/}"
# Privileges that allow changing calendar objects (rfc3744)
_WRITE_PRIVILEGES = {"write", "write-content", "all"}
_ALL_COMPONENTS = frozenset({"VEVENT", "VTODO", "VJOURNAL"})
# "503 Service Unavailable", "429 Too Many Requests", or our own "503 on PROPFIND"
_THROTTLE_STATUS = re.compile(r"\b(?:429|503)\b(?=\s+(?:Service|Too|on)\b)")

_PROPFIND = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/" xmlns:a="http://apple.com/ns/ical/">
  <d:prop>{props}</d:prop>
</d:propfind>"""
# No calendar-color here: iCloud answers 500 for a shared calendar when it's asked
# for, and the color is only needed once (see calendar_color)
_LIST_BODY = _PROPFIND.format(
    props="<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/>"
    "<d:current-user-privilege-set/><cs:getctag/><d:sync-token/>"
)
# For a calendar that failed in the listing: only what's needed to show it
_BASIC_BODY = _PROPFIND.format(
    props="<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/>"
)
_CTAG_BODY = _PROPFIND.format(props="<d:resourcetype/><cs:getctag/><d:sync-token/>")
_COLOR_BODY = _PROPFIND.format(props="<a:calendar-color/>")


class Throttled(Exception):
    """The server asked to wait before the next request."""

    def __init__(self, seconds: float) -> None:
        """Remember how long is left to wait."""
        super().__init__(f"The server asked to wait {seconds:.0f} more seconds")
        self.seconds = seconds

    @property
    def minutes(self) -> int:
        """Return the minutes left, rounded up."""
        return max(1, int(-(-self.seconds // 60)))


def throttle_seconds(err: BaseException) -> float | None:
    """Return how long to wait if ``err`` means "too many requests", else None."""
    if isinstance(err, RateLimitError):
        return float(err.retry_after_seconds or DEFAULT_BACKOFF)
    if isinstance(err, DAVError) and _THROTTLE_STATUS.search(str(err)):
        return float(DEFAULT_BACKOFF)
    return None


class Throttle:
    """When the server lets us ask again. Kept per config entry, across reloads."""

    def __init__(self) -> None:
        """Start unthrottled."""
        self._until = 0.0

    def left(self) -> float:
        """Return the seconds left to wait."""
        return max(0.0, self._until - time.monotonic())

    def check(self) -> None:
        """Raise Throttled while waiting."""
        if (left := self.left()) > 0:
            raise Throttled(left)

    def note(self, err: BaseException) -> Throttled | None:
        """Start waiting if ``err`` means "too many requests"; return the wait."""
        if (seconds := throttle_seconds(err)) is None:
            return None
        if self.left() <= 0:
            _LOGGER.warning(
                "The CalDAV server asked to wait %d minutes; until then the cached events are shown",
                max(1, round(seconds / 60)),
            )
        self._until = max(self._until, time.monotonic() + seconds)
        return Throttled(self.left())


@dataclass
class CalendarInfo:
    """A calendar collection as listed on the server."""

    key: str  # the calendar's absolute URL, as the account knows it
    calendar: Calendar
    name: str | None
    components: frozenset[str]
    writable: bool
    ctag: str | None


@dataclass
class _Cache:
    ctag: str | None  # the change tag the objects were read under
    fetched: float  # time.monotonic() of the read
    start: datetime
    end: datetime
    objects: list[ICalendar] = field(default_factory=list)


def discover(
    client: DAVClient, throttle: Throttle, known: dict[str, dict[str, Any]] | None = None
) -> AccountStore:
    """List the account's calendars with one PROPFIND (after finding the home set).

    ``known`` is the listing saved last time (see AccountStore.listing). A calendar
    the server fails to describe now is set up from it, so it isn't lost while the
    server has trouble; it shows as unavailable until it can be read.
    """
    known = known or {}
    throttle.check()
    try:
        principal = client.get_principal()
        home = URL.objectify(principal.calendar_home_set.url)
        tree = _propfind(client, home, _LIST_BODY)
    except DAVError as err:
        if (throttled := throttle.note(err)) is not None:
            raise throttled from err
        raise
    calendars = []
    for href, props in _parse(tree):
        if not props and _is_member(home, href):
            # The server failed this collection; ask for the basics on their own
            props = _basic_props(client, home, href)
            if not props:
                if saved := known.get(str(home.join(href))):
                    _LOGGER.warning(
                        "The server can't describe calendar %s now; it's set up as last seen",
                        saved.get("name"),
                    )
                    calendars.append(_saved_info(client, home, href, saved))
                else:
                    _LOGGER.warning("Skipped a collection the server can't describe: %s", href)
                continue
        if info := _calendar_info(client, home, href, props):
            calendars.append(info)
    return AccountStore(client, home, calendars, throttle)


def _saved_info(client: DAVClient, home: URL, href: str, saved: dict[str, Any]) -> CalendarInfo:
    url = home.join(href)
    name = saved.get("name")
    return CalendarInfo(
        key=str(url),
        calendar=Calendar(client=client, url=url, name=name, id=str(url).rstrip("/").split("/")[-1]),
        name=name,
        components=frozenset(saved.get("components") or _ALL_COMPONENTS),
        writable=bool(saved.get("writable", True)),
        ctag=None,
    )


def _is_member(home: URL, href: str) -> bool:
    """Return whether href is a collection inside the home set (not the home itself)."""
    return str(home.join(href)).rstrip("/") != str(home).rstrip("/")


def _basic_props(client: DAVClient, home: URL, href: str) -> dict[str, Any]:
    try:
        tree = _propfind(client, home.join(href), _BASIC_BODY, depth="0")
    except DAVError as err:
        _LOGGER.debug("Cannot describe %s: %s", href, err)
        return {}
    return next((props for _, props in _parse(tree)), {})


def calendar_color(client: DAVClient, calendar: Calendar) -> str | None:
    """Read the color of one calendar (blocking); None if it has none or can't be read."""
    try:
        tree = _propfind(client, URL.objectify(calendar.url), _COLOR_BODY, depth="0")
    except DAVError as err:
        _LOGGER.debug("Cannot read the color of %s: %s", calendar.url, err)
        return None
    return next(
        (_color(_element_text(props.get(f"{_APPLE}calendar-color"))) for _, props in _parse(tree)),
        None,
    )


class AccountStore:
    """The calendars of one account and a cache of their events."""

    def __init__(
        self, client: DAVClient, home: URL, calendars: list[CalendarInfo], throttle: Throttle
    ) -> None:
        """Keep the listing; the tags in it count as just checked."""
        self.client = client
        self.home = home
        self.throttle = throttle
        self.calendars = {info.key: info for info in calendars}
        # UID -> URL of its object, per calendar; finding an object by UID isn't
        # possible on every server (iCloud refuses that REPORT).
        self.urls: dict[str, dict[str, str]] = {key: {} for key in self.calendars}
        self._caches: dict[str, _Cache] = {}
        self._locks = {key: threading.Lock() for key in self.calendars}
        self._ctag_lock = threading.Lock()
        self._ctags_checked = time.monotonic()

    def listing(self) -> dict[str, dict[str, Any]]:
        """Return what discover() needs to set up the calendars without the server."""
        return {
            key: {
                "name": info.name,
                "components": sorted(info.components),
                "writable": info.writable,
            }
            for key, info in self.calendars.items()
        }

    def call(self, function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Send a request unless throttled, and notice when the server throttles."""
        self.throttle.check()
        try:
            return function(*args, **kwargs)
        except DAVError as err:
            if (throttled := self.throttle.note(err)) is not None:
                raise throttled from err
            raise

    def refresh_ctags(self, max_age: float = CTAG_MAX_AGE) -> None:
        """Read the change tags of all calendars, unless read less than max_age ago."""
        with self._ctag_lock:
            if time.monotonic() - self._ctags_checked < max_age:
                return
            try:
                tree = self.call(_propfind, self.client, self.home, _CTAG_BODY)
            except DAVError:
                # Don't ask again on every dashboard request while it fails
                self._ctags_checked = time.monotonic()
                raise
            for href, props in _parse(tree):
                # A calendar the server failed in this answer keeps its last tag
                if props and (info := self.calendars.get(str(self.home.join(href)))):
                    info.ctag = _ctag(props)
            self._ctags_checked = time.monotonic()

    def invalidate(self, key: str) -> None:
        """Forget a calendar's cached events, after changing it."""
        if (cache := self._caches.get(key)) is not None:
            cache.ctag = None
            cache.fetched = 0.0
        self._ctags_checked = 0.0

    def is_cached(self, key: str) -> bool:
        """Return whether events of a calendar are cached."""
        return key in self._caches

    def objects(self, key: str, start: datetime, end: datetime) -> list[ICalendar]:
        """Return the calendar objects with events between start and end.

        They come from the cache while the calendar's change tag is unchanged, and
        from a stale cache while the server asks to wait.
        """
        try:
            self.refresh_ctags()
        except Throttled:
            pass  # judged by the cache below
        except AuthorizationError:
            raise
        except DAVError as err:
            # The check failed, not necessarily this calendar; go by the last tags
            _LOGGER.debug("Cannot check the calendars for changes: %s", err)
        info = self.calendars[key]
        with self._locks[key]:
            cache = self._caches.get(key)
            covered = cache is not None and cache.start <= start and end <= cache.end
            if covered and (self._is_fresh(cache, info) or self.throttle.left() > 0):
                return cache.objects  # type: ignore[union-attr]
            same = cache is not None and cache.ctag is not None and cache.ctag == info.ctag
            fetch_start, fetch_end, keep = _window(start, end, cache if same else None)
            ctag = info.ctag  # read before the objects, so a change in between is fetched next time
            resources = self.call(
                info.calendar.search, start=fetch_start, end=fetch_end, event=True, expand=False
            )
            objects: list[ICalendar] = []
            urls = self.urls[key]
            for resource in resources:
                try:
                    ical = resource.icalendar_instance
                except Exception as err:  # noqa: BLE001 - one broken object mustn't hide the rest
                    _LOGGER.warning("Skipped a calendar object that can't be read: %s", err)
                    continue
                objects.append(ical)
                if resource.url:
                    for component in ical.walk("VEVENT"):
                        if "UID" in component:
                            urls[str(component["UID"])] = str(resource.url)
            if keep:
                self._caches[key] = _Cache(ctag, time.monotonic(), fetch_start, fetch_end, objects)
            return objects

    @staticmethod
    def _is_fresh(cache: _Cache, info: CalendarInfo) -> bool:
        if info.ctag is None or cache.ctag is None:
            # Without a change tag (some shared calendars have none) a read is
            # trusted for a while; an invalidated cache has fetched == 0
            return cache.ctag is None and info.ctag is None and (
                time.monotonic() - cache.fetched < NO_CTAG_MAX_AGE
            )
        return cache.ctag == info.ctag


def _window(
    start: datetime, end: datetime, cache: _Cache | None
) -> tuple[datetime, datetime, bool]:
    """Return the range to fetch and whether to cache it.

    A little more than asked is fetched, so moving a week or a month in the
    calendar panel doesn't need the server again. Far-away ranges aren't cached.
    """
    today = dt_util.start_of_local_day()
    fetch_start = min(start, today - WINDOW_PAST)
    fetch_end = max(end, today + WINDOW_FUTURE)
    if cache is not None:
        fetch_start = min(fetch_start, cache.start)
        fetch_end = max(fetch_end, cache.end)
    if fetch_end - fetch_start > MAX_WINDOW:
        return start, end, False
    return fetch_start, fetch_end, True


def _propfind(client: DAVClient, url: URL, body: str, depth: str = "1") -> Any:
    """Send a PROPFIND and return the parsed XML."""
    response = client.request(
        str(url),
        "PROPFIND",
        body,
        {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
    )
    if not 200 <= response.status < 300 or response.tree is None:
        raise DAVError(url=str(url), reason=f"{response.status} on PROPFIND")
    return response.tree


def _parse(tree: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield the href and the found properties of each response in a multistatus."""
    if tree is None:
        return
    for response in tree.iter(f"{_DAV}response"):
        href = (response.findtext(f"{_DAV}href") or "").strip()
        if not href:
            continue
        props: dict[str, Any] = {}
        for propstat in response.iter(f"{_DAV}propstat"):
            status = propstat.findtext(f"{_DAV}status") or ""
            prop = propstat.find(f"{_DAV}prop")
            if " 200" in status and prop is not None:
                for element in prop:
                    props[element.tag] = element
        yield href, props


def _calendar_info(
    client: DAVClient, home: URL, href: str, props: dict[str, Any]
) -> CalendarInfo | None:
    resource_type = props.get(f"{_DAV}resourcetype")
    if resource_type is None or resource_type.find(f"{_CALDAV}calendar") is None:
        return None
    url = home.join(href)
    # The same id caldav gives a calendar, so unique ids don't change
    cal_id = str(url).rstrip("/").split("/")[-1]
    if not cal_id:
        return None
    name = _element_text(props.get(f"{_DAV}displayname"))
    component_set = props.get(f"{_CALDAV}supported-calendar-component-set")
    components = (
        frozenset(
            str(comp.get("name")).upper() for comp in component_set.iter(f"{_CALDAV}comp")
        )
        if component_set is not None
        else _ALL_COMPONENTS  # rfc4791 5.2.3: absent means any
    )
    return CalendarInfo(
        key=str(url),
        calendar=Calendar(client=client, url=url, name=name, id=cal_id),
        name=name,
        components=components,
        writable=_writable(props.get(f"{_DAV}current-user-privilege-set")),
        ctag=_ctag(props),
    )


def _writable(privilege_set: Any) -> bool:
    """Return whether the privileges allow writing; unknown counts as writable."""
    if privilege_set is None:
        return True
    names = {
        element.tag.rsplit("}", 1)[-1]
        for element in privilege_set.iter()
        if isinstance(element.tag, str)
    }
    if "privilege" not in names:
        return True
    return bool(names & _WRITE_PRIVILEGES)


def _color(value: str | None) -> str | None:
    # Apple Calendar, DAVx5 and SOGo append an alpha channel
    if value and value.startswith("#") and len(value) == 9:
        return value[:7]
    return value or None


def _ctag(props: dict[str, Any]) -> str | None:
    return _element_text(props.get(f"{_CS}getctag")) or _element_text(
        props.get(f"{_DAV}sync-token")
    )


def _element_text(element: Any) -> str | None:
    if element is None or element.text is None:
        return None
    return element.text.strip() or None
