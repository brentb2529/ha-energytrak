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
from hashlib import sha256
from typing import Any

# Matches both shapes the controller emits:
#   "2026-04-19T11:20:17"              (no zone -> UTC)
#   "2026-04-19 07:20:17.000 -04:00"   (explicit offset)
_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*(Z|[+-]\d{2}:?\d{2})?$"
)

_NUMERIC_STRIP_RE = re.compile(r"[^\d.\-]")

# How stale the equipment snapshot may be before "the generator is off, so the
# correct output is 0" stops being a fair inference and becomes an invented
# reading. A day covers any plausible reporting gap on a working unit.
_OFF_MEANS_ZERO_MAX_AGE_SECONDS = 24 * 60 * 60

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

    # MessageEventData carries several timestamps and they do NOT agree. On a
    # real unit ActualDateUTC and Created both sat at the original message
    # (2026-05-01) while ActualDate tracked the live upload to the second —
    # so a first-match priority list picked a three-month-old value and
    # declared perfectly fresh telemetry ancient. Take the newest instead:
    # whichever field a given firmware keeps current is the one that wins,
    # with no assumption about which that is.
    candidates = [
        _parse_timestamp(_find([raw], [path]))
        for path in (
            "Event.MessageEventData.ActualDate",
            "Event.MessageEventData.ActualDateUTC",
            "Event.MessageEventData.Created",
        )
    ]
    dated = [ts for ts in candidates if ts is not None]
    event_ts = max(dated) if dated else None
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


def _candidates(labelled: list[tuple[str, Any]], paths: list[str]) -> dict[str, Any]:
    """Every value each source holds for ``paths``.

    The cumulative counters are read from the first of several candidate
    fields that carries a value, which means the reported number alone does
    not say *where* it came from. When a counter looks wrong on someone
    else's unit — hardware we cannot see — this is the only way to tell a
    faithful pass-through of an odd vendor number from us reading the wrong
    field, or one in different units. Surfaced through diagnostics only.
    """
    found: dict[str, Any] = {}
    for label, root in labelled:
        for path in paths:
            value = _find([root], [path])
            if value is not None:
                found[f"{label}:{path}"] = value
    return found


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

# Engine hours, in priority order, each with the conversion that turns it
# into decimal hours. Different firmware populates different ones and they
# are NOT all in the same unit despite the naming.
#
# `EquipmentEventData.EngineHours` is the controller's LCD clock display
# serialized with a dot in place of the colon: a unit whose LCD read 91:48
# (91 h 48 min) carried the string "91.48" in this field at the same moment.
# So "91.48" means 91.8 decimal hours, not 91.48 — see _clock_hours below.
# The earlier reading of "90.47" on the same fleet was 90:47 all along.
#
# The unit is attached to the field rather than inferred from the value.
# Guessing "this number looks too big, divide it" would corrupt the perfectly
# legitimate case of an older generator retrofitted with a monitor, whose
# hours vastly exceed its EnergyTrak commissioning date.
#
# `engineRuntimeHours` and `EngineHoursTP` are the SAME counter under two
# names — a unit reporting both carried the identical raw value in each — so
# they must share a factor. Whatever else changes here, changing one of
# those two without the other reintroduces a phantom disagreement between
# two copies of one number.
#
# There is no vendor conversion to copy. The EnergyTrak Pro web app
# (energytrak.io, traced in its compiled Flutter bundle) reads this same
# `cleanState.engineRuntimeHours` field and formats it as literal decimal
# hours — no factor at all — so on firmware that writes the raw counter into
# it (genmon 86q observed), the vendor's own UI misrenders it too. The
# generator's physical hour meter is the only ground truth available.
#
# Calibration, all from one real unit (B&S 50BSPP-0, genmon firmware 86q):
#   2026-07-29  counter  1623
#   2026-08-19  counter  5283   physical hour meter read 163 h
# 163 h / 5283 counts = 111.07 s per count — evidently one increment per
# firmware telemetry cycle (~111 s on this unit) rather than any standard
# time unit. Minutes would display 88.05 h and tenths of an hour 528.3 h
# against that meter. Single-pair calibration: a second simultaneous
# (counter, meter) pair refines the factor via the delta between pairs, and
# the cross-check below flags firmware where it is wrong.
_RUNTIME_COUNTER_TO_HOURS = 163.0 / 5283.0  # 0.030854 h/count, 111.07 s


def _counter_hours(value: float) -> float:
    """Convert the shared telemetry-cycle runtime counter to decimal hours."""
    return value * _RUNTIME_COUNTER_TO_HOURS


def _clock_hours(value: float) -> float:
    """Convert an LCD-style H.MM reading ("91.48" = 91:48) to decimal hours.

    The fractional digits are minutes, not hundredths. A fractional part that
    cannot be minutes (>= 60) falls back to reading the value as plain
    decimal hours, in case some firmware genuinely writes decimals here —
    that fallback is only reachable for .60–.99, so a wrong guess there costs
    at most 0.4 h and only on firmware never observed so far.
    """
    whole = int(value)
    minutes = round((value - whole) * 100)
    if 0 <= minutes < 60:
        return whole + minutes / 60
    return value


_ENGINE_HOUR_SOURCES: list[tuple[str, Any]] = [
    ("engineRuntimeHours", _counter_hours),
    ("Event.EquipmentEventData.EngineHours", _clock_hours),
    ("Event.DeviceEventData.EngineHoursTP", _counter_hours),
    ("Equipment.EngineHours", _clock_hours),
]
_ENGINE_HOUR_PATHS = [path for path, _ in _ENGINE_HOUR_SOURCES]
_STARTS_PATHS = ["Event.EquipmentEventData.StartsCount"]
_TRIPS_PATHS = ["Event.EquipmentEventData.TripsCount"]


@dataclass
class EquipmentFreshness:
    """What earlier polls learned about the equipment block's liveness.

    ``Event.MessageEventData.ActualDateUTC`` is the controller's own claim
    about when it last uploaded telemetry, and on at least one real unit it
    is simply wrong: the block's *contents* advance — engine hours and the
    start count both stepped within seconds of a weekly exercise — while the
    embedded timestamp stays frozen months in the past. Trusting it reported
    a 92-day-old snapshot for data that was minutes old, and, because the
    output-field gate keys off that age, suppressed live RPM and voltage for
    the entire run.

    Content movement is proof of life that a self-reported timestamp is not.
    Note the asymmetry: a *changed* signature proves the block is live, but
    an unchanged one proves nothing (a healthy idle generator repeats the
    same payload for days). So this only ever makes the age younger, never
    older, and a first sighting establishes nothing — freshness requires
    seeing a transition from a previously known signature.
    """

    signature: str | None = None
    seen_at: datetime | None = None


def _equipment_signature(raw_state: Any) -> str | None:
    """Stable digest of the equipment block, or None when it is absent."""
    block = _find([raw_state], ["Event.EquipmentEventData"])
    if not isinstance(block, dict) or not block:
        return None
    return sha256(
        json.dumps(block, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def normalize_site(
    site_id: str,
    site_name: str | None,
    device_docs: list[dict[str, Any]],
    *,
    stale_threshold_seconds: int,
    site_doc: dict[str, Any] | None = None,
    now: datetime | None = None,
    freshness: EquipmentFreshness | None = None,
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
    # Same list, labelled by role rather than by device id — the ids embed the
    # unit's serial, and diagnostics get shared.
    labelled_sources: list[tuple[str, Any]] = [
        ("cleanState", clean_state),
        (f"rawState({snapshot.kind})", raw_state),
        ("device", parsed_device),
    ]

    # --- Equipment-snapshot age -------------------------------------
    # The controller's own timestamp is a *lower* bound on freshness, not the
    # truth — see EquipmentFreshness. Having watched the block's contents
    # move is stronger evidence, so the effective age is measured from
    # whichever is more recent.
    equipment_ts = snapshot.event_ts
    previous = freshness or EquipmentFreshness()
    signature = _equipment_signature(raw_state)

    if signature is None or previous.signature is None:
        # Nothing to compare against yet: carry forward what we knew, and
        # record the signature so the *next* poll can detect a transition.
        content_seen_at = previous.seen_at
    elif signature != previous.signature:
        content_seen_at = now
    else:
        content_seen_at = previous.seen_at

    effective_ts = equipment_ts
    if content_seen_at is not None and (
        effective_ts is None or content_seen_at > effective_ts
    ):
        effective_ts = content_seen_at

    # Three cases, and the third is the one that matters. A stale verdict
    # resting only on a field we have proven can be permanently frozen is not
    # a verdict, it is a guess — and raising "Problem" on a unit that is
    # demonstrably delivering data is exactly the kind of confident-wrong
    # answer this module exists to avoid. Say "unknown" instead.
    # Clamp at zero: a controller whose clock runs fast would otherwise
    # produce a negative age, which reads as "fresher than now".
    reported_age = max(0.0, (now - equipment_ts).total_seconds()) if equipment_ts else None
    if content_seen_at is not None:
        # We have watched the block move: a real clock, and one that will
        # correctly age into "stale" if the feed later dies.
        basis = "observed"
    elif reported_age is not None and reported_age <= _OFF_MEANS_ZERO_MAX_AGE_SECONDS:
        # The claim is plausible, so believe it. Note the bound is the
        # credibility window, *not* the user's staleness threshold: a
        # three-hour-old snapshot is ordinary for a cellular check-in and
        # says nothing bad about the clock, even though the default 15-minute
        # threshold will (correctly) call it stale.
        basis = "reported"
    else:
        # A claim older than any plausible check-in, never corroborated.
        # "The feed died" and "the clock field is stuck" are indistinguishable
        # from here, and both leave every reading unusable anyway — so the
        # only thing lost by declining to guess is a confident wrong alarm.
        basis = "unknown"

    equipment_age: float | None = None
    equipment_stale: bool | None = None
    if basis != "unknown" and effective_ts is not None:
        equipment_age = max(0.0, (now - effective_ts).total_seconds())
        equipment_stale = equipment_age > stale_threshold_seconds
    else:
        effective_ts = None

    # Worth surfacing: it means the vendor timestamp is lying, and it is the
    # difference between "your generator stopped reporting" and "your
    # generator is fine, its clock field is stuck".
    timestamp_unreliable = (
        content_seen_at is not None
        and equipment_ts is not None
        and content_seen_at > equipment_ts
    )

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
        # Only rawState-sourced values are subject to snapshot staleness, and
        # they pass only when the snapshot is *known* to be fresh. Note the
        # `is not False`: an unknown age must suppress exactly like a stale
        # one, or withholding the staleness verdict would silently start
        # publishing month-old readings as current.
        if _find(fresh_sources, paths) is None and equipment_stale is not False:
            return None
        return _to_number(value)

    def generator_output(paths: list[str]) -> float | int | None:
        """Read a generator-output field.

        When the unit is known to be off, zero is the *correct* current value,
        so a recent stale zero is still accurate and worth reporting. When it
        is running, a stale zero is suspect (the snapshot may predate the
        start), so report nothing rather than a false zero.

        The off-means-zero shortcut needs the age to be both *known* and
        recent. An unknown age is not a young one — see the freshness basis
        below — so it suppresses too. It is sound for a
        snapshot minutes or hours old, but some units never upload equipment
        telemetry at all — one real generator produced zero non-zero readings
        across 450,000 polls spanning five months and twenty-odd exercise
        runs. Synthesising a confident `0 rpm` from a nine-month-old snapshot
        presents an *absence of data* as a measurement. Past the bound, report
        nothing so the gap is visible.

        The shortcut also requires the field to *exist* in the payload. Some
        controllers send no ``EquipmentEventData`` block at all: on one real
        unit every output field was absent, and returning 0 for each of them
        invented sixteen measurements — which then became sixteen entities,
        because entity creation keys off having a non-None value. A confident
        ``0 W`` for a quantity the generator has never reported is worse than
        the gap it papers over.
        """
        reported = _find(sources, paths)
        if (
            generator_running is False
            and reported is not None
            and equipment_age is not None
            and equipment_age <= _OFF_MEANS_ZERO_MAX_AGE_SECONDS
        ):
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
    engine_hours: float | int | None = None
    engine_hours_source: str | None = None
    saw_zero = False
    for path, to_hours in _ENGINE_HOUR_SOURCES:
        candidate = _to_number(_find(sources, [path]))
        if candidate is None:
            continue
        if candidate == 0:
            saw_zero = True
            continue
        engine_hours = round(to_hours(candidate), 2)
        engine_hours_source = path
        break
    if engine_hours is None and saw_zero:
        engine_hours = 0

    # Self-calibration. When a controller populates more than one runtime
    # field, the candidates measure the same quantity and must agree once
    # converted — so any disagreement means a conversion factor above is
    # wrong for this firmware. A ratio near 1.9 is the signature of the
    # 111-second counter read as minutes, near 3.2 of it read as tenths of
    # an hour, near 32 of it read as hours, near 3600 of seconds/hours.
    #
    # This needs no knowledge of what the vendor's own app displays: the
    # payload cross-checks itself. It only works on units that report two
    # or more sources, which is why the commissioning-age guard exists as
    # well — that one covers the single-source case.
    candidates_in_hours = {
        path: to_hours(_to_number(_find(sources, [path])))
        for path, to_hours in _ENGINE_HOUR_SOURCES
        if _to_number(_find(sources, [path])) not in (None, 0)
    }
    engine_hours_disagreement: float | None = None
    if len(candidates_in_hours) > 1:
        low, high = min(candidates_in_hours.values()), max(candidates_in_hours.values())
        # Tolerate ordinary skew: the sources are snapshots taken at
        # different moments, so they differ by minutes, not by factors.
        if low > 0 and high / low > 1.05 and high - low > 0.5:
            engine_hours_disagreement = round(high / low, 2)

    # A unit cannot have run for longer than it has existed. This does not
    # correct the value — a generator retrofitted with a monitor legitimately
    # carries hours predating its EnergyTrak commissioning — but a reading
    # that fails it is the signature of a source whose unit we have guessed
    # wrong, which is exactly how the minutes-vs-hours bug reached users. It
    # is surfaced rather than silently patched.
    site_fields = _site_fields(site_doc)
    commissioned_at = site_fields.get("commissioned_at")
    hours_since_commissioning: float | None = None
    if isinstance(commissioned_at, datetime):
        hours_since_commissioning = (now - commissioned_at).total_seconds() / 3600
    engine_hours_implausible = (
        engine_hours is not None
        and hours_since_commissioning is not None
        and engine_hours > hours_since_commissioning
    )

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
        "starts_count": _to_number(_find(sources, _STARTS_PATHS)),
        "trips_count": _to_number(_find(sources, _TRIPS_PATHS)),
        # Provenance for the cumulative counters — diagnostics only, no entity.
        "engine_hours_source": engine_hours_source,
        "engine_hours_implausible": engine_hours_implausible,
        "engine_hours_disagreement_ratio": engine_hours_disagreement,
        "engine_hours_candidates_hours": {
            path: round(value, 3) for path, value in candidates_in_hours.items()
        },
        # The telemetry blocks verbatim, for diagnostics only. We have been
        # guessing at field semantics from names alone; if a controller does
        # declare its units anywhere, it is in here. Telemetry rather than
        # identity, and diagnostics redacts on top.
        "raw_event_blocks": {
            name: block
            for name in ("EquipmentEventData", "DeviceEventData", "MessageEventData")
            if isinstance(block := _find([raw_state], [f"Event.{name}"]), dict)
        },
        "counter_sources": {
            "engine_hours": _candidates(labelled_sources, _ENGINE_HOUR_PATHS),
            "starts_count": _candidates(labelled_sources, _STARTS_PATHS),
            "trips_count": _candidates(labelled_sources, _TRIPS_PATHS),
        },
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
        **site_fields,
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
        # The effective timestamp, so this and the age below cannot disagree.
        "equipment_data_timestamp": effective_ts,
        "equipment_data_age_seconds": (
            int(equipment_age) if equipment_age is not None else None
        ),
        "equipment_data_stale": equipment_stale,
        # The vendor's own claim, kept verbatim next to the age we actually
        # trust — when the two disagree, that disagreement is the diagnosis.
        "equipment_reported_timestamp": equipment_ts,
        "equipment_content_seen_at": content_seen_at,
        "equipment_timestamp_unreliable": timestamp_unreliable,
        # "observed" | "reported" | "unknown" — what the age above rests on.
        "equipment_freshness_basis": basis,
        "equipment_signature": signature,
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
