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
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DOMAIN,
    LEGACY_DEFAULT_DATA,
)

from .conftest import LOGIN_DATA

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


async def test_options_flow_sets_interval_and_reloads(hass: HomeAssistant, fake_client, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 60})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_SCAN_INTERVAL] == 60
    assert config_entry.runtime_data.update_interval.total_seconds() == 3600
