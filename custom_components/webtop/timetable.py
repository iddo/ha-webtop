"""Work out which lesson is on right now from a timetable and a bell schedule.

Pure functions: no Home Assistant, no I/O, so the logic is easy to test.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .models import BellSchedule, Lesson, parse_date, parse_time

STATE_BREAK = "Break"
STATE_NO_LESSON = "No lesson"


def webtop_weekday(day: dt.date) -> int:
    """Webtop numbers weekdays 1 = Sunday ... 7 = Saturday."""
    return day.isoweekday() % 7 + 1


def lessons_on(lessons: list[Lesson], day: dt.date) -> list[Lesson]:
    """Lessons for one day, whether the timetable is keyed by date or by weekday."""
    dated = [lesson for lesson in lessons if parse_date(lesson.date) == day]
    if dated:
        return dated
    if any(parse_date(lesson.date) for lesson in lessons):
        return []  # a dated timetable that simply has nothing on this day
    weekday = webtop_weekday(day)
    return [lesson for lesson in lessons if lesson.weekday == weekday]


def lesson_times(lesson: Lesson, bell: BellSchedule) -> tuple[dt.time, dt.time] | None:
    """A lesson's own times win; otherwise its lesson number is looked up in the bell schedule."""
    start, end = parse_time(lesson.start), parse_time(lesson.end)
    if start and end and start < end:
        return start, end
    if lesson.hour is not None and lesson.hour in bell:
        return bell[lesson.hour]
    return None


@dataclass
class Slot:
    """Everything happening in one time span (parallel groups share a slot)."""

    start: dt.datetime
    end: dt.datetime
    lessons: list[Lesson] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return " / ".join(dict.fromkeys(lesson.subject for lesson in self.lessons))

    @property
    def teacher(self) -> str:
        return " / ".join(dict.fromkeys(lesson.teacher for lesson in self.lessons if lesson.teacher))

    @property
    def room(self) -> str:
        return " / ".join(dict.fromkeys(lesson.room for lesson in self.lessons if lesson.room))

    @property
    def hour(self) -> int | None:
        return self.lessons[0].hour if self.lessons else None

    @property
    def changed(self) -> bool:
        return any(lesson.changed for lesson in self.lessons)


def day_slots(lessons: list[Lesson], bell: BellSchedule, day: dt.date, tzinfo: dt.tzinfo | None) -> list[Slot]:
    """Today's lessons as time-ordered slots; lessons without known times are left out."""
    slots: dict[tuple[dt.time, dt.time], Slot] = {}
    for lesson in lessons_on(lessons, day):
        times = lesson_times(lesson, bell)
        if times is None:
            continue
        slot = slots.setdefault(
            times,
            Slot(
                start=dt.datetime.combine(day, times[0], tzinfo),
                end=dt.datetime.combine(day, times[1], tzinfo),
            ),
        )
        slot.lessons.append(lesson)
    return sorted(slots.values(), key=lambda s: (s.start, s.end))


@dataclass
class Now:
    """The answer for one instant."""

    state: str | None  # subject(s), STATE_BREAK, STATE_NO_LESSON, or None when times are unknown
    current: Slot | None = None
    upcoming: Slot | None = None
    # When the state will next change today; None once the school day is over.
    next_change: dt.datetime | None = None
    lessons_today: int = 0


def resolve(lessons: list[Lesson], bell: BellSchedule, now: dt.datetime) -> Now:
    """What is on at ``now`` (an aware datetime in the school's timezone)."""
    today = lessons_on(lessons, now.date())
    slots = day_slots(lessons, bell, now.date(), now.tzinfo)
    if not slots:
        # Lessons exist today but none could be placed on the clock: say so rather than guess.
        return Now(state=None if today else STATE_NO_LESSON, lessons_today=len(today))

    current = next((slot for slot in slots if slot.start <= now < slot.end), None)
    upcoming = next((slot for slot in slots if slot.start > now), None)
    if current is not None:
        change = min(current.end, upcoming.start) if upcoming else current.end
        return Now(current.subject, current, upcoming, change, len(today))
    if upcoming is not None:
        state = STATE_BREAK if now >= slots[0].start else STATE_NO_LESSON
        return Now(state, None, upcoming, upcoming.start, len(today))
    return Now(STATE_NO_LESSON, None, None, None, len(today))
