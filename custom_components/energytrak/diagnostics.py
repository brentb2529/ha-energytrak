"""Diagnostics support for EnergyTrak."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EnergyTrakConfigEntry
from .const import CONF_API_KEY, CONF_EMAIL, CONF_REFRESH_TOKEN

TO_REDACT = {
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    "latitude",
    "longitude",
    "serial_number",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EnergyTrakConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "sites": {
            site_id: async_redact_data(data, TO_REDACT)
            for site_id, data in (coordinator.data or {}).items()
        },
    }
