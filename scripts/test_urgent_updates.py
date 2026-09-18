"""Exercise LocalBridge._on_state's urgent-change detection without HA."""
import sys, types, pathlib

# Minimal HA stubs so local_source imports standalone.
def _mk(name, **attrs):
    m = types.ModuleType(name); [setattr(m, k, v) for k, v in attrs.items()]; sys.modules[name] = m; return m
_mk("homeassistant")
_mk("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
scheduled = []
_mk("homeassistant.helpers")
_mk("homeassistant.helpers.event",
    async_call_later=lambda hass, delay, cb: scheduled.append((delay, cb)) or (lambda: None))

sys.path.insert(0, str(pathlib.Path("custom_components").resolve()))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "ls", "custom_components/energytrak/local_source.py")
ls = importlib.util.module_from_spec(spec)
sys.modules["ls"] = ls
try:
    spec.loader.exec_module(ls)
except Exception as e:
    print("import failed:", type(e).__name__, e); raise SystemExit(1)

b = ls.LocalBridge.__new__(ls.LocalBridge)
b.hass = None
b._values = {}
b._keys = {1: "common_shutdown", 2: "battery_voltage", 3: "utility_power_failure"}
b._urgent_oids = frozenset({"common_shutdown", "utility_power_failure"})
b._urgent_listeners = []
b._urgent_timer = None

class S:
    def __init__(self, key, state, missing=False):
        self.key, self.state, self.missing_state = key, state, missing

def fired():
    n = len(scheduled); scheduled.clear(); return n

cases = []
b._on_state(S(1, False));  cases.append(("first value of an alarm (absent->False)", fired(), 0))
b._on_state(S(1, False));  cases.append(("alarm republished unchanged", fired(), 0))
b._on_state(S(1, True));   cases.append(("ALARM ASSERTS", fired(), 1))
b._urgent_timer = None
b._on_state(S(1, False));  cases.append(("alarm clears", fired(), 1))
b._urgent_timer = None
b._on_state(S(2, 13.1));   cases.append(("first battery voltage", fired(), 0))
b._on_state(S(2, 12.4));   cases.append(("battery voltage CHANGES (not urgent)", fired(), 0))
b._on_state(S(3, False));  cases.append(("first utility_power_failure", fired(), 0))
b._on_state(S(3, True));   cases.append(("UTILITY POWER LOST", fired(), 1))

# coalescing: a burst inside one window must schedule exactly one timer
b._urgent_timer = None
b._on_state(S(1, True)); b._on_state(S(3, False)); b._on_state(S(1, False))
cases.append(("burst of 3 urgent changes coalesced", fired(), 1))

ok = True
for name, got, want in cases:
    good = got == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {name:42} fired={got} expected={want}")
print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
