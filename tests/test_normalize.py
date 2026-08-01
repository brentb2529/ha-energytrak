"""Offline checks for the parts of the integration that need no Home Assistant.

Run with ``python3 tests/test_normalize.py`` from the repository root. These
cover the Firestore decoding, the staleness rules and the magic-link parser —
the logic most likely to break, and the only logic that can be exercised
without a live EnergyTrak account.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "energytrak"
sys.path.insert(0, str(COMPONENT))

import normalize as N  # noqa: E402

NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)

FAILURES: list[str] = []


def check(label: str, got: object, want: object) -> None:
    """Assert and report one expectation."""
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{label}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(label)


# --- Firestore typed-value builders ------------------------------------


def s(v):
    return {"stringValue": v}


def d(v):
    return {"doubleValue": v}


def i(v):
    return {"integerValue": str(v)}


def b(v):
    return {"booleanValue": v}


def m(fields):
    return {"mapValue": {"fields": fields}}


def device_doc(*, clean, raw):
    return {
        "name": "projects/x/databases/(default)/documents/device/dev1",
        "updateTime": "2026-07-29T11:59:30.123456Z",
        "fields": {"details": m({"state": m(clean), "rawState": m(raw)})},
    }


def equip(ts, **kv):
    return {
        "Event": m(
            {
                "MessageEventData": m({"ActualDateUTC": s(ts)}),
                "EquipmentEventData": m(kv),
                "Alarms": m(
                    {
                        "Alarm1": m(
                            {
                                "HighCoolantTemperatureAlarm": s("0"),
                                "LowOilPressureAlarm": s("1"),
                            }
                        ),
                        "Alarm2": m({"OverspeedAlarm": s("1")}),
                    }
                ),
            }
        )
    }


# --- Stale snapshot, generator known OFF -> output fields read 0 --------
print("stale snapshot, generator off")
r = N.normalize_site(
    "site1",
    "Home",
    [device_doc(
        clean={
            "batteryVoltage": s("13.1"),
            "generatorRunning": b(False),
            "state": s("Ready"),
            "engineRuntimeHours": i(0),
            "utilityPresent": b(True),
            "lastUpdated": s("2026-07-29T11:59:00"),
        },
        raw=equip(
            "2026-04-19T11:20:17",
            EngineSpeed=i(0),
            GeneratorL1L2Voltage=i(0),
            LoadTotalPower=i(0),
            MainsL1L2Voltage=i(243),
            MainsFrequency=d(59.9),
            EngineHours=d(412.5),
            StartsCount=i(37),
            TripsCount=i(2),
            UtilityPowerMonitor=s("STOPPED UNDER 190V"),
        ),
    )],
    stale_threshold_seconds=900,
    now=NOW,
)
check("battery_voltage", r["battery_voltage"], 13.1)
check("engine_hours skips the cleanState 0", r["engine_hours"], 412.5)
check("ancient snapshot: no invented zero", r["engine_speed"], None)
check("ancient snapshot: no invented zero", r["output_voltage"], None)
check("grid_voltage passes through stale", r["grid_voltage"], 243)
check("grid_frequency passes through stale", r["grid_frequency"], 59.9)
check("starts_count survives staleness", r["starts_count"], 37)
check("equipment_data_stale", r["equipment_data_stale"], True)
check("grid_status from utilityPresent", r["grid_status"], "Present")
check("active_alarms", r["active_alarms"], ["LowOilPressureAlarm", "OverspeedAlarm"])
check("active_alarm_count", r["active_alarm_count"], 2)
check("alarm_flags cleared entry", r["alarm_flags"]["HighCoolantTemperatureAlarm"], False)
check(
    "naive timestamp treated as UTC",
    r["equipment_data_timestamp"],
    datetime(2026, 4, 19, 11, 20, 17, tzinfo=UTC),
)
check(
    "age in seconds",
    r["equipment_data_age_seconds"],
    int((NOW - datetime(2026, 4, 19, 11, 20, 17, tzinfo=UTC)).total_seconds()),
)
check("active", r["active"], False)

# --- Stale snapshot, generator RUNNING -> output fields report nothing ---
print("stale snapshot, generator running")
r = N.normalize_site(
    "site1",
    None,
    [device_doc(
        clean={
            "generatorRunning": b(True),
            "state": s("Running"),
            "batteryVoltage": s("12.9"),
            "utilityPresent": b(False),
        },
        raw=equip(
            "2026-04-19 07:20:17.000 -04:00",
            EngineSpeed=i(0),
            GeneratorL1L2Voltage=i(0),
            LoadTotalPower=i(0),
        ),
    )],
    stale_threshold_seconds=900,
    now=NOW,
)
check("engine_speed nulled when running and stale", r["engine_speed"], None)
check("output_voltage nulled when running and stale", r["output_voltage"], None)
check("load_power nulled when running and stale", r["load_power"], None)
check("active", r["active"], True)
check("grid_status lost", r["grid_status"], "Lost")
check(
    "offset timestamp parsed",
    r["equipment_data_timestamp"].astimezone(UTC),
    datetime(2026, 4, 19, 11, 20, 17, tzinfo=UTC),
)


# --- Recently-stale snapshot still gets the off-means-zero shortcut -------
print("recently stale snapshot, generator off")
recent = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
r = N.normalize_site(
    "site1", None,
    [device_doc(
        clean={"generatorRunning": b(False), "state": s("Ready")},
        raw=equip(recent, EngineSpeed=i(0), GeneratorL1L2Voltage=i(0),
                  LoadTotalPower=i(0)),
    )],
    stale_threshold_seconds=900, now=NOW,
)
check("3h-old snapshot with unit off still reads 0", r["engine_speed"], 0)
check("and output voltage", r["output_voltage"], 0)
check("but is still flagged stale", r["equipment_data_stale"], True)

# --- Fresh snapshot -> everything live ---------------------------------
print("fresh snapshot, running")
fresh = (NOW - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
r = N.normalize_site(
    "site1",
    None,
    [device_doc(
        clean={"generatorRunning": b(True), "state": s("Running")},
        raw=equip(
            fresh,
            EngineSpeed=i(3600),
            GeneratorL1L2Voltage=d(241.2),
            GeneratorL1NVoltage=d(120.4),
            GeneratorL2NVoltage=d(120.8),
            LoadTotalPower=i(4200),
            LoadL1Current=d(17.5),
            GeneratorAveragePowerFactor=d(0.98),
            GeneratorRFrequency=d(60.1),
            LoadTotalApparentPower=i(4400),
            LoadTotalReactivePower=i(300),
        ),
    )],
    stale_threshold_seconds=900,
    now=NOW,
)
check("not stale", r["equipment_data_stale"], False)
check("engine_speed", r["engine_speed"], 3600)
check("output_voltage", r["output_voltage"], 241.2)
check("output_voltage_l2n", r["output_voltage_l2n"], 120.8)
check("load_power", r["load_power"], 4200)
check("load_l1_current", r["load_l1_current"], 17.5)
check("power_factor", r["power_factor"], 0.98)
check("generator_frequency", r["generator_frequency"], 60.1)
check("load_apparent_power", r["load_apparent_power"], 4400)

# --- Utility-monitor string parsing ------------------------------------
print("utility-monitor string parsing")
for text, want in [
    ("STOPPED UNDER 190V", "Monitoring"),
    ("RUNNING OVER 260V", "Monitoring"),
    ("OK", "Good"),
    ("STOPPED", "Stopped"),
    ("weird", "weird"),
]:
    check(f"grid_status({text})", N._grid_status(None, text), want)

# --- Odd payload shapes -------------------------------------------------
print("rawState stored as a JSON string")
doc = {
    "updateTime": "2026-07-29T11:59:30Z",
    "fields": {
        "details": m(
            {
                "state": m({"generatorRunning": b(False)}),
                "rawState": s(
                    json.dumps({"Event": {"EquipmentEventData": {"StartsCount": 12}}})
                ),
            }
        )
    },
}
r = N.normalize_site("s", None, [doc], stale_threshold_seconds=900, now=NOW)
check("starts_count from JSON-string rawState", r["starts_count"], 12)

print("brand-new unit where every source reads 0 hours")
r = N.normalize_site(
    "s",
    None,
    [device_doc(clean={"engineRuntimeHours": i(0)}, raw=equip("2026-07-29T11:59:00"))],
    stale_threshold_seconds=900,
    now=NOW,
)
check("engine_hours stays 0 rather than unknown", r["engine_hours"], 0)

print("device reference extraction")
check(
    "extract_device_ids",
    N.extract_device_ids(
        {
            "fields": {
                "devices": {
                    "arrayValue": {
                        "values": [
                            {
                                "referenceValue": "projects/p/databases/(default)"
                                "/documents/device/abc123"
                            }
                        ]
                    }
                }
            }
        }
    ),
    ["abc123"],
)


# --- Multi-device site (the real shape) ---------------------------------
# A real site links -generator, -grid and the genmon monitor. They carry
# differently aged copies of the telemetry; the monitor's snapshot was 190
# days fresher, with higher counters. Reading only the first device (what the
# old Node service did) reports stale counters.
print("multi-device site merge")


def plain_equip(ts, **kv):
    """Equipment event with no Alarms block — the shape real units send."""
    block = equip(ts, **kv)
    del block["Event"]["mapValue"]["fields"]["Alarms"]
    return block


def named_doc(device_id, *, root=None, clean, raw):
    doc = device_doc(clean=clean, raw=raw)
    doc["name"] = f"projects/x/databases/(default)/documents/device/{device_id}"
    if root:
        doc["fields"].update({k: v for k, v in root.items()})
    return doc


gen_doc = named_doc(
    "1234567890-generator",
    root={"deviceType": s("generator"), "make": s(""), "modelNumber": s(""),
          "serialNumber": s(""), "name": s("Example Generator")},
    clean={"state": s("standby"), "status": s("healthy"), "health": s("healthy"),
           "fault": b(False), "generatorRunning": b(False), "batteryVoltage": s("13.1"),
           "engineRuntimeHours": s("0"), "utilityPresent": b(True),
           "smartModeEnabled": b(True)},
    raw=plain_equip("2025-10-23T11:20:17", EngineHours=s("75.61667"), StartsCount=s("225"),
              TripsCount=s("2"), MainsL1L2Voltage=s("244"), EngineSpeed=s("0")),
)
mon_doc = named_doc(
    "1234567890-genmon",
    root={"serialNumber": s("1234567890"), "name": s("Example Site")},
    clean={"state": s("connected"), "simNetworkConnected": b(True),
           "networkType": s("wifi"), "networkStrength": s("excellent"),
           "firmwareUpdateStatus": s("UP_TO_DATE"), "triggeredFaults": {"arrayValue": {}},
           "utilityPower": s("normal")},
    raw=plain_equip("2026-05-01T03:47:20", EngineHours=s("90.47"), StartsCount=s("274"),
              TripsCount=s("2"), MainsL1L2Voltage=s("241.0"), EngineSpeed=s("0"),
              FuelType=s("NG"), CurrentDgStatus=s("Stopped")),
)
grid_doc = named_doc(
    "1234567890-grid",
    root={"deviceType": s("grid")},
    clean={"state": s("online"), "utilityPresent": b(True), "voltage": s("244"),
           "utilityVoltage": i(241), "utilityPower": s("normal")},
    raw=plain_equip("2025-10-23T11:20:17", EngineHours=s("75.61667"), StartsCount=s("225")),
)

r = N.normalize_site("genmon-x", None, [gen_doc, mon_doc, grid_doc],
                     stale_threshold_seconds=900, now=NOW)
check("engine hours from the freshest snapshot", r["engine_hours"], 90.47)
check("starts from the freshest snapshot", r["starts_count"], 274)
check("snapshot sourced from the monitor", r["equipment_source"], "1234567890-genmon")
check("all devices recorded", len(r["device_ids"]), 3)
check("grid voltage from the grid heartbeat, not the old snapshot",
      r["grid_voltage"], 241)
check("status is the run state, not the health grade", r["status"], "standby")
check("health keeps the health grade", r["health"], "healthy")
check("boolean fault renders as text", r["fault_condition"], "None")
check("fuel type only present on the monitor", r["fuel_type"], "NG")
check("serial falls back to the monitor", r["serial_number"], "1234567890")
check("empty-string make is not used as a value", r["make"], None)
check("monitor connectivity", r["monitor_online"], True)
check("network strength", r["network_strength"], "excellent")
check("generator name preferred", r["name"], "Example Generator")
check("ancient snapshot: no invented zero", r["engine_speed"], None)

# Provenance: the reported 90.47 must be traceable to the field it came from,
# and the losing candidates must be visible alongside it. Without this, a
# counter that looks wrong on hardware we cannot see is unfalsifiable.
prov = r["counter_sources"]["engine_hours"]
check("cleanState's zero is recorded, not silently dropped",
      prov.get("cleanState:engineRuntimeHours"), "0")
check("the winning field is named",
      prov.get("rawState(monitor):Event.EquipmentEventData.EngineHours"), "90.47")
check("provenance labels the source by role, not by serial",
      any("1234567890" in k for k in prov), False)
check("starts provenance recorded",
      r["counter_sources"]["starts_count"].get(
          "rawState(monitor):Event.EquipmentEventData.StartsCount"), "274")

print("triggeredFaults become alarms")
mon_faults = named_doc(
    "site-genmon",
    clean={"triggeredFaults": {"arrayValue": {"values": [s("LowBattery"),
           m({"name": s("OverCrank")})]}}},
    raw=plain_equip("2026-07-29T11:59:00"),
)
r = N.normalize_site("x", None, [gen_doc, mon_faults], stale_threshold_seconds=900, now=NOW)
check("fault names extracted", r["active_alarms"], ["LowBattery", "OverCrank"])
check("fault text lists them", "LowBattery" in r["fault_condition"], True)


# --- Site document: exercise history ------------------------------------
# The equipment feed can be dormant for months while the unit still exercises
# weekly. The site document is then the only record that it ran at all.
print("site document exercise history")

site_doc = {
    "name": "projects/x/databases/(default)/documents/site/genmon-x",
    "updateTime": "2026-07-25T15:22:10.255236Z",
    "fields": {
        "state": s("ready"),
        "health": s("healthy"),
        "isSubscriptionActive": b(True),
        "commissionedAt": s("2025-03-12T20:09:11.415Z"),
        "exercise": m({
            "generatorRunSession": {"nullValue": None},
            "lastActivity": m({
                "finishedAt": s("2026-07-25T15:22:10.207Z"),
                "duration": i(1217984),
            }),
            "exerciseNotifications": m({
                "intervalDays": i(8),
                "nextExerciseDue": s("2026-08-02T15:22:10.207Z"),
            }),
        }),
        "malfunction": m({"hasMalfunction": b(False), "description": s("")}),
    },
}

r = N.normalize_site(
    "genmon-x", None, [gen_doc, mon_doc, grid_doc],
    stale_threshold_seconds=900, site_doc=site_doc, now=NOW,
)
check("last exercise timestamp", r["last_exercise_at"],
      datetime(2026, 7, 25, 15, 22, 10, 207000, tzinfo=UTC))
check("duration converted from ms to s", r["last_exercise_duration_seconds"], 1218)
check("next exercise due", r["next_exercise_due"],
      datetime(2026, 8, 2, 15, 22, 10, 207000, tzinfo=UTC))
check("exercise interval", r["exercise_interval_days"], 8)
check("no malfunction", r["has_malfunction"], False)
check("empty description is not surfaced", r["malfunction_description"], None)
check("site state", r["site_state"], "ready")
check("subscription", r["subscription_active"], True)
check("no run in progress", r["run_session"], None)
check("exercise data survives a dormant equipment feed",
      r["equipment_data_stale"], True)

print("site document absent")
r = N.normalize_site("x", None, [gen_doc], stale_threshold_seconds=900, now=NOW)
check("exercise fields default to None", r["last_exercise_at"], None)
check("malfunction defaults to None", r["has_malfunction"], None)

# --- Magic-link parsing -------------------------------------------------
# api.py imports aiohttp, which we do not want to require just to test pure
# string handling. Stub the two names it uses.
print("magic-link parsing")
stub = types.ModuleType("aiohttp")
stub.ClientError = type("ClientError", (Exception,), {})
stub.ClientSession = object
sys.modules.setdefault("aiohttp", stub)

# api.py uses relative imports, so load it under a synthetic package rooted at
# the component directory rather than importing the real one (whose __init__
# pulls in Home Assistant).
_pkg = types.ModuleType("_et")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["_et"] = _pkg
_spec = importlib.util.spec_from_file_location("_et.api", COMPONENT / "api.py")
A = importlib.util.module_from_spec(_spec)
sys.modules["_et.api"] = A
_spec.loader.exec_module(A)

check(
    "direct link",
    A.EnergyTrakClient.parse_magic_link(
        "https://app.energytrak.com/signin?apiKey=AIzaKEY&oobCode=CODE123&mode=signIn"
    ),
    ("AIzaKEY", "CODE123"),
)
check(
    "link wrapped in continueUrl",
    A.EnergyTrakClient.parse_magic_link(
        "https://example.page.link/?link=https%3A%2F%2Fapp.energytrak.com%2F"
        "%3FapiKey%3DAIzaKEY%26oobCode%3DCODE123"
    ),
    ("AIzaKEY", "CODE123"),
)
try:
    A.EnergyTrakClient.parse_magic_link("https://app.energytrak.com/signin")
except A.EnergyTrakAuthError as err:
    check("rejects a link with no code", str(err), "invalid_magic_link")
else:
    FAILURES.append("rejects a link with no code")
    print("  FAIL rejects a link with no code: no exception raised")

print()
print(f"{len(FAILURES)} failure(s)" + (": " + ", ".join(FAILURES) if FAILURES else ""))
sys.exit(1 if FAILURES else 0)
