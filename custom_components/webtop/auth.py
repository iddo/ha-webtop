"""Login plumbing that pywebtop leaves to the caller.

Two things the Webtop web client does on every password login, taken from its
own JavaScript bundle:

* It asks the server for a login-attempt counter (``api/user/getInput``).
* It sends ``Data`` = AES-CBC(JSON(username + counter)) with a fixed key and IV
  that ship in the bundle. pywebtop's built-in default for ``Data`` is that
  value for one particular username, so it is wrong for everyone else.

This module computes the right ``Data`` and builds the pywebtop client off the
event loop (httpx loads the TLS trust store from disk when a client is made).

It never calls ``api/user/setInput``: that counter is what decides whether the
portal demands a captcha, and resetting it would be defeating bot detection.
If the server answers ``CaptchaRequired`` the integration reports it and stops.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from functools import partial
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client

from webtop import WebtopClient, WebtopLoginError

from .const import CONF_USER_AGENT, DEFAULT_USER_AGENT, LEGACY_DEFAULT_DATA, REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://webtopserver.smartschool.co.il"
_COUNTER_PATH = "/server/api/user/getInput"

# Constants from the public Webtop web client (rsaService.encryptStringToServer).
_DATA_KEY = b"01234567890000000150778345678901"
_DATA_IV = b"6543210987654321"

# errorDescription values the web client special-cases on a failed login.
ERROR_CAPTCHA_REQUIRED = "CaptchaRequired"
ERROR_TOO_MANY_RETRIES = "MoreThan5Retries"


def compute_login_data(username: str, counter: int = 0) -> str:
    """The ``Data`` field for ``LoginByUserNameAndPassword``."""
    plain = json.dumps(f"{username}{counter}").encode("utf-8")
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_DATA_KEY), modes.CBC(_DATA_IV)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def configured_user_agent(data: Mapping[str, Any], options: Mapping[str, Any] | None = None) -> str:
    """The client identification to send: the option if set, else the setup value, else the default.

    An option that is present but empty means "back to the default", whatever was typed at setup.
    """
    if options is not None and CONF_USER_AGENT in options:
        return (options[CONF_USER_AGENT] or "").strip() or DEFAULT_USER_AGENT
    return (data.get(CONF_USER_AGENT) or "").strip() or DEFAULT_USER_AGENT


async def async_fetch_login_counter(hass: HomeAssistant, user_agent: str = DEFAULT_USER_AGENT) -> int:
    """Read the server's login-attempt counter; 0 if it cannot be read."""
    try:
        response = await get_async_client(hass).post(
            f"{BASE_URL}{_COUNTER_PATH}",
            json={},
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": user_agent},
            timeout=REQUEST_TIMEOUT,
        )
        value = response.json()
    except Exception as err:
        _LOGGER.debug("Could not read the Webtop login counter (%s); assuming 0", err)
        return 0
    if isinstance(value, dict):
        value = value.get("data")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def is_data_override(value: str | None) -> bool:
    """True when the user supplied their own ``Data`` value."""
    return bool(value) and value != LEGACY_DEFAULT_DATA


def _build_client(username: str, password: str, data: str, user_agent: str) -> WebtopClient:
    client = WebtopClient(
        username=username,
        password=password,
        data=data,
        timeout=REQUEST_TIMEOUT,
        auto_login=False,
    )
    # pywebtop has no parameter for this, so the header goes on its HTTP client. Without it the
    # requests would carry the HTTP library's own name, which says nothing about who is calling.
    http = getattr(client, "_http", None)
    if http is not None:
        http.headers["User-Agent"] = user_agent
    return client


async def async_create_client(
    hass: HomeAssistant,
    username: str,
    password: str,
    data_override: str | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> WebtopClient:
    """Build a pywebtop client with a correct ``Data`` value, in the executor."""
    if is_data_override(data_override):
        data = data_override
    else:
        counter = await async_fetch_login_counter(hass, user_agent)
        data = compute_login_data(username, counter)
    return await hass.async_add_executor_job(partial(_build_client, username, password, data, user_agent))


_HTTP_403 = "(403)"
# Text of the block pages a CDN or web firewall answers with, as opposed to the portal's own replies.
_FIREWALL_MARKERS = ("cloudfront", "request blocked", "the request could not be satisfied", "access denied", "<html")


def is_firewall_block(err: Exception) -> bool:
    """True when a request was refused in front of the portal, before the portal saw it.

    The portal sits behind a CDN with a web firewall. When that refuses a request it
    answers HTTP 403 with an HTML block page. The portal's own "no" is JSON, or a 401/403
    with an empty body for an expired session.
    """
    text = str(err).casefold()
    return _HTTP_403 in text and any(marker in text for marker in _FIREWALL_MARKERS)


def login_failure_reason(err: WebtopLoginError) -> str:
    """Classify a login failure: blocked, transient, captcha, locked or rejected."""
    if is_firewall_block(err):
        return "blocked"
    text = str(err)
    folded = text.casefold()
    if ERROR_CAPTCHA_REQUIRED.casefold() in folded:
        return "captcha_required"
    if ERROR_TOO_MANY_RETRIES.casefold() in folded:
        return "too_many_attempts"
    if any(marker in folded for marker in ("timed out", "request failed", "not json", "login failed (5")):
        return "cannot_connect"
    return "invalid_auth"
