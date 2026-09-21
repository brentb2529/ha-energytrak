"""Settings and actions Home Assistant may use on a local B-Infohub bridge.

Everything else this integration creates is read-only, and the generator bus
stays that way: the bridge never writes to the controller. These are settings
and maintenance actions of the BRIDGE itself, and only the ones its firmware
contract lists under `controls` -- an allowlist, enforced both when the
contract is generated and again in LocalBridge._learn_controls.

Deliberately absent: Wi-Fi, the web login, factory reset and every username or
password. A bad Wi-Fi value takes the bridge off the network HA would need to
fix it, and credentials would sit in HA as plain entity states.

A cloud-only install, or a bridge on firmware that predates a control, gets no
entity at all rather than one that is permanently unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EnergyTrakCoordinator
from .entity import EnergyTrakEntity

_CATEGORIES = {"config": EntityCategory.CONFIG, "diagnostic": EntityCategory.DIAGNOSTIC}


def async_setup_controls(
    entry: Any,
    kind: str,
    factory: Callable[[EnergyTrakCoordinator, str, str, dict[str, Any]], Entity],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create an entity for each control of `kind` the bridge reports.

    Re-checked on every refresh, like the sensors: the bridge may not have
    connected yet when the platform loads, and its controls are only known
    once it has listed its entities.
    """
    coordinator: EnergyTrakCoordinator = entry.runtime_data
    local = coordinator.local
    if local is None:
        return

    created: set[str] = set()

    @callback
    def _discover() -> None:
        site_id = coordinator.local_site_id
        if site_id is None:
            return
        new = [
            factory(coordinator, site_id, oid, meta)
            for oid, meta in local.controls.items()
            if meta["kind"] == kind and oid not in created
        ]
        created.update(entity.control_oid for entity in new)
        if new:
            async_add_entities(new)

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))


class EnergyTrakControlEntity(EnergyTrakEntity):
    """Base for one bridge control."""

    _attr_attribution = None

    def __init__(
        self,
        coordinator: EnergyTrakCoordinator,
        site_id: str,
        object_id: str,
        meta: dict[str, Any],
    ) -> None:
        """Initialise from the contract's metadata for this control."""
        super().__init__(coordinator, site_id, object_id)
        self.control_oid = object_id
        self._meta = meta
        self._attr_name = meta.get("name") or object_id.replace("_", " ").title()
        self._attr_entity_category = _CATEGORIES.get(meta.get("category") or "")
        if icon := meta.get("icon"):
            self._attr_icon = icon

    @property
    def available(self) -> bool:
        """Available whenever the bridge is connected.

        Deliberately NOT tied to bus health or to the coordinator's data: these
        belong to the bridge, and they have to work with the generator's bus
        down or the cloud unreachable -- that is when you would reach for them.
        """
        local = self.coordinator.local
        return local is not None and local.connected

    @property
    def raw_value(self) -> Any:
        """The bridge's current value for this control."""
        return self.coordinator.local.raw(self.control_oid)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Identity attributes, as on every EnergyTrak entity."""
        return dict(self.base_state_attributes)

    async def async_send(self, value: Any = None) -> None:
        """Send to the bridge, raising so the UI shows a failure."""
        if not await self.coordinator.local.async_control(self.control_oid, value):
            raise HomeAssistantError(
                f"Could not reach the B-Infohub bridge to change {self._attr_name}"
            )
        self.async_write_ha_state()
