"""Config, reauth and options flow tests."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from webtop import WebtopLoginError

from custom_components.webtop.const import (
    CONF_DATA,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_DATA,
    DOMAIN,
)

from .conftest import LOGIN_DATA

USER_INPUT = {CONF_USERNAME: " parent ", CONF_PASSWORD: "secret", CONF_DATA: DEFAULT_DATA}


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
    # The validation client was closed, and the coordinator's own client logged in.
    assert fake_client.created[0].closed is True
    assert fake_client.created[0].auto_login is False


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
        result["flow_id"], {CONF_PASSWORD: "new-secret", CONF_DATA: DEFAULT_DATA}
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
        result["flow_id"], {CONF_PASSWORD: "x", CONF_DATA: DEFAULT_DATA}
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
        result["flow_id"], {CONF_PASSWORD: "x", CONF_DATA: DEFAULT_DATA}
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
