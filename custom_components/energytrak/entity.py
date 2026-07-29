"""Shared base entity for EnergyTrak."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import EnergyTrakCoordinator


class EnergyTrakEntity(CoordinatorEntity[EnergyTrakCoordinator]):
    """Base entity bound to one EnergyTrak site."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EnergyTrakCoordinator, site_id: str, key: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._site_id = site_id
        self._attr_unique_id = f"{site_id}_{key}"

    @property
    def site_data(self) -> dict[str, Any]:
        """Latest normalised payload for this site."""
        return (self.coordinator.data or {}).get(self._site_id, {})

    @property
    def available(self) -> bool:
        """Return True when we have data for this site."""
        return super().available and bool(self.site_data)

    @property
    def device_info(self) -> DeviceInfo:
        """Represent the site as a single generator device."""
        data = self.site_data
        return DeviceInfo(
            identifiers={(DOMAIN, self._site_id)},
            name=data.get("name") or self._site_id,
            manufacturer=data.get("make") or MANUFACTURER,
            model=data.get("model"),
            serial_number=data.get("serial_number"),
            sw_version=data.get("firmware_version"),
        )
