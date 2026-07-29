"""Shared base entity for EnergyTrak."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import EnergyTrakCoordinator


def async_setup_reported_entities(
    coordinator: EnergyTrakCoordinator,
    descriptions: Iterable[Any],
    factory: Callable[[EnergyTrakCoordinator, str, Any], Entity],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> Callable[[], None]:
    """Create entities only for fields this account actually reports.

    Not every generator sends every field. One real unit has never reported
    RPM, output voltage, load or power factor — not once in 450,000 polls —
    because its controller does not upload equipment telemetry at all.
    Creating those entities anyway leaves a row of permanently unknown values
    that look like a broken integration rather than an absent feature.

    So a field earns its entity by being reported at least once. Descriptions
    flagged ``always`` are exempt: those are the diagnostics that explain *why*
    data is missing, which have to exist precisely when everything else does
    not.

    The check repeats on every refresh, so if a dormant feed wakes up the new
    entities appear on their own without a reload. Returns the unsubscribe
    callback for the listener.
    """
    created: set[tuple[str, str]] = set()

    @callback
    def _discover() -> None:
        new: list[Entity] = []
        for site_id in coordinator.site_ids:
            data = (coordinator.data or {}).get(site_id) or {}
            if not data:
                continue
            for description in descriptions:
                token = (site_id, description.key)
                if token in created:
                    continue
                if not getattr(description, "always", False):
                    try:
                        if description.value_fn(data) is None:
                            continue
                    except Exception:  # noqa: BLE001 - a broken reader must not
                        continue  # block the rest of the platform
                created.add(token)
                new.append(factory(coordinator, site_id, description))
        if new:
            async_add_entities(new)

    _discover()
    return coordinator.async_add_listener(_discover)


class EnergyTrakEntity(CoordinatorEntity[EnergyTrakCoordinator]):
    """Base entity bound to one EnergyTrak site."""

    _attr_has_entity_name = True
    _attr_attribution = "Data provided by EnergyTrak"

    def __init__(self, coordinator: EnergyTrakCoordinator, site_id: str, key: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._site_id = site_id
        self._field_key = key
        self._attr_unique_id = f"{site_id}_{key}"

    @property
    def base_state_attributes(self) -> dict[str, Any]:
        """Machine-readable identity, carried on every entity.

        Home Assistant's REST `/api/states` exposes no integration, device or
        registry information, so an external consumer (a wall-panel dashboard,
        say) has no reliable way to tell which entities belong to this
        integration or which measurement each one is. Friendly names are not
        usable for that — the user can rename them, and entity ids shift when a
        device is assigned to an area. These two keys are stable for the life
        of the entity and cost almost nothing to carry.
        """
        return {"energytrak_site": self._site_id, "energytrak_field": self._field_key}

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
