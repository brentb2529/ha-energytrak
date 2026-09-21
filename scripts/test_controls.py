"""Bridge controls: only allowlisted settings are writable, and writes land.

Guards the one place this integration writes to the bridge. The bridge has
other number entities -- the MQTT port among them -- and a control that is
not in the contract's allowlist must never become writable from Home
Assistant, even if the bridge reports it.
"""
import sys, types, importlib.util, asyncio
def _mk(n, **a):
    m=types.ModuleType(n); [setattr(m,k,v) for k,v in a.items()]; sys.modules[n]=m; return m
_mk("homeassistant"); _mk("homeassistant.core", HomeAssistant=object, callback=lambda f:f)
_mk("homeassistant.helpers"); _mk("homeassistant.helpers.event", async_call_later=lambda *a,**k:(lambda:None))
pkg=types.ModuleType("et"); pkg.__path__=["custom_components/energytrak"]; sys.modules["et"]=pkg
_mk("et.const", DOMAIN="energytrak")
spec=importlib.util.spec_from_file_location("et.local_source","custom_components/energytrak/local_source.py")
ls=importlib.util.module_from_spec(spec); sys.modules["et.local_source"]=ls
try: spec.loader.exec_module(ls)
except Exception as e: print("import failed:",type(e).__name__,e); raise SystemExit(1)

def info(cls, oid, key):
    return type(cls, (), {"object_id": oid, "key": key})()
sent = []
class FakeClient:
    def number_command(self, key, state): sent.append(("number", key, state))
    def switch_command(self, key, state): sent.append(("switch", key, state))
    def text_command(self, key, state): sent.append(("text", key, state))
    def button_command(self, key): sent.append(("button", key))

ok = True
def check(label, got, want):
    global ok; good = got == want; ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label:52} {got!r}")

contract = ls.load_contract()
b = ls.LocalBridge.__new__(ls.LocalBridge)
b._contract = contract; b._values = {}; b._client = FakeClient(); b._connected = True

allowed = contract.get("controls", {})
check("the bundled contract lists the LED control", "status_led_brightness" in allowed, True)
# Real object_ids, as the bridge reports them (note the hyphen in "wi-fi").
for oid in ("wi-fi_ssid", "wi-fi_password", "apply_wi-fi", "mqtt_username",
            "mqtt_password", "web_username", "web_password", "apply_web_login",
            "factory_reset"):
    check(f"never allowlisted: {oid}", oid in allowed, False)

b._learn_controls([
    [info("NumberInfo", "status_led_brightness", 7), info("NumberInfo", "mqtt_port", 8)],
    [info("TextInfo", "mqtt_broker", 10), info("TextInfo", "wi-fi_password", 11)],
    [info("SwitchInfo", "feature__infohub_passthrough", 12)],
    [info("ButtonInfo", "restart_bridge", 13), info("ButtonInfo", "factory_reset", 14)],
    [info("SensorInfo", "battery_voltage", 9)],
    # right name, wrong kind: must not be treated as the control
    [info("SensorInfo", "reset_bus_counters", 15)],
])
check("only allowlisted controls of the right kind are learned", sorted(b.controls),
      ["feature__infohub_passthrough", "mqtt_broker", "mqtt_port",
       "restart_bridge", "status_led_brightness"])

check("number: write succeeds", asyncio.run(b.async_control("status_led_brightness", 30)), True)
check("  ...shows the new value immediately", b.raw("status_led_brightness"), 30.0)
check("switch: write succeeds", asyncio.run(b.async_control("feature__infohub_passthrough", 1)), True)
check("text: write succeeds", asyncio.run(b.async_control("mqtt_broker", "10.0.0.5")), True)
check("button: press succeeds", asyncio.run(b.async_control("restart_bridge")), True)
check("  ...a button leaves no value behind", b.raw("restart_bridge"), None)
check("each command reached the client with the right key and type", sent,
      [("number", 7, 30.0), ("switch", 12, True), ("text", 10, "10.0.0.5"), ("button", 13)])

sent.clear()
check("a bridge entity NOT on the allowlist is refused",
      asyncio.run(b.async_control("wi-fi_password", "x")), False)
check("factory reset is refused", asyncio.run(b.async_control("factory_reset")), False)
check("  ...and nothing is sent", sent, [])

b._connected = False
check("a write while disconnected returns False",
      asyncio.run(b.async_control("status_led_brightness", 50)), False)

b._connected = True
b._learn_controls([[info("SensorInfo", "battery_voltage", 9)]])
check("older firmware without the controls -> none", b.controls, {})

print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
