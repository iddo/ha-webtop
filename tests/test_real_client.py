"""Checks against the real pywebtop client (no network).

pywebtop builds an httpx client in its constructor, and httpx reads the TLS
trust store from disk while doing so. Home Assistant flags that as a blocking
call when it happens on the event loop, but its own detector skips TLS calls
under test, so these tests watch ``load_verify_locations`` directly.
"""

from __future__ import annotations

import ssl
import threading
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from webtop import WebtopClient, WebtopLoginError

from custom_components.webtop.auth import async_create_client, compute_login_data
from custom_components.webtop.config_flow import validate_login
from custom_components.webtop.const import CONF_PASSWORD, CONF_USERNAME


@pytest.fixture
def tls_loads() -> Generator[list[bool]]:
    """Record, per trust-store load, whether it ran on the event-loop thread."""
    loop_thread = threading.get_ident()
    on_loop: list[bool] = []
    original = ssl.SSLContext.load_verify_locations

    def spy(self: ssl.SSLContext, *args, **kwargs):
        on_loop.append(threading.get_ident() == loop_thread)
        return original(self, *args, **kwargs)

    with patch.object(ssl.SSLContext, "load_verify_locations", spy):
        yield on_loop


async def test_real_client_is_created_off_the_event_loop(hass: HomeAssistant, tls_loads: list[bool]) -> None:
    with patch("custom_components.webtop.auth.async_fetch_login_counter", AsyncMock(return_value=1)):
        client = await async_create_client(hass, "someone", "pw")
    try:
        assert isinstance(client, WebtopClient)
        assert client._data == compute_login_data("someone", 1)
        assert client.is_logged_in is False
    finally:
        await client.close()
    assert tls_loads, "expected pywebtop to load a TLS trust store; the spy is not hooked up"
    assert not any(tls_loads)


async def test_validate_login_with_real_client_does_not_block(hass: HomeAssistant, tls_loads: list[bool]) -> None:
    with (
        patch("custom_components.webtop.auth.async_fetch_login_counter", AsyncMock(return_value=0)),
        patch.object(WebtopClient, "login", AsyncMock(side_effect=WebtopLoginError("Login returned status=false"))),
        pytest.raises(WebtopLoginError),
    ):
        await validate_login(hass, {CONF_USERNAME: "someone", CONF_PASSWORD: "pw"})
    assert tls_loads and not any(tls_loads)
