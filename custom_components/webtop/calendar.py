"""Calendar platform: one school calendar per student.

Holds the exams, school events and vacations the portal's tests board lists
for the student's class. Items with lesson numbers are placed on the clock
using the school's bell schedule; everything else is an all-day entry.
"""

from __future__ import annotations

import datetime as dt

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import WebtopConfigEntry, WebtopCoordinator
from .entity import WebtopStudentEntity
from .models import BellSchedule, SchoolEvent, Student


def to_calendar_event(event: SchoolEvent, bell: BellSchedule) -> CalendarEvent:
    """Convert a tests-board item into a Home Assistant calendar event."""
    parts = [event.title]
    if event.subject and event.subject not in event.title:
        parts.append(event.subject)
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
        summary=" - ".join(parts),
        description=event.note or None,
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WebtopConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one calendar per student, including students that appear later."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_students() -> None:
        new = [s for s in coordinator.data.students if s.key not in known]
        if new:
            known.update(s.key for s in new)
            async_add_entities(WebtopSchoolCalendar(coordinator, student) for student in new)

    _add_new_students()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_students))
