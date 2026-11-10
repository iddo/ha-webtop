"""Diagnostics for the Webtop (SmartSchool) integration.

Names, credentials, tokens and encrypted student ids are redacted. The raw
payload samples are included (redacted) because several Webtop response shapes
are only partially known; attach the download to a bug report when a sensor
shows unknown or the wrong value.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_DATA, CONF_PASSWORD, CONF_USERNAME
from .coordinator import WebtopConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_DATA,
    "token",
    "id",
    "Id",
    "studentId",
    "StudentId",
    "userId",
    "studentLogin",
    "firstName",
    "lastName",
    "studentName",
    "fullName",
    "student_F_name",
    "student_L_name",
    "name",
    "sender",
    "teacher",
    "teacherName",
    "unique_id",
    "email",
    "phone",
    "idNumber",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WebtopConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "unique_id": "**REDACTED**" if entry.unique_id else None,
        },
        "account": {"school_name": data.account.school_name},
        "students": [
            {
                "key": "**REDACTED**",
                "class_code": s.class_code,
                "class_number": s.class_number,
                "class_name": s.class_name,
                "study_year": s.study_year,
                "school_name": s.school_name,
                "has_class_params": s.has_class_params,
                "homework_ok": data.homework_ok.get(s.key),
                "homework_items": len(data.homework.get(s.key, [])),
                "discipline_ok": data.discipline_ok.get(s.key),
                "discipline_events": len(data.discipline.get(s.key, [])),
                "schedule_ok": data.schedule_ok.get(s.key),
                "lessons": len(data.schedule.get(s.key, [])),
            }
            for s in data.students
        ],
        "messages": len(data.messages),
        "unread_notifications": data.unread_notifications,
        "last_update_success": coordinator.last_update_success,
        "raw_samples": async_redact_data(data.raw_samples, TO_REDACT),
    }
