"""Night pause, poll jitter, the refresh button, the school-day sensor and subject icons."""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.webtop.const import CONF_NIGHT_PAUSE, CONF_SCAN_INTERVAL
from custom_components.webtop.models import parse_bell_schedule, parse_schedule
from custom_components.webtop.timetable import (
    ICON_BREAK,
    ICON_LESSON,
    ICON_NO_LESSON,
    school_day,
    subject_icon,
)

from .conftest import LESSONS_WEEK, TESTS_BOARD, TESTS_BOARD_UNUSED, WEEKLY_WITHOUT_TIMES

LESSON = "sensor.nv_m_lvy_current_lesson"
DAY_END = "sensor.nv_m_lvy_school_day_ends"
REFRESH = "button.webtop_dnh_lvy_refresh"


@pytest.fixture
async def in_israel(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Asia/Jerusalem")


async def _setup(hass: HomeAssistant, config_entry, **options) -> None:
    config_entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(config_entry, options=options)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def _advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, **delta) -> None:
    freezer.tick(timedelta(**delta))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _polls(fake_client) -> int:
    """How many times the portal's unread badges were read: once per real poll."""
    return len([c for c in fake_client.created[0].calls if c[0] == "GetMenuCounters"])


# --- night pause ------------------------------------------------------------
async def test_portal_is_left_alone_at_night(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    freezer.move_to("2026-11-10 22:20:00+02:00")
    await _setup(hass, config_entry)
    assert _polls(fake_client) == 1

    await _advance(hass, freezer, minutes=31)  # 22:51, still evening
    assert _polls(fake_client) == 2
    await _advance(hass, freezer, minutes=31)  # 23:22
    await _advance(hass, freezer, hours=3)  # 02:22
    await _advance(hass, freezer, hours=3)  # 05:22
    assert _polls(fake_client) == 2
    # Entities stay available on the last data all night.
    assert hass.states.get("sensor.nv_m_lvy_latest_grade").state == "92"

    await _advance(hass, freezer, minutes=45)  # 06:07
    assert _polls(fake_client) == 3


async def test_first_poll_runs_even_at_night(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    """A restart at 02:00 must not leave the integration without data until morning."""
    freezer.move_to("2026-11-11 02:00:00+02:00")
    await _setup(hass, config_entry)
    assert _polls(fake_client) == 1
    assert hass.states.get("sensor.nv_m_lvy_latest_grade").state == "92"


async def test_night_pause_can_be_switched_off(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    freezer.move_to("2026-11-11 02:00:00+02:00")
    await _setup(hass, config_entry, **{CONF_NIGHT_PAUSE: False, CONF_SCAN_INTERVAL: 30})
    await _advance(hass, freezer, minutes=31)
    assert _polls(fake_client) == 2


async def test_date_dependent_sensors_roll_over_during_the_night_pause(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    freezer.move_to("2026-11-10 22:50:00+02:00")
    await _setup(hass, config_entry)
    assert hass.states.get("sensor.nv_m_lvy_lessons_today").state == "2"  # Tuesday
    await _advance(hass, freezer, minutes=31)  # 23:21
    await _advance(hass, freezer, hours=1)  # 00:21 on Wednesday, no poll
    assert _polls(fake_client) == 1
    assert hass.states.get("sensor.nv_m_lvy_lessons_today").state == "1"  # Wednesday's single lesson
    assert hass.states.get(DAY_END).state == "2026-11-11T06:45:00+00:00"


# --- refresh button ---------------------------------------------------------
async def test_refresh_button_polls_now_even_at_night(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    freezer.move_to("2026-11-11 02:00:00+02:00")
    await _setup(hass, config_entry)
    assert hass.states.get(REFRESH) is not None
    entry = er.async_get(hass).async_get(REFRESH)
    assert entry.unique_id == f"{config_entry.entry_id}_refresh"

    await hass.services.async_call("button", "press", {"entity_id": REFRESH}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _polls(fake_client) == 2
    # The force applies to that one poll; the night pause holds for the scheduled ones after it.
    await _advance(hass, freezer, minutes=31)
    assert _polls(fake_client) == 2


# --- jitter -----------------------------------------------------------------
async def test_polls_are_jittered_around_the_configured_interval(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    await _setup(hass, config_entry, **{CONF_SCAN_INTERVAL: 60})
    coordinator = config_entry.runtime_data
    assert coordinator.update_interval == timedelta(minutes=60)  # the conftest pins the factor to 1.0
    seen = set()
    for factor in (0.9, 1.0, 1.1):
        with patch("custom_components.webtop.coordinator.random.uniform", return_value=factor) as uniform:
            await coordinator.async_refresh()
        assert uniform.call_args.args == (0.9, 1.1)
        seen.add(coordinator.update_interval)
    # Always derived from the configured interval, never compounding on the previous jitter.
    assert seen == {timedelta(minutes=54), timedelta(minutes=60), timedelta(minutes=66)}


# --- school day -------------------------------------------------------------
async def test_school_day_end_sensor(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    freezer.move_to("2026-11-10 07:00:00+02:00")
    await _setup(hass, config_entry)
    state = hass.states.get(DAY_END)
    assert state.attributes["device_class"] == "timestamp"
    assert state.state == "2026-11-10T07:30:00+00:00"  # 09:30 in Israel: the end of lesson 2
    assert state.attributes["starts"] == "2026-11-10T08:00:00+02:00"
    assert state.attributes["lessons"] == 2
    # The sibling has no lessons this week.
    assert hass.states.get("sensor.myh_lvy_school_day_ends").state == "unknown"


async def test_school_day_end_unknown_without_lesson_times(
    hass: HomeAssistant, fake_client, config_entry, in_israel
) -> None:
    fake_client.responses["EventsTestsBoard"] = TESTS_BOARD_UNUSED
    fake_client.responses["WeeklySchedualeForToday"] = WEEKLY_WITHOUT_TIMES
    await _setup(hass, config_entry)
    state = hass.states.get(DAY_END)
    assert state.state == "unknown" and state.attributes["lessons"] == 2


def test_school_day_bounds() -> None:
    lessons = parse_schedule(LESSONS_WEEK["data"])
    bell = parse_bell_schedule(TESTS_BOARD["data"])
    tz = dt.timezone(dt.timedelta(hours=2))
    start, end = school_day(lessons, bell, dt.date(2026, 11, 10), tz)
    assert (start.strftime("%H:%M"), end.strftime("%H:%M")) == ("08:00", "09:30")
    assert school_day(lessons, bell, dt.date(2026, 11, 13), tz) is None  # no lessons on Friday
    assert school_day(lessons, {}, dt.date(2026, 11, 10), tz) is None  # lessons, but no times


# --- icons ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("subject", "icon"),
    [
        ("מתמטיקה 5", "mdi:calculator-variant"),
        ("חשבון", "mdi:calculator-variant"),
        ("אנגלית 5", "mdi:translate"),
        ("English", "mdi:translate"),
        ("פיזיקה", "mdi:atom"),
        ("מדעים", "mdi:microscope"),
        ('תנ"ך', "mdi:script-text"),
        ("שבילי מורשת", "mdi:script-text"),
        ("היסטוריה", "mdi:bank"),
        ("כישורי שפה", "mdi:book-open-variant"),
        ("חינוך גופני", "mdi:run"),
        ("שפת התיאטרון", "mdi:drama-masks"),
        ("תיאטרון", "mdi:drama-masks"),
        ("מחשבים", "mdi:laptop"),
        ("מוזיקה", "mdi:music"),
        ("שעת חינוך", "mdi:account-group"),
        ("נושא לא מוכר", ICON_LESSON),
        ("", ICON_LESSON),
        (None, ICON_LESSON),
    ],
)
def test_subject_icon(subject, icon) -> None:
    assert subject_icon(subject) == icon


async def test_current_lesson_icon_follows_the_state(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, in_israel
) -> None:
    freezer.move_to("2026-11-10 07:30:00+02:00")
    await _setup(hass, config_entry, **{CONF_SCAN_INTERVAL: 720})
    assert hass.states.get(LESSON).attributes["icon"] == ICON_NO_LESSON

    async def at(local: str) -> str:
        freezer.move_to(f"2026-11-10 {local}+02:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        return hass.states.get(LESSON).attributes["icon"]

    assert await at("08:00:01") == "mdi:calculator-variant"
    assert await at("08:45:01") == "mdi:translate"
    assert await at("09:30:01") == ICON_NO_LESSON
    assert ICON_BREAK != ICON_NO_LESSON
