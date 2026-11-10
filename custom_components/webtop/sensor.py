"""Sensor platform for the Webtop (SmartSchool) integration.

Per student: homework due today, behaviour events, lessons today and class.
Per account: unread messages, last message and unread notifications. Lists
live in attributes, since a sensor state is capped at 255 characters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import MAX_LIST_ATTR_ITEMS
from .coordinator import WebtopConfigEntry, WebtopCoordinator, WebtopData
from .entity import WebtopAccountEntity, WebtopStudentEntity
from .models import Student


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


def _homework_value(data: WebtopData, student: Student) -> int | None:
    if not data.homework_ok.get(student.key):
        return None
    return len(data.homework[student.key])


def _homework_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    items = data.homework.get(student.key, [])
    return {
        "items": [h.as_dict() for h in items[:MAX_LIST_ATTR_ITEMS]],
        "text": "\n".join(f"{h.subject}: {h.homework}" for h in items) or "No homework",
    }


def _discipline_value(data: WebtopData, student: Student) -> int | None:
    if not data.discipline_ok.get(student.key):
        return None
    return len(data.discipline[student.key])


def _discipline_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    events = sorted(data.discipline.get(student.key, []), key=lambda e: e.date, reverse=True)
    return {"events": [e.as_dict() for e in events[:MAX_LIST_ATTR_ITEMS]]}


def _lessons_value(data: WebtopData, student: Student) -> int | None:
    if not data.schedule_ok.get(student.key):
        return None
    return sum(1 for lesson in data.schedule[student.key] if _is_today(lesson.date))


def _lessons_attrs(data: WebtopData, student: Student) -> dict[str, Any]:
    lessons = sorted(
        data.schedule.get(student.key, []),
        key=lambda lesson: (lesson.date, lesson.hour if lesson.hour is not None else 99),
    )
    today = [lesson.as_dict() for lesson in lessons if _is_today(lesson.date)]
    return {
        "today": today,
        "week": [lesson.as_dict() for lesson in lessons[: MAX_LIST_ATTR_ITEMS * 2]],
    }


STUDENT_SENSORS: tuple[StudentSensorDescription, ...] = (
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
    return data.messages[0].as_dict()


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="unread_messages",
        translation_key="unread_messages",
        icon="mdi:email-alert",
        native_unit_of_measurement="messages",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: sum(1 for m in data.messages if not m.has_read),
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
        async_add_entities(
            WebtopStudentSensor(coordinator, student, description)
            for student in new
            for description in STUDENT_SENSORS
        )

    _add_new_students()
    # A child added to the account later gets entities without a reload.
    entry.async_on_unload(coordinator.async_add_listener(_add_new_students))
