"""Button platform: refresh from the portal on demand."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WebtopConfigEntry, WebtopCoordinator
from .entity import WebtopAccountEntity


class WebtopRefreshButton(WebtopAccountEntity, ButtonEntity):
    """Poll the portal now instead of waiting for the next scheduled poll.

    It also works during the night pause, when scheduled polls are skipped.
    """

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:cloud-refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WebtopCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_refresh"

    @property
    def available(self) -> bool:
        return True  # the whole point is to be usable when the last poll failed

    async def async_press(self) -> None:
        await self.coordinator.async_force_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WebtopConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([WebtopRefreshButton(entry.runtime_data)])
