"""The Webtop (SmartSchool) integration.

Built on pywebtop (https://github.com/t0mer/pywebtop). A data update
coordinator polls the student portal and the sensor platform exposes the
result as native Home Assistant entities.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import WebtopConfigEntry, WebtopCoordinator
from .entity import ensure_account_device

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: WebtopConfigEntry) -> bool:
    """Set up Webtop from a config entry."""
    coordinator = WebtopCoordinator(hass, entry)
    try:
        # Raises ConfigEntryAuthFailed (-> reauth) or ConfigEntryNotReady (-> retry).
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_close()
        raise
    entry.runtime_data = coordinator
    coordinator.account_device_id = ensure_account_device(coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WebtopConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
