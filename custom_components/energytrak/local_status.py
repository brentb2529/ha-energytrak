"""Entities that describe the local bridge itself, rather than the generator.

Hand-written, unlike local_entities.py -- these are not registers, they are the
answer to "which source am I looking at right now, and should I believe it".

Without these the source switch is invisible: values would silently change
provenance mid-graph with nothing in Home Assistant recording that it happened.
When a reading looks wrong the first question is always which path produced it.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime

from .binary_sensor import EnergyTrakBinarySensorDescription
from .sensor import EnergyTrakSensorDescription, _key

LOCAL_STATUS_SENSORS: tuple[EnergyTrakSensorDescription, ...] = (
    EnergyTrakSensorDescription(
        key="telemetry_source",
        translation_key="telemetry_source",
        icon="mdi:transit-connection-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=["local", "cloud"],
        value_fn=_key("telemetry_source"),
        # The address rides along as attributes rather than as its own entity:
        # "local" and "which box" are one question asked twice, and splitting
        # them lets a dashboard show one without the other.
        attributes_fn=lambda data: {
            "bridge_host": data.get("local_bridge_host"),
            "bridge_port": data.get("local_bridge_port"),
            "bridge_connected": data.get("local_bridge_connected"),
            "bus_healthy": data.get("local_bus_healthy"),
        },
    ),
    EnergyTrakSensorDescription(
        key="local_bus_age_seconds",
        translation_key="local_bus_age_seconds",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_key("local_bus_age_seconds"),
    ),
)

LOCAL_STATUS_BINARY_SENSORS: tuple[EnergyTrakBinarySensorDescription, ...] = (
    EnergyTrakBinarySensorDescription(
        key="monitor_online",
        translation_key="monitor_online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_key("monitor_online"),
    ),
    # THE TWO WAYS THE BRIDGE GOES DARK, KEPT APART ON PURPOSE.
    #
    # They look alike in Home Assistant -- values stop moving -- but they are
    # different faults with different fixes, and collapsing them into one
    # "bridge unhealthy" flag would send you to the wrong end of the install:
    #
    #   bridge_reachable off  -> the ESP32 is not answering. Power, Wi-Fi, the
    #                            board itself. Nothing local is arriving at all.
    #   bus_healthy off       -> the ESP32 is fine and talking to Home Assistant,
    #                            but the generator has stopped answering IT.
    #                            RS-485 pair, termination, the controller.
    #
    # The second is the sneaky one: the bridge is up, the entities exist, the
    # integration is happy, and the data is simply old. That is precisely the
    # state a naive "is it online" check calls healthy.
    EnergyTrakBinarySensorDescription(
        key="local_bridge_connected",
        translation_key="local_bridge_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_key("local_bridge_connected"),
    ),
    EnergyTrakBinarySensorDescription(
        key="local_bus_healthy",
        translation_key="local_bus_healthy",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_key("local_bus_healthy"),
    ),
)
