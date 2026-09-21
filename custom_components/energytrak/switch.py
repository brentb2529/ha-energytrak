"""Feature switches on a local B-Infohub bridge. See controls.py."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyTrakConfigEntry
from .controls import EnergyTrakControlEntity, async_setup_controls


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyTrakConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bridge's switch controls."""
    async_setup_controls(entry, "switch", EnergyTrakBridgeSwitch, async_add_entities)


class EnergyTrakBridgeSwitch(EnergyTrakControlEntity, SwitchEntity):
    """One on/off bridge feature."""

    @property
    def is_on(self) -> bool | None:
        """The bridge's current state."""
        value = self.raw_value
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the feature on."""
        await self.async_send(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the feature off."""
        await self.async_send(False)
