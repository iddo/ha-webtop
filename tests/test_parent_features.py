"""Current lesson, tests, grades, schedule changes and the school calendar inside Home Assistant."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed
from webtop import WebtopRequestError

from custom_components.webtop.const import CONF_SCAN_INTERVAL, EVENT_NEW_GRADE, EVENT_NEW_TEST
from custom_components.webtop.diagnostics import async_get_config_entry_diagnostics

from .conftest import GRADES, TESTS_BOARD

LESSON = "sensor.nv_m_lvy_current_lesson"


@pytest.fixture
async def in_israel(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Asia/Jerusalem")


async def _setup(hass: HomeAssistant, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def _go_to(hass: HomeAssistant, freezer: FrozenDateTimeFactory, local: str) -> None:
    freezer.move_to(f"2026-11-10 {local}+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_current_lesson_follows_the_clock_between_polls(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    freezer.move_to("2026-11-10 07:30:00+02:00")
    # Poll only every 12 hours, so any change below can only come from the entity's own timer.
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_SCAN_INTERVAL: 720})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    polls = len(fake_client.created[0].calls)

    state = hass.states.get(LESSON)
    assert state.state == "No lesson"
    assert state.attributes["next_subject"] == "מתמטיקה"
    assert state.attributes["next_start"] == "2026-11-10T08:00:00+02:00"
    assert state.attributes["lessons_today"] == 2 and state.attributes["times_known"] is True

    await _go_to(hass, freezer, "08:00:01")
    state = hass.states.get(LESSON)
    assert state.state == "מתמטיקה"
    assert (state.attributes["teacher"], state.attributes["hour"]) == ("רחל", 1)
    assert state.attributes["end"] == "2026-11-10T08:45:00+02:00"
    assert state.attributes["next_subject"] == "אנגלית"

    await _go_to(hass, freezer, "08:45:01")
    state = hass.states.get(LESSON)
    assert state.state == "אנגלית" and state.attributes["changed"] is True

    await _go_to(hass, freezer, "09:30:01")
    assert hass.states.get(LESSON).state == "No lesson"
    # All of that happened on the entity's own timer: the portal was not polled again.
    assert len(fake_client.created[0].calls) == polls


async def test_current_lesson_rolls_over_at_midnight(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    freezer.move_to("2026-11-10 22:00:00+02:00")
    await _setup(hass, config_entry)
    assert hass.states.get(LESSON).state == "No lesson"
    assert hass.states.get(LESSON).attributes["lessons_today"] == 2

    freezer.move_to("2026-11-11 00:00:06+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(LESSON)
    assert state.attributes["lessons_today"] == 1
    assert state.attributes["next_subject"] == "מדעים"
    assert state.attributes["next_start"] == "2026-11-11T08:00:00+02:00"


async def test_current_lesson_unknown_without_lesson_times(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    fake_client.responses["EventsTestsBoard"] = WebtopRequestError("POST /x failed (500): oops")
    freezer.move_to("2026-11-10 08:10:00+02:00")
    await _setup(hass, config_entry)
    state = hass.states.get(LESSON)
    assert state.state == "unknown"
    assert state.attributes["times_known"] is False and state.attributes["lessons_today"] == 2
    # Tests are unknown too, but the rest of the integration is unaffected.
    assert hass.states.get("sensor.nv_m_lvy_next_test").state == "unknown"
    assert hass.states.get("sensor.nv_m_lvy_homework_today").state == "1"


async def test_test_grade_and_change_sensors(hass: HomeAssistant, fake_client, config_entry, in_israel) -> None:
    await _setup(hass, config_entry)

    next_test = hass.states.get("sensor.nv_m_lvy_next_test")
    assert next_test.state == "2026-11-12"  # the 5 November exam is in the past
    assert next_test.attributes["subject"] == "אנגלית"
    assert (next_test.attributes["start"], next_test.attributes["end"]) == ("08:00", "08:45")
    assert next_test.attributes["days_until"] == 2

    upcoming = hass.states.get("sensor.nv_m_lvy_upcoming_tests")
    assert upcoming.state == "2"
    assert [t["subject"] for t in upcoming.attributes["tests"]] == ["אנגלית", "היסטוריה"]
    # The sibling in another class sees only her own exam.
    assert hass.states.get("sensor.myh_lvy_upcoming_tests").state == "1"
    assert hass.states.get("sensor.myh_lvy_next_test").state == "2026-11-25"

    grade = hass.states.get("sensor.nv_m_lvy_latest_grade")
    assert grade.state == "92"
    assert (grade.attributes["subject"], grade.attributes["teacher"]) == ("מדעים", "שפירא נועה")
    assert [g["grade"] for g in grade.attributes["recent"]] == ["92", "78"]

    changes = hass.states.get("sensor.nv_m_lvy_schedule_changes_today")
    assert changes.state == "2"
    assert changes.attributes["changes"][0] == {
        "kind": "change", "text": "מילוי מקום", "hour": 2, "teacher": "כהן רחל", "room": "12", "note": "",
    }  # fmt: skip

    # The tests board is read once per month in the window, per session, not per student.
    client = fake_client.created[0]
    board_calls = [c for c in client.calls if c[0] == "EventsTestsBoard"]
    assert len(board_calls) == 2
    assert board_calls[0][1]["institutionCode"] == 440099 and board_calls[0][1]["selectedView"] == 3
    assert [c[1]["selectedDate"][:7] for c in board_calls] == ["2026-11", "2026-12"]
    assert len([c for c in client.calls if c[0] == "GetGrades"]) == 2


async def test_school_calendar(hass: HomeAssistant, fake_client, config_entry, in_israel) -> None:
    await _setup(hass, config_entry)
    calendar = hass.states.get("calendar.nv_m_lvy_school_calendar")
    assert calendar is not None
    assert calendar.attributes["message"] == "בוחן - אנגלית"
    assert calendar.attributes["start_time"] == "2026-11-12 08:00:00"
    assert calendar.attributes["all_day"] is False

    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.nv_m_lvy_school_calendar",
            "start_date_time": "2026-11-01T00:00:00+02:00",
            "end_date_time": "2026-12-31T00:00:00+02:00",
        },
        blocking=True,
        return_response=True,
    )
    events = response["calendar.nv_m_lvy_school_calendar"]["events"]
    assert [(e["summary"], e["start"], e["end"]) for e in events] == [
        ("מבחן - מתמטיקה", "2026-11-05T08:00:00+02:00", "2026-11-05T09:30:00+02:00"),
        ("בוחן - אנגלית", "2026-11-12T08:00:00+02:00", "2026-11-12T08:45:00+02:00"),
        ("מבחן - היסטוריה", "2026-11-18T08:45:00+02:00", "2026-11-18T10:35:00+02:00"),
        ("יום ספורט", "2026-11-20T08:00:00+02:00", "2026-11-20T11:20:00+02:00"),
        ("חופשת חנוכה", "2026-12-15", "2026-12-23"),  # all-day, exclusive end
    ]
    assert events[2]["description"] == "פרקים 4-6"


async def test_new_grade_and_new_test_events(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    grade_events = async_capture_events(hass, EVENT_NEW_GRADE)
    test_events = async_capture_events(hass, EVENT_NEW_TEST)
    await _setup(hass, config_entry)
    assert grade_events == [] and test_events == []  # the first poll is only a baseline

    client = fake_client.created[0]
    new_grade = {"subject": "היסטוריה", "title": "עבודה", "date": "2026-11-09T00:00:00", "grade": 95}
    client.responses["GetGrades"] = {
        "status": True,
        "data": {"dataTable": [*GRADES["data"]["dataTable"], new_grade]},
    }
    board = {**TESTS_BOARD["data"]}
    board["exams"] = [
        *board["exams"],
        {"title": "מבחן", "subject": "ספרות", "date": "2026-11-26T00:00:00", "fromHour": 1, "toHour": 3,
         "classCode": 3, "classNumber": 2},
    ]  # fmt: skip
    client.responses["EventsTestsBoard"] = {"status": True, "data": board}

    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert sorted((e.data["student"], e.data["subject"], e.data["grade"]) for e in grade_events) == [
        ("מאיה לוי", "היסטוריה", "95"),
        ("נועם לוי", "היסטוריה", "95"),
    ]
    # The new exam is for class 3/2 only, so one event, for the one student in that class.
    assert [(e.data["student"], e.data["subject"], e.data["date"], e.data["start"]) for e in test_events] == [
        ("נועם לוי", "ספרות", "2026-11-26", "08:00")
    ]
    assert hass.states.get("sensor.nv_m_lvy_latest_grade").state == "95"

    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(grade_events) == 2 and len(test_events) == 1


async def test_diagnostics_cover_new_data_without_personal_values(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    first = diag["students"][0]
    assert first["bell_schedule_hours"] == [1, 2, 3, 4]
    assert first["calendar_items"] == {"exam": 3, "event": 1, "vacation": 1}
    assert (first["grades"], first["changes_today"]) == (2, 2)
    dumped = str(diag)
    for personal in ("שפירא", "כל הכבוד", "440099", "טיול שנתי", "עמוד 45", "92"):
        assert personal not in dumped, personal
