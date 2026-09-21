"""Maintenance actions on a local B-Infohub bridge. See controls.py."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyTrakConfigEntry
from .controls import EnergyTrakControlEntity, async_setup_controls
from .coordinator import EnergyTrakCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyTrakConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bridge's buttons."""
    async_setup_controls(entry, "button", EnergyTrakBridgeButton, async_add_entities)


class EnergyTrakBridgeButton(EnergyTrakControlEntity, ButtonEntity):
    """One bridge action."""

    def __init__(
        self,
        coordinator: EnergyTrakCoordinator,
        site_id: str,
        object_id: str,
        meta: dict[str, Any],
    ) -> None:
        """Mark the restart button as one, so HA words and styles it that way."""
        super().__init__(coordinator, site_id, object_id, meta)
        if object_id == "restart_bridge":
            self._attr_device_class = ButtonDeviceClass.RESTART

    async def async_press(self) -> None:
        """Press it on the bridge."""
        await self.async_send()
