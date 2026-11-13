"""Work out which lesson is on right now from a timetable and a bell schedule.

Pure functions: no Home Assistant, no I/O, so the logic is easy to test.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
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
    def topic(self) -> str:
        return " / ".join(dict.fromkeys(lesson.topic for lesson in self.lessons if lesson.topic))

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


def school_day(
    lessons: list[Lesson], bell: BellSchedule, day: dt.date, tzinfo: dt.tzinfo | None
) -> tuple[dt.datetime, dt.datetime] | None:
    """When the first lesson starts and the last one ends on ``day``; None if that is unknown."""
    slots = day_slots(lessons, bell, day, tzinfo)
    if not slots:
        return None
    return slots[0].start, max(slot.end for slot in slots)


def _week_start(day: dt.date) -> dt.date:
    """The Sunday that starts the school week containing ``day``."""
    return day - dt.timedelta(days=day.isoweekday() % 7)


MAX_RANGE_DAYS = 120


def placed_lessons(
    dated: list[Lesson],
    standing: list[Lesson],
    bell: BellSchedule,
    start: dt.date,
    end: dt.date,
    tzinfo: dt.tzinfo | None,
    is_day_off: Callable[[dt.date], bool] = lambda _day: False,
) -> list[tuple[Lesson, dt.datetime, dt.datetime]]:
    """Every lesson between two dates (inclusive) that can be placed on the clock.

    Weeks the portal gave dated lessons for are shown as taught, with their topics.
    Other weeks come from the standing weekday timetable, minus known days off.
    Lessons whose times are unknown are left out.
    """
    end = min(end, start + dt.timedelta(days=MAX_RANGE_DAYS))
    known_weeks = {_week_start(parsed) for lesson in dated if (parsed := parse_date(lesson.date))}
    undated = [lesson for lesson in standing if not parse_date(lesson.date)]
    out: list[tuple[Lesson, dt.datetime, dt.datetime]] = []
    day = start
    while day <= end:
        if _week_start(day) in known_weeks:
            todays = [lesson for lesson in dated if parse_date(lesson.date) == day]
        elif is_day_off(day):
            todays = []
        else:
            weekday = webtop_weekday(day)
            todays = [lesson for lesson in undated if lesson.weekday == weekday]
        for lesson in todays:
            times = lesson_times(lesson, bell)
            if times is not None:
                out.append(
                    (lesson, dt.datetime.combine(day, times[0], tzinfo), dt.datetime.combine(day, times[1], tzinfo))
                )
        day += dt.timedelta(days=1)
    out.sort(key=lambda item: (item[1], item[2], item[0].subject))
    return out


# Subject keyword stems (Hebrew and English) -> Material Design icon. First match wins.
_SUBJECT_ICONS: tuple[tuple[str, str], ...] = (
    ("מתמט|חשבון|הנדסה|גאומטר|גיאומטר|אלגבר|math", "mdi:calculator-variant"),
    ("אנגלית|english|ערבית|צרפתית|ספרדית|רוסית", "mdi:translate"),
    ("פיזיק|physics", "mdi:atom"),
    ("כימיה|chemistry", "mdi:flask"),
    ("ביולוג|biology", "mdi:dna"),
    ("מדע|טבע|science", "mdi:microscope"),
    ("מחשב|תכנות|סייבר|רובוט|טכנולוג|computer", "mdi:laptop"),
    ("תנך|תנ\"ך|תורה|משנה|גמרא|תלמוד|תושב|יהדות|מורשת|מחשבת", "mdi:script-text"),
    ("היסטור|אזרחות|history|civics", "mdi:bank"),
    ("גאוגרפ|גיאוגרפ|מולדת|geograph", "mdi:earth"),
    ("ספרות|לשון|עברית|שפה|הבעה|קריאה|כתיבה|literature", "mdi:book-open-variant"),
    ("חינוך גופני|חנג|חנ\"ג|ספורט|התעמלות|מחול|sport", "mdi:run"),
    ("אמנות|אומנות|ציור|פיסול|יצירה|עיצוב|art", "mdi:palette"),
    ("מוזיק|music|נגינה|מקהלה", "mdi:music"),
    ("תיאטרון|תאטרון|דרמה|קולנוע|theat", "mdi:drama-masks"),
    ("חינוך|כישורי חיים|מחנך|שעת חינוך", "mdi:account-group"),
)
_COMPILED_SUBJECT_ICONS = tuple((re.compile(pattern), icon) for pattern, icon in _SUBJECT_ICONS)
ICON_LESSON = "mdi:human-male-board"
ICON_BREAK = "mdi:coffee"
ICON_NO_LESSON = "mdi:home-clock"


def subject_icon(subject: str | None) -> str:
    """An icon for a lesson subject, or the generic lesson icon."""
    text = (subject or "").casefold()
    for pattern, icon in _COMPILED_SUBJECT_ICONS:
        if pattern.search(text):
            return icon
    return ICON_LESSON
