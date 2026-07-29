"""Polling coordinator for EnergyTrak sites."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    EnergyTrakAuthError,
    EnergyTrakClient,
    EnergyTrakError,
)
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SITE_IDS,
    CONF_SITE_NAMES,
    CONF_STALE_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_MINUTES,
    DEVICE_COLLECTION,
    DOMAIN,
    SITE_COLLECTION,
)
from .normalize import extract_device_ids, normalize_device

_LOGGER = logging.getLogger(__name__)

# Values that a healthy unit is expected to move within an hour or so. If the
# whole tuple is byte-identical poll after poll, EnergyTrak itself is stuck —
# which is a different failure from "our polling stopped", and users need to
# be able to tell them apart.
_FRESHNESS_KEYS = (
    "clean_state_last_updated",
    "equipment_data_timestamp",
    "battery_voltage",
    "engine_hours",
    "status",
    "active",
    "status_color",
)


@dataclass
class SiteRuntime:
    """Per-site cached state that survives a failed poll."""

    site_name: str | None = None
    device_id: str | None = None
    last_received_at: datetime | None = None
    last_changed_at: datetime | None = None
    signature: tuple[Any, ...] | None = field(default=None, repr=False)


class EnergyTrakCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetch every configured site once per interval."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EnergyTrakClient,
    ) -> None:
        """Initialise the coordinator."""
        self.client = client
        self.site_ids: list[str] = list(entry.data.get(CONF_SITE_IDS, []))
        site_names: dict[str, str] = entry.data.get(CONF_SITE_NAMES, {})
        self.sites: dict[str, SiteRuntime] = {
            site_id: SiteRuntime(site_name=site_names.get(site_id))
            for site_id in self.site_ids
        }

        # A prolonged vendor outage would otherwise log the same warning on
        # every poll forever, so only report when the failure changes.
        self._last_error: str | None = None

        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self._stale_seconds = (
            int(entry.options.get(CONF_STALE_MINUTES, DEFAULT_STALE_MINUTES)) * 60
        )

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Poll every configured site."""
        results: dict[str, dict[str, Any]] = dict(self.data or {})
        errors: list[str] = []

        for site_id in self.site_ids:
            try:
                results[site_id] = await self._async_fetch_site(site_id)
            except EnergyTrakAuthError as err:
                raise ConfigEntryAuthFailed(
                    f"EnergyTrak authentication failed: {err}"
                ) from err
            except EnergyTrakError as err:
                # Transient upstream blips are common. Keep serving the last
                # good payload for this site — `last_received_at` deliberately
                # does *not* advance, so the staleness sensors keep climbing
                # and tell the honest story.
                errors.append(f"{site_id}: {err}")
                _LOGGER.debug("Poll failed for site %s: %s", site_id, err)

        if errors and not results:
            raise UpdateFailed("; ".join(errors))

        summary = "; ".join(errors) if errors else None
        if summary and summary != self._last_error:
            _LOGGER.warning(
                "Some EnergyTrak sites failed to update, serving last known values: %s",
                summary,
            )
        elif summary:
            _LOGGER.debug("EnergyTrak sites still failing: %s", summary)
        elif self._last_error:
            _LOGGER.info("EnergyTrak polling recovered")
        self._last_error = summary

        return results

    async def _async_fetch_site(self, site_id: str) -> dict[str, Any]:
        """Read one site and its first device, normalised."""
        runtime = self.sites.setdefault(site_id, SiteRuntime())

        # The device id is stable, so only resolve it once per restart.
        if runtime.device_id is None:
            site_doc = await self.client.async_get_document(SITE_COLLECTION, site_id)
            device_ids = extract_device_ids(site_doc)
            if not device_ids:
                raise EnergyTrakError(f"no device linked to site {site_id}")
            runtime.device_id = device_ids[0]

        device_doc = await self.client.async_get_document(
            DEVICE_COLLECTION, runtime.device_id
        )
        data = normalize_device(
            site_id,
            runtime.site_name,
            device_doc,
            stale_threshold_seconds=self._stale_seconds,
        )

        now = datetime.now(UTC)
        signature = tuple(str(data.get(key)) for key in _FRESHNESS_KEYS)
        runtime.last_received_at = now
        if runtime.signature != signature:
            runtime.signature = signature
            runtime.last_changed_at = now

        data["last_received_at"] = runtime.last_received_at
        data["last_changed_at"] = runtime.last_changed_at
        return data
