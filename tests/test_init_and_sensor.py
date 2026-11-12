"""Setup, polling, re-login, multi-student switching, sensors, events, diagnostics."""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed
from webtop import WebtopLoginError, WebtopRequestError

from custom_components.webtop.auth import compute_login_data
from custom_components.webtop.const import DOMAIN, EVENT_NEW_BEHAVIOUR, EVENT_NEW_HOMEWORK, EVENT_NEW_MESSAGE
from custom_components.webtop.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    DASHBOARD,
    DISCIPLINE,
    ENCRYPTED_ID_A,
    ENCRYPTED_ID_B,
    INBOX,
    KEY_A,
    KEY_B,
    LESSONS_WEEK,
    LOGIN_DATA,
    SCHOOL,
    Seq,
)


async def _setup(hass: HomeAssistant, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str):
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} missing; have {sorted(hass.states.async_entity_ids('sensor'))}"
    return state


async def test_setup_creates_devices_and_sensors(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    device_registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)
    assert {d.name for d in devices} == {"Webtop דנה לוי", "נועם לוי", "מאיה לוי"}

    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
    unique_ids = {e.unique_id for e in entries}
    assert f"{config_entry.entry_id}_{KEY_A}_homework_today" in unique_ids
    assert f"{config_entry.entry_id}_{KEY_B}_lessons_today" in unique_ids
    assert f"{config_entry.entry_id}_unread_messages" in unique_ids
    assert not any(ENCRYPTED_ID_A in uid for uid in unique_ids)  # that id changes every school year
    # 3 account sensors, then per student 9 sensors and a calendar.
    assert len(entries) == 3 + 2 * (9 + 1)

    # Account sensors: the counts are the portal's own menu badges.
    unread = _state(hass, "sensor.webtop_dnh_lvy_unread_messages")
    assert unread.state == "1"
    assert unread.attributes["total"] == 2
    assert unread.attributes["messages"][0]["sender"] == "כהן דוד"
    assert _state(hass, "sensor.webtop_dnh_lvy_last_message").state == "טיול שנתי"
    notifications = _state(hass, "sensor.webtop_dnh_lvy_unread_notifications")
    assert notifications.state == "3"
    assert [n["module"] for n in notifications.attributes["latest"]] == ["lessonSubjectHomework", "disciplineEvents"]

    # Student A: homework and lessons come from the dated week.
    hw = _state(hass, "sensor.nv_m_lvy_homework_today")
    assert hw.state == "1"
    assert hw.attributes["items"][0]["subject"] == "מתמטיקה"
    assert "עמוד 45" in hw.attributes["text"]
    assert [(h["date"], h["subject"]) for h in hw.attributes["week"]] == [
        ("2026-11-09", "תורה"),
        ("2026-11-10", "מתמטיקה"),
    ]
    assert hw.attributes["week_count"] == 2

    behaviour = _state(hass, "sensor.nv_m_lvy_behaviour_events")
    assert behaviour.state == "3"  # the last seven days; the September commendation is older
    assert behaviour.attributes["today"] == 2 and behaviour.attributes["total"] == 4
    assert behaviour.attributes["by_type"] == {"ציון לשבח": 2, "הפרעה": 1, "איחור": 1}
    assert behaviour.attributes["events"][0]["event"] == "הפרעה"

    lessons = _state(hass, "sensor.nv_m_lvy_lessons_today")
    assert lessons.state == "2"
    assert [lesson["subject"] for lesson in lessons.attributes["today"]] == ["מתמטיקה", "אנגלית"]
    assert lessons.attributes["today"][0]["topic"] == "שברים"
    assert len(lessons.attributes["week"]) == 4
    assert _state(hass, "sensor.nv_m_lvy_class").state == "ג'2"

    # Student B has an empty week.
    assert _state(hass, "sensor.myh_lvy_homework_today").state == "0"
    assert _state(hass, "sensor.myh_lvy_lessons_today").state == "0"
    assert _state(hass, "sensor.myh_lvy_class").state == "ו'1"

    # One login, no student switching for a single-school account.
    client = fake_client.created[0]
    assert client.login_count == 1
    assert not [c for c in client.calls if c[0] == "switch_student"]
    week_calls = [c[1] for c in client.calls if c[0] == "GetPupilLessonsAndHomework"]
    assert [c["studentID"] for c in week_calls] == [ENCRYPTED_ID_A, ENCRYPTED_ID_B]
    assert week_calls[0] == {
        "path": "/server/api/PupilCard/GetPupilLessonsAndHomework",
        "weekIndex": 0, "viewType": 0, "studyYear": 2027, "studentID": ENCRYPTED_ID_A, "classCode": 3, "moduleID": 10,
    }  # fmt: skip
    # With the dated week available, the older per-day endpoints are not called at all.
    assert not [c for c in client.calls if c[0] in ("get_homework", "get_pupil_schedule", "get_discipline_events")]
    # Lesson times came with the tests board, so the weekly-schedule lookup was not needed.
    assert not [c for c in client.calls if c[0] == "WeeklySchedualeForToday"]


async def test_unload_closes_client(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    client = fake_client.created[0]
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert client.closed is True


async def test_setup_bad_credentials_starts_reauth(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["login"] = WebtopLoginError("Login returned status=false. errorDescription='bad'")
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"
    assert fake_client.created[0].closed is True


@pytest.mark.parametrize("description", ["CaptchaRequired", "MoreThan5Retries"])
async def test_setup_captcha_or_lockout_starts_reauth(
    hass: HomeAssistant, fake_client, config_entry, description: str, caplog: pytest.LogCaptureFixture
) -> None:
    fake_client.responses["login"] = WebtopLoginError(
        f"Login returned status=false. errorDescription={description!r}, errorId=None"
    )
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert ("captcha" in caplog.text) is (description == "CaptchaRequired")
    assert ("too many attempts" in caplog.text) is (description == "MoreThan5Retries")


async def test_client_is_built_off_the_event_loop_with_computed_data(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    fake_client.counter.return_value = 2
    await _setup(hass, config_entry)
    assert fake_client.built_on_loop_thread == [False]
    client = fake_client.created[0]
    assert client.data == compute_login_data("parent", 2)
    assert client.auto_login is False


async def test_setup_timeout_retries(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["login"] = WebtopLoginError("Login request timed out: x")
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_expired_session_triggers_single_relogin(
    hass: HomeAssistant, fake_client, config_entry, unauthorized
) -> None:
    # First dashboard call is rejected with 401, the retry succeeds.
    fake_client.responses["get_students"] = Seq([unauthorized, DASHBOARD])
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    # A rejected session is replaced by a brand new client and login.
    first, second = fake_client.created
    assert first.closed is True and second.closed is False
    assert (first.login_count, second.login_count) == (1, 1)
    assert _state(hass, "sensor.nv_m_lvy_homework_today").state == "1"


async def test_status_false_without_text_is_treated_as_expired(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["get_students"] = Seq(
        [{"status": False, "errorDescription": None, "errorId": None}, DASHBOARD]
    )
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert len(fake_client.created) == 2


async def test_relogin_failure_becomes_reauth(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory, unauthorized
) -> None:
    await _setup(hass, config_entry)
    client = fake_client.created[0]
    client.responses["get_students"] = unauthorized
    client.responses["login"] = WebtopLoginError("Login returned status=false")

    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert _state(hass, "sensor.nv_m_lvy_homework_today").state == "unavailable"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_server_error_marks_unavailable_then_recovers(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, config_entry)
    client = fake_client.created[0]
    client.responses["get_messages_inbox"] = WebtopRequestError("POST /x failed (500): oops")

    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_messages").state == "unavailable"
    assert len(fake_client.created) == 1  # a 500 is not a session problem

    client.responses.pop("get_messages_inbox")
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_messages").state == "1"


async def test_optional_endpoint_failure_keeps_other_data(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["GetGrades"] = {"status": False, "errorDescription": "view is blocked"}
    fake_client.responses["GetPupilDiciplineEvents"] = WebtopRequestError("POST /x failed (500): oops")
    fake_client.responses["get_discipline_events"] = WebtopRequestError("POST /x failed (500): oops")
    fake_client.responses["get_preview_unread_notifications"] = WebtopRequestError("POST /x failed (502): bad")
    fake_client.responses["GetMenuCounters"] = WebtopRequestError("POST /x failed (502): bad")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor.nv_m_lvy_homework_today").state == "1"
    assert _state(hass, "sensor.nv_m_lvy_latest_grade").state == "unknown"
    assert _state(hass, "sensor.nv_m_lvy_behaviour_events").state == "unknown"
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_notifications").state == "unknown"
    # Without the menu badge, unread messages are counted from the inbox page.
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_messages").state == "1"


async def test_week_unavailable_falls_back_to_timetable_and_todays_homework(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    fake_client.responses["GetPupilLessonsAndHomework"] = {"status": False, "errorDescription": "view is blocked"}
    await _setup(hass, config_entry)
    client = fake_client.created[0]
    assert [c[1]["encrypted_student_id"] for c in client.calls if c[0] == "get_homework"] == [
        ENCRYPTED_ID_A,
        ENCRYPTED_ID_B,
    ]
    assert len([c for c in client.calls if c[0] == "get_pupil_schedule"]) == 2

    hw = _state(hass, "sensor.nv_m_lvy_homework_today")
    assert hw.state == "1" and hw.attributes["items"][0]["subject"] == "מתמטיקה"
    # The standing timetable is keyed by weekday; Tuesday has two lessons.
    lessons = _state(hass, "sensor.nv_m_lvy_lessons_today")
    assert lessons.state == "2"
    assert lessons.attributes["today"][0]["teacher"] == "כהן רחל"


async def test_behaviour_falls_back_to_the_dashboard_widget(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["GetPupilDiciplineEvents"] = WebtopRequestError("POST /x failed (500): oops")
    await _setup(hass, config_entry)
    behaviour = _state(hass, "sensor.nv_m_lvy_behaviour_events")
    assert behaviour.state == "2" and behaviour.attributes["total"] == 2  # the widget lists only the latest


async def test_features_the_school_switched_off_are_not_requested(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    dashboard = {
        **DASHBOARD["data"],
        "allowGetGrades": False,
        "allowGetPupilDiciplineEvents": False,
        "allowChangesAndMessagesDataForToday": False,
    }
    fake_client.responses["get_students"] = {"status": True, "data": dashboard}
    await _setup(hass, config_entry)
    called = {c[0] for c in fake_client.created[0].calls}
    skipped = {"GetGrades", "GetPupilDiciplineEvents", "get_discipline_events", "ChangesAndMessagesDataForToday"}
    assert not called & skipped
    for entity in ("latest_grade", "behaviour_events", "schedule_changes_today"):
        assert _state(hass, f"sensor.nv_m_lvy_{entity}").state == "unknown"
    assert _state(hass, "sensor.nv_m_lvy_homework_today").state == "1"


async def test_widget_answering_not_allowed_is_unknown_not_zero(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    empty = {"messages": [], "events": [], "changes": [], "exams": [], "allowToViewThis": False}
    fake_client.responses["ChangesAndMessagesDataForToday"] = {"status": True, "data": empty}
    fake_client.responses["GetGrades"] = {"status": True, "data": {"allowToViewThis": True, "dataTable": []}}
    await _setup(hass, config_entry)
    assert _state(hass, "sensor.nv_m_lvy_schedule_changes_today").state == "unknown"
    assert _state(hass, "sensor.nv_m_lvy_latest_grade").state == "unknown"  # allowed, but no grades yet


async def test_student_without_class_params(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["get_students"] = {
        "status": True,
        "data": {"childrens": [{"id": "enc-x", "studentId": 5, "firstName": "רון", "lastName": "לוי"}]},
    }
    await _setup(hass, config_entry)
    assert _state(hass, "sensor.rvn_lvy_homework_today").state == "unknown"
    assert _state(hass, "sensor.rvn_lvy_class").state == "unknown"
    assert not [c for c in fake_client.created[0].calls if c[0] == "get_homework"]


async def test_dashboard_without_students_falls_back_to_login_session(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    """A pupil's own login has no children list; the session itself is the student."""
    fake_client.responses["get_students"] = {"status": True, "data": {"apps": []}}
    fake_client.responses["login"] = {**LOGIN_DATA, "id": ENCRYPTED_ID_A, "classCode": 4, "classNum": 1}
    await _setup(hass, config_entry)
    assert _state(hass, "sensor.dnh_lvy_homework_today").state == "1"
    assert _state(hass, "sensor.dnh_lvy_class").state == "ד'1"


LINKED = [
    # As on the wire: "studentId" is the encrypted id of the *login* for that school.
    {"isTeacher": 2, "institutionCode": 440099, "school_name": "School A", "studentLogin": "AAA111",
     "studentId": LOGIN_DATA["id"]},
    {"isTeacher": 2, "institutionCode": 550011, "school_name": "School B", "studentLogin": "BBB222",
     "studentId": "enc-login-b=="},
]  # fmt: skip


async def test_multi_school_account_switches_between_linked_students(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    dashboard_a = {"status": True, "data": {**DASHBOARD["data"], "childrens": [DASHBOARD["data"]["childrens"][0]]}}
    dashboard_b = {"status": True, "data": {**DASHBOARD["data"], "childrens": [DASHBOARD["data"]["childrens"][1]]}}
    inbox_b = {
        "status": True,
        "data": [{"subject": "הודעה מבית ספר ב", "sendingDate": "2026-11-10T07:00:00", "hasRead": 0}],
    }
    fake_client.responses.update(
        {
            "get_linked_students": LINKED,
            "switch_student": {
                **LOGIN_DATA, "id": "enc-login-b==", "schoolName": "School B", "institutionCode": 550011,
            },
            "get_students": Seq([dashboard_a, dashboard_b]),
            "get_messages_inbox": Seq([INBOX, inbox_b]),
        }
    )  # fmt: skip
    await _setup(hass, config_entry)

    client = fake_client.created[0]
    # The session already is the first login, so only the second school needs a switch.
    assert [c[1]["student_id"] for c in client.calls if c[0] == "switch_student"] == ["enc-login-b=="]
    assert _state(hass, "sensor.nv_m_lvy_class").attributes["school"] == SCHOOL
    assert _state(hass, "sensor.myh_lvy_class").attributes["school"] == "School B"
    # Each school's requests carry that school's institution code.
    assert [c[1]["institutionCode"] for c in client.calls if c[0] == "ChangesAndMessagesDataForToday"] == [
        440099,
        550011,
    ]
    unread = _state(hass, "sensor.webtop_dnh_lvy_unread_messages")
    assert unread.state == "2"  # the badge of both schools, added up
    assert unread.attributes["total"] == 3
    assert unread.attributes["messages"][0]["student"] == "מאיה לוי"
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_notifications").state == "6"


async def test_next_poll_switches_back_to_the_first_school(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    session_a = dict(LOGIN_DATA)
    session_b = {**LOGIN_DATA, "id": "enc-login-b==", "schoolName": "School B", "institutionCode": 550011}
    fake_client.responses.update(
        {"get_linked_students": LINKED, "switch_student": Seq([session_b, session_a, session_b])}
    )
    await _setup(hass, config_entry)
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    switches = [c[1]["student_id"] for c in fake_client.created[0].calls if c[0] == "switch_student"]
    assert switches == ["enc-login-b==", LOGIN_DATA["id"], "enc-login-b=="]


async def test_failed_switch_skips_that_school_only(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses.update(
        {
            "get_linked_students": [{"studentId": "enc-login-x=="}, {"studentId": "enc-login-b=="}],
            "switch_student": Seq([WebtopLoginError("Switch student failed: {}"), LOGIN_DATA]),
            "get_students": {
                "status": True,
                "data": {**DASHBOARD["data"], "childrens": [DASHBOARD["data"]["childrens"][1]]},
            },
        }
    )
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.myh_lvy_homework_today") is not None
    assert hass.states.get("sensor.nv_m_lvy_homework_today") is None


async def test_new_student_gets_entities_without_reload(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    one_child = {**DASHBOARD["data"], "childrens": [DASHBOARD["data"]["childrens"][0]]}
    fake_client.responses["get_students"] = {"status": True, "data": one_child}
    await _setup(hass, config_entry)
    assert hass.states.get("sensor.myh_lvy_homework_today") is None

    fake_client.created[0].responses["get_students"] = DASHBOARD
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.myh_lvy_homework_today").state == "0"


async def test_events_fire_only_for_new_items_after_baseline(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    homework_events = async_capture_events(hass, EVENT_NEW_HOMEWORK)
    message_events = async_capture_events(hass, EVENT_NEW_MESSAGE)
    behaviour_events = async_capture_events(hass, EVENT_NEW_BEHAVIOUR)
    await _setup(hass, config_entry)
    assert homework_events == [] and message_events == [] and behaviour_events == []

    client = fake_client.created[0]
    week = copy.deepcopy(LESSONS_WEEK)
    tuesday_hour_two = week["data"][2]["hoursData"][1]["scheduale"][0]
    assert tuesday_hour_two["subject_name"] == "אנגלית"
    tuesday_hour_two["homeWork"] = "ללמוד לבוחן"  # the teacher replaced "אין" with real homework
    client.responses["GetPupilLessonsAndHomework"] = week  # answered for both children
    new_record = {**DISCIPLINE["data"]["diciplineEvents"][0], "eventID": 99, "eventDate": "2026-11-10T00:00:00"}
    client.responses["GetPupilDiciplineEvents"] = {
        "status": True,
        "data": {"diciplineEvents": [*DISCIPLINE["data"]["diciplineEvents"], new_record]},
    }
    client.responses["get_messages_inbox"] = {
        "status": True,
        "data": [{"subject": "חדש", "sendingDate": "2026-11-10T10:00:00", "hasRead": 0}, *INBOX["data"]],
    }
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Student A already had two homework items in the baseline; B had none, so B gets all three.
    assert sorted((e.data["student"], e.data["subject"], e.data["date"]) for e in homework_events) == [
        ("מאיה לוי", "אנגלית", "2026-11-10"),
        ("מאיה לוי", "מתמטיקה", "2026-11-10"),
        ("מאיה לוי", "תורה", "2026-11-09"),
        ("נועם לוי", "אנגלית", "2026-11-10"),
    ]
    assert homework_events[0].data["entry_id"] == config_entry.entry_id
    assert [e.data["subject"] for e in message_events] == ["חדש"]
    assert sorted((e.data["student"], e.data["event"]) for e in behaviour_events) == [
        ("מאיה לוי", "ציון לשבח"),
        ("נועם לוי", "ציון לשבח"),
    ]

    # Nothing new on the next poll: no further events.
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert (len(homework_events), len(message_events), len(behaviour_events)) == (4, 1, 2)


async def test_diagnostics_redact_personal_data(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["entry"]["data"]["password"] == "**REDACTED**"
    assert diag["entry"]["data"]["username"] == "**REDACTED**"
    assert diag["students"][0]["class_code"] == 3
    assert diag["messages"] == 2
    dumped = str(diag)
    personal = ("secret", "parent", ENCRYPTED_ID_A, "נועם", "דנה", "כהן", "0521111111", SCHOOL, "שברים",
                "עמוד 45", "דיבר בשיעור", "עודכנו שיעורי בית", "enc-item-1", "enc-message-1", "440099")  # fmt: skip
    for secret in personal:
        assert secret not in dumped, secret
