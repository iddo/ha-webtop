"""Current lesson, tests, grades, schedule changes and the school calendar inside Home Assistant."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from custom_components.webtop.const import CONF_LESSON_TIMES, CONF_SCAN_INTERVAL, EVENT_NEW_GRADE, EVENT_NEW_TEST
from custom_components.webtop.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    GRADES_CARD,
    TESTS_BOARD,
    TESTS_BOARD_UNUSED,
    TIMETABLE_WITH_LABELS,
    WEEKLY_WITHOUT_TIMES,
    _exam,
)

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
    await hass.async_block_till_done(wait_background_tasks=True)


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
    assert state.state == "אנגלית" and state.attributes["topic"] == "Unit 3"

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
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(LESSON)
    assert state.attributes["lessons_today"] == 1
    assert state.attributes["next_subject"] == "מדעים"
    assert state.attributes["next_start"] == "2026-11-11T08:00:00+02:00"


async def test_lesson_times_fall_back_to_the_weekly_schedule(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    """A school without a tests board can still publish lesson times on its weekly schedule."""
    fake_client.responses["EventsTestsBoard"] = TESTS_BOARD_UNUSED
    freezer.move_to("2026-11-10 08:10:00+02:00")
    await _setup(hass, config_entry)
    assert hass.states.get(LESSON).state == "מתמטיקה"
    # No board: without the pupil card either, tests are unknown rather than a misleading zero.
    assert hass.states.get("sensor.myh_lvy_upcoming_tests").state == "0"  # card readable, nothing pending

    client = fake_client.created[0]
    assert len([c for c in client.calls if c[0] == "EventsTestsBoard"]) == 1  # not once per month
    assert len([c for c in client.calls if c[0] == "WeeklySchedualeForToday"]) == 2  # once per child

    # Neither lookup is repeated on later polls the same day.
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len([c for c in client.calls if c[0] == "EventsTestsBoard"]) == 1
    assert len([c for c in client.calls if c[0] == "WeeklySchedualeForToday"]) == 2
    assert hass.states.get(LESSON).state == "מתמטיקה"


async def test_current_lesson_unknown_when_the_school_publishes_no_times(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    """The shape of the first real capture: no tests board, and ":" for every lesson time."""
    fake_client.responses["EventsTestsBoard"] = TESTS_BOARD_UNUSED
    fake_client.responses["WeeklySchedualeForToday"] = WEEKLY_WITHOUT_TIMES
    freezer.move_to("2026-11-10 08:10:00+02:00")
    await _setup(hass, config_entry)
    state = hass.states.get(LESSON)
    assert state.state == "unknown"
    assert state.attributes["times_known"] is False and state.attributes["lessons_today"] == 2
    # The rest of the integration is unaffected.
    assert hass.states.get("sensor.nv_m_lvy_homework_today").state == "1"
    assert hass.states.get("sensor.nv_m_lvy_lessons_today").state == "2"


async def test_typed_lesson_times_fill_the_gap_and_win_over_the_portal(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    fake_client.responses["EventsTestsBoard"] = TESTS_BOARD_UNUSED
    fake_client.responses["WeeklySchedualeForToday"] = WEEKLY_WITHOUT_TIMES
    freezer.move_to("2026-11-10 08:10:00+02:00")
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_SCAN_INTERVAL: 30, CONF_LESSON_TIMES: "07:30-08:20, 08:20-09:10"}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(LESSON)
    assert state.state == "מתמטיקה"
    assert state.attributes["end"] == "2026-11-10T08:20:00+02:00"
    assert state.attributes["next_start"] == "2026-11-10T08:20:00+02:00"
    # With times typed in, the portal is not asked for them.
    assert not [c for c in fake_client.created[0].calls if c[0] == "WeeklySchedualeForToday"]


async def test_bad_lesson_times_option_is_ignored_not_fatal(
    hass: HomeAssistant, fake_client, config_entry, caplog: pytest.LogCaptureFixture, in_israel
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_LESSON_TIMES: "whenever"})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert "Ignoring the lesson times option" in caplog.text
    assert hass.states.get(LESSON).attributes["times_known"] is True  # the portal's times are used


async def test_test_grade_and_change_sensors(hass: HomeAssistant, fake_client, config_entry, in_israel) -> None:
    await _setup(hass, config_entry)

    next_test = hass.states.get("sensor.nv_m_lvy_next_test")
    assert next_test.state == "2026-11-12"  # the 5 November exam is in the past
    assert next_test.attributes["summary"] == "בוחן: אנגלית - אוצר מילים"
    assert (next_test.attributes["type"], next_test.attributes["subject"]) == ("בוחן", "אנגלית")
    assert (next_test.attributes["start"], next_test.attributes["end"]) == ("08:00", "08:45")
    assert next_test.attributes["days_until"] == 2

    upcoming = hass.states.get("sensor.nv_m_lvy_upcoming_tests")
    assert upcoming.state == "2"
    # Not the theatre assignment: it lists the class, but the child is not in that study group.
    assert [t["subject"] for t in upcoming.attributes["tests"]] == ["אנגלית", "היסטוריה"]
    # The sibling's week is empty, so her study groups are unknown and her class decides.
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
    # Grades come from the pupil card (the whole year), once per child.
    assert len([c for c in client.calls if c[0] == "GetPupilGrades"]) == 2
    assert not [c for c in client.calls if c[0] == "GetGrades"]


async def test_lesson_times_can_come_from_the_timetables_own_labels(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    fake_client.responses["EventsTestsBoard"] = TESTS_BOARD_UNUSED
    fake_client.responses["get_pupil_schedule"] = TIMETABLE_WITH_LABELS
    freezer.move_to("2026-11-10 08:10:00+02:00")
    await _setup(hass, config_entry)
    state = hass.states.get(LESSON)
    assert state.state == "מתמטיקה" and state.attributes["room"] == "חדר 12"
    assert state.attributes["end"] == "2026-11-10T08:45:00+02:00"
    assert not [c for c in fake_client.created[0].calls if c[0] == "WeeklySchedualeForToday"]


async def test_without_a_tests_board_pending_evaluations_are_the_upcoming_tests(
    hass: HomeAssistant, fake_client, config_entry, in_israel
) -> None:
    fake_client.responses["EventsTestsBoard"] = TESTS_BOARD_UNUSED
    pending = [
        *GRADES_CARD["data"],  # includes a history test on 8 November that is still unmarked
        {**GRADES_CARD["data"][2], "evaluationID": 704, "subject": "תנ\"ך", "title": "מבחן מסכם", "type": "מבחן",
         "date": "2026-11-24T00:00:00"},
    ]  # fmt: skip
    fake_client.responses["GetPupilGrades"] = {"status": True, "data": pending}
    await _setup(hass, config_entry)

    next_test = hass.states.get("sensor.nv_m_lvy_next_test")
    assert next_test.state == "2026-11-24"  # the 8 November one is in the past
    assert next_test.attributes["summary"] == 'מבחן: תנ"ך - מבחן מסכם'
    assert next_test.attributes["start"] == ""  # the pupil card has no lesson numbers
    assert hass.states.get("sensor.nv_m_lvy_upcoming_tests").state == "1"
    assert hass.states.get("sensor.nv_m_lvy_latest_grade").state == "92"
    calendar = hass.states.get("calendar.nv_m_lvy_school_calendar")
    assert calendar.attributes["all_day"] is True and calendar.attributes["message"] == 'מבחן: תנ"ך - מבחן מסכם'


async def test_without_board_or_pupil_card_tests_are_unknown(
    hass: HomeAssistant, fake_client, config_entry, in_israel
) -> None:
    fake_client.responses["EventsTestsBoard"] = TESTS_BOARD_UNUSED
    fake_client.responses["GetPupilGrades"] = {"status": False, "errorDescription": "view is blocked"}
    await _setup(hass, config_entry)
    assert hass.states.get("sensor.nv_m_lvy_next_test").state == "unknown"
    assert hass.states.get("sensor.nv_m_lvy_upcoming_tests").state == "unknown"
    assert hass.states.get("sensor.nv_m_lvy_latest_grade").state == "92"  # the dashboard still has grades


async def test_school_calendar(hass: HomeAssistant, fake_client, config_entry, in_israel) -> None:
    await _setup(hass, config_entry)
    calendar = hass.states.get("calendar.nv_m_lvy_school_calendar")
    assert calendar is not None
    assert calendar.attributes["message"] == "בוחן: אנגלית - אוצר מילים"
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
        ("מבחן: מתמטיקה - שברים", "2026-11-05T08:00:00+02:00", "2026-11-05T09:30:00+02:00"),
        ("בוחן: אנגלית - אוצר מילים", "2026-11-12T08:00:00+02:00", "2026-11-12T08:45:00+02:00"),
        ("מבחן: היסטוריה - פרקים 4-6", "2026-11-18T08:45:00+02:00", "2026-11-18T10:35:00+02:00"),
        ("יום ספורט", "2026-11-20T08:00:00+02:00", "2026-11-20T11:20:00+02:00"),
        ("חופשת חנוכה", "2026-12-15", "2026-12-23"),  # all-day, exclusive end
    ]


async def test_new_grade_and_new_test_events(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    grade_events = async_capture_events(hass, EVENT_NEW_GRADE)
    test_events = async_capture_events(hass, EVENT_NEW_TEST)
    await _setup(hass, config_entry)
    assert grade_events == [] and test_events == []  # the first poll is only a baseline

    client = fake_client.created[0]
    # The history evaluation that was waiting for a mark gets one.
    marked = [{**row, "grade": 95} if row["grade"] is None else row for row in GRADES_CARD["data"]]
    client.responses["GetPupilGrades"] = {"status": True, "data": marked}
    board = {**TESTS_BOARD["data"]}
    board["exams"] = [*board["exams"], _exam(506, "2026-11-26", (1, 2), "מבחן", "שירה", subject="ספרות")]
    client.responses["EventsTestsBoard"] = {"status": True, "data": board}

    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

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
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(grade_events) == 2 and len(test_events) == 1


async def test_diagnostics_cover_new_data_without_personal_values(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    first = diag["students"][0]
    assert first["bell_schedule_hours"] == [1, 2, 3, 4, 5]
    assert first["calendar_items"] == {"exam": 3, "event": 1, "vacation": 1}
    assert (first["grades"], first["changes_today"]) == (2, 2)
    dumped = str(diag)
    for personal in ("שפירא", "כל הכבוד", "440099", "טיול שנתי", "עמוד 45", "92", "אוצר מילים", "קבוצה 3002"):
        assert personal not in dumped, personal
