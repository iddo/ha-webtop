"""Calendar platform: two calendars per student.

The school calendar holds the exams, school events and vacations the portal's
tests board lists for the student. Items with lesson numbers are placed on the
clock using the school's bell schedule; everything else is an all-day entry.

The timetable calendar holds the lessons themselves.
"""

from __future__ import annotations

import datetime as dt

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import WebtopConfigEntry, WebtopCoordinator
from .entity import WebtopStudentEntity
from .models import BellSchedule, Lesson, SchoolEvent, Student, is_no_homework, normalize_name
from .timetable import placed_lessons


def to_calendar_event(event: SchoolEvent, bell: BellSchedule) -> CalendarEvent:
    """Convert a tests-board item into a Home Assistant calendar event."""
    times = event.times(bell) if event.start_date == event.end_date else None
    if times:
        tz = dt_util.get_default_time_zone()
        start: dt.date | dt.datetime = dt.datetime.combine(event.start_date, times[0], tz)
        end: dt.date | dt.datetime = dt.datetime.combine(event.end_date, times[1], tz)
    else:
        start = event.start_date
        end = event.end_date + dt.timedelta(days=1)  # all-day end dates are exclusive
    return CalendarEvent(
        start=start,
        end=end,
        summary=event.summary,
        description=event.note or None,
        location=event.room or None,
        uid=event.identity(),
    )


class WebtopSchoolCalendar(WebtopStudentEntity, CalendarEntity):
    """Exams, events and vacations for one student."""

    _attr_translation_key = "school"
    _attr_icon = "mdi:calendar-school"

    def __init__(self, coordinator: WebtopCoordinator, student: Student) -> None:
        super().__init__(coordinator, student)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{student.key}_school_calendar"

    def _all_events(self) -> list[CalendarEvent]:
        data = self.coordinator.data
        bell = data.bell.get(self.student_key, {})
        events = [to_calendar_event(e, bell) for e in data.calendar.get(self.student_key, [])]
        return sorted(events, key=lambda e: e.start_datetime_local)

    @property
    def event(self) -> CalendarEvent | None:
        """The current or next upcoming item."""
        now = dt_util.now()
        return next((e for e in self._all_events() if e.end_datetime_local > now), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: dt.datetime, end_date: dt.datetime
    ) -> list[CalendarEvent]:
        return [
            e for e in self._all_events() if e.end_datetime_local > start_date and e.start_datetime_local < end_date
        ]


TIMETABLE_LOOKAHEAD_DAYS = 8


class WebtopTimetableCalendar(WebtopStudentEntity, CalendarEntity):
    """The student's lessons as calendar events.

    Last week and this week show what was taught, with the topic and homework in the
    description. Other weeks are drawn from the standing timetable. A lesson appears
    only when its time is known, from the portal or from typed lesson times.
    """

    _attr_translation_key = "timetable"
    _attr_icon = "mdi:timetable"

    def __init__(self, coordinator: WebtopCoordinator, student: Student) -> None:
        super().__init__(coordinator, student)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{student.key}_timetable_calendar"

    def _events(self, start: dt.date, end: dt.date) -> list[CalendarEvent]:
        data = self.coordinator.data
        key = self.student_key
        vacations = [e for e in data.calendar.get(key, []) if e.kind == "vacation"]
        placed = placed_lessons(
            [*data.previous_schedule.get(key, []), *data.schedule.get(key, [])],
            data.standing.get(key, []),
            data.bell.get(key, {}),
            start,
            end,
            dt_util.get_default_time_zone(),
            is_day_off=lambda day: any(v.start_date <= day <= v.end_date for v in vacations),
        )
        return [lesson_to_calendar_event(lesson, begins, ends) for lesson, begins, ends in placed]

    @property
    def event(self) -> CalendarEvent | None:
        """The lesson on now, or the next one within the coming week."""
        now = dt_util.now()
        upcoming = self._events(now.date(), now.date() + dt.timedelta(days=TIMETABLE_LOOKAHEAD_DAYS))
        return next((e for e in upcoming if e.end_datetime_local > now), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: dt.datetime, end_date: dt.datetime
    ) -> list[CalendarEvent]:
        local_start, local_end = dt_util.as_local(start_date), dt_util.as_local(end_date)
        events = self._events(local_start.date(), local_end.date())
        return [e for e in events if e.end_datetime_local > start_date and e.start_datetime_local < end_date]


def lesson_to_calendar_event(lesson: Lesson, begins: dt.datetime, ends: dt.datetime) -> CalendarEvent:
    """One lesson as a calendar event: subject, with teacher, topic and homework underneath."""
    lines = [lesson.teacher, lesson.topic]
    if lesson.homework and not is_no_homework(lesson.homework):
        lines.append(f"\U0001f4dd {lesson.homework}")
    description = "\n".join(line for line in lines if line)
    return CalendarEvent(
        start=begins,
        end=ends,
        summary=lesson.subject,
        description=description or None,
        location=lesson.room or None,
        uid=f"{begins.date().isoformat()}-{lesson.hour}-{normalize_name(lesson.subject)}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WebtopConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a school calendar and a timetable calendar per student, including students that appear later."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_students() -> None:
        new = [s for s in coordinator.data.students if s.key not in known]
        if new:
            known.update(s.key for s in new)
            entities: list[CalendarEntity] = [WebtopSchoolCalendar(coordinator, student) for student in new]
            entities += [WebtopTimetableCalendar(coordinator, student) for student in new]
            async_add_entities(entities)

    _add_new_students()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_students))
