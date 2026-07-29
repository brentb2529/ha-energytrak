"""Turn raw EnergyTrak Firestore documents into a flat telemetry dict.

This is a direct port of the extraction logic that used to live in the
standalone Node wrapper service. The awkward parts are all upstream quirks:

* Firestore's REST API returns a verbose ``{"stringValue": "..."}`` encoding
  that has to be unwrapped recursively.
* The same measurement can appear in up to four places on the device document
  (``details.state``, ``details.rawState.Event.*``, the device root), with
  inconsistent casing, so lookups walk a priority list of dotted paths.
* ``rawState.Event.EquipmentEventData`` is a *snapshot* from the last full
  telemetry upload, not a live feed. On cellular genmon units it can be hours
  or months old, and blindly trusting it paints false zeros. See
  ``_StalenessRules`` below for how each field category is treated.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

# Matches both shapes the controller emits:
#   "2026-04-19T11:20:17"              (no zone -> UTC)
#   "2026-04-19 07:20:17.000 -04:00"   (explicit offset)
_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*(Z|[+-]\d{2}:?\d{2})?$"
)

_NUMERIC_STRIP_RE = re.compile(r"[^\d.\-]")

_RUNNING_STATES = {"running"}
_STOPPED_STATES = {"stopped", "ready", "idle", "standby", "off"}


# ----------------------------------------------------------------------
# Firestore value decoding
# ----------------------------------------------------------------------


def parse_firestore_value(value: Any) -> Any:
    """Recursively unwrap one Firestore typed value."""
    if not isinstance(value, dict) or not value:
        return None

    key = next(iter(value))
    raw = value[key]

    if key == "integerValue":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if key == "doubleValue":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if key == "mapValue":
        fields = (raw or {}).get("fields") or {}
        return {k: parse_firestore_value(v) for k, v in fields.items()}
    if key == "arrayValue":
        return [parse_firestore_value(v) for v in (raw or {}).get("values") or []]
    if key == "nullValue":
        return None
    # stringValue, booleanValue, timestampValue, referenceValue, bytesValue…
    return raw


def parse_firestore_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a whole Firestore document's ``fields`` block."""
    fields = doc.get("fields")
    if not isinstance(fields, dict):
        return {}
    return {key: parse_firestore_value(value) for key, value in fields.items()}


def extract_device_ids(site_doc: dict[str, Any]) -> list[str]:
    """Return the device ids referenced by a site document."""
    values = (
        (site_doc.get("fields") or {}).get("devices", {}).get("arrayValue", {})
    ).get("values") or []

    device_ids: list[str] = []
    for entry in values:
        raw = entry.get("stringValue") or entry.get("referenceValue")
        if raw:
            device_ids.append(str(raw).split("/")[-1])
    return device_ids


# ----------------------------------------------------------------------
# Lookup helpers
# ----------------------------------------------------------------------


def _to_number(value: Any) -> float | int | None:
    """Coerce a possibly unit-suffixed value ("13.1 V") to a number."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    cleaned = _NUMERIC_STRIP_RE.sub("", str(value))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _find(sources: list[Any], paths: list[str]) -> Any:
    """First non-empty value found by walking ``paths`` through ``sources``.

    Keys are matched exactly first, then case-insensitively — EnergyTrak is
    inconsistent about capitalisation between the clean and raw payloads.
    """
    for root in sources:
        if not isinstance(root, dict):
            continue
        for path in paths:
            current: Any = root
            for part in path.split("."):
                if not isinstance(current, dict):
                    current = None
                    break
                if part in current:
                    current = current[part]
                    continue
                match = next(
                    (k for k in current if k.lower() == part.lower()),
                    None,
                )
                if match is None:
                    current = None
                    break
                current = current[match]
            if current is not None and current != "":
                return current
    return None


def _parse_timestamp(raw: Any) -> datetime | None:
    """Parse an EnergyTrak event timestamp; naive values are treated as UTC."""
    if raw is None:
        return None
    match = _TIMESTAMP_RE.match(str(raw).strip())
    if not match:
        return None

    date_part, time_part, zone = match.groups()
    if not zone or zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"

    try:
        return datetime.fromisoformat(f"{date_part}T{time_part}{zone}")
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------


def normalize_device(
    site_id: str,
    site_name: str | None,
    device_doc: dict[str, Any],
    *,
    stale_threshold_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the flat telemetry dict the entities read from."""
    now = now or datetime.now(UTC)
    parsed_device = parse_firestore_document(device_doc)

    details = parsed_device.get("details") or {}
    if not isinstance(details, dict):
        details = {}

    # `state` holds pre-computed, frequently-refreshed values; `rawState` is
    # the full (possibly ancient) equipment-event dump.
    clean_state = details.get("state") or {}
    if not isinstance(clean_state, dict):
        clean_state = {}

    raw_state = details.get("rawState") or parsed_device.get("rawState") or {}
    if isinstance(raw_state, str):
        try:
            raw_state = json.loads(raw_state)
        except ValueError:
            raw_state = {}
    if not isinstance(raw_state, dict):
        raw_state = {}

    sources: list[Any] = [clean_state, raw_state, parsed_device]
    fresh_sources: list[Any] = [clean_state, parsed_device]

    # --- Equipment-snapshot age -------------------------------------
    equipment_ts = _parse_timestamp(
        _find(
            sources,
            [
                "Event.MessageEventData.ActualDateUTC",
                "Event.MessageEventData.ActualDate",
                "Event.MessageEventData.Created",
            ],
        )
    )
    equipment_age: float | None = None
    if equipment_ts is not None:
        equipment_age = (now - equipment_ts).total_seconds()
    equipment_stale = equipment_age is not None and equipment_age > stale_threshold_seconds

    # --- Fresh "is it running?" signal, from cleanState only ---------
    # The composite `active` flag below also consults stale rawState fields;
    # here we want a signal we can trust to decide how to treat stale data.
    run_flag = _find([clean_state], ["generatorRunning"])
    state_text = str(_find([clean_state], ["state"]) or "").lower()
    if run_flag is True or state_text in _RUNNING_STATES:
        generator_running: bool | None = True
    elif run_flag is False or state_text in _STOPPED_STATES:
        generator_running = False
    else:
        generator_running = None

    def instantaneous(paths: list[str]) -> float | int | None:
        """Read a field that is only meaningful when it is fresh."""
        value = _find(sources, paths)
        if value is None:
            return None
        # Only rawState-sourced values are subject to snapshot staleness.
        if _find(fresh_sources, paths) is None and equipment_stale:
            return None
        return _to_number(value)

    def generator_output(paths: list[str]) -> float | int | None:
        """Read a generator-output field.

        When the unit is known to be off, zero is the *correct* current value,
        so a stale zero is still accurate and worth reporting. When it is
        running, a stale zero is suspect (the snapshot may predate the start),
        so report nothing rather than a false zero.
        """
        if generator_running is False:
            return 0
        return instantaneous(paths)

    def utility_reading(paths: list[str]) -> float | int | None:
        """Read a slow-moving utility-side field.

        Grid voltage and frequency are bounded and stable — the utility is
        always ~240V / 60Hz when it is present — so a stale-but-plausible
        number is strictly more useful than a gap. Whether the utility is
        actually there *right now* is answered separately, and freshly, by
        ``grid_present``; and ``equipment_data_age`` says how old this
        reading is. Pass through at any age; report nothing only when the
        field is genuinely absent from every source.
        """
        return _to_number(_find(sources, paths))

    # --- Engine hours ------------------------------------------------
    # A monotonic counter that is always > 0 on an installed unit. cleanState
    # sometimes carries a literal 0, which would otherwise shadow the real
    # counter from the equipment event, so skip zeros unless every source
    # agrees (a genuinely brand-new install).
    hour_candidates = [
        _to_number(_find(sources, ["engineRuntimeHours"])),
        _to_number(_find(sources, ["Event.EquipmentEventData.EngineHours"])),
        _to_number(_find(sources, ["Event.DeviceEventData.EngineHoursTP"])),
        _to_number(_find(sources, ["Equipment.EngineHours"])),
    ]
    engine_hours = next((v for v in hour_candidates if v not in (None, 0)), None)
    if engine_hours is None and any(v == 0 for v in hour_candidates):
        engine_hours = 0

    # --- Alarms ------------------------------------------------------
    # Event.Alarms nests ~25 individual flags under Alarm1..Alarm14 groups,
    # each a "0"/"1" string. Flatten to a list of firing names plus the full
    # map so automations can trigger on a specific alarm.
    alarms_block = _find(sources, ["Event.Alarms"]) or {}
    alarm_flags: dict[str, bool] = {}
    active_alarms: list[str] = []
    if isinstance(alarms_block, dict):
        for group in alarms_block.values():
            if not isinstance(group, dict):
                continue
            for name, value in group.items():
                firing = value in ("1", 1, True)
                alarm_flags[name] = firing
                if firing:
                    active_alarms.append(name)
    active_alarms.sort()

    # --- Grid status -------------------------------------------------
    utility_monitor = _find(sources, ["Event.EquipmentEventData.UtilityPowerMonitor"])
    utility_present = _find(sources, ["utilityPresent"])
    grid_status = _grid_status(utility_present, utility_monitor)

    is_running = (
        _find(sources, ["generatorRunning"]) is True
        or str(_find(sources, ["Event.EquipmentEventData.EngineRun"])).lower() == "true"
        or str(_find(sources, ["Event.EquipmentEventData.IgnitionStatus"])).lower() == "on"
    )

    name = (
        _find(sources, ["name", "Device.ItemName", "Equipment.EquipmentName"])
        or site_name
        or "Generator"
    )

    return {
        "site_id": site_id,
        "name": name,
        "status": _find(sources, ["status", "state"]) or "Unknown",
        "active": is_running,
        "model": _find(sources, ["model", "Equipment.Model"]),
        "make": _find(sources, ["make", "Equipment.Make"]),
        "engine_manufacturer": _find(sources, ["Equipment.EngineMfg"]),
        "engine_model": _find(sources, ["Equipment.EngineModel"]),
        "serial_number": _find(sources, ["Equipment.EquipmentSerial"]),
        # Battery is refreshed in cleanState on every heartbeat.
        "battery_voltage": _to_number(
            _find(
                sources,
                [
                    "batteryVoltage",
                    "Event.EquipmentEventData.BatteryVoltage",
                    "Event.DeviceEventData.BatteryVoltageTP",
                ],
            )
        ),
        "engine_hours": engine_hours,
        # Utility side
        "grid_voltage": utility_reading(
            [
                "gridVoltage",
                "Event.EquipmentEventData.MainsL1L2Voltage",
                "Event.EquipmentEventData.MainsL1NVoltage",
            ]
        ),
        "grid_frequency": utility_reading(
            ["Event.EquipmentEventData.MainsFrequency", "gridStatus.frequency"]
        ),
        "grid_present": utility_present if isinstance(utility_present, bool) else None,
        "grid_status": grid_status,
        "utility_monitor": utility_monitor,
        # Generator output
        "output_voltage": generator_output(
            [
                "generatorVoltage",
                "Event.EquipmentEventData.GeneratorL1L2Voltage",
                "Event.EquipmentEventData.GeneratorL1NVoltage",
            ]
        ),
        "output_voltage_l1n": generator_output(
            ["Event.EquipmentEventData.GeneratorL1NVoltage"]
        ),
        "output_voltage_l2n": generator_output(
            ["Event.EquipmentEventData.GeneratorL2NVoltage"]
        ),
        "generator_frequency": generator_output(
            ["Event.EquipmentEventData.GeneratorRFrequency", "gridStatus.frequency"]
        ),
        "engine_speed": generator_output(["Event.EquipmentEventData.EngineSpeed"]),
        "load_power": generator_output(["Event.EquipmentEventData.LoadTotalPower"]),
        "load_l1_power": generator_output(["Event.EquipmentEventData.LoadL1Power"]),
        "load_l2_power": generator_output(["Event.EquipmentEventData.LoadL2Power"]),
        "load_l1_current": generator_output(["Event.EquipmentEventData.LoadL1Current"]),
        "load_l2_current": generator_output(["Event.EquipmentEventData.LoadL2Current"]),
        "load_l1_apparent_power": generator_output(
            ["Event.EquipmentEventData.LoadL1ApparentPower"]
        ),
        "load_l2_apparent_power": generator_output(
            ["Event.EquipmentEventData.LoadL2ApparentPower"]
        ),
        "load_apparent_power": generator_output(
            ["Event.EquipmentEventData.LoadTotalApparentPower"]
        ),
        "load_l1_reactive_power": generator_output(
            ["Event.EquipmentEventData.LoadL1ReactivePower"]
        ),
        "load_l2_reactive_power": generator_output(
            ["Event.EquipmentEventData.LoadL2ReactivePower"]
        ),
        "load_reactive_power": generator_output(
            ["Event.EquipmentEventData.LoadTotalReactivePower"]
        ),
        "power_factor_l1": generator_output(
            ["Event.EquipmentEventData.GeneratorPowerFactorL1"]
        ),
        "power_factor_l2": generator_output(
            ["Event.EquipmentEventData.GeneratorPowerFactorL2"]
        ),
        "power_factor": generator_output(
            ["Event.EquipmentEventData.GeneratorAveragePowerFactor"]
        ),
        # Cumulative counters stay informative even when the snapshot is old.
        "starts_count": _to_number(_find(sources, ["Event.EquipmentEventData.StartsCount"])),
        "trips_count": _to_number(_find(sources, ["Event.EquipmentEventData.TripsCount"])),
        # Status / metadata
        "fuel_type": _find(
            sources, ["Event.EquipmentEventData.FuelType", "Equipment.FuelTypeTP"]
        ),
        "fault_condition": _find(
            sources, ["Event.EquipmentEventData.FaultCondition", "fault"]
        ),
        "ignition_status": _find(sources, ["Event.EquipmentEventData.IgnitionStatus"]),
        "operation_mode": _find(
            sources,
            [
                "Event.EquipmentEventData.DgOperationMode",
                "Event.EquipmentEventData.CurrentDgStatus",
            ],
        ),
        "health": _find(sources, ["health", "siteHealth"]),
        "smart_mode_enabled": _find([clean_state], ["smartModeEnabled"]),
        "smart_mode_detection": _find([clean_state], ["smartModeDetection"]),
        "status_color": _find([clean_state], ["generatorStatusColor"]),
        # Alarms
        "active_alarms": active_alarms,
        "active_alarm_count": len(active_alarms),
        "alarm_flags": alarm_flags,
        # Location (GPS-capable cellular units only)
        "latitude": _to_number(_find(sources, ["Event.Location.Lat"])),
        "longitude": _to_number(_find(sources, ["Event.Location.Lon"])),
        "location_city": _find(sources, ["Event.Location.City"]),
        "location_state": _find(sources, ["Event.Location.State"]),
        # Freshness
        "clean_state_last_updated": _find([clean_state], ["lastUpdated"]),
        "document_updated_at": device_doc.get("updateTime"),
        "equipment_data_timestamp": equipment_ts,
        "equipment_data_age_seconds": (
            int(equipment_age) if equipment_age is not None else None
        ),
        "equipment_data_stale": equipment_stale,
    }


def _grid_status(utility_present: Any, utility_monitor: Any) -> str:
    """Describe the utility feed in one word."""
    if utility_present is True:
        return "Present"
    if utility_present is False:
        return "Lost"
    if utility_monitor in (None, ""):
        return "Unknown"

    # The genmon reports its armed voltage-window guard as e.g.
    # "STOPPED UNDER 190V" / "RUNNING OVER 260V". Both shapes mean the utility
    # is present and being watched — only OK/READY or a bare STOPPED differ.
    text = str(utility_monitor).lower().strip()
    if "ok" in text or "ready" in text:
        return "Good"
    if re.fullmatch(r"(stopped|running)\s+(over|under)\s+\d+\s*v?", text):
        return "Monitoring"
    if "running" in text:
        return "Monitoring"
    if "stopped" in text:
        return "Stopped"
    return str(utility_monitor)
