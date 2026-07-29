"""The EnergyTrak integration."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnergyTrakAuthError, EnergyTrakClient, EnergyTrakError
from .const import (
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    IMAGE_DIR,
    IMAGE_URL_BASE,
)
from .coordinator import EnergyTrakCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

type EnergyTrakConfigEntry = ConfigEntry[EnergyTrakCoordinator]


async def _async_register_images(hass: HomeAssistant) -> None:
    """Serve the generator artwork so entities can reference it by URL."""
    if hass.data.get(f"{DOMAIN}_images_registered"):
        return
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                IMAGE_URL_BASE,
                str(Path(__file__).parent / IMAGE_DIR),
                # Artwork only changes when the integration is updated.
                cache_headers=True,
            )
        ]
    )
    hass.data[f"{DOMAIN}_images_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: EnergyTrakConfigEntry) -> bool:
    """Set up EnergyTrak from a config entry."""
    await _async_register_images(hass)

    client = EnergyTrakClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        api_key=entry.data[CONF_API_KEY],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
    )

    try:
        await client.async_get_token()
    except EnergyTrakAuthError as err:
        raise ConfigEntryAuthFailed(
            f"EnergyTrak credentials are no longer valid: {err}"
        ) from err
    except EnergyTrakError as err:
        raise ConfigEntryNotReady(f"Could not reach EnergyTrak: {err}") from err

    coordinator = EnergyTrakCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnergyTrakConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: EnergyTrakConfigEntry) -> None:
    """Reload when the options (poll interval, staleness window) change."""
    await hass.config_entries.async_reload(entry.entry_id)
