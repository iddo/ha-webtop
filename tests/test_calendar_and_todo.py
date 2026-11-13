"""The timetable calendar and the homework to-do list."""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.webtop.models import parse_bell_schedule, parse_schedule
from custom_components.webtop.timetable import placed_lessons

from .conftest import (
    ENCRYPTED_ID_A,
    KEY_A,
    LAST_WEEK,
    LESSONS_WEEK,
    LESSONS_WEEK_EMPTY,
    TESTS_BOARD,
    TESTS_BOARD_UNUSED,
    TIMETABLE,
    WEEKLY_WITHOUT_TIMES,
)

TIMETABLE_CAL = "calendar.nv_m_lvy_timetable"
HOMEWORK = "todo.nv_m_lvy_homework"


@pytest.fixture
async def in_israel(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Asia/Jerusalem")


def _both_weeks(payload: dict[str, Any]) -> dict[str, Any]:
    """Student A: this week and the one before. Anyone else: nothing."""
    if payload["studentID"] != ENCRYPTED_ID_A:
        return LESSONS_WEEK_EMPTY
    return LESSONS_WEEK if payload["weekIndex"] == 0 else LAST_WEEK


async def _setup(hass: HomeAssistant, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def _events(hass: HomeAssistant, start: str, end: str) -> list[dict[str, Any]]:
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": TIMETABLE_CAL,
            "start_date_time": f"{start}T00:00:00+02:00",
            "end_date_time": f"{end}T00:00:00+02:00",
        },
        blocking=True,
        return_response=True,
    )
    return response[TIMETABLE_CAL]["events"]


def _brief(events: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(e["summary"], e["start"][:16], e["end"][11:16]) for e in events]


# --- timetable calendar -----------------------------------------------------
async def test_this_week_shows_what_was_taught(hass: HomeAssistant, fake_client, config_entry, in_israel) -> None:
    await _setup(hass, config_entry)
    events = await _events(hass, "2026-11-08", "2026-11-15")
    assert _brief(events) == [
        ("תורה", "2026-11-09T08:00", "08:45"),
        ("מתמטיקה", "2026-11-10T08:00", "08:45"),
        ("אנגלית", "2026-11-10T08:45", "09:30"),
        ("מדעים", "2026-11-11T08:00", "08:45"),
    ]
    maths = events[1]
    assert maths["location"] == "חדר 12"  # from the standing timetable
    assert maths["description"] == "רחל\nשברים\n\U0001f4dd עמוד 45 תרגילים 1-5"  # teacher, topic, homework
    english = events[2]
    assert english["description"] == "דוד\nUnit 3"  # "אין" is not homework
    assert "location" not in events[0]  # Monday's lesson has no room on the timetable

    # At 22:00 on Tuesday the next lesson is Wednesday's.
    state = hass.states.get(TIMETABLE_CAL)
    assert state.attributes["message"] == "מדעים"
    assert state.attributes["start_time"] == "2026-11-11 08:00:00"


async def test_other_weeks_come_from_the_standing_timetable(
    hass: HomeAssistant, fake_client, config_entry, in_israel
) -> None:
    await _setup(hass, config_entry)
    events = await _events(hass, "2026-11-15", "2026-11-22")
    assert _brief(events) == [
        ("מתמטיקה", "2026-11-17T08:00", "08:45"),
        ("אנגלית", "2026-11-17T08:45", "09:30"),
        ("מתמטיקה", "2026-11-18T08:00", "08:45"),
    ]
    assert events[0]["location"] == "חדר 12" and events[0]["description"] == "כהן רחל"
    # The Hanukkah vacation on the tests board (15 to 22 December) has no lessons.
    assert _brief(await _events(hass, "2026-12-13", "2026-12-24")) == [("מתמטיקה", "2026-12-23T08:00", "08:45")]


async def test_last_week_is_shown_as_taught_not_from_the_standing_timetable(
    hass: HomeAssistant, fake_client, config_entry, in_israel
) -> None:
    fake_client.responses["GetPupilLessonsAndHomework"] = _both_weeks
    await _setup(hass, config_entry)
    events = await _events(hass, "2026-11-01", "2026-11-08")
    # Only Thursday's double science lesson; the standing timetable's Tuesday is not invented for a known week.
    assert _brief(events) == [("מדעים", "2026-11-05T09:50", "10:35"), ("מדעים", "2026-11-05T10:35", "11:20")]
    assert events[0]["description"] == "יעל\nמחזור המים\n\U0001f4dd להכין מצגת על מחזור המים"


async def test_no_lesson_times_means_no_timetable_events(
    hass: HomeAssistant, fake_client, config_entry, in_israel
) -> None:
    fake_client.responses["EventsTestsBoard"] = TESTS_BOARD_UNUSED
    fake_client.responses["WeeklySchedualeForToday"] = WEEKLY_WITHOUT_TIMES
    await _setup(hass, config_entry)
    assert await _events(hass, "2026-11-08", "2026-11-22") == []
    assert hass.states.get(TIMETABLE_CAL).state == "off"


def test_placed_lessons_range_is_bounded_and_ordered() -> None:
    bell = parse_bell_schedule(TESTS_BOARD["data"])
    standing = parse_schedule(TIMETABLE["data"])
    tz = dt.timezone(dt.timedelta(hours=2))
    year = placed_lessons([], standing, bell, dt.date(2026, 1, 1), dt.date(2027, 1, 1), tz)
    assert year[-1][1].date() <= dt.date(2026, 1, 1) + dt.timedelta(days=120)  # a runaway range is capped
    assert [item[1] for item in year] == sorted(item[1] for item in year)
    days_off = placed_lessons([], standing, bell, dt.date(2026, 11, 15), dt.date(2026, 11, 21), tz, lambda day: True)
    assert days_off == []
    assert placed_lessons([], standing, {}, dt.date(2026, 11, 15), dt.date(2026, 11, 21), tz) == []


# --- homework to-do list ----------------------------------------------------
async def _items(hass: HomeAssistant) -> list[dict[str, Any]]:
    response = await hass.services.async_call(
        "todo", "get_items", {"entity_id": HOMEWORK}, blocking=True, return_response=True
    )
    return response[HOMEWORK]["items"]


async def _tick(hass: HomeAssistant, uid: str, status: str = "completed", **extra: Any) -> None:
    await hass.services.async_call(
        "todo", "update_item", {"entity_id": HOMEWORK, "item": uid, "status": status, **extra}, blocking=True
    )
    await hass.async_block_till_done()


async def test_homework_is_a_todo_list(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["GetPupilLessonsAndHomework"] = _both_weeks
    await _setup(hass, config_entry)
    assert hass.states.get(HOMEWORK).state == "3"  # items not yet ticked
    items = await _items(hass)
    assert [(i["summary"], i["status"]) for i in items] == [
        ("מתמטיקה: עמוד 45 תרגילים 1-5", "needs_action"),
        ("תורה: לקרוא פרק ג", "needs_action"),
        ("מדעים: להכין מצגת על מחזור המים", "needs_action"),  # last week's, a double lesson
    ]
    assert items[0]["description"] == "2026-11-10 · lesson 1 · רחל"
    assert items[2]["description"] == "2026-11-05 · lessons 3, 4 · יעל"
    # The sibling has no homework: an empty list, not a missing one.
    assert hass.states.get("todo.myh_lvy_homework").state == "0"


async def test_ticking_homework_is_remembered_across_a_reload(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    await _setup(hass, config_entry)
    maths, torah = await _items(hass)
    await _tick(hass, maths["uid"])
    assert hass.states.get(HOMEWORK).state == "1"
    statuses = [(i["summary"][:3], i["status"]) for i in await _items(hass)]
    assert statuses == [("מתמ", "completed"), ("תור", "needs_action")]

    # Renaming is not ours to do: the text mirrors the portal.
    await _tick(hass, torah["uid"], status="needs_action", rename="something else")
    assert (await _items(hass))[1]["summary"] == "תורה: לקרוא פרק ג"

    freezer.tick(timedelta(seconds=5))  # let the delayed save land
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    stored = hass_storage[f"webtop.{config_entry.entry_id}.homework_done"]["data"]
    assert stored == {KEY_A: [maths["uid"]]}

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(HOMEWORK).state == "1"
    assert (await _items(hass))[0]["status"] == "completed"

    await _tick(hass, maths["uid"], status="needs_action")  # and it can be un-ticked
    assert hass.states.get(HOMEWORK).state == "2"


async def test_ticks_for_homework_the_portal_dropped_are_forgotten(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    await _setup(hass, config_entry)
    maths, torah = await _items(hass)
    await _tick(hass, maths["uid"])
    await _tick(hass, torah["uid"])

    # Two weeks on, neither item is listed any more.
    fake_client.created[0].responses["GetPupilLessonsAndHomework"] = LESSONS_WEEK_EMPTY
    freezer.tick(timedelta(hours=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await _items(hass) == []
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass_storage[f"webtop.{config_entry.entry_id}.homework_done"]["data"] == {}


async def test_homework_list_is_unavailable_when_homework_could_not_be_read(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    blocked = {"status": False, "errorDescription": "view is blocked", "data": None}
    fake_client.responses["GetPupilLessonsAndHomework"] = blocked
    dashboard_without_homework = {"status": False, "errorDescription": "view is blocked", "data": None}
    fake_client.responses["get_homework"] = dashboard_without_homework
    await _setup(hass, config_entry)
    assert hass.states.get(HOMEWORK).state == "unavailable"  # unknown is not the same as "nothing to do"


async def test_removing_the_entry_deletes_the_saved_ticks(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    await _setup(hass, config_entry)
    await _tick(hass, (await _items(hass))[0]["uid"])
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    key = f"webtop.{config_entry.entry_id}.homework_done"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
