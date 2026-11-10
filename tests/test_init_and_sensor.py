"""Setup, polling, re-login, multi-student switching, sensors, events, diagnostics."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed
from webtop import WebtopLoginError, WebtopRequestError

from custom_components.webtop.const import DOMAIN, EVENT_NEW_HOMEWORK, EVENT_NEW_MESSAGE
from custom_components.webtop.diagnostics import async_get_config_entry_diagnostics

from .conftest import DASHBOARD, ENCRYPTED_ID_A, ENCRYPTED_ID_B, HOMEWORK_A, INBOX, LOGIN_DATA, Seq


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
    assert f"{config_entry.entry_id}_1001_homework_today" in unique_ids
    assert f"{config_entry.entry_id}_1002_lessons_today" in unique_ids
    assert f"{config_entry.entry_id}_unread_messages" in unique_ids
    assert len(entries) == 3 + 2 * 4

    # Account sensors
    unread = _state(hass, "sensor.webtop_dnh_lvy_unread_messages")
    assert unread.state == "1"
    assert unread.attributes["total"] == 2
    assert unread.attributes["messages"][0]["sender"] == "כהן דוד"
    assert _state(hass, "sensor.webtop_dnh_lvy_last_message").state == "טיול שנתי"
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_notifications").state == "3"

    # Student A
    hw = _state(hass, "sensor.nv_m_lvy_homework_today")
    assert hw.state == "1"
    assert hw.attributes["items"][0]["subject"] == "מתמטיקה"
    assert "עמוד 45" in hw.attributes["text"]
    assert _state(hass, "sensor.nv_m_lvy_behaviour_events").state == "1"
    lessons = _state(hass, "sensor.nv_m_lvy_lessons_today")
    assert lessons.state == "2"
    assert [lesson["subject"] for lesson in lessons.attributes["today"]] == ["מתמטיקה", "אנגלית"]
    assert len(lessons.attributes["week"]) == 3
    assert _state(hass, "sensor.nv_m_lvy_class").state == "ג'2"

    # Student B
    assert _state(hass, "sensor.myh_lvy_homework_today").state == "0"
    assert _state(hass, "sensor.myh_lvy_class").state == "6-1"

    # One login, no student switching for a single-school account.
    client = fake_client.created[0]
    assert client.login_count == 1
    assert not [c for c in client.calls if c[0] == "switch_student"]
    assert [c[1]["encrypted_student_id"] for c in client.calls if c[0] == "get_homework"] == [
        ENCRYPTED_ID_A,
        ENCRYPTED_ID_B,
    ]


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
    client = fake_client.created[0]
    assert client.login_count == 2
    assert _state(hass, "sensor.nv_m_lvy_homework_today").state == "1"


async def test_status_false_without_text_is_treated_as_expired(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["get_students"] = Seq(
        [{"status": False, "errorDescription": None, "errorId": None}, DASHBOARD]
    )
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert fake_client.created[0].login_count == 2


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
    assert client.login_count == 1  # a 500 is not a session problem

    client.responses.pop("get_messages_inbox")
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_messages").state == "1"


async def test_optional_endpoint_failure_keeps_other_data(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses["get_pupil_schedule"] = {"status": False, "errorDescription": "view is blocked"}
    fake_client.responses["get_discipline_events"] = WebtopRequestError("POST /x failed (500): oops")
    fake_client.responses["get_preview_unread_notifications"] = WebtopRequestError("POST /x failed (502): bad")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor.nv_m_lvy_homework_today").state == "1"
    assert _state(hass, "sensor.nv_m_lvy_lessons_today").state == "unknown"
    assert _state(hass, "sensor.nv_m_lvy_behaviour_events").state == "unknown"
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_notifications").state == "unknown"


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
    fake_client.responses["get_students"] = {"status": True, "data": {"apps": []}}
    fake_client.responses["login"] = {**LOGIN_DATA, "classCode": 4, "classNum": 1}
    await _setup(hass, config_entry)
    # The login session (studentId 1001, name from login data) is the one student.
    assert _state(hass, "sensor.dnh_lvy_homework_today").state == "1"


async def test_multi_school_account_switches_between_linked_students(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    linked = [
        {"studentId": "link-a", "studentLogin": "noam", "school_name": "School A"},
        {"studentId": "link-b", "studentLogin": "maya", "school_name": "School B"},
    ]
    dashboard_a = {"status": True, "data": {"childrens": [DASHBOARD["data"]["childrens"][0]]}}
    dashboard_b = {"status": True, "data": {"childrens": [DASHBOARD["data"]["childrens"][1]]}}
    inbox_b = {
        "status": True,
        "data": [{"subject": "הודעה מבית ספר ב", "sendingDate": "2026-11-10T07:00:00", "hasRead": 0}],
    }
    fake_client.responses.update(
        {
            "get_linked_students": linked,
            "switch_student": Seq(
                [
                    {**LOGIN_DATA, "schoolName": "School A"},
                    {**LOGIN_DATA, "schoolName": "School B"},
                ]
            ),
            "get_students": Seq([dashboard_a, dashboard_b]),
            "get_messages_inbox": Seq([INBOX, inbox_b]),
            "get_preview_unread_notifications": Seq(
                [{"status": True, "data": [1]}, {"status": True, "data": [1, 2]}]
            ),
        }
    )
    await _setup(hass, config_entry)

    client = fake_client.created[0]
    assert [c[1]["student_id"] for c in client.calls if c[0] == "switch_student"] == ["link-a", "link-b"]
    assert _state(hass, "sensor.nv_m_lvy_class").attributes["school"] == "School A"
    assert _state(hass, "sensor.myh_lvy_class").attributes["school"] == "School B"
    unread = _state(hass, "sensor.webtop_dnh_lvy_unread_messages")
    assert unread.state == "2"
    assert unread.attributes["total"] == 3
    assert unread.attributes["messages"][0]["student"] == "מאיה לוי"
    assert _state(hass, "sensor.webtop_dnh_lvy_unread_notifications").state == "3"


async def test_failed_switch_skips_that_student_only(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.responses.update(
        {
            "get_linked_students": [{"studentId": "link-a"}, {"studentId": "link-b"}],
            "switch_student": Seq([WebtopLoginError("Switch student failed: {}"), LOGIN_DATA]),
            "get_students": {"status": True, "data": {"childrens": [DASHBOARD["data"]["childrens"][1]]}},
        }
    )
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.myh_lvy_homework_today") is not None
    assert hass.states.get("sensor.nv_m_lvy_homework_today") is None


async def test_new_student_gets_entities_without_reload(
    hass: HomeAssistant, fake_client, config_entry, freezer: FrozenDateTimeFactory
) -> None:
    fake_client.responses["get_students"] = {"status": True, "data": {"childrens": [DASHBOARD["data"]["childrens"][0]]}}
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
    await _setup(hass, config_entry)
    assert homework_events == [] and message_events == []

    client = fake_client.created[0]
    client.responses["get_homework"] = {
        "status": True,
        "data": {
            "dataTable": [
                *HOMEWORK_A["data"]["dataTable"],
                {"lesson": 'תנ"ך', "teacher": "א", "homeworkData": "פרק ב"},
            ]
        },
    }
    client.responses["get_messages_inbox"] = {
        "status": True,
        "data": [{"subject": "חדש", "sendingDate": "2026-11-10T10:00:00", "hasRead": 0}, *INBOX["data"]],
    }
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # The canned response now answers for both students: A already had the
    # maths item in its baseline, B had nothing, so B gets two new items.
    assert sorted((e.data["student"], e.data["subject"]) for e in homework_events) == [
        ("מאיה לוי", "מתמטיקה"),
        ("מאיה לוי", 'תנ"ך'),
        ("נועם לוי", 'תנ"ך'),
    ]
    assert homework_events[0].data["entry_id"] == config_entry.entry_id
    assert [e.data["subject"] for e in message_events] == ["חדש"]

    # Nothing new on the next poll: no further events.
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(homework_events) == 3 and len(message_events) == 1


async def test_diagnostics_redact_personal_data(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["entry"]["data"]["password"] == "**REDACTED**"
    assert diag["entry"]["data"]["username"] == "**REDACTED**"
    assert diag["students"][0]["class_code"] == 3
    assert diag["messages"] == 2
    dumped = str(diag)
    for secret in ("secret", "parent", ENCRYPTED_ID_A, "נועם", "דנה", "כהן"):
        assert secret not in dumped, secret
