"""Tests for the iCalendar editing logic (no Home Assistant needed)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from icalendar import Calendar
import pytest
import recurring_ical_events

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "icloud_calendar"))
import editing  # noqa: E402

WAW = ZoneInfo("Europe/Warsaw")

WEEKLY = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Apple Inc.//macOS 26.0//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:SERIES-1
DTSTAMP:20261001T080000Z
SEQUENCE:0
SUMMARY:Standup
LOCATION:Office
X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC
DTSTART;TZID=Europe/Warsaw:20261005T100000
DTEND;TZID=Europe/Warsaw:20261005T103000
RRULE:FREQ=WEEKLY;COUNT=8
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT10M
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
END:VCALENDAR
"""

ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:BIRTHDAYS
DTSTAMP:20261001T080000Z
SUMMARY:Garbage day
DTSTART;VALUE=DATE:20261102
DTEND;VALUE=DATE:20261103
RRULE:FREQ=DAILY;INTERVAL=7
END:VEVENT
END:VCALENDAR
"""

FLOATING = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:FLOAT
DTSTAMP:20261001T080000Z
SUMMARY:Gym
DTSTART:20261103T180000
DTEND:20261103T190000
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
END:VCALENDAR
"""

SINGLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:ONE
DTSTAMP:20261001T080000Z
SUMMARY:Dentist
DTSTART;TZID=Europe/Warsaw:20261110T090000
DTEND;TZID=Europe/Warsaw:20261110T100000
END:VEVENT
END:VCALENDAR
"""


def load(text: str) -> Calendar:
    return Calendar.from_ical(text)


def roundtrip(cal: Calendar) -> Calendar:
    """Serialize and parse again, as the server would store it."""
    cal.add_missing_timezones()
    return Calendar.from_ical(cal.to_ical())


def starts(cal: Calendar, start=date(2026, 1, 1), end=date(2027, 12, 31)) -> list:
    return [e.decoded("DTSTART") for e in recurring_ical_events.of(cal).between(start, end)]


def occurrence(cal: Calendar, n: int):
    return recurring_ical_events.of(cal).between(date(2026, 1, 1), date(2027, 12, 31))[n]


def rid_of(component) -> str:
    return editing.format_recurrence_id(component.decoded("RECURRENCE-ID"))


# ---- recurrence ids --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 11, 2), "20261102"),
        (datetime(2026, 11, 2, 10, 0, tzinfo=WAW), "20261102T090000Z"),
        (datetime(2026, 7, 2, 10, 0, tzinfo=WAW), "20260702T080000Z"),
        (datetime(2026, 11, 2, 18, 0), "20261102T180000"),
    ],
)
def test_recurrence_id_roundtrip(value, expected) -> None:
    assert editing.format_recurrence_id(value) == expected
    parsed = editing.parse_recurrence_id(expected)
    assert editing.format_recurrence_id(parsed) == expected


def test_invalid_recurrence_id() -> None:
    with pytest.raises(editing.EditError):
        editing.parse_recurrence_id("yesterday")


# ---- delete ------------------------------------------------------------------------


def test_delete_single_event() -> None:
    assert editing.delete_event(load(SINGLE)).delete_resource


def test_delete_whole_series() -> None:
    assert editing.delete_event(load(WEEKLY)).delete_resource


def test_delete_one_occurrence_across_dst() -> None:
    """Delete the 4th occurrence (after DST ends on Oct 25): still 10:00 local."""
    cal = load(WEEKLY)
    fourth = occurrence(cal, 3)
    assert fourth.decoded("DTSTART") == datetime(2026, 10, 26, 10, 0, tzinfo=WAW)
    rid = editing.format_recurrence_id(fourth.decoded("RECURRENCE-ID"))

    result = editing.delete_event(cal, rid)
    assert not result.delete_resource
    cal = roundtrip(cal)
    remaining = starts(cal)
    assert len(remaining) == 7
    assert datetime(2026, 10, 26, 10, 0, tzinfo=WAW) not in remaining
    master = editing.get_master(cal)
    assert int(master["SEQUENCE"]) == 1
    assert "TZID=Europe/Warsaw" in master.to_ical().decode().replace("\r\n ", "")


def test_delete_changed_occurrence_removes_override() -> None:
    cal = load(WEEKLY)
    rid = editing.format_recurrence_id(occurrence(cal, 1).decoded("RECURRENCE-ID"))
    editing.update_event(cal, {"summary": "Moved standup"}, rid)
    assert len(cal.walk("VEVENT")) == 2
    editing.delete_event(cal, rid)
    cal = roundtrip(cal)
    assert len(cal.walk("VEVENT")) == 1
    assert len(starts(cal)) == 7


def test_delete_this_and_future() -> None:
    cal = load(WEEKLY)
    third = occurrence(cal, 2)
    rid = editing.format_recurrence_id(third.decoded("RECURRENCE-ID"))
    editing.update_event(cal, {"summary": "Later change"}, editing.format_recurrence_id(occurrence(cal, 5).decoded("RECURRENCE-ID")))

    result = editing.delete_event(cal, rid, this_and_future=True)
    assert not result.delete_resource
    cal = roundtrip(cal)
    assert starts(cal) == [
        datetime(2026, 10, 5, 10, 0, tzinfo=WAW),
        datetime(2026, 10, 12, 10, 0, tzinfo=WAW),
    ]
    assert len(cal.walk("VEVENT")) == 1  # the later change is gone too
    rule = editing.get_master(cal)["RRULE"]
    assert "COUNT" not in rule
    assert rule["UNTIL"][0].tzinfo is not None  # UTC, as rfc5545 requires with TZID


def test_delete_this_and_future_from_first_deletes_all() -> None:
    cal = load(WEEKLY)
    rid = editing.format_recurrence_id(occurrence(cal, 0).decoded("RECURRENCE-ID"))
    assert editing.delete_event(cal, rid, this_and_future=True).delete_resource


def test_delete_all_day_occurrence() -> None:
    cal = load(ALL_DAY)
    rid = editing.format_recurrence_id(occurrence(cal, 1).decoded("RECURRENCE-ID"))
    assert rid == "20261109"
    editing.delete_event(cal, rid)
    text = editing.get_master(cal).to_ical().decode()
    assert "EXDATE;VALUE=DATE:20261109" in text
    cal = roundtrip(cal)
    assert date(2026, 11, 9) not in starts(cal, date(2026, 11, 1), date(2026, 12, 1))
    assert date(2026, 11, 16) in starts(cal, date(2026, 11, 1), date(2026, 12, 1))


def test_delete_floating_occurrence() -> None:
    cal = load(FLOATING)
    rid = editing.format_recurrence_id(occurrence(cal, 2).decoded("RECURRENCE-ID"))
    assert rid == "20261105T180000"
    editing.delete_event(cal, rid)
    cal = roundtrip(cal)
    assert datetime(2026, 11, 5, 18, 0) not in starts(cal)
    assert len(starts(cal)) == 4


# ---- update ------------------------------------------------------------------------


def test_update_single_event_keeps_other_fields() -> None:
    cal = load(SINGLE)
    new_start = datetime(2026, 11, 10, 11, 0, tzinfo=WAW)
    editing.update_event(cal, {"summary": "Dentist (moved)", "dtstart": new_start, "dtend": new_start + timedelta(hours=1)})
    cal = roundtrip(cal)
    event = editing.get_master(cal)
    assert str(event["SUMMARY"]) == "Dentist (moved)"
    assert event.decoded("DTSTART") == new_start
    assert event.decoded("DTEND") == datetime(2026, 11, 10, 12, 0, tzinfo=WAW)
    assert int(event["SEQUENCE"]) == 1


def test_update_rejects_end_before_start() -> None:
    cal = load(SINGLE)
    start = datetime(2026, 11, 10, 11, 0, tzinfo=WAW)
    with pytest.raises(editing.EditError):
        editing.update_event(cal, {"dtstart": start, "dtend": start - timedelta(minutes=5)})


def test_update_one_occurrence() -> None:
    cal = load(WEEKLY)
    second = occurrence(cal, 1)
    rid = editing.format_recurrence_id(second.decoded("RECURRENCE-ID"))
    moved = datetime(2026, 10, 12, 14, 0, tzinfo=WAW)
    editing.update_event(
        cal,
        {"summary": "Standup (afternoon)", "dtstart": moved, "dtend": moved + timedelta(minutes=30), "rrule": "FREQ=WEEKLY;COUNT=8"},
        rid,
    )
    cal = roundtrip(cal)
    events = cal.walk("VEVENT")
    assert len(events) == 2
    override = next(e for e in events if "RECURRENCE-ID" in e)
    assert rid_of(override) == rid
    assert "RRULE" not in override and "EXDATE" not in override
    assert str(override["LOCATION"]) == "Office"  # copied from the series
    assert "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR" in override
    assert override.walk("VALARM")  # alarm copied
    instances = recurring_ical_events.of(cal).between(date(2026, 10, 1), date(2026, 12, 31))
    assert len(instances) == 8
    changed = [i for i in instances if str(i["SUMMARY"]) == "Standup (afternoon)"]
    assert [i.decoded("DTSTART") for i in changed] == [moved]


def test_update_same_occurrence_twice_changes_override() -> None:
    cal = load(WEEKLY)
    rid = editing.format_recurrence_id(occurrence(cal, 1).decoded("RECURRENCE-ID"))
    editing.update_event(cal, {"summary": "first change"}, rid)
    editing.update_event(cal, {"summary": "second change"}, rid)
    overrides = [e for e in cal.walk("VEVENT") if "RECURRENCE-ID" in e]
    assert len(overrides) == 1
    assert str(overrides[0]["SUMMARY"]) == "second change"
    assert int(overrides[0]["SEQUENCE"]) >= 2


def test_update_whole_series_title() -> None:
    cal = load(WEEKLY)
    rid = editing.format_recurrence_id(occurrence(cal, 0).decoded("RECURRENCE-ID"))
    editing.update_event(cal, {"summary": "Daily sync"}, rid, this_and_future=True)
    cal = roundtrip(cal)
    assert {str(i["SUMMARY"]) for i in recurring_ical_events.of(cal).between(date(2026, 1, 1), date(2027, 1, 1))} == {"Daily sync"}


def test_update_series_time_drops_stale_exceptions() -> None:
    cal = load(WEEKLY)
    rid = editing.format_recurrence_id(occurrence(cal, 1).decoded("RECURRENCE-ID"))
    editing.delete_event(cal, rid)
    new_start = datetime(2026, 10, 5, 12, 0, tzinfo=WAW)
    editing.update_event(cal, {"dtstart": new_start, "dtend": new_start + timedelta(minutes=30)})
    cal = roundtrip(cal)
    assert "EXDATE" not in editing.get_master(cal)
    assert all(s.hour == 12 for s in starts(cal))
    assert len(starts(cal)) == 8


def test_update_this_and_future_splits_series() -> None:
    cal = load(WEEKLY)
    fourth = occurrence(cal, 3)
    rid = editing.format_recurrence_id(fourth.decoded("RECURRENCE-ID"))
    new_start = datetime(2026, 10, 26, 11, 0, tzinfo=WAW)
    result = editing.update_event(
        cal, {"summary": "Standup v2", "dtstart": new_start, "dtend": new_start + timedelta(minutes=15)}, rid, this_and_future=True
    )
    assert len(result.new_calendars) == 1
    old = roundtrip(cal)
    new = roundtrip(result.new_calendars[0])
    assert len(starts(old)) == 3
    new_starts = starts(new)
    assert len(new_starts) == 5  # COUNT=8 kept: 3 + 5
    assert new_starts[0] == new_start
    new_master = editing.get_master(new)
    assert str(new_master["UID"]) != "SERIES-1"
    assert str(new_master["SUMMARY"]) == "Standup v2"
    assert new_master.walk("VALARM")


def test_update_this_and_future_moves_later_changes() -> None:
    cal = load(WEEKLY)
    later_rid = editing.format_recurrence_id(occurrence(cal, 6).decoded("RECURRENCE-ID"))
    editing.update_event(cal, {"summary": "Special"}, later_rid)
    rid = editing.format_recurrence_id(occurrence(cal, 3).decoded("RECURRENCE-ID"))
    result = editing.update_event(cal, {"location": "Room 2"}, rid, this_and_future=True)
    new = roundtrip(result.new_calendars[0])
    summaries = [str(i["SUMMARY"]) for i in recurring_ical_events.of(new).between(date(2026, 1, 1), date(2027, 1, 1))]
    assert summaries.count("Special") == 1
    assert all("RECURRENCE-ID" not in e for e in roundtrip(cal).walk("VEVENT"))


def test_update_series_remove_rule() -> None:
    cal = load(WEEKLY)
    rid = editing.format_recurrence_id(occurrence(cal, 2).decoded("RECURRENCE-ID"))
    editing.update_event(cal, {"summary": "x"}, rid)
    editing.update_event(cal, {"rrule": ""})
    cal = roundtrip(cal)
    assert len(cal.walk("VEVENT")) == 1
    assert len(starts(cal)) == 1


def test_update_all_day_occurrence() -> None:
    cal = load(ALL_DAY)
    rid = editing.format_recurrence_id(occurrence(cal, 2).decoded("RECURRENCE-ID"))
    editing.update_event(cal, {"dtstart": date(2026, 11, 17), "dtend": date(2026, 11, 18)}, rid)
    cal = roundtrip(cal)
    days = starts(cal, date(2026, 11, 1), date(2026, 12, 1))
    assert date(2026, 11, 16) not in days
    assert date(2026, 11, 17) in days


def test_update_this_and_future_with_unchanged_rule_from_editor() -> None:
    """The editor sends the series' rule back; the series must not grow by COUNT again."""
    cal = load(WEEKLY)
    rid = editing.format_recurrence_id(occurrence(cal, 3).decoded("RECURRENCE-ID"))
    result = editing.update_event(cal, {"summary": "v2", "rrule": "FREQ=WEEKLY;COUNT=8"}, rid, this_and_future=True)
    assert len(starts(roundtrip(cal))) == 3
    assert len(starts(roundtrip(result.new_calendars[0]))) == 5


def test_update_this_and_future_with_new_rule() -> None:
    cal = load(WEEKLY)
    rid = editing.format_recurrence_id(occurrence(cal, 3).decoded("RECURRENCE-ID"))
    result = editing.update_event(cal, {"rrule": "FREQ=DAILY;COUNT=3"}, rid, this_and_future=True)
    new_starts = starts(roundtrip(result.new_calendars[0]))
    assert new_starts == [datetime(2026, 10, 26, 10, 0, tzinfo=WAW) + timedelta(days=i) for i in range(3)]
