"""Base entities and device descriptions."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, WEBTOP_PORTAL_URL
from .coordinator import WebtopCoordinator
from .models import Student


def account_device_identifier(entry_id: str) -> str:
    return entry_id


def account_device_info(coordinator: WebtopCoordinator) -> DeviceInfo:
    """Device info for the account-level device (the parent of every student device)."""
    entry = coordinator.config_entry
    account = coordinator.data.account
    return DeviceInfo(
        identifiers={(DOMAIN, account_device_identifier(entry.entry_id))},
        name=f"Webtop {account.name or entry.data['username']}".strip(),
        manufacturer=MANUFACTURER,
        model="Webtop account",
        configuration_url=WEBTOP_PORTAL_URL,
    )


def ensure_account_device(coordinator: WebtopCoordinator) -> str:
    """Register the account device now, so student devices can point at it by id."""
    info = account_device_info(coordinator)
    device = dr.async_get(coordinator.hass).async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers=info["identifiers"],
        name=info["name"],
        manufacturer=info["manufacturer"],
        model=info["model"],
        configuration_url=info["configuration_url"],
    )
    return device.id


def student_device_identifier(entry_id: str, key: str) -> str:
    return f"{entry_id}_student_{key}"


class WebtopEntity(CoordinatorEntity[WebtopCoordinator]):
    """Common base: entity names come from translation keys."""

    _attr_has_entity_name = True


class WebtopAccountEntity(WebtopEntity):
    """An entity on the account-level device."""

    def __init__(self, coordinator: WebtopCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = account_device_info(coordinator)


class WebtopStudentEntity(WebtopEntity):
    """An entity on a per-student device."""

    def __init__(self, coordinator: WebtopCoordinator, student: Student) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.student_key = student.key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, student_device_identifier(entry.entry_id, student.key))},
            name=student.name or f"Student {student.key}",
            manufacturer=MANUFACTURER,
            model=f"Webtop student{f' ({student.school_name})' if student.school_name else ''}",
            via_device_id=coordinator.account_device_id,
            configuration_url=WEBTOP_PORTAL_URL,
        )

    @property
    def student(self) -> Student | None:
        return self.coordinator.data.student(self.student_key)

    @property
    def available(self) -> bool:
        return super().available and self.student is not None
