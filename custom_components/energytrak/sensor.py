"""Sensor platform for EnergyTrak."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyTrakConfigEntry
from .coordinator import EnergyTrakCoordinator
from .entity import EnergyTrakEntity, async_setup_reported_entities

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EnergyTrakSensorDescription(SensorEntityDescription):
    """Describes an EnergyTrak sensor."""

    value_fn: Callable[[dict[str, Any]], Any] = lambda data: None
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Created even when the value is missing: these are the entities that
    # explain why other data is absent, so they must exist in exactly that case.
    always: bool = False


def _key(key: str) -> Callable[[dict[str, Any]], Any]:
    """Read a normalised field straight through."""
    return lambda data: data.get(key)


def _timestamp(key: str) -> Callable[[dict[str, Any]], datetime | None]:
    """Read a field that should surface as a timestamp entity."""

    def _read(data: dict[str, Any]) -> datetime | None:
        value = data.get(key)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if isinstance(value, (int, float)):
            # Epoch milliseconds (EnergyTrak's heartbeat format).
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        return None

    return _read


SENSORS: tuple[EnergyTrakSensorDescription, ...] = (
    # ---- Primary readings -------------------------------------------
    EnergyTrakSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_key("battery_voltage"),
    ),
    EnergyTrakSensorDescription(
        key="engine_hours",
        translation_key="engine_hours",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=_key("engine_hours"),
    ),
    EnergyTrakSensorDescription(
        key="status",
        translation_key="status",
        always=True,
        value_fn=_key("status"),
    ),
    EnergyTrakSensorDescription(
        key="grid_status",
        translation_key="grid_status",
        value_fn=_key("grid_status"),
    ),
    EnergyTrakSensorDescription(
        key="load_power",
        translation_key="load_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_key("load_power"),
    ),
    EnergyTrakSensorDescription(
        key="output_voltage",
        translation_key="output_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_key("output_voltage"),
    ),
    EnergyTrakSensorDescription(
        key="grid_voltage",
        translation_key="grid_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_key("grid_voltage"),
    ),
    EnergyTrakSensorDescription(
        key="generator_frequency",
        translation_key="generator_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_key("generator_frequency"),
    ),
    EnergyTrakSensorDescription(
        key="grid_frequency",
        translation_key="grid_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_key("grid_frequency"),
    ),
    EnergyTrakSensorDescription(
        key="engine_speed",
        translation_key="engine_speed",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:engine",
        value_fn=_key("engine_speed"),
    ),
    EnergyTrakSensorDescription(
        key="active_alarm_count",
        translation_key="active_alarm_count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alarm-light",
        value_fn=_key("active_alarm_count"),
        attributes_fn=lambda data: {
            "active_alarms": data.get("active_alarms") or [],
            "alarm_flags": data.get("alarm_flags") or {},
        },
    ),
    # ---- Exercise history -------------------------------------------
    # From the site document, not the devices. On a unit whose equipment feed
    # has gone dormant these are the only proof the generator still runs.
    EnergyTrakSensorDescription(
        key="last_exercise",
        translation_key="last_exercise",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-check",
        value_fn=_timestamp("last_exercise_at"),
        attributes_fn=lambda data: {
            "duration_seconds": data.get("last_exercise_duration_seconds"),
            "run_session": data.get("run_session"),
        },
    ),
    EnergyTrakSensorDescription(
        key="last_exercise_duration",
        translation_key="last_exercise_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=1,
        icon="mdi:timer-outline",
        value_fn=_key("last_exercise_duration_seconds"),
    ),
    EnergyTrakSensorDescription(
        key="next_exercise",
        translation_key="next_exercise",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
        value_fn=_timestamp("next_exercise_due"),
        attributes_fn=lambda data: {
            "interval_days": data.get("exercise_interval_days")
        },
    ),
    # ---- Counters ---------------------------------------------------
    EnergyTrakSensorDescription(
        key="starts_count",
        translation_key="starts_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:restart",
        value_fn=_key("starts_count"),
    ),
    EnergyTrakSensorDescription(
        key="trips_count",
        translation_key="trips_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:flash-alert",
        value_fn=_key("trips_count"),
    ),
    # ---- Per-phase detail (off by default; split-phase units only) ---
    EnergyTrakSensorDescription(
        key="output_voltage_l1n",
        translation_key="output_voltage_l1n",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("output_voltage_l1n"),
    ),
    EnergyTrakSensorDescription(
        key="output_voltage_l2n",
        translation_key="output_voltage_l2n",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("output_voltage_l2n"),
    ),
    EnergyTrakSensorDescription(
        key="load_l1_power",
        translation_key="load_l1_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_l1_power"),
    ),
    EnergyTrakSensorDescription(
        key="load_l2_power",
        translation_key="load_l2_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_l2_power"),
    ),
    EnergyTrakSensorDescription(
        key="load_l1_current",
        translation_key="load_l1_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_l1_current"),
    ),
    EnergyTrakSensorDescription(
        key="load_l2_current",
        translation_key="load_l2_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_l2_current"),
    ),
    EnergyTrakSensorDescription(
        key="load_apparent_power",
        translation_key="load_apparent_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_apparent_power"),
    ),
    EnergyTrakSensorDescription(
        key="load_l1_apparent_power",
        translation_key="load_l1_apparent_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_l1_apparent_power"),
    ),
    EnergyTrakSensorDescription(
        key="load_l2_apparent_power",
        translation_key="load_l2_apparent_power",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_l2_apparent_power"),
    ),
    EnergyTrakSensorDescription(
        key="load_reactive_power",
        translation_key="load_reactive_power",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_reactive_power"),
    ),
    EnergyTrakSensorDescription(
        key="load_l1_reactive_power",
        translation_key="load_l1_reactive_power",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_l1_reactive_power"),
    ),
    EnergyTrakSensorDescription(
        key="load_l2_reactive_power",
        translation_key="load_l2_reactive_power",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=_key("load_l2_reactive_power"),
    ),
    EnergyTrakSensorDescription(
        key="power_factor",
        translation_key="power_factor",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=_key("power_factor"),
    ),
    EnergyTrakSensorDescription(
        key="power_factor_l1",
        translation_key="power_factor_l1",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=_key("power_factor_l1"),
    ),
    EnergyTrakSensorDescription(
        key="power_factor_l2",
        translation_key="power_factor_l2",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=_key("power_factor_l2"),
    ),
    # ---- Diagnostics -------------------------------------------------
    EnergyTrakSensorDescription(
        key="operation_mode",
        translation_key="operation_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_key("operation_mode"),
    ),
    EnergyTrakSensorDescription(
        key="fault_condition",
        translation_key="fault_condition",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_key("fault_condition"),
    ),
    EnergyTrakSensorDescription(
        key="fuel_type",
        translation_key="fuel_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_key("fuel_type"),
    ),
    EnergyTrakSensorDescription(
        key="ignition_status",
        translation_key="ignition_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_key("ignition_status"),
    ),
    EnergyTrakSensorDescription(
        key="health",
        translation_key="health",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_key("health"),
    ),
    EnergyTrakSensorDescription(
        key="utility_monitor",
        translation_key="utility_monitor",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_key("utility_monitor"),
    ),
    # Age of the equipment snapshot: the single most useful number for
    # telling "the generator is quiet" apart from "EnergyTrak stopped
    # sending equipment telemetry".
    EnergyTrakSensorDescription(
        key="equipment_data_age",
        translation_key="equipment_data_age",
        always=True,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_key("equipment_data_age_seconds"),
        attributes_fn=lambda data: {
            "equipment_source": data.get("equipment_source"),
            "devices": data.get("device_ids") or [],
        },
    ),
    # Monitor-side diagnostics. When telemetry stops arriving these explain
    # why; the generator document alone cannot.
    EnergyTrakSensorDescription(
        key="utility_power",
        translation_key="utility_power",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_key("utility_power"),
    ),
    EnergyTrakSensorDescription(
        key="network_strength",
        translation_key="network_strength",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_key("network_strength"),
        attributes_fn=lambda data: {"network_type": data.get("network_type")},
    ),
    EnergyTrakSensorDescription(
        key="monitor_state",
        translation_key="monitor_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_key("monitor_state"),
    ),
    EnergyTrakSensorDescription(
        key="firmware_update_status",
        translation_key="firmware_update_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_key("firmware_update_status"),
    ),
    EnergyTrakSensorDescription(
        key="equipment_data_timestamp",
        translation_key="equipment_data_timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_timestamp("equipment_data_timestamp"),
    ),
    # last_received = our poll succeeded. last_changed = the vendor's payload
    # actually moved. Both are needed: a vendor that serves an identical
    # payload forever looks perfectly healthy on the first metric alone.
    EnergyTrakSensorDescription(
        key="last_received",
        translation_key="last_received",
        always=True,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_timestamp("last_received_at"),
    ),
    EnergyTrakSensorDescription(
        key="last_changed",
        translation_key="last_changed",
        always=True,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_timestamp("last_changed_at"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyTrakConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the EnergyTrak sensors."""
    coordinator = entry.runtime_data
    entry.async_on_unload(
        async_setup_reported_entities(
            coordinator, SENSORS, EnergyTrakSensor, async_add_entities
        )
    )


class EnergyTrakSensor(EnergyTrakEntity, SensorEntity):
    """A single EnergyTrak measurement."""

    entity_description: EnergyTrakSensorDescription

    def __init__(
        self,
        coordinator: EnergyTrakCoordinator,
        site_id: str,
        description: EnergyTrakSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, site_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        return self.entity_description.value_fn(self.site_data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, when the description provides them."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.site_data)
