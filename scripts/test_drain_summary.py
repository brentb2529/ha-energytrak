"""Exercise BacklogDrain's recovery summary without Home Assistant."""
import sys, types, importlib.util, pathlib, asyncio
def _mk(n, **a):
    m = types.ModuleType(n); [setattr(m,k,v) for k,v in a.items()]; sys.modules[n]=m; return m
fired = []
class Bus:
    def async_fire(self, ev, data): fired.append((ev, data))
class Hass:
    bus = Bus()
    def async_add_executor_job(self, f, *a): pass
_mk("homeassistant")
_mk("homeassistant.core", HomeAssistant=Hass, callback=lambda f: f)
_mk("homeassistant.util"); _mk("homeassistant.util.dt", utcnow=lambda: None)
_mk("homeassistant.components"); _mk("homeassistant.components.recorder")
_mk("homeassistant.components.recorder.statistics", async_add_external_statistics=lambda *a, **k: None)
_mk("homeassistant.components.recorder.models", StatisticData=dict, StatisticMetaData=dict, StatisticMeanType=types.SimpleNamespace(ARITHMETIC=1))

# backlog.py does `from .const import DOMAIN`, so give it a real package.
pkg = types.ModuleType("et"); pkg.__path__ = ["custom_components/energytrak"]
sys.modules["et"] = pkg
_mk("et.const", DOMAIN="energytrak")
spec = importlib.util.spec_from_file_location("et.backlog", "custom_components/energytrak/backlog.py")
bl = importlib.util.module_from_spec(spec); sys.modules["et.backlog"] = bl
try: spec.loader.exec_module(bl)
except Exception as e:
    print("import failed:", type(e).__name__, e); raise SystemExit(1)

NAMES = ["low_oil_pressure", "high_coolant_temperature", "battery_low_voltage"]
class FakeBridge:
    connected = True
    def __init__(self, batches): self.batches = list(batches)
    def raw(self, _): return self.batches.pop(0) if self.batches else "-"
    async def async_call(self, *a, **k): return True

def drain_with(records):
    d = bl.BacklogDrain(Hass(), FakeBridge([records, "-"]), NAMES, None)
    fired.clear()
    asyncio.run(d.async_drain())
    return [f for f in fired if f[0] == bl.HA_EVENT_SUMMARY]

TS = 1789000000
def rec(seq, typ, idx, ts=TS): return f"{seq}:{ts}:{typ}:{idx}"

ok = True
def check(label, got, want):
    global ok
    good = got == want; ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label:52} {got!r}")

# 1. an alarm that fired AND cleared while HA was away -- the whole point
s = drain_with("|".join([rec(1, bl.EVENT_ALARM_SET, 0), rec(2, bl.EVENT_ALARM_CLEAR, 0)]))
check("set+clear during outage still reported", bool(s), True)
check("  ...and NOT marked still active", s[0][1]["still_active"] if s else None, [])
check("  ...names it", s[0][1]["alarms"] if s else None, ["low_oil_pressure"])

# 2. still asserted at the end
s = drain_with(rec(1, bl.EVENT_ALARM_SET, 1))
check("alarm still asserted is flagged", s[0][1]["still_active"] if s else None, ["high_coolant_temperature"])

# 3. boots only -- must NOT notify
s = drain_with("|".join([rec(1, bl.EVENT_BOOT, 0), rec(2, bl.EVENT_BOOT, 0)]))
check("boot-only drain fires no summary", s, [])

# 4. several alarms coalesce into ONE summary
s = drain_with("|".join([rec(1, bl.EVENT_ALARM_SET, 0), rec(2, bl.EVENT_ALARM_SET, 2)]))
check("multiple alarms -> one summary", len(s), 1)
check("  ...count", s[0][1]["count"] if s else None, 2)

# 5. unknown clock (ts=0) still reported
s = drain_with(rec(1, bl.EVENT_ALARM_SET, 0, ts=0))
check("ts=0 (no clock) still reported", bool(s), True)
check("  ...any_time_known False", s[0][1]["any_time_known"] if s else None, False)

print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
