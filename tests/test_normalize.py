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
r = N.normalize_device(
    "site1",
    "Home",
    device_doc(
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
    ),
    stale_threshold_seconds=900,
    now=NOW,
)
check("battery_voltage", r["battery_voltage"], 13.1)
check("engine_hours skips the cleanState 0", r["engine_hours"], 412.5)
check("engine_speed reads 0 when off", r["engine_speed"], 0)
check("output_voltage reads 0 when off", r["output_voltage"], 0)
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
r = N.normalize_device(
    "site1",
    None,
    device_doc(
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
    ),
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

# --- Fresh snapshot -> everything live ---------------------------------
print("fresh snapshot, running")
fresh = (NOW - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
r = N.normalize_device(
    "site1",
    None,
    device_doc(
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
    ),
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
r = N.normalize_device("s", None, doc, stale_threshold_seconds=900, now=NOW)
check("starts_count from JSON-string rawState", r["starts_count"], 12)

print("brand-new unit where every source reads 0 hours")
r = N.normalize_device(
    "s",
    None,
    device_doc(clean={"engineRuntimeHours": i(0)}, raw=equip("2026-07-29T11:59:00")),
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
