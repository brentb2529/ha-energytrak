"""Polling coordinator for EnergyTrak sites."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
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
    FRESHNESS_STORE_VERSION,
    SITE_COLLECTION,
)
from .normalize import EquipmentFreshness, extract_device_ids, normalize_site

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
    "last_exercise_at",
)


@dataclass
class SiteRuntime:
    """Per-site cached state that survives a failed poll."""

    site_name: str | None = None
    device_ids: list[str] = field(default_factory=list)
    last_received_at: datetime | None = None
    last_changed_at: datetime | None = None
    signature: tuple[Any, ...] | None = field(default=None, repr=False)
    freshness: EquipmentFreshness = field(default_factory=EquipmentFreshness)


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

        # Liveness is established by watching the equipment block change
        # between polls, which on an idle generator can take until the next
        # weekly exercise. Losing that observation on every restart would
        # mean a week of falsely-stale readings, so it is persisted.
        self._store: Store[dict[str, Any]] = Store(
            hass, FRESHNESS_STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.freshness"
        )
        self._store_loaded = False

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

    async def _async_load_freshness(self) -> None:
        """Restore what earlier runs learned about equipment liveness."""
        self._store_loaded = True
        stored = await self._store.async_load() or {}
        for site_id, record in (stored.get("sites") or {}).items():
            runtime = self.sites.setdefault(site_id, SiteRuntime())
            seen_raw = record.get("seen_at")
            seen_at: datetime | None = None
            if seen_raw:
                try:
                    seen_at = datetime.fromisoformat(seen_raw)
                except ValueError:
                    seen_at = None
            runtime.freshness = EquipmentFreshness(
                signature=record.get("signature"), seen_at=seen_at
            )

    async def _async_save_freshness(self) -> None:
        """Persist the current signature/observation for every site."""
        await self._store.async_save(
            {
                "sites": {
                    site_id: {
                        "signature": runtime.freshness.signature,
                        "seen_at": (
                            runtime.freshness.seen_at.isoformat()
                            if runtime.freshness.seen_at
                            else None
                        ),
                    }
                    for site_id, runtime in self.sites.items()
                    if runtime.freshness.signature
                }
            }
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Poll every configured site."""
        if not self._store_loaded:
            await self._async_load_freshness()

        results: dict[str, dict[str, Any]] = dict(self.data or {})
        errors: list[str] = []
        freshness_changed = False

        for site_id in self.site_ids:
            before = self.sites.get(site_id, SiteRuntime()).freshness
            try:
                results[site_id] = await self._async_fetch_site(site_id)
                if self.sites[site_id].freshness != before:
                    freshness_changed = True
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

        # Rare by construction — the signature only moves when the vendor
        # actually pushes new equipment telemetry — so this is not a
        # per-poll write.
        if freshness_changed:
            await self._async_save_freshness()

        return results

    async def _async_fetch_site(self, site_id: str) -> dict[str, Any]:
        """Read one site and all of its devices, normalised into one payload."""
        runtime = self.sites.setdefault(site_id, SiteRuntime())

        # The site document is read every poll, not just to discover devices:
        # it carries the exercise history, which is the only record that the
        # generator ran when the equipment feed has gone dormant.
        site_doc = await self.client.async_get_document(SITE_COLLECTION, site_id)
        device_ids = extract_device_ids(site_doc) or runtime.device_ids
        if not device_ids:
            raise EnergyTrakError(f"no device linked to site {site_id}")
        runtime.device_ids = device_ids

        # A site's devices hold differently-aged copies of the telemetry, so
        # fetch them all and let normalize_site pick the best source per field.
        device_docs = await asyncio.gather(
            *(
                self.client.async_get_document(DEVICE_COLLECTION, device_id)
                for device_id in device_ids
            )
        )
        data = normalize_site(
            site_id,
            runtime.site_name,
            list(device_docs),
            stale_threshold_seconds=self._stale_seconds,
            site_doc=site_doc,
            freshness=runtime.freshness,
        )
        runtime.freshness = EquipmentFreshness(
            signature=data.get("equipment_signature"),
            seen_at=data.get("equipment_content_seen_at"),
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
