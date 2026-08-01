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
from .const import IMAGE_URL_BASE
from .coordinator import EnergyTrakCoordinator
from .entity import EnergyTrakEntity, async_setup_reported_entities

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


def _generator_picture(data: dict[str, Any]) -> str:
    """Pick the artwork that matches the generator's current state."""
    if not data:
        return f"{IMAGE_URL_BASE}/generator_unavailable.svg"
    if _has_fault(data):
        return f"{IMAGE_URL_BASE}/generator_fault.svg"
    if data.get("active"):
        return f"{IMAGE_URL_BASE}/generator_running.svg"
    return f"{IMAGE_URL_BASE}/generator_idle.svg"


@dataclass(frozen=True, kw_only=True)
class EnergyTrakBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an EnergyTrak binary sensor."""

    value_fn: Callable[[dict[str, Any]], bool | None]
    picture_fn: Callable[[dict[str, Any]], str | None] | None = None
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Created even when the value is missing: these are the entities that
    # explain why other data is absent, so they must exist in exactly that case.
    always: bool = False


BINARY_SENSORS: tuple[EnergyTrakBinarySensorDescription, ...] = (
    EnergyTrakBinarySensorDescription(
        key="running",
        translation_key="running",
        always=True,
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: data.get("active"),
        # Carries the generator artwork, so a picture-entity card (or the
        # more-info dialog) shows the unit and its state at a glance.
        picture_fn=_generator_picture,
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
        always=True,
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_has_fault,
    ),
    # Surfaced as a real entity so automations can suppress alerts that would
    # otherwise fire off month-old snapshot data.
    EnergyTrakBinarySensorDescription(
        key="equipment_data_stale",
        translation_key="equipment_data_stale",
        always=True,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("equipment_data_stale"),
        # Unknown is a real answer here, not a gap: on a controller whose
        # timestamp is frozen we cannot tell a dead feed from a stuck clock
        # until the block is seen to move. This says which case you are in.
        attributes_fn=lambda data: {
            "freshness_basis": data.get("equipment_freshness_basis"),
            "reported_timestamp": data.get("equipment_reported_timestamp"),
            "content_last_seen": data.get("equipment_content_seen_at"),
        },
    ),
    # Site-level malfunction flag, raised by EnergyTrak's own outage manager
    # rather than by the controller.
    EnergyTrakBinarySensorDescription(
        key="malfunction",
        translation_key="malfunction",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: data.get("has_malfunction"),
    ),
    EnergyTrakBinarySensorDescription(
        key="monitor_online",
        translation_key="monitor_online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("monitor_online"),
    ),
    EnergyTrakBinarySensorDescription(
        key="smart_mode",
        translation_key="smart_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("smart_mode_enabled"),
        # Smart mode has a detection strategy ("AUTO") worth carrying alongside
        # the on/off, and it has nowhere else to live.
        attributes_fn=lambda data: {"detection": data.get("smart_mode_detection")},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyTrakConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the EnergyTrak binary sensors."""
    coordinator = entry.runtime_data
    entry.async_on_unload(
        async_setup_reported_entities(
            coordinator, BINARY_SENSORS, EnergyTrakBinarySensor, async_add_entities
        )
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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Identity attributes, plus whatever the description adds."""
        attributes = dict(self.base_state_attributes)
        if self.entity_description.attributes_fn is not None:
            attributes.update(self.entity_description.attributes_fn(self.site_data))
        return attributes

    @property
    def entity_picture(self) -> str | None:
        """Return state-matched artwork, when this entity carries any."""
        if self.entity_description.picture_fn is None:
            return None
        return self.entity_description.picture_fn(self.site_data)
