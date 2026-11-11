"""Turn calendar objects into the occurrences of a time range.

The objects are read from the server once and expanded here, instead of asking the
server to expand repeating events on every request. Nothing in this module needs
Home Assistant, so it can be tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging

from icalendar import Calendar, Event
import recurring_ical_events

try:
    from .editing import format_recurrence_id
except ImportError:  # imported on its own, by the tests
    from editing import format_recurrence_id  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One occurrence of an event, the way a calendar shows it."""

    summary: str
    start: date | datetime
    end: date | datetime
    location: str | None
    description: str | None
    uid: str | None
    # Only set for occurrences of a repeating event (see format_recurrence_id)
    recurrence_id: str | None
    # The series' RRULE, for occurrences of a repeating event
    rrule: str | None
    # Upper-case rfc5545 STATUS, if any
    status: str | None


def expand(objects: Iterable[Calendar], start: datetime, end: datetime) -> list[Occurrence]:
    """Return the occurrences between ``start`` and ``end``, cancelled ones left out."""
    result: list[Occurrence] = []
    for cal in objects:
        rules: dict[str, str] = {}
        series: set[str] = set()
        for component in cal.walk("VEVENT"):
            uid = str(component.get("UID", ""))
            if "RECURRENCE-ID" in component:
                series.add(uid)  # a changed occurrence, possibly without its series
            elif "RRULE" in component or "RDATE" in component:
                series.add(uid)
                if "RRULE" in component:
                    rules[uid] = component["RRULE"].to_ical().decode()
        try:
            occurrences = recurring_ical_events.of(cal, components=["VEVENT"]).between(
                start, end
            )
        except Exception as err:  # noqa: BLE001 - one broken object mustn't hide the rest
            _LOGGER.warning("Skipped a calendar object that can't be expanded: %s", err)
            continue
        for component in occurrences:
            occurrence = _occurrence(component, series, rules)
            if occurrence is not None:
                result.append(occurrence)
    return result


def _occurrence(component: Event, series: set[str], rules: dict[str, str]) -> Occurrence | None:
    status = str(component["STATUS"]).upper() if component.get("STATUS") else None
    if status == "CANCELLED":
        return None
    begin = component.decoded("DTSTART")
    uid = str(component["UID"]) if "UID" in component else None
    recurrence_id = None
    # The expansion gives every occurrence a RECURRENCE-ID, even of a single event
    if uid in series and "RECURRENCE-ID" in component:
        recurrence_id = format_recurrence_id(component.decoded("RECURRENCE-ID"))
    return Occurrence(
        summary=str(component.get("SUMMARY", "")),
        start=begin,
        end=_end(component, begin),
        location=_text(component, "LOCATION"),
        description=_text(component, "DESCRIPTION"),
        uid=uid,
        recurrence_id=recurrence_id,
        rrule=rules.get(uid or "") if recurrence_id else None,
        status=status,
    )


def _end(component: Event, begin: date | datetime) -> date | datetime:
    """Return the end the way the built-in integration shows it."""
    if "DTEND" in component:
        end = component.decoded("DTEND")
    elif "DURATION" in component:
        end = begin + component.decoded("DURATION")
    else:
        end = begin + timedelta(days=1)
    # An all-day event's end is exclusive, so it can't be its start
    if not isinstance(end, datetime) and end == begin:
        end += timedelta(days=1)
    return end


def _text(component: Event, name: str) -> str | None:
    value = component.get(name)
    return None if value is None else str(value)
