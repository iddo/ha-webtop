"""Tests for the login helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from webtop import WebtopLoginError

from custom_components.webtop.auth import (
    async_fetch_login_counter,
    compute_login_data,
    is_data_override,
    login_failure_reason,
)
from custom_components.webtop.const import LEGACY_DEFAULT_DATA


@pytest.mark.parametrize(
    ("username", "counter", "expected"),
    [
        ("parent", 0, "WYZfgP1Q5aHzd5vDjmbLCA=="),
        ("parent", 2, "ojVkZCCcLhKxIflHZm16lg=="),
        ("a-much-longer-username", 0, "taP4VewVXBjwJ6+AMKoZKWd7tUBk5F4sV/2J4nZqcnk="),
    ],
)
def test_compute_login_data_vectors(username: str, counter: int, expected: str) -> None:
    """AES-256-CBC over JSON(username + counter), as the Webtop web client does."""
    assert compute_login_data(username, counter) == expected


def test_data_depends_on_username() -> None:
    assert compute_login_data("alice1") != compute_login_data("alice2")
    assert compute_login_data("alice1") != LEGACY_DEFAULT_DATA


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), (LEGACY_DEFAULT_DATA, False), ("captured==", True)],
)
def test_is_data_override(value, expected) -> None:
    assert is_data_override(value) is expected


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("Login returned status=false. errorDescription=None, errorId=None", "invalid_auth"),
        ("Login returned status=false. errorDescription='CaptchaRequired', errorId=None", "captcha_required"),
        ("Login returned status=false. errorDescription='MoreThan5Retries', errorId=3", "too_many_attempts"),
        ("Login request timed out: x", "cannot_connect"),
        ("Login request failed: dns", "cannot_connect"),
        ("Login failed (503): unavailable", "cannot_connect"),
        ("Login failed (401): nope", "invalid_auth"),
    ],
)
def test_login_failure_reason(message: str, reason: str) -> None:
    assert login_failure_reason(WebtopLoginError(message)) == reason


@pytest.mark.parametrize(
    ("body", "expected"),
    [(2, 2), (0, 0), ({"status": True, "data": 1}, 1), (None, 0), ("x", 0), (True, 0), (-1, 0), ({"data": None}, 0)],
)
async def test_fetch_login_counter(hass: HomeAssistant, body, expected) -> None:
    response = MagicMock()
    response.json.return_value = body
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    with patch("custom_components.webtop.auth.get_async_client", return_value=client):
        assert await async_fetch_login_counter(hass) == expected
    url = client.post.call_args.args[0]
    assert url.endswith("/server/api/user/getInput")
    assert client.post.call_args.kwargs["json"] == {}


async def test_fetch_login_counter_survives_network_errors(hass: HomeAssistant) -> None:
    client = MagicMock()
    client.post = AsyncMock(side_effect=OSError("down"))
    with patch("custom_components.webtop.auth.get_async_client", return_value=client):
        assert await async_fetch_login_counter(hass) == 0
