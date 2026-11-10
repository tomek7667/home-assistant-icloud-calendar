"""Change and delete events in iCalendar data, including single occurrences of a series.

Everything here works on one CalDAV calendar object resource: an ``icalendar.Calendar``
holding a VEVENT "master" (with RRULE for a series) and optional overrides, VEVENTs
that share the UID and carry a RECURRENCE-ID. The functions change that calendar in
place and report what has to happen on the server. They know nothing about CalDAV or
Home Assistant, so they can be tested on their own.

Occurrences are identified by a *recurrence id* string (``format_recurrence_id``):
``YYYYMMDD`` for all-day events, ``YYYYMMDDTHHMMSSZ`` (UTC) for events with a time zone
and ``YYYYMMDDTHHMMSS`` for floating times.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
import uuid

from dateutil import rrule as du_rrule
from icalendar import Calendar, Event, vDDDLists, vRecur

UPDATE_FIELDS = ("summary", "dtstart", "dtend", "description", "location", "rrule")
# Properties that belong to the series, not to a single occurrence
_SERIES_ONLY = ("RRULE", "RDATE", "EXDATE", "EXRULE", "RECURRENCE-ID", "DTSTART", "DTEND", "DURATION")


class EditError(Exception):
    """The requested change can't be made to this event."""


@dataclass
class EditResult:
    """What the caller has to do on the server after an edit."""

    delete_resource: bool = False  # nothing left: delete the whole resource
    new_calendars: list[Calendar] = field(default_factory=list)  # create these as new resources


# ---- recurrence ids ---------------------------------------------------------------


def format_recurrence_id(value: date | datetime) -> str:
    """Return the recurrence id string for an occurrence's original start."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.strftime("%Y%m%dT%H%M%S")
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


def parse_recurrence_id(value: str) -> date | datetime:
    """Parse a recurrence id string back into a date or datetime."""
    value = value.strip()
    try:
        if len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").date()
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError as err:
        raise EditError(f"Invalid recurrence id: {value}") from err


def _key(value: date | datetime) -> str:
    return format_recurrence_id(value)


# ---- components -------------------------------------------------------------------


def _events(cal: Calendar) -> list[Event]:
    return [c for c in cal.subcomponents if c.name == "VEVENT"]


def get_master(cal: Calendar) -> Event | None:
    """The VEVENT without RECURRENCE-ID (the single event or the series)."""
    return next((e for e in _events(cal) if "RECURRENCE-ID" not in e), None)


def _overrides(cal: Calendar) -> list[Event]:
    return [e for e in _events(cal) if "RECURRENCE-ID" in e]


def _rid_value(component: Event) -> date | datetime:
    return component.decoded("RECURRENCE-ID")


def _find_override(cal: Calendar, rid: str) -> Event | None:
    return next((e for e in _overrides(cal) if _key(_rid_value(e)) == rid), None)


def is_recurring(master: Event | None) -> bool:
    return master is not None and ("RRULE" in master or "RDATE" in master)


def _start(component: Event) -> date | datetime:
    return component.decoded("DTSTART")


def _duration(component: Event) -> timedelta:
    start = _start(component)
    if "DTEND" in component:
        return component.decoded("DTEND") - start
    if "DURATION" in component:
        return component.decoded("DURATION")
    return timedelta(days=1) if not isinstance(start, datetime) else timedelta(0)


def _like(value: date | datetime, reference: date | datetime) -> date | datetime:
    """Convert an occurrence start to the value type and time zone of ``reference``."""
    if not isinstance(reference, datetime):
        return value.date() if isinstance(value, datetime) else value
    if not isinstance(value, datetime):
        value = datetime.combine(value, reference.timetz().replace(tzinfo=None), tzinfo=reference.tzinfo)
    if reference.tzinfo is None:
        return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone().replace(tzinfo=None)
    return value.astimezone(reference.tzinfo) if value.tzinfo else value.replace(tzinfo=reference.tzinfo)


def _same_instant(a: date | datetime, b: date | datetime) -> bool:
    return _key(a) == _key(_like(b, a))


def _touch(component: Event) -> None:
    """Mark a component as changed (SEQUENCE, DTSTAMP, LAST-MODIFIED)."""
    now = datetime.now(UTC).replace(microsecond=0)
    seq = int(component.get("SEQUENCE", 0))
    for prop in ("SEQUENCE", "DTSTAMP", "LAST-MODIFIED"):
        component.pop(prop, None)
    component.add("SEQUENCE", seq + 1)
    component.add("DTSTAMP", now)
    component.add("LAST-MODIFIED", now)


def _set(component: Event, name: str, value: Any) -> None:
    component.pop(name, None)
    if value not in (None, ""):
        component.add(name, value)


def _set_times(component: Event, start: date | datetime, end: date | datetime | None) -> None:
    component.pop("DURATION", None)
    _set(component, "DTSTART", start)
    if end is not None:
        _set(component, "DTEND", end)


def _apply_fields(component: Event, changes: dict[str, Any], *, allow_rrule: bool) -> None:
    """Apply Home Assistant event fields to a VEVENT; fields not given are kept."""
    for name in ("summary", "description", "location"):
        if name in changes:
            _set(component, name.upper(), changes[name])
    if "dtstart" in changes or "dtend" in changes:
        start = changes.get("dtstart", _start(component))
        end = changes.get("dtend")
        if end is None:
            end = start + _duration(component)
        same_kind = isinstance(start, datetime) == isinstance(end, datetime)
        if same_kind and end <= start:
            raise EditError("The event must end after it starts")
        _set_times(component, start, end)
    if "rrule" in changes:
        if not allow_rrule:
            raise EditError("A single occurrence can't get its own repeat rule")
        component.pop("RRULE", None)
        if changes["rrule"]:
            component.add("RRULE", vRecur.from_ical(changes["rrule"]))


def _exdates(component: Event) -> list[date | datetime]:
    raw = component.get("EXDATE")
    if raw is None:
        return []
    lists = raw if isinstance(raw, list) else [raw]
    return [d.dt for item in lists if isinstance(item, vDDDLists) for d in item.dts]


def _set_exdates(component: Event, values: list[date | datetime]) -> None:
    component.pop("EXDATE", None)
    for value in values:
        # rfc5545: EXDATE defaults to DATE-TIME, a date needs VALUE=DATE
        params = {} if isinstance(value, datetime) else {"VALUE": "DATE"}
        component.add("EXDATE", value, parameters=params)


def _until_before(occurrence: date | datetime, start: date | datetime) -> date | datetime:
    """The RRULE UNTIL that ends a series just before ``occurrence`` (rfc5545 3.3.10)."""
    if not isinstance(start, datetime):
        return occurrence - timedelta(days=1)
    last = occurrence - timedelta(seconds=1)
    # With a time zone UNTIL must be UTC; with floating times it stays floating.
    return last.astimezone(UTC) if last.tzinfo else last


def _end_series_before(master: Event, occurrence: date | datetime) -> None:
    rule = master.get("RRULE")
    if rule is not None:
        rule = vRecur(copy.deepcopy(dict(rule)))
        rule.pop("COUNT", None)
        rule["UNTIL"] = [_until_before(occurrence, _start(master))]
        master.pop("RRULE")
        master.add("RRULE", rule)
    rdates = master.get("RDATE")
    if rdates is not None:
        kept = [
            d.dt for item in (rdates if isinstance(rdates, list) else [rdates]) for d in item.dts
            if _key(d.dt) < _key(_like(occurrence, d.dt))
        ]
        master.pop("RDATE")
        for value in kept:
            master.add("RDATE", value)
    _set_exdates(master, [d for d in _exdates(master) if _key(d) < _key(_like(occurrence, d))])


def _shift_until(master: Event, delta: timedelta) -> None:
    """Move UNTIL with the series when the split moved its start, so no occurrence is lost."""
    rule = master.get("RRULE")
    if rule is None or "UNTIL" not in rule or not delta:
        return
    rule = vRecur(copy.deepcopy(dict(rule)))
    until = rule["UNTIL"][0]
    if not isinstance(until, datetime) and delta % timedelta(days=1):
        return  # all-day series: only whole days can move
    rule["UNTIL"] = [until + delta]
    master.pop("RRULE")
    master.add("RRULE", rule)


def _count_to_until(master: Event) -> None:
    """Turn COUNT into UNTIL, so a series can be split without changing its length."""
    rule = master.get("RRULE")
    if rule is None or "COUNT" not in rule:
        return
    start = _start(master)
    dtstart = start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time())
    occurrences = list(du_rrule.rrulestr(rule.to_ical().decode(), dtstart=dtstart))
    if not occurrences:
        return
    last = occurrences[-1] if isinstance(start, datetime) else occurrences[-1].date()
    rule = vRecur(copy.deepcopy(dict(rule)))
    rule.pop("COUNT")
    rule["UNTIL"] = [last.astimezone(UTC) if isinstance(last, datetime) and last.tzinfo else last]
    master.pop("RRULE")
    master.add("RRULE", rule)


# ---- public operations ------------------------------------------------------------


def delete_event(
    cal: Calendar, recurrence_id: str | None = None, this_and_future: bool = False
) -> EditResult:
    """Delete an event, one occurrence of a series, or an occurrence and all after it."""
    master = get_master(cal)
    if recurrence_id is None or not is_recurring(master):
        if recurrence_id is not None and master is None:
            # Only overrides here (e.g. an invitation to one occurrence): drop that one
            if (override := _find_override(cal, recurrence_id)) is not None:
                cal.subcomponents.remove(override)
            return EditResult(delete_resource=not _events(cal))
        return EditResult(delete_resource=True)

    occurrence = parse_recurrence_id(recurrence_id)
    occurrence = _like(occurrence, _start(master))
    if this_and_future:
        if _key(occurrence) <= _key(_start(master)):
            return EditResult(delete_resource=True)  # from the first one on: all of it
        _end_series_before(master, occurrence)
        for override in _overrides(cal):
            if _key(_rid_value(override)) >= _key(_like(occurrence, _rid_value(override))):
                cal.subcomponents.remove(override)
        _touch(master)
        return EditResult()

    if (override := _find_override(cal, recurrence_id)) is not None:
        cal.subcomponents.remove(override)
    exdates = _exdates(master)
    if not any(_same_instant(d, occurrence) for d in exdates):
        exdates.append(occurrence)
    _set_exdates(master, exdates)
    _touch(master)
    return EditResult()


def update_event(
    cal: Calendar,
    changes: dict[str, Any],
    recurrence_id: str | None = None,
    this_and_future: bool = False,
) -> EditResult:
    """Change an event, one occurrence of a series, or an occurrence and all after it."""
    changes = {k: v for k, v in changes.items() if k in UPDATE_FIELDS}
    master = get_master(cal)
    if master is None:
        if recurrence_id is None or (override := _find_override(cal, recurrence_id)) is None:
            raise EditError("Event not found in this calendar object")
        _apply_fields(override, changes, allow_rrule=False)
        _touch(override)
        return EditResult()

    if recurrence_id is None or not is_recurring(master):
        _update_series(cal, master, changes)
        return EditResult()

    occurrence = _like(parse_recurrence_id(recurrence_id), _start(master))
    if this_and_future:
        if _key(occurrence) <= _key(_start(master)):
            _update_series(cal, master, changes)
            return EditResult()
        return _split_series(cal, master, occurrence, changes)

    # An occurrence can't carry its own rule; the editor may send the series' rule along.
    changes.pop("rrule", None)
    override = _find_override(cal, recurrence_id)
    if override is None:
        override = Event()
        for name, value in master.items():  # one entry per property name, lists stay lists
            if name.upper() not in _SERIES_ONLY:
                override[name] = copy.deepcopy(value)
        for sub in master.subcomponents:  # alarms and the like
            override.add_component(copy.deepcopy(sub))
        override.add("RECURRENCE-ID", occurrence)
        _set_times(override, occurrence, occurrence + _duration(master))
        cal.add_component(override)
    _apply_fields(override, changes, allow_rrule=False)
    _touch(override)
    return EditResult()


def _update_series(cal: Calendar, master: Event, changes: dict[str, Any]) -> None:
    old_start = _start(master)
    _apply_fields(master, changes, allow_rrule=True)
    if not is_recurring(master):
        for override in _overrides(cal):  # no series any more
            cal.subcomponents.remove(override)
        master.pop("EXDATE", None)
    elif _key(_start(master)) != _key(_like(old_start, _start(master))):
        # The series moved: old occurrence ids no longer line up, so their
        # exceptions and changed occurrences would point at nothing.
        for override in _overrides(cal):
            cal.subcomponents.remove(override)
        master.pop("EXDATE", None)
    _touch(master)


def _split_series(
    cal: Calendar, master: Event, occurrence: date | datetime, changes: dict[str, Any]
) -> EditResult:
    """End the series before ``occurrence`` and start a new one there with the changes."""
    # The editor sends the series' rule back unchanged; that keeps the series' length.
    if "rrule" in changes and master.get("RRULE") is not None and changes["rrule"]:
        if dict(vRecur.from_ical(changes["rrule"])) == dict(master["RRULE"]):
            changes = {k: v for k, v in changes.items() if k != "rrule"}
    _count_to_until(master)
    new_master = copy.deepcopy(master)
    _end_series_before(master, occurrence)
    _touch(master)

    new_master.pop("UID")
    new_master.add("UID", str(uuid.uuid4()).upper())
    new_master.pop("SEQUENCE", None)
    _set_times(new_master, occurrence, occurrence + _duration(master))
    _set_exdates(new_master, [d for d in _exdates(new_master) if _key(d) >= _key(_like(occurrence, d))])
    new_cal = Calendar()
    for name, value in cal.items():
        new_cal[name] = copy.deepcopy(value)
    new_cal.add_component(new_master)
    # Changed occurrences from the split point on move to the new series
    for override in _overrides(cal):
        if _key(_rid_value(override)) >= _key(_like(occurrence, _rid_value(override))):
            cal.subcomponents.remove(override)
            moved = copy.deepcopy(override)
            moved.pop("UID")
            moved.add("UID", new_master["UID"])
            new_cal.add_component(moved)
    _apply_fields(new_master, changes, allow_rrule=True)
    if "rrule" not in changes:
        _shift_until(new_master, _start(new_master) - _like(occurrence, _start(new_master)))
    _touch(new_master)
    for component in cal.subcomponents:
        if component.name == "VTIMEZONE":
            new_cal.add_component(copy.deepcopy(component))
    return EditResult(new_calendars=[new_cal])
