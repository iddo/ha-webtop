"""Polling lightly: a quiet poll reads only the portal's change feed, and re-reads what it announces."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.webtop import const
from custom_components.webtop.const import CONF_NIGHT_PAUSE, CONF_SCAN_INTERVAL

from .conftest import DASHBOARD, ENCRYPTED_ID_A, ENCRYPTED_ID_B, LOGIN_DATA, NOTIFICATIONS, Seq

REFRESH = "button.webtop_dnh_lvy_refresh"
QUIET_POLL = {
    "GetMenuCounters": 1,  # the unread badges
    "get_preview_unread_notifications": 1,  # the portal's list of what is new
    "ChangesAndMessagesDataForToday": 2,  # today's substitutions, per child
}


async def _setup(hass: HomeAssistant, config_entry, **options: Any) -> Any:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_SCAN_INTERVAL: 30, CONF_NIGHT_PAUSE: False, **options}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, fake_client, **delta: float) -> Counter[str]:
    """Advance the clock past a poll and return the requests that poll made."""
    client = fake_client.created[-1]
    before = len(client.calls)
    freezer.tick(timedelta(**(delta or {"minutes": 31})))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    return Counter(call[0] for call in client.calls[before:])


def _announce(fake_client, module: str, student_id: str | None, message: str = "משהו חדש") -> None:
    """Put a new notification at the top of the portal's list."""
    row = {"moduleName": module, "date": "2026-11-10T21:00:00", "message": message, "studentID": student_id}
    preview = {**NOTIFICATIONS["data"], "personalNotifications": [row, *NOTIFICATIONS["data"]["personalNotifications"]]}
    fake_client.created[-1].responses["get_preview_unread_notifications"] = {"status": True, "data": preview}


def test_default_poll_interval_is_an_hour() -> None:
    assert const.DEFAULT_SCAN_INTERVAL == 60


async def test_a_quiet_poll_reads_only_the_change_feed(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    first = Counter(call[0] for call in fake_client.created[0].calls)
    # The first poll reads everything: the dashboard, every view for both children, the inbox...
    assert sum(first.values()) >= 20 and first["get_students"] == 1 and first["GetPupilGrades"] == 2

    assert await _poll(hass, freezer, fake_client) == QUIET_POLL
    assert await _poll(hass, freezer, fake_client) == QUIET_POLL
    # ...and nothing shown has gone missing in the meantime.
    assert hass.states.get("sensor.nv_m_lvy_homework_today").state == "1"
    assert hass.states.get("sensor.nv_m_lvy_latest_grade").state == "92"
    assert hass.states.get("sensor.webtop_dnh_lvy_last_message").state == "טיול שנתי"


async def test_a_homework_notification_rereads_that_childs_week_only(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    _announce(fake_client, "lessonSubjectHomework", ENCRYPTED_ID_A)
    made = await _poll(hass, freezer, fake_client)
    assert made == {**QUIET_POLL, "GetPupilLessonsAndHomework": 1}
    week = [c[1] for c in fake_client.created[0].calls if c[0] == "GetPupilLessonsAndHomework"][-1]
    assert (week["studentID"], week["weekIndex"]) == (ENCRYPTED_ID_A, 0)
    # The notification is acted on once, not on every poll that still lists it.
    assert await _poll(hass, freezer, fake_client) == QUIET_POLL


async def test_a_behaviour_notification_rereads_behaviour(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    _announce(fake_client, "lessonEvents", ENCRYPTED_ID_B)
    made = await _poll(hass, freezer, fake_client)
    assert made == {**QUIET_POLL, "GetPupilDiciplineEvents": 1}


async def test_an_unfamiliar_notification_rereads_everything_about_that_child(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    _announce(fake_client, "somethingNew", ENCRYPTED_ID_A)
    made = await _poll(hass, freezer, fake_client)
    assert made == {**QUIET_POLL, "GetPupilLessonsAndHomework": 1, "GetPupilDiciplineEvents": 1, "GetPupilGrades": 1}


async def test_a_notification_about_nobody_in_particular_rereads_every_child(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    _announce(fake_client, "lessonSubjectHomework", None)
    made = await _poll(hass, freezer, fake_client)
    assert made == {**QUIET_POLL, "GetPupilLessonsAndHomework": 2}


async def test_a_changed_unread_badge_rereads_the_inbox(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    counters = {"status": True, "data": {"unreadMessages": 2, "unreadNotifications": 3}}
    fake_client.created[0].responses["GetMenuCounters"] = counters
    made = await _poll(hass, freezer, fake_client)
    assert made == {**QUIET_POLL, "get_messages_inbox": 1}
    assert await _poll(hass, freezer, fake_client) == QUIET_POLL  # the badge is steady again


async def test_views_are_reread_when_their_copy_is_old(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    first_poll = len(fake_client.created[0].calls)
    total: Counter[str] = Counter()
    for _ in range(13):  # six and a half hours of half-hourly polls
        total += await _poll(hass, freezer, fake_client)
    per_child = 2
    assert total["GetMenuCounters"] == 13
    assert total["GetPupilLessonsAndHomework"] == 3 * per_child + per_child * 2  # every 2 h, plus last week twice
    assert total["GetPupilDiciplineEvents"] == 2 * per_child  # every 3 h
    assert total["GetPupilGrades"] == 1 * per_child  # every 6 h
    assert total["get_messages_inbox"] == 3  # every 2 h
    assert total["EventsTestsBoard"] == 2  # every 6 h, two months
    assert total["get_students"] == 0 and total["get_linked_students"] == 0  # twice a day at most
    assert total["get_pupil_schedule"] == 0  # the standing timetable: once a day
    # Thirteen polls cost well under half of what thirteen full reads would.
    assert sum(total.values()) < 13 * first_poll / 2


async def test_refresh_button_reads_everything(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    assert await _poll(hass, freezer, fake_client) == QUIET_POLL
    client = fake_client.created[0]
    before = len(client.calls)
    await hass.services.async_call("button", "press", {"entity_id": REFRESH}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    made = Counter(call[0] for call in client.calls[before:])
    for view in ("get_linked_students", "get_students", "get_messages_inbox"):
        assert made[view] == 1, view
    for view in ("GetPupilLessonsAndHomework", "GetPupilDiciplineEvents", "GetPupilGrades", "EventsTestsBoard"):
        assert made[view] == 2, view
    # The force lasts for that one read.
    assert await _poll(hass, freezer, fake_client) == QUIET_POLL


async def test_three_schools_quiet_poll(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    """A parent with a child in each of three schools: what a quiet poll costs."""
    linked = [{"studentId": LOGIN_DATA["id"]}, {"studentId": "enc-login-b=="}, {"studentId": "enc-login-c=="}]
    sessions = [
        {**LOGIN_DATA, "id": login["studentId"], "institutionCode": 440099 + n} for n, login in enumerate(linked)
    ]
    child = DASHBOARD["data"]["childrens"]
    third = {**child[1], "id": "enc-student-c==", "firstName": "יונתן"}
    dashboards = [{"status": True, "data": {**DASHBOARD["data"], "childrens": [kid]}} for kid in (*child, third)]
    fake_client.responses.update(
        {
            "get_linked_students": linked,
            # The session walks B, C, then back to A at the start of each later poll.
            "switch_student": Seq([sessions[1], sessions[2], sessions[0], sessions[1], sessions[2], sessions[0]]),
            "get_students": Seq(dashboards),
        }
    )
    await _setup(hass, config_entry)
    first = sum(Counter(call[0] for call in fake_client.created[0].calls).values())
    quiet = await _poll(hass, freezer, fake_client)
    assert quiet == {
        "switch_student": 3,
        "GetMenuCounters": 3,
        "get_preview_unread_notifications": 3,
        "ChangesAndMessagesDataForToday": 3,
    }
    assert sum(quiet.values()) == 12 and first >= 40
    print(f"three schools: full read {first} requests, quiet poll {sum(quiet.values())}")
