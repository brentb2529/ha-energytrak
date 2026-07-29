"""Turn raw EnergyTrak Firestore documents into a flat telemetry dict.

The awkward parts are all upstream quirks:

* Firestore's REST API returns a verbose ``{"stringValue": "..."}`` encoding
  that has to be unwrapped recursively.
* The same measurement can appear in several places on a device document
  (``details.state``, ``details.rawState.Event.*``, the document root), with
  inconsistent casing, so lookups walk a priority list of dotted paths.
* ``rawState.Event.EquipmentEventData`` is a *snapshot* from the last full
  telemetry upload, not a live feed. On cellular genmon units it can be hours
  or months old, and blindly trusting it paints false zeros. See
  ``generator_output`` / ``utility_reading`` for how each field category is
  treated.
* A site links to **several** device documents — typically ``-generator``,
  ``-grid`` and the genmon monitor. They carry overlapping but differently
  aged copies of the telemetry: on a real unit the monitor's snapshot was 190
  days fresher than the generator's, with a higher engine-hour and start
  count. Reading only the first device (as the old Node service did) reports
  stale counters, so ``normalize_site`` merges them by role and takes the
  equipment snapshot from whichever device has the newest one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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


@dataclass
class _DeviceView:
    """One device document, unpacked into the pieces we read from."""

    device_id: str
    kind: str  # "generator" | "grid" | "monitor"
    root: dict[str, Any]
    clean: dict[str, Any]
    raw: dict[str, Any]
    event_ts: datetime | None
    updated_at: str | None


def _view(doc: dict[str, Any]) -> _DeviceView:
    """Unpack one raw Firestore device document."""
    root = parse_firestore_document(doc)
    device_id = str(doc.get("name", "")).split("/")[-1]

    details = root.get("details")
    if not isinstance(details, dict):
        details = {}

    clean = details.get("state")
    if not isinstance(clean, dict):
        clean = {}

    raw = details.get("rawState") or root.get("rawState") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    # `deviceType` is present on the generator and grid documents; the genmon
    # monitor has no such field, so anything unlabelled is treated as the
    # monitor.
    kind = str(root.get("deviceType") or "").strip().lower()
    if kind not in ("generator", "grid"):
        lowered = device_id.lower()
        if lowered.endswith("-generator"):
            kind = "generator"
        elif lowered.endswith("-grid"):
            kind = "grid"
        else:
            kind = "monitor"

    event_ts = _parse_timestamp(
        _find(
            [raw],
            [
                "Event.MessageEventData.ActualDateUTC",
                "Event.MessageEventData.ActualDate",
                "Event.MessageEventData.Created",
            ],
        )
    )
    return _DeviceView(
        device_id, kind, root, clean, raw, event_ts, doc.get("updateTime")
    )


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


def normalize_site(
    site_id: str,
    site_name: str | None,
    device_docs: list[dict[str, Any]],
    *,
    stale_threshold_seconds: int,
    site_doc: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the flat telemetry dict the entities read from.

    ``device_docs`` is every device document linked to the site. Each has a
    role: the generator carries the authoritative heartbeat, the grid device
    carries a *fresh* utility voltage in its heartbeat (rather than a months-old
    snapshot), and the monitor carries connectivity plus — often — the newest
    equipment snapshot and the live fault list.

    ``site_doc`` carries what none of the devices do: the exercise history.
    That matters because the equipment pipeline can go dormant for months
    while the unit keeps exercising weekly — the site document is then the
    only evidence the generator ran at all.
    """
    now = now or datetime.now(UTC)
    views = [_view(doc) for doc in device_docs if doc]
    if not views:
        raise ValueError(f"no device documents for site {site_id}")

    generator = next((v for v in views if v.kind == "generator"), views[0])
    grid = next((v for v in views if v.kind == "grid"), None)
    monitor = next((v for v in views if v.kind == "monitor"), None)

    # The equipment snapshot is whichever device uploaded most recently. On a
    # real site these differ by months, and the newest one also carries the
    # higher (correct) engine-hour and start counts.
    dated = [v for v in views if v.event_ts is not None]
    snapshot = max(dated, key=lambda v: v.event_ts) if dated else generator

    clean_state = generator.clean
    raw_state = snapshot.raw
    parsed_device = generator.root
    grid_clean = grid.clean if grid else {}
    monitor_clean = monitor.clean if monitor else {}

    sources: list[Any] = [clean_state, raw_state, parsed_device]
    fresh_sources: list[Any] = [clean_state, parsed_device]

    # --- Equipment-snapshot age -------------------------------------
    equipment_ts = snapshot.event_ts
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
    # Two possible sources. Older payloads nest ~25 individual flags under
    # Event.Alarms / Alarm1..Alarm14, each a "0"/"1" string. Current firmware
    # instead publishes a live `triggeredFaults` list on the monitor's
    # heartbeat — which is fresh, where the Alarms block shares the equipment
    # snapshot's age. Use both.
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

    for fault in _find([monitor_clean, clean_state], ["triggeredFaults"]) or []:
        label = _fault_label(fault)
        if label:
            alarm_flags[label] = True
            active_alarms.append(label)

    active_alarms = sorted(set(active_alarms))

    # --- Grid status -------------------------------------------------
    utility_monitor = _find(sources, ["Event.EquipmentEventData.UtilityPowerMonitor"])
    utility_present = _find([clean_state, grid_clean], ["utilityPresent"])
    grid_status = _grid_status(utility_present, utility_monitor)

    is_running = (
        _find(sources, ["generatorRunning"]) is True
        or str(_find(sources, ["Event.EquipmentEventData.EngineRun"])).lower() == "true"
        or str(_find(sources, ["Event.EquipmentEventData.IgnitionStatus"])).lower() == "on"
    )

    name = (
        _find([clean_state, parsed_device], ["name"])
        or _find(sources, ["Device.ItemName", "Equipment.EquipmentName"])
        or site_name
        or "Generator"
    )

    # The grid device publishes a live utility voltage on its heartbeat, which
    # beats the equipment snapshot's months-old copy.
    grid_voltage = _to_number(_find([grid_clean], ["utilityVoltage", "voltage"]))
    if grid_voltage is None:
        grid_voltage = utility_reading(
            [
                "gridVoltage",
                "Event.EquipmentEventData.MainsL1L2Voltage",
                "Event.EquipmentEventData.MainsL1NVoltage",
            ]
        )

    return {
        "site_id": site_id,
        "name": name,
        # `state` is the run state ("standby"/"running"); `status` and
        # `health` are both the health grade, so they belong on the health
        # sensor rather than here.
        "status": _find([clean_state], ["state", "status"]) or "Unknown",
        "active": is_running,
        "model": _first_text(
            parsed_device.get("modelNumber"),
            parsed_device.get("productModel"),
            parsed_device.get("productName"),
            _find(sources, ["model", "Equipment.Model"]),
        ),
        "make": _first_text(
            parsed_device.get("make"), _find(sources, ["make", "Equipment.Make"])
        ),
        "engine_manufacturer": _find(sources, ["Equipment.EngineMfg"]),
        "engine_model": _find(sources, ["Equipment.EngineModel"]),
        "serial_number": _first_text(
            parsed_device.get("serialNumber"),
            monitor.root.get("serialNumber") if monitor else None,
            _find(sources, ["Equipment.EquipmentSerial"]),
        ),
        "firmware_version": _first_text(
            parsed_device.get("firmwareVersion"),
            monitor.root.get("firmwareVersion") if monitor else None,
        ),
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
        "grid_voltage": grid_voltage,
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
        # cleanState carries `fault` as a bool, the equipment event as a text
        # code. Normalise so the sensor never reads a bare "False".
        "fault_condition": _fault_text(
            _find(sources, ["Event.EquipmentEventData.FaultCondition", "fault"]),
            active_alarms,
        ),
        "ignition_status": _find(sources, ["Event.EquipmentEventData.IgnitionStatus"]),
        "operation_mode": _find(
            sources,
            [
                "Event.EquipmentEventData.DgOperationMode",
                "Event.EquipmentEventData.CurrentDgStatus",
            ],
        ),
        "health": _find([clean_state], ["health", "status", "siteHealth"]),
        # Monitor-side diagnostics: on a cellular/wifi genmon these explain
        # *why* telemetry stopped arriving, which the generator document
        # cannot tell you.
        "monitor_state": _find([monitor_clean], ["state"]),
        "monitor_online": _first_present(
            monitor_clean.get("simNetworkConnected"),
            (
                str(monitor_clean.get("state") or "").lower() == "connected"
                if monitor_clean.get("state")
                else None
            ),
        ),
        "network_type": _find([monitor_clean], ["networkType"]),
        "network_strength": _find([monitor_clean], ["networkStrength"]),
        "firmware_update_status": _find([monitor_clean], ["firmwareUpdateStatus"]),
        "utility_power": _find([grid_clean, monitor_clean], ["utilityPower"]),
        # Which device supplied the equipment snapshot, and what else we saw.
        "equipment_source": snapshot.device_id,
        "device_ids": [v.device_id for v in views],
        # Site-level exercise history and faults.
        **_site_fields(site_doc),
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
        "document_updated_at": generator.updated_at,
        "equipment_data_timestamp": equipment_ts,
        "equipment_data_age_seconds": (
            int(equipment_age) if equipment_age is not None else None
        ),
        "equipment_data_stale": equipment_stale,
    }


def _site_fields(site_doc: dict[str, Any] | None) -> dict[str, Any]:
    """Pull exercise history and site-level faults out of the site document.

    The exercise block is the only place a weekly test run is recorded. On a
    unit whose equipment telemetry has gone dormant, this is the difference
    between "the generator has not run since May" and "it ran on Saturday for
    twenty minutes and everything is fine".
    """
    empty: dict[str, Any] = {
        "site_state": None,
        "site_health": None,
        "last_exercise_at": None,
        "last_exercise_duration_seconds": None,
        "next_exercise_due": None,
        "exercise_interval_days": None,
        "run_session": None,
        "has_malfunction": None,
        "malfunction_description": None,
        "subscription_active": None,
        "commissioned_at": None,
    }
    if not site_doc:
        return empty

    site = parse_firestore_document(site_doc)
    if not site:
        return empty

    exercise = site.get("exercise") if isinstance(site.get("exercise"), dict) else {}
    last = exercise.get("lastActivity") if isinstance(exercise.get("lastActivity"), dict) else {}
    notify = (
        exercise.get("exerciseNotifications")
        if isinstance(exercise.get("exerciseNotifications"), dict)
        else {}
    )
    malfunction = (
        site.get("malfunction") if isinstance(site.get("malfunction"), dict) else {}
    )

    # `duration` is milliseconds (a ~20 minute exercise reads as ~1_218_000).
    duration_ms = _to_number(last.get("duration"))
    duration_s = round(duration_ms / 1000) if duration_ms is not None else None

    return {
        "site_state": site.get("state"),
        "site_health": site.get("health") or site.get("status"),
        "last_exercise_at": _parse_timestamp(last.get("finishedAt")),
        "last_exercise_duration_seconds": duration_s,
        "next_exercise_due": _parse_timestamp(notify.get("nextExerciseDue")),
        "exercise_interval_days": _to_number(notify.get("intervalDays")),
        # Populated only while a run is in progress, so useful as evidence the
        # generator is running even when the equipment feed is dormant.
        "run_session": exercise.get("generatorRunSession"),
        "has_malfunction": malfunction.get("hasMalfunction"),
        "malfunction_description": _first_text(malfunction.get("description")),
        "subscription_active": site.get("isSubscriptionActive"),
        "commissioned_at": _parse_timestamp(site.get("commissionedAt")),
    }


def _first_text(*values: Any) -> str | None:
    """First value that is a non-empty string.

    EnergyTrak fills unknown metadata with empty strings rather than omitting
    the key, so a plain ``or`` chain would stop on the wrong one.
    """
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_present(*values: Any) -> Any:
    """First value that is not None."""
    for value in values:
        if value is not None:
            return value
    return None


def _fault_label(fault: Any) -> str | None:
    """Name one entry from the monitor's ``triggeredFaults`` list."""
    if isinstance(fault, str):
        return fault.strip() or None
    if isinstance(fault, dict):
        for key in ("name", "faultName", "description", "code", "faultCode", "id"):
            value = fault.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (int, float)):
                return str(value)
    return None


def _fault_text(raw: Any, active_alarms: list[str]) -> str:
    """Render the fault condition as text rather than a bare bool."""
    # A firing alarm outranks the boolean: the generator's `fault` flag and the
    # monitor's fault list come from different pipelines and can disagree, and
    # naming the alarm is always more useful than "None".
    if active_alarms:
        return ", ".join(active_alarms)
    if isinstance(raw, bool):
        return "Fault" if raw else "None"
    if raw is None or str(raw).strip() == "":
        return ", ".join(active_alarms) if active_alarms else "None"
    return str(raw)


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
