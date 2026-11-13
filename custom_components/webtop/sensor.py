"""Sensor platform for the Webtop (SmartSchool) integration.

Per student: the current lesson, next test, upcoming tests, latest grade,
today's schedule changes, homework due today, behaviour events, lessons today
and class.
Per account: unread messages, last message and unread notifications. Lists
live in attributes, since a sensor state is capped at 255 characters.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import MAX_LIST_ATTR_ITEMS, MESSAGE_BODY_ATTR_CHARS
from .coordinator import WebtopConfigEntry, WebtopCoordinator, WebtopData
from .entity import WebtopAccountEntity, WebtopStudentEntity
from .models import HomeworkItem, Lesson, SchoolEvent, Student
from .timetable import (
    ICON_BREAK,
    ICON_LESSON,
    ICON_NO_LESSON,
    STATE_BREAK,
    Now,
    lessons_on,
    resolve,
    school_day,
    subject_icon,
)


def _today() -> str:
    # Home Assistant's configured timezone, not the host clock.
    return dt_util.now().strftime("%Y-%m-%d")


def _is_today(date: str) -> bool:
    return date[:10] == _today()


# ---------------------------------------------------------------------------
# per-student sensors
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class StudentSensorDescription(SensorEntityDescription):
    """A sensor computed from one student's slice of the snapshot."""

    value_fn: Callable[[WebtopData, Student], Any]
    attrs_fn: Callable[[WebtopData, Student], dict[str, Any]] | None = None


def _homework_today(data: WebtopData, student: Student) -> list[HomeworkItem]:
    return [h for h in data.homework.get(student.key, []) if _is_today(h.date)]


def _homework_value(data: WebtopData, student: Student) -> int | None:
    if not data.homework_ok.get(student.key):
        return None
    return len(_homework_today(data, student))


def _homework_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    today = _homework_today(data, student)
    everything = data.homework.get(student.key, [])
    now = dt_util.now().date()
    sunday = (now - dt.timedelta(days=now.isoweekday() % 7)).isoformat()  # the school week starts on Sunday
    week = [h for h in everything if h.date[:10] >= sunday]
    last_week = [h for h in everything if h.date[:10] < sunday]
    return {
        "items": [h.as_dict() for h in today[:MAX_LIST_ATTR_ITEMS]],
        "text": "\n".join(f"{h.subject}: {h.homework}" for h in today) or "No homework",
        # Everything written on this week's lessons, oldest first.
        "week": [h.as_dict() for h in week[: MAX_LIST_ATTR_ITEMS * 2]],
        "week_count": len(week),
        # Last week's, newest first: what was given on Thursday is usually due on Sunday.
        "last_week": [h.as_dict() for h in reversed(last_week[-MAX_LIST_ATTR_ITEMS:])],
    }


BEHAVIOUR_WINDOW_DAYS = 7


def _discipline_value(data: WebtopData, student: Student) -> int | None:
    """Behaviour records from the last week (the portal keeps the whole year)."""
    if not data.discipline_ok.get(student.key):
        return None
    since = (dt_util.now().date() - dt.timedelta(days=BEHAVIOUR_WINDOW_DAYS - 1)).isoformat()
    return sum(1 for e in data.discipline[student.key] if e.date[:10] >= since)


def _discipline_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    events = data.discipline.get(student.key, [])
    by_type: dict[str, int] = {}
    for event in events:
        by_type[event.event] = by_type.get(event.event, 0) + 1
    return {
        "today": sum(1 for e in events if _is_today(e.date)),
        "total": len(events),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "events": [e.as_dict() for e in events[:MAX_LIST_ATTR_ITEMS]],
    }


def _lessons_today(data: WebtopData, student: Student) -> list[Lesson]:
    lessons = lessons_on(data.schedule.get(student.key, []), dt_util.now().date())
    return sorted(lessons, key=lambda lesson: lesson.hour if lesson.hour is not None else 99)


def _lessons_value(data: WebtopData, student: Student) -> int | None:
    if not data.schedule_ok.get(student.key):
        return None
    return len(_lessons_today(data, student))


def _lessons_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    lessons = sorted(
        data.schedule.get(student.key, []),
        key=lambda lesson: (lesson.date, lesson.weekday or 0, lesson.hour if lesson.hour is not None else 99),
    )
    return {
        "today": [lesson.as_dict() for lesson in _lessons_today(data, student)],
        "week": [lesson.as_dict() for lesson in lessons[: MAX_LIST_ATTR_ITEMS * 2]],
    }


def _upcoming_exams(data: WebtopData, student: Student) -> list[SchoolEvent]:
    today = dt_util.now().date()
    return [e for e in data.calendar.get(student.key, []) if e.kind == "exam" and e.end_date >= today]


def _next_test_value(data: WebtopData, student: Student) -> dt.date | None:
    if not data.calendar_ok.get(student.key):
        return None
    exams = _upcoming_exams(data, student)
    return exams[0].start_date if exams else None


def _next_test_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    exams = _upcoming_exams(data, student)
    if not exams:
        return {}
    bell = data.bell.get(student.key, {})
    return {**exams[0].as_dict(bell), "days_until": (exams[0].start_date - dt_util.now().date()).days}


def _upcoming_tests_value(data: WebtopData, student: Student) -> int | None:
    if not data.calendar_ok.get(student.key):
        return None
    return len(_upcoming_exams(data, student))


def _upcoming_tests_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    bell = data.bell.get(student.key, {})
    return {"tests": [e.as_dict(bell) for e in _upcoming_exams(data, student)[:MAX_LIST_ATTR_ITEMS]]}


def _latest_grade_value(data: WebtopData, student: Student) -> str | None:
    grades = data.grades.get(student.key, [])
    if not data.grades_ok.get(student.key) or not grades:
        return None
    return grades[0].grade[:255]


def _latest_grade_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    grades = data.grades.get(student.key, [])
    if not grades:
        return {}
    return {**grades[0].as_dict(), "recent": [g.as_dict() for g in grades[:MAX_LIST_ATTR_ITEMS]]}


def _changes_value(data: WebtopData, student: Student) -> int | None:
    if not data.changes_ok.get(student.key):
        return None
    return len(data.changes[student.key])


def _changes_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    return {"changes": [c.as_dict() for c in data.changes.get(student.key, [])[:MAX_LIST_ATTR_ITEMS]]}


def _school_day(data: WebtopData, student: Student) -> tuple[dt.datetime, dt.datetime] | None:
    if not data.schedule_ok.get(student.key):
        return None
    now = dt_util.now()
    return school_day(data.schedule[student.key], data.bell.get(student.key, {}), now.date(), now.tzinfo)


def _school_day_end_value(data: WebtopData, student: Student) -> dt.datetime | None:
    bounds = _school_day(data, student)
    return bounds[1] if bounds else None


def _school_day_end_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    bounds = _school_day(data, student)
    return {
        "starts": bounds[0].isoformat() if bounds else None,
        "lessons": len(_lessons_today(data, student)) if data.schedule_ok.get(student.key) else None,
    }


STUDENT_SENSORS: tuple[StudentSensorDescription, ...] = (
    StudentSensorDescription(
        key="school_day_end",
        translation_key="school_day_end",
        icon="mdi:bus-school",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_school_day_end_value,
        attrs_fn=_school_day_end_attrs,
    ),
    StudentSensorDescription(
        key="next_test",
        translation_key="next_test",
        icon="mdi:clipboard-text-clock",
        device_class=SensorDeviceClass.DATE,
        value_fn=_next_test_value,
        attrs_fn=_next_test_attrs,
    ),
    StudentSensorDescription(
        key="upcoming_tests",
        translation_key="upcoming_tests",
        icon="mdi:clipboard-list",
        native_unit_of_measurement="tests",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_upcoming_tests_value,
        attrs_fn=_upcoming_tests_attrs,
    ),
    StudentSensorDescription(
        key="latest_grade",
        translation_key="latest_grade",
        icon="mdi:school",
        value_fn=_latest_grade_value,
        attrs_fn=_latest_grade_attrs,
    ),
    StudentSensorDescription(
        key="schedule_changes_today",
        translation_key="schedule_changes_today",
        icon="mdi:calendar-alert",
        native_unit_of_measurement="changes",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_changes_value,
        attrs_fn=_changes_attrs,
    ),
    StudentSensorDescription(
        key="homework_today",
        translation_key="homework_today",
        icon="mdi:book-open-variant",
        native_unit_of_measurement="items",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_homework_value,
        attrs_fn=_homework_attrs,
    ),
    StudentSensorDescription(
        key="behaviour_events",
        translation_key="behaviour_events",
        icon="mdi:account-alert",
        native_unit_of_measurement="events",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_discipline_value,
        attrs_fn=_discipline_attrs,
    ),
    StudentSensorDescription(
        key="lessons_today",
        translation_key="lessons_today",
        icon="mdi:timetable",
        native_unit_of_measurement="lessons",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_lessons_value,
        attrs_fn=_lessons_attrs,
    ),
    StudentSensorDescription(
        key="class",
        translation_key="class",
        icon="mdi:school",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, student: student.class_label or None,
        attrs_fn=lambda data, student: {
            "school": student.school_name,
            "class_code": student.class_code,
            "class_number": student.class_number,
            "study_year": student.study_year,
        },
    ),
)


class WebtopStudentSensor(WebtopStudentEntity, SensorEntity):
    """A per-student sensor."""

    entity_description: StudentSensorDescription

    def __init__(
        self, coordinator: WebtopCoordinator, student: Student, description: StudentSensorDescription
    ) -> None:
        super().__init__(coordinator, student)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{student.key}_{description.key}"

    @property
    def native_value(self) -> Any:
        student = self.student
        if student is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, student)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        student = self.student
        if student is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, student)


class WebtopCurrentLessonSensor(WebtopStudentEntity, SensorEntity):
    """The lesson the student is in right now.

    The state is the subject during a lesson, "Break" between lessons and
    "No lesson" outside school hours, so the entity's history reads as the
    day's timetable. It re-evaluates exactly when a lesson starts or ends,
    not only when the portal is polled.
    """

    _attr_translation_key = "current_lesson"

    def __init__(self, coordinator: WebtopCoordinator, student: Student) -> None:
        super().__init__(coordinator, student)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{student.key}_current_lesson"
        self._unsub_timer: CALLBACK_TYPE | None = None

    def _resolve(self) -> Now | None:
        data = self.coordinator.data
        if not data.schedule_ok.get(self.student_key):
            return None
        return resolve(data.schedule[self.student_key], data.bell.get(self.student_key, {}), dt_util.now())

    @property
    def native_value(self) -> str | None:
        now = self._resolve()
        return now.state[:255] if now and now.state else None

    @property
    def icon(self) -> str:
        """An icon for the subject being taught, a cup for a break, a house outside school hours."""
        now = self._resolve()
        if now is None or now.state is None:
            return ICON_LESSON
        if now.current is not None:
            return subject_icon(now.current.lessons[0].subject)
        return ICON_BREAK if now.state == STATE_BREAK else ICON_NO_LESSON

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        now = self._resolve()
        if now is None:
            return None
        current, upcoming = now.current, now.upcoming
        return {
            "teacher": current.teacher if current else None,
            "topic": current.topic if current else None,
            "room": current.room if current else None,
            "hour": current.hour if current else None,
            "start": current.start.isoformat() if current else None,
            "end": current.end.isoformat() if current else None,
            "changed": current.changed if current else None,
            "next_subject": upcoming.subject if upcoming else None,
            "next_start": upcoming.start.isoformat() if upcoming else None,
            "lessons_today": now.lessons_today,
            # False when the portal gave lessons but no lesson times to place them on the clock.
            "times_known": now.state is not None,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_next_change()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_timer()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_next_change()
        super()._handle_coordinator_update()

    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _schedule_next_change(self) -> None:
        """Wake up at the next lesson boundary, or just after midnight."""
        self._cancel_timer()
        now = self._resolve()
        when = now.next_change if now else None
        if when is None:
            tomorrow = dt_util.now().date() + dt.timedelta(days=1)
            when = dt.datetime.combine(tomorrow, dt.time(0, 0, 5), dt_util.now().tzinfo)
        self._unsub_timer = async_track_point_in_time(self.hass, self._on_boundary, when)

    @callback
    def _on_boundary(self, _now: dt.datetime) -> None:
        self._unsub_timer = None
        self.async_write_ha_state()
        self._schedule_next_change()


# ---------------------------------------------------------------------------
# account-level sensors
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    """A sensor computed from the account-wide part of the snapshot."""

    value_fn: Callable[[WebtopData], Any]
    attrs_fn: Callable[[WebtopData], dict[str, Any]] | None = None


def _last_message_attrs(data: WebtopData) -> dict[str, Any]:
    if not data.messages:
        return {}
    newest = data.messages[0]
    # Only this one message carries its text: a list of full texts would outgrow what the recorder keeps.
    return {**newest.as_dict(), "body": newest.body[:MESSAGE_BODY_ATTR_CHARS]}


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="unread_messages",
        translation_key="unread_messages",
        icon="mdi:email-alert",
        native_unit_of_measurement="messages",
        state_class=SensorStateClass.MEASUREMENT,
        # The portal's own badge when available; otherwise count the first inbox page.
        value_fn=lambda data: (
            data.unread_messages
            if data.unread_messages is not None
            else sum(1 for m in data.messages if not m.has_read)
        ),
        attrs_fn=lambda data: {
            "total": len(data.messages),
            "messages": [m.as_dict() for m in data.messages[:MAX_LIST_ATTR_ITEMS]],
        },
    ),
    AccountSensorDescription(
        key="last_message",
        translation_key="last_message",
        icon="mdi:email-newsletter",
        value_fn=lambda data: data.messages[0].subject[:255] if data.messages else None,
        attrs_fn=_last_message_attrs,
    ),
    AccountSensorDescription(
        key="unread_notifications",
        translation_key="unread_notifications",
        icon="mdi:bell-badge",
        native_unit_of_measurement="notifications",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.unread_notifications,
        attrs_fn=lambda data: {"latest": [n.as_dict() for n in data.notifications[:MAX_LIST_ATTR_ITEMS]]},
    ),
)


class WebtopAccountSensor(WebtopAccountEntity, SensorEntity):
    """An account-level sensor."""

    entity_description: AccountSensorDescription

    def __init__(self, coordinator: WebtopCoordinator, description: AccountSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    entry: WebtopConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create account sensors, then one set of sensors per student."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    async_add_entities(WebtopAccountSensor(coordinator, d) for d in ACCOUNT_SENSORS)

    @callback
    def _add_new_students() -> None:
        new = [s for s in coordinator.data.students if s.key not in known]
        if not new:
            return
        known.update(s.key for s in new)
        entities: list[SensorEntity] = [WebtopCurrentLessonSensor(coordinator, student) for student in new]
        entities += [
            WebtopStudentSensor(coordinator, student, description) for student in new for description in STUDENT_SENSORS
        ]
        async_add_entities(entities)

    _add_new_students()
    # A child added to the account later gets entities without a reload.
    entry.async_on_unload(coordinator.async_add_listener(_add_new_students))
