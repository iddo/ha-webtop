"""Config, reauth and options flow tests."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from webtop import WebtopLoginError

from custom_components.webtop.auth import compute_login_data
from custom_components.webtop.const import (
    CONF_DATA,
    CONF_LESSON_TIMES,
    CONF_NIGHT_PAUSE,
    CONF_PASSWORD,
    CONF_READ_UNREAD_MESSAGES,
    CONF_SCAN_INTERVAL,
    CONF_STUDENT_LESSON_TIMES,
    CONF_USER_AGENT,
    CONF_USERNAME,
    DOMAIN,
    LEGACY_DEFAULT_DATA,
)

from .conftest import DASHBOARD, KEY_A, KEY_B, LOGIN_DATA

USER_INPUT = {CONF_USERNAME: " parent ", CONF_PASSWORD: "secret"}


async def test_user_flow_creates_entry(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "דנה לוי (בית ספר לדוגמה)"
    assert result["data"][CONF_USERNAME] == "parent"
    assert result["result"].unique_id == "u-42"
    assert result["data"][CONF_DATA] == ""
    # The validation client was closed, and the coordinator's own client logged in.
    validation = fake_client.created[0]
    assert validation.closed is True and validation.auto_login is False
    # Data is derived from the trimmed username, and no client is built on the loop.
    assert validation.data == compute_login_data("parent", 0)
    assert not any(fake_client.built_on_loop_thread)


async def test_user_flow_uses_server_login_counter(hass: HomeAssistant, fake_client) -> None:
    fake_client.counter.return_value = 1
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert fake_client.created[0].data == compute_login_data("parent", 1)


async def test_user_flow_data_override_and_legacy_default(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    fake_client.responses["login"] = WebtopLoginError("Login returned status=false")
    # pywebtop's old built-in default is not a real override.
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_DATA: LEGACY_DEFAULT_DATA}
    )
    assert fake_client.created[-1].data == compute_login_data("parent", 0)
    # Anything else the user typed is sent as is.
    await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_DATA: " captured== "})
    assert fake_client.created[-1].data == "captured=="


@pytest.mark.parametrize(
    ("description", "error"),
    [("CaptchaRequired", "captcha_required"), ("MoreThan5Retries", "too_many_attempts"), (None, "invalid_auth")],
)
async def test_user_flow_distinguishes_server_refusals(
    hass: HomeAssistant, fake_client, description, error
) -> None:
    fake_client.responses["login"] = WebtopLoginError(
        f"Login returned status=false. errorDescription={description!r}, errorId=None"
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": error}


async def test_user_flow_invalid_auth(hass: HomeAssistant, fake_client) -> None:
    fake_client.responses["login"] = WebtopLoginError(
        "Login returned status=false. errorDescription=None, errorId=None"
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, fake_client) -> None:
    fake_client.responses["login"] = WebtopLoginError("Login request timed out: x")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(hass: HomeAssistant, fake_client) -> None:
    fake_client.responses["login"] = RuntimeError("boom")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_already_configured(hass: HomeAssistant, fake_client, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_unique_id_falls_back_to_username(hass: HomeAssistant, fake_client) -> None:
    fake_client.responses["login"] = {**LOGIN_DATA, "userId": None, "schoolName": None}
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["result"].unique_id == "parent"
    assert result["title"] == "דנה לוי"


async def test_reauth_flow(hass: HomeAssistant, fake_client, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-secret"
    assert config_entry.data[CONF_USERNAME] == "parent"


async def test_reauth_rejects_other_account(hass: HomeAssistant, fake_client, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    fake_client.responses["login"] = {**LOGIN_DATA, "userId": "someone-else"}
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "x"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_reauth_shows_error_on_bad_password(hass: HomeAssistant, fake_client, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    fake_client.responses["login"] = WebtopLoginError("Login returned status=false")
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "x"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def _open_options(hass: HomeAssistant, config_entry) -> str:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    return result["flow_id"]


async def test_options_flow_walks_each_student_and_reloads(hass: HomeAssistant, fake_client, config_entry) -> None:
    flow_id = await _open_options(hass, config_entry)

    # Lesson times that cannot be read keep the form open with an error on that field.
    result = await hass.config_entries.options.async_configure(
        flow_id, {CONF_SCAN_INTERVAL: 60, CONF_NIGHT_PAUSE: True, CONF_LESSON_TIMES: "09:00-08:00"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    assert result["errors"] == {CONF_LESSON_TIMES: "invalid_lesson_times"}

    result = await hass.config_entries.options.async_configure(
        flow_id, {CONF_SCAN_INTERVAL: 60, CONF_NIGHT_PAUSE: True, CONF_LESSON_TIMES: " 08:00-08:45, 08:45-09:30 "}
    )
    # Two children on the account: each gets a step of their own, named, with what the portal offers.
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "student"
    assert result["description_placeholders"] == {"student": "נועם לוי", "portal_hours": "5"}

    result = await hass.config_entries.options.async_configure(flow_id, {CONF_LESSON_TIMES: "nonsense"})
    assert result["step_id"] == "student" and result["errors"] == {CONF_LESSON_TIMES: "invalid_lesson_times"}
    assert result["description_placeholders"]["student"] == "נועם לוי"  # still the same child

    result = await hass.config_entries.options.async_configure(flow_id, {})  # use the portal's for the first
    assert result["step_id"] == "student" and result["description_placeholders"]["student"] == "מאיה לוי"
    result = await hass.config_entries.options.async_configure(flow_id, {CONF_LESSON_TIMES: "08:10-09:00"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_SCAN_INTERVAL: 60,
        CONF_NIGHT_PAUSE: True,
        CONF_READ_UNREAD_MESSAGES: False,  # off unless asked for: it marks messages as read on the portal
        CONF_LESSON_TIMES: "08:00-08:45, 08:45-09:30",
        CONF_USER_AGENT: "",  # empty: the integration identifies as itself
        CONF_STUDENT_LESSON_TIMES: {KEY_B: "08:10-09:00"},
    }
    assert config_entry.runtime_data.update_interval.total_seconds() == 3600


async def test_options_flow_shows_and_clears_previously_typed_student_times(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={CONF_STUDENT_LESSON_TIMES: {KEY_A: "08:00-08:40", "a-child-who-left": "07:00-07:45"}},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        flow_id, {CONF_SCAN_INTERVAL: 30, CONF_NIGHT_PAUSE: True}
    )
    schema = result["data_schema"].schema
    suggested = next(key.description["suggested_value"] for key in schema if key == CONF_LESSON_TIMES)
    assert suggested == "08:00-08:40"  # what was typed before is offered back

    result = await hass.config_entries.options.async_configure(flow_id, {CONF_LESSON_TIMES: ""})  # clear it
    result = await hass.config_entries.options.async_configure(flow_id, {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Cleared for the first child, and the entry for a child no longer on the account is dropped.
    assert config_entry.options[CONF_STUDENT_LESSON_TIMES] == {}


async def test_options_flow_with_one_student_has_no_extra_step(hass: HomeAssistant, fake_client, config_entry) -> None:
    one_child = {**DASHBOARD["data"], "childrens": [DASHBOARD["data"]["childrens"][0]]}
    fake_client.responses["get_students"] = {"status": True, "data": one_child}
    flow_id = await _open_options(hass, config_entry)
    result = await hass.config_entries.options.async_configure(
        flow_id, {CONF_SCAN_INTERVAL: 45, CONF_NIGHT_PAUSE: False, CONF_READ_UNREAD_MESSAGES: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_SCAN_INTERVAL: 45,
        CONF_NIGHT_PAUSE: False,
        CONF_READ_UNREAD_MESSAGES: True,
        CONF_LESSON_TIMES: "",
        CONF_USER_AGENT: "",
        CONF_STUDENT_LESSON_TIMES: {},
    }
