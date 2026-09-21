"""Text settings on a local B-Infohub bridge. See controls.py.

Never a password: the contract allowlist keeps credentials out, because a text
entity's value is a plain state in the UI and the recorder.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.text import TextEntity, TextMode
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
    """Set up the bridge's text controls."""
    async_setup_controls(entry, "text", EnergyTrakBridgeText, async_add_entities)


class EnergyTrakBridgeText(EnergyTrakControlEntity, TextEntity):
    """One text bridge setting."""

    _attr_mode = TextMode.TEXT

    def __init__(
        self,
        coordinator: EnergyTrakCoordinator,
        site_id: str,
        object_id: str,
        meta: dict[str, Any],
    ) -> None:
        """Initialise the length limits from the contract."""
        super().__init__(coordinator, site_id, object_id, meta)
        self._attr_native_min = meta.get("min_length", 0)
        self._attr_native_max = meta.get("max_length", 255)

    @property
    def native_value(self) -> str | None:
        """The bridge's current value."""
        value = self.raw_value
        return None if value is None else str(value)

    async def async_set_value(self, value: str) -> None:
        """Send the new value to the bridge."""
        await self.async_send(value)
