"""Tests for the local expansion of calendar objects (no Home Assistant needed)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "icloud_calendar"))
import editing  # noqa: E402
import events  # noqa: E402

WAW = ZoneInfo("Europe/Warsaw")
START = datetime(2026, 11, 9, tzinfo=WAW)
END = datetime(2027, 1, 1, tzinfo=WAW)

SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Apple Inc.//macOS 26.0//EN
BEGIN:VEVENT
UID:S1
DTSTART;TZID=Europe/Warsaw:20261110T140000
DTEND;TZID=Europe/Warsaw:20261110T150000
RRULE:FREQ=WEEKLY;COUNT=5
EXDATE;TZID=Europe/Warsaw:20261124T140000
SUMMARY:weekly
LOCATION:Office
END:VEVENT
BEGIN:VEVENT
UID:S1
RECURRENCE-ID;TZID=Europe/Warsaw:20261117T140000
DTSTART;TZID=Europe/Warsaw:20261117T160000
DTEND;TZID=Europe/Warsaw:20261117T170000
SUMMARY:moved
X-RECURRENCE-EXCEPTION:True
END:VEVENT
BEGIN:VEVENT
UID:S1
RECURRENCE-ID;TZID=Europe/Warsaw:20261201T140000
DTSTART;TZID=Europe/Warsaw:20261201T140000
DTEND;TZID=Europe/Warsaw:20261201T150000
SUMMARY:called off
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


def _cal(text: str) -> Calendar:
    return Calendar.from_ical(text)


def _single(body: str) -> Calendar:
    return _cal(f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n{body}\nEND:VEVENT\nEND:VCALENDAR\n")


def test_series_with_override_exdate_and_cancelled_occurrence() -> None:
    found = events.expand([_cal(SERIES)], START, END)
    assert [(o.summary, o.start) for o in found] == [
        ("weekly", datetime(2026, 11, 10, 14, 0, tzinfo=WAW)),
        ("moved", datetime(2026, 11, 17, 16, 0, tzinfo=WAW)),
        ("weekly", datetime(2026, 12, 8, 14, 0, tzinfo=WAW)),
    ]
    # Every occurrence of the series carries its id and the series' rule
    assert [o.recurrence_id for o in found] == ["20261110T130000Z", "20261117T130000Z", "20261208T130000Z"]
    assert {o.rrule for o in found} == {"FREQ=WEEKLY;COUNT=5"}
    assert found[0].location == "Office"
    assert found[0].end == datetime(2026, 11, 10, 15, 0, tzinfo=WAW)


def test_recurrence_ids_find_the_occurrence_when_edited() -> None:
    """The ids the expansion gives are the ones editing.py looks for."""
    found = events.expand([_cal(SERIES)], START, END)
    cal = _cal(SERIES)
    editing.delete_event(cal, found[2].recurrence_id)
    assert [o.start.day for o in events.expand([cal], START, END)] == [10, 17]


def test_single_event_has_no_recurrence_id() -> None:
    found = events.expand(
        [_single("UID:E1\nDTSTART;TZID=Europe/Warsaw:20261112T090000\nDURATION:PT45M\nSUMMARY:dentist")],
        START,
        END,
    )
    assert len(found) == 1
    assert found[0].recurrence_id is None and found[0].rrule is None
    assert found[0].end == datetime(2026, 11, 12, 9, 45, tzinfo=WAW)


def test_all_day_event_without_end_lasts_one_day() -> None:
    (found,) = events.expand([_single("UID:A1\nDTSTART;VALUE=DATE:20261112\nSUMMARY:holiday")], START, END)
    assert (found.start, found.end) == (date(2026, 11, 12), date(2026, 11, 13))


def test_all_day_event_ending_on_its_start_lasts_one_day() -> None:
    (found,) = events.expand(
        [_single("UID:A2\nDTSTART;VALUE=DATE:20261112\nDTEND;VALUE=DATE:20261112\nSUMMARY:x")], START, END
    )
    assert found.end == date(2026, 11, 13)


def test_floating_time_stays_floating() -> None:
    (found,) = events.expand(
        [_single("UID:F1\nDTSTART:20261113T090000\nDTEND:20261113T100000\nSUMMARY:floating")], START, END
    )
    assert found.start == datetime(2026, 11, 13, 9, 0) and found.start.tzinfo is None


def test_override_without_its_series_is_an_occurrence() -> None:
    """An invitation to a single occurrence holds just the override."""
    (found,) = events.expand(
        [
            _single(
                "UID:INV\nRECURRENCE-ID;TZID=Europe/Warsaw:20261119T100000\n"
                "DTSTART;TZID=Europe/Warsaw:20261119T100000\nDTEND;TZID=Europe/Warsaw:20261119T110000\n"
                "SUMMARY:invited"
            )
        ],
        START,
        END,
    )
    assert found.recurrence_id == "20261119T090000Z" and found.rrule is None


def test_status_is_reported_and_cancelled_events_left_out() -> None:
    found = events.expand(
        [
            _single("UID:T1\nDTSTART;VALUE=DATE:20261112\nSTATUS:tentative\nSUMMARY:maybe"),
            _single("UID:C1\nDTSTART;VALUE=DATE:20261113\nSTATUS:CANCELLED\nSUMMARY:off"),
        ],
        START,
        END,
    )
    assert [(o.summary, o.status) for o in found] == [("maybe", "TENTATIVE")]


def test_events_outside_the_range_are_left_out() -> None:
    assert events.expand([_single("UID:O1\nDTSTART;VALUE=DATE:20270301\nSUMMARY:later")], START, END) == []


def test_object_that_fails_to_expand_does_not_hide_the_others(monkeypatch) -> None:
    broken = _single("UID:B1\nDTSTART;VALUE=DATE:20261112\nSUMMARY:broken")
    good = _single("UID:G1\nDTSTART;VALUE=DATE:20261114\nSUMMARY:good")
    real_of = events.recurring_ical_events.of

    def of(cal, **kwargs):
        if cal is broken:
            raise ValueError("cannot expand")
        return real_of(cal, **kwargs)

    monkeypatch.setattr(events.recurring_ical_events, "of", of)
    assert [o.summary for o in events.expand([broken, good], START, END)] == ["good"]
