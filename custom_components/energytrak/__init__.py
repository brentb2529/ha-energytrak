"""The EnergyTrak integration."""

from __future__ import annotations

import logging
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
    CONF_LOCAL_HOST,
    CONF_LOCAL_KEY,
    CONF_LOCAL_PORT,
    CONF_REFRESH_TOKEN,
    DEFAULT_LOCAL_PORT,
    DOMAIN,
    IMAGE_DIR,
    IMAGE_URL_BASE,
)
from .coordinator import EnergyTrakCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

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

    # An entry may configure the cloud account, a local bridge, or both. Only
    # build and authenticate the cloud client when there is an account -- a
    # bridge-only user has no EnergyTrak credentials and never will.
    client = None
    if entry.data.get(CONF_EMAIL):
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

    # Optional local bridge. Started BEFORE the first refresh so that a site
    # whose cloud feed is dormant still has live values on the very first
    # update, rather than a screen of "unknown" until the next poll.
    local = None
    if host := entry.data.get(CONF_LOCAL_HOST):
        from .local_source import LocalBridge

        local = LocalBridge(
            hass,
            host,
            entry.data.get(CONF_LOCAL_PORT, DEFAULT_LOCAL_PORT),
            entry.data.get(CONF_LOCAL_KEY),
        )
        try:
            await local.async_start()
        except Exception as err:  # noqa: BLE001
            # A missing bridge must never block the cloud path -- that would
            # turn an optional accessory into a hard dependency.
            _LOGGER.warning("Local B-Infohub bridge at %s did not start: %s", host, err)
            local = None
        else:
            entry.async_on_unload(local.async_stop)

    if client is None and local is None:
        # Neither half survived setup. With a cloud account this would have
        # raised above; here it means a bridge-only entry whose bridge did not
        # start, which is a retry, not a broken configuration.
        raise ConfigEntryNotReady(
            "No EnergyTrak account configured and the local bridge is unreachable"
        )

    coordinator = EnergyTrakCoordinator(hass, entry, client, local=local)
    await coordinator.async_config_entry_first_refresh()

    # Attach AFTER the first refresh, so coordinator.data exists before any
    # urgent change can arrive. Attaching earlier would simply drop the first
    # one, which is the worst possible one to drop: the bridge replays its
    # whole state on connect, and a generator already faulted when Home
    # Assistant starts would go unannounced until the next poll.
    coordinator.async_start_urgent_updates()
    entry.async_on_unload(coordinator.async_stop_urgent_updates)

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
