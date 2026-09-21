"""Numeric settings on a local B-Infohub bridge. See controls.py."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyTrakConfigEntry
from .controls import EnergyTrakControlEntity, async_setup_controls
from .coordinator import EnergyTrakCoordinator

_MODES = {"slider": NumberMode.SLIDER, "box": NumberMode.BOX}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyTrakConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bridge's number controls."""
    async_setup_controls(entry, "number", EnergyTrakBridgeNumber, async_add_entities)


class EnergyTrakBridgeNumber(EnergyTrakControlEntity, NumberEntity):
    """One numeric bridge setting."""

    def __init__(
        self,
        coordinator: EnergyTrakCoordinator,
        site_id: str,
        object_id: str,
        meta: dict[str, Any],
    ) -> None:
        """Initialise the range and mode from the contract."""
        super().__init__(coordinator, site_id, object_id, meta)
        self._attr_native_min_value = meta["min"]
        self._attr_native_max_value = meta["max"]
        self._attr_native_step = meta["step"]
        self._attr_native_unit_of_measurement = meta.get("unit")
        self._attr_mode = _MODES.get(meta.get("mode") or "", NumberMode.AUTO)

    @property
    def native_value(self) -> float | None:
        """The bridge's current value."""
        value = self.raw_value
        if not isinstance(value, (int, float)) or value != value:
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Send the new value to the bridge."""
        await self.async_send(value)
