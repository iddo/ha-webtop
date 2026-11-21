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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from webtop import WebtopLoginError

from .auth import async_create_client, configured_user_agent, login_failure_reason
from .const import (
    CONF_DATA,
    CONF_LESSON_TIMES,
    CONF_NIGHT_PAUSE,
    CONF_PASSWORD,
    CONF_READ_UNREAD_MESSAGES,
    CONF_SCAN_INTERVAL,
    CONF_STUDENT_LESSON_TIMES,
    CONF_USER_AGENT,
    CONF_USERNAME,
    DEFAULT_NIGHT_PAUSE,
    DEFAULT_READ_UNREAD_MESSAGES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USER_AGENT,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import WebtopConfigEntry
from .models import parse_manual_bell

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(CONF_DATA): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        vol.Optional(CONF_USER_AGENT): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(CONF_DATA): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
    }
)


class AccountInfo:
    """What a successful validation login tells us about the account."""

    def __init__(self, unique_id: str, title: str) -> None:
        self.unique_id = unique_id
        self.title = title


async def validate_login(
    hass: HomeAssistant, data: Mapping[str, Any], options: Mapping[str, Any] | None = None
) -> AccountInfo:
    """Log in once with a throwaway client.

    Raises WebtopLoginError on failure; the caller sorts transient from auth.
    """
    client = await async_create_client(
        hass,
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data.get(CONF_DATA),
        configured_user_agent(data, options),
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
        return login_failure_reason(err)
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
            user_input[CONF_DATA] = (user_input.get(CONF_DATA) or "").strip()
            user_input[CONF_USER_AGENT] = (user_input.get(CONF_USER_AGENT) or "").strip()
            try:
                info = await validate_login(self.hass, user_input)
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
            candidate = {**entry.data, **user_input, CONF_DATA: (user_input.get(CONF_DATA) or "").strip()}
            try:
                info = await validate_login(self.hass, candidate, entry.options)
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
    """Options: polling, the night pause, and lesson times for schools that publish none.

    With several students on the account, a second step per student lets each one
    have their own lesson times, since siblings rarely share a bell schedule.
    """

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._students: list[tuple[str, str, int]] = []  # key, name, lesson times the portal publishes
        self._index = 0

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            user_input[CONF_LESSON_TIMES] = (user_input.get(CONF_LESSON_TIMES) or "").strip()
            # Always stored, even empty, so that clearing the field really returns to the default.
            user_input[CONF_USER_AGENT] = (user_input.get(CONF_USER_AGENT) or "").strip()
            try:
                parse_manual_bell(user_input[CONF_LESSON_TIMES])
            except ValueError:
                errors[CONF_LESSON_TIMES] = "invalid_lesson_times"
            else:
                self._options = {
                    **user_input,
                    CONF_STUDENT_LESSON_TIMES: dict(options.get(CONF_STUDENT_LESSON_TIMES) or {}),
                }
                coordinator = getattr(self.config_entry, "runtime_data", None)
                students = coordinator.data.students if coordinator is not None and coordinator.data else []
                self._students = [
                    (s.key, s.name or s.key, len(coordinator.portal_bell.get(s.key, {}))) for s in students
                ]
                # Forget times typed for students who are no longer on the account.
                known = {key for key, _, _ in self._students}
                if known:
                    typed = self._options[CONF_STUDENT_LESSON_TIMES]
                    self._options[CONF_STUDENT_LESSON_TIMES] = {k: v for k, v in typed.items() if k in known}
                self._index = 0
                if len(self._students) > 1:
                    return await self.async_step_student()
                return self.async_create_entry(data=self._options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Required(
                    CONF_NIGHT_PAUSE, default=options.get(CONF_NIGHT_PAUSE, DEFAULT_NIGHT_PAUSE)
                ): BooleanSelector(),
                vol.Required(
                    CONF_READ_UNREAD_MESSAGES,
                    default=options.get(CONF_READ_UNREAD_MESSAGES, DEFAULT_READ_UNREAD_MESSAGES),
                ): BooleanSelector(),
                vol.Optional(CONF_LESSON_TIMES): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
                vol.Optional(CONF_USER_AGENT): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            }
        )
        suggested = user_input or {
            CONF_LESSON_TIMES: options.get(CONF_LESSON_TIMES, ""),
            # Shown only when it was changed; an empty field means the integration's own name.
            CONF_USER_AGENT: self._custom_user_agent(),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    def _custom_user_agent(self) -> str:
        """The client identification in effect, or "" when it is the default."""
        current = configured_user_agent(self.config_entry.data, self.config_entry.options)
        return "" if current == DEFAULT_USER_AGENT else current

    async def async_step_student(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Lesson times for one student; shown once per student."""
        if self._index >= len(self._students):
            return self.async_create_entry(data=self._options)
        key, name, portal_hours = self._students[self._index]
        typed: dict[str, str] = self._options[CONF_STUDENT_LESSON_TIMES]
        errors: dict[str, str] = {}
        if user_input is not None:
            text = (user_input.get(CONF_LESSON_TIMES) or "").strip()
            try:
                parse_manual_bell(text)
            except ValueError:
                errors[CONF_LESSON_TIMES] = "invalid_lesson_times"
            else:
                if text:
                    typed[key] = text
                else:
                    typed.pop(key, None)
                self._index += 1
                return await self.async_step_student()

        schema = vol.Schema(
            {
                vol.Optional(CONF_LESSON_TIMES): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                )
            }
        )
        suggested = user_input or {CONF_LESSON_TIMES: typed.get(key, "")}
        return self.async_show_form(
            step_id="student",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            description_placeholders={"student": name, "portal_hours": str(portal_hours)},
            errors=errors,
        )
