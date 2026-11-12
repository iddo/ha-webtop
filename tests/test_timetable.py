"""Pure-logic tests: time parsing, bell schedule, tests board, grades, current lesson."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.webtop.models import (
    Lesson,
    Student,
    parse_bell_schedule,
    parse_changes,
    parse_date,
    parse_grades,
    parse_schedule,
    parse_tests_board,
    parse_time,
)
from custom_components.webtop.timetable import (
    STATE_BREAK,
    STATE_NO_LESSON,
    lessons_on,
    resolve,
    webtop_weekday,
)

from .conftest import CHANGES, GRADES, LESSONS_WEEK, TESTS_BOARD, TIMETABLE

TZ = ZoneInfo("Asia/Jerusalem")
BELL = parse_bell_schedule(TESTS_BOARD["data"])
TUESDAY = dt.date(2026, 11, 10)


def at(hour: int, minute: int, day: dt.date = TUESDAY) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), TZ)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("08:00", dt.time(8, 0)), ("8:05", dt.time(8, 5)), ("08:45:00", dt.time(8, 45)),
     ("2026-11-10T13:15:00", dt.time(13, 15)), ("8.30", dt.time(8, 30)),
     ("", None), ("25:00", None), (None, None), ("lunch", None)],
)  # fmt: skip
def test_parse_time(value, expected) -> None:
    assert parse_time(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-11-18T00:00:00", dt.date(2026, 11, 18)), ("2026-11-18", dt.date(2026, 11, 18)),
     ("18/11/2026", dt.date(2026, 11, 18)), ("2026-13-40", None), ("", None), (None, None)],
)  # fmt: skip
def test_parse_date(value, expected) -> None:
    assert parse_date(value) == expected


def test_webtop_weekday() -> None:
    assert webtop_weekday(dt.date(2026, 11, 8)) == 1  # Sunday
    assert webtop_weekday(TUESDAY) == 3
    assert webtop_weekday(dt.date(2026, 11, 14)) == 7  # Saturday


def test_lessons_on_dated_and_weekday_timetables() -> None:
    dated = parse_schedule(LESSONS_WEEK["data"])
    assert [x.subject for x in lessons_on(dated, TUESDAY)] == ["מתמטיקה", "אנגלית"]
    assert lessons_on(dated, dt.date(2026, 11, 13)) == []  # dated week, empty Friday
    # A dated week says nothing about other weeks, even on the same weekday.
    assert lessons_on(dated, TUESDAY + dt.timedelta(days=7)) == []
    standing = parse_schedule(TIMETABLE["data"])
    assert [x.subject for x in lessons_on(standing, TUESDAY)] == ["מתמטיקה", "אנגלית"]
    assert [x.subject for x in lessons_on(standing, TUESDAY + dt.timedelta(days=7))] == ["מתמטיקה", "אנגלית"]
    assert lessons_on(standing, dt.date(2026, 11, 14)) == []  # Saturday


LESSONS = [
    Lesson(date="2026-11-10T00:00:00", hour=1, subject="מתמטיקה", teacher="רחל", room="12"),
    Lesson(date="2026-11-10T00:00:00", hour=2, subject="אנגלית", teacher="דוד"),
    Lesson(date="2026-11-10T00:00:00", hour=4, subject="מדעים", teacher="יעל", changed=True),
]


@pytest.mark.parametrize(
    ("when", "state", "next_subject", "next_change"),
    [
        (at(7, 30), STATE_NO_LESSON, "מתמטיקה", at(8, 0)),
        (at(8, 0), "מתמטיקה", "אנגלית", at(8, 45)),
        (at(8, 44), "מתמטיקה", "אנגלית", at(8, 45)),
        (at(8, 45), "אנגלית", "מדעים", at(9, 30)),
        (at(9, 30), STATE_BREAK, "מדעים", at(10, 35)),  # hour 3 is free, hour 4 starts at 10:35
        (at(10, 40), "מדעים", None, at(11, 20)),
        (at(11, 20), STATE_NO_LESSON, None, None),
        (at(23, 0), STATE_NO_LESSON, None, None),
    ],
)
def test_resolve_through_the_day(when, state, next_subject, next_change) -> None:
    now = resolve(LESSONS, BELL, when)
    assert now.state == state
    assert (now.upcoming.subject if now.upcoming else None) == next_subject
    assert now.next_change == next_change
    assert now.lessons_today == 3


def test_resolve_details_and_edge_cases() -> None:
    now = resolve(LESSONS, BELL, at(8, 10))
    assert (now.current.teacher, now.current.room, now.current.hour, now.current.changed) == ("רחל", "12", 1, False)
    assert resolve(LESSONS, BELL, at(10, 40)).current.changed is True
    # A day without lessons.
    assert resolve(LESSONS, BELL, at(9, 0, dt.date(2026, 11, 14))).state == STATE_NO_LESSON
    # Lessons today but no way to place them on the clock: unknown, not a guess.
    unknown = resolve(LESSONS, {}, at(9, 0))
    assert unknown.state is None and unknown.lessons_today == 3
    # A lesson's own times beat the bell schedule.
    own = [Lesson(date="2026-11-10", hour=1, subject="ספורט", start="07:15", end="07:55")]
    assert resolve(own, BELL, at(7, 20)).state == own[0].subject


def test_parallel_groups_share_a_slot() -> None:
    groups = [
        Lesson(date="2026-11-10", hour=1, subject="אנגלית 5 יח", teacher="א"),
        Lesson(date="2026-11-10", hour=1, subject="אנגלית 4 יח", teacher="ב"),
        Lesson(date="2026-11-10", hour=1, subject="אנגלית 5 יח", teacher="א"),
    ]
    now = resolve(groups, BELL, at(8, 5))
    assert now.state == "אנגלית 5 יח / אנגלית 4 יח"
    assert now.current.teacher == "א / ב"


def test_tests_board() -> None:
    items = parse_tests_board(TESTS_BOARD["data"])
    assert [(e.kind, e.subject or e.title, e.start_date.isoformat()) for e in items] == [
        ("exam", "מתמטיקה", "2026-11-05"),
        ("exam", "אנגלית", "2026-11-12"),
        ("exam", "היסטוריה", "2026-11-18"),
        ("event", "יום ספורט", "2026-11-20"),
        ("exam", "מדעים", "2026-11-25"),
        ("vacation", "חופשת חנוכה", "2026-12-15"),
    ]
    history = items[2]
    assert history.times(BELL) == (dt.time(8, 45), dt.time(10, 35))  # hours 2-3; to_hour is exclusive
    assert history.as_dict(BELL)["start"] == "08:45" and history.note == "פרקים 4-6"
    assert items[-1].end_date == dt.date(2026, 12, 22) and items[-1].times(BELL) is None
    third_grader = Student(student_id="x", key="1", class_code=3, class_number=2)
    sixth_grader = Student(student_id="y", key="2", class_code=6, class_number=1)
    assert [e.subject for e in items if e.kind == "exam" and e.applies_to(third_grader)] == [
        "מתמטיקה", "אנגלית", "היסטוריה",
    ]  # fmt: skip
    assert [e.subject for e in items if e.kind == "exam" and e.applies_to(sixth_grader)] == ["מדעים"]
    assert all(e.applies_to(third_grader) for e in items if e.kind != "exam")  # whole-school items
    assert parse_tests_board(None) == [] and parse_tests_board({"exams": [{"title": "no date"}]}) == []


def test_grades_newest_first() -> None:
    grades = parse_grades(GRADES["data"])
    assert [(g.subject, g.grade, g.date[:10]) for g in grades] == [
        ("מדעים", "92", "2026-11-03"),
        ("אנגלית", "78", "2026-10-20"),
    ]
    assert grades[0].teacher == "שפירא נועה" and grades[0].remark == "כל הכבוד" and grades[0].weight == "10"
    assert grades[0].identity() != grades[1].identity()
    assert parse_grades({"dataTable": [{"subject": "x"}, None]}) == [] and parse_grades(None) == []


def test_changes_ordered_by_hour() -> None:
    changes = parse_changes(CHANGES["data"])
    assert [(c.kind, c.hour, c.text) for c in changes] == [("change", 2, "מילוי מקום"), ("event", 5, "אסיפת שכבה")]
    assert changes[0].teacher == "כהן רחל" and changes[0].room == "12"
    assert parse_changes(None) == [] and parse_changes({"changes": "x"}) == []
