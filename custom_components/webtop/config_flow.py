"""Config flow for the Webtop (SmartSchool) integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from webtop import WebtopClient, WebtopLoginError

from .const import (
    CONF_DATA,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_DATA,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    REQUEST_TIMEOUT,
)
from .coordinator import WebtopConfigEntry, is_transient_login_error

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(CONF_DATA, default=DEFAULT_DATA): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(CONF_DATA, default=DEFAULT_DATA): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
    }
)


class AccountInfo:
    """What a successful validation login tells us about the account."""

    def __init__(self, unique_id: str, title: str) -> None:
        self.unique_id = unique_id
        self.title = title


async def validate_login(data: Mapping[str, Any]) -> AccountInfo:
    """Log in once with a throwaway client.

    Raises WebtopLoginError on failure; the caller sorts transient from auth.
    """
    client = WebtopClient(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        data=data.get(CONF_DATA) or DEFAULT_DATA,
        timeout=REQUEST_TIMEOUT,
        auto_login=False,
    )
    try:
        session = await client.login()
    finally:
        await client.close()
    unique_id = str(session.user_id) if session.user_id else data[CONF_USERNAME].strip().casefold()
    name = f"{session.first_name or ''} {session.last_name or ''}".strip()
    title = name or data[CONF_USERNAME]
    if session.school_name:
        title = f"{title} ({session.school_name})"
    return AccountInfo(unique_id=unique_id, title=title)


def _error_key(err: Exception) -> str:
    if isinstance(err, WebtopLoginError):
        return "cannot_connect" if is_transient_login_error(err) else "invalid_auth"
    return "unknown"


class WebtopConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: WebtopConfigEntry) -> WebtopOptionsFlow:
        return WebtopOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect credentials and validate them against Webtop."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_USERNAME] = user_input[CONF_USERNAME].strip()
            try:
                info = await validate_login(user_input)
            except Exception as err:
                errors["base"] = _error_key(err)
                if errors["base"] == "unknown":
                    _LOGGER.exception("Unexpected error validating Webtop credentials")
            else:
                await self.async_set_unique_id(info.unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info.title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Credentials were rejected during a poll."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password and validate it."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**entry.data, **user_input}
            try:
                info = await validate_login(candidate)
            except Exception as err:
                errors["base"] = _error_key(err)
                if errors["base"] == "unknown":
                    _LOGGER.exception("Unexpected error validating Webtop credentials")
            else:
                await self.async_set_unique_id(info.unique_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data=candidate)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )


class WebtopOptionsFlow(OptionsFlowWithReload):
    """Options: polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
