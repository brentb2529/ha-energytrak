"""Binary sensor platform for EnergyTrak."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyTrakConfigEntry
from .coordinator import EnergyTrakCoordinator
from .entity import EnergyTrakEntity

PARALLEL_UPDATES = 0

_NO_FAULT = {"", "none", "no fault", "normal", "ok", "0", "false"}


def _has_fault(data: dict[str, Any]) -> bool:
    """True when an alarm is firing or the controller reports a fault."""
    if (data.get("active_alarm_count") or 0) > 0:
        return True
    fault = data.get("fault_condition")
    if fault is None or isinstance(fault, bool):
        return bool(fault)
    return str(fault).strip().lower() not in _NO_FAULT


@dataclass(frozen=True, kw_only=True)
class EnergyTrakBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an EnergyTrak binary sensor."""

    value_fn: Callable[[dict[str, Any]], bool | None]


BINARY_SENSORS: tuple[EnergyTrakBinarySensorDescription, ...] = (
    EnergyTrakBinarySensorDescription(
        key="running",
        translation_key="running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: data.get("active"),
    ),
    EnergyTrakBinarySensorDescription(
        key="grid_present",
        translation_key="grid_present",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda data: data.get("grid_present"),
    ),
    EnergyTrakBinarySensorDescription(
        key="fault",
        translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_has_fault,
    ),
    # Surfaced as a real entity so automations can suppress alerts that would
    # otherwise fire off month-old snapshot data.
    EnergyTrakBinarySensorDescription(
        key="equipment_data_stale",
        translation_key="equipment_data_stale",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("equipment_data_stale"),
    ),
    EnergyTrakBinarySensorDescription(
        key="smart_mode",
        translation_key="smart_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("smart_mode_enabled"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyTrakConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the EnergyTrak binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        EnergyTrakBinarySensor(coordinator, site_id, description)
        for site_id in coordinator.site_ids
        for description in BINARY_SENSORS
    )


class EnergyTrakBinarySensor(EnergyTrakEntity, BinarySensorEntity):
    """A single EnergyTrak boolean state."""

    entity_description: EnergyTrakBinarySensorDescription

    def __init__(
        self,
        coordinator: EnergyTrakCoordinator,
        site_id: str,
        description: EnergyTrakBinarySensorDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, site_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the current state."""
        value = self.entity_description.value_fn(self.site_data)
        return None if value is None else bool(value)
