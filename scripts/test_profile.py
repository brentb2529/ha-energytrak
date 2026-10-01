"""The commissioning profile: parsed, flattened, and applied to alarm counting.

Guards three things. The profile string from packages/commissioning.yaml must
flatten into the keys the entities read; the device model must follow the
learned family and never assume GC-1032 on an unknown controller; and the
four-state alarm rule must stop conditions that read 0 or F at rest from
being counted as faults, while still counting a real 1 -> 0 transition.
Older firmware (no profile) must behave exactly as before.
"""
import sys, types, importlib.util, json
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

ok = True
def check(label, got, want):
    global ok; good = got == want; ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label:60} {got!r}")

contract = ls.load_contract()
bits = contract.get("alarm_bits", {})
check("contract carries alarm_bits for the counted alarms",
      sum(1 for k in contract["alarm_keys"] if k in bits) >= 30, True)

def bridge(profile, values):
    b = ls.LocalBridge.__new__(ls.LocalBridge)
    b._contract = contract; b._build_maps()
    b._values = dict(values); b._connected = True; b._device_info = None
    if profile is not None: b._values["controller_profile"] = json.dumps(profile)
    return b

# Brent's real profile, captured 2026-09-29 (base = the August baseline block).
BASE = "11001111FF111111F0F0111F010000111001111100000F011F1111FE"
PROFILE = {"v":2,"fam":"gc103x","proto":"8003","fw":"4113","model":"FFFF","n":87,
           "map":"FFFFFFFFFFFFFFFF007FFFFF00000000","alt":"00000000","base":BASE,
           "prov":1,"batt":1,"util":1,"t":1790737519}
check("v2 profile string fits Home Assistant's 255-char state limit", len(json.dumps(PROFILE, separators=(",",":"))) < 255, True)

# --- flattening -------------------------------------------------------------
f = bridge(PROFILE, {})._profile_fields()
check("commissioned", f["commissioned"], True)
check("family", f["controller_family"], "gc103x")
check("protocol shown as hex", f["controller_protocol"], "0x8003")
check("model word FFFF shown as absent", f["controller_model_word"], "absent")
check("registers present", f["registers_present"], 87)
check("indeterminate count (derived from base)", f["alarm_inputs_indeterminate"], 17)
check("ok / absent / unexpected (derived from base)",
      (f["alarm_inputs_ok"], f["alarm_inputs_absent"], f["alarm_inputs_unexpected"]), (30, 8, 1))
check("baseline flagged provisional", f["alarm_baseline_provisional"], True)
check("no profile -> commissioned False only", bridge(None, {})._profile_fields(), {"commissioned": False})
check("garbage profile string is ignored", bridge(None, {"controller_profile": "not commissioned"}).profile(), None)

# --- device model -------------------------------------------------------------
check("model from learned family", bridge(PROFILE, {}).device_identity()["model"], "GC-1030 series")
check("unknown family is not called a GC-1032",
      bridge({**PROFILE, "fam":"unknown"}, {}).device_identity()["model"], "Unknown controller")
check("snapshot is None while the bus is not healthy (trust decision unchanged)",
      bridge(PROFILE, {}).snapshot(), None)
check("old firmware keeps the historical label", bridge(None, {}).device_identity()["model"], "GC-1032")

# --- four-state alarm rule ----------------------------------------------------
# On Brent's own build the counted alarm_keys already exclude every condition
# that read 0 or F in the August baseline (gen_alarms.py disabled them), so
# the rule changes nothing HERE. It matters on a controller whose inputs
# differ from that build-time baseline: conditions the firmware counts, that
# read 0 at rest on THAT machine. Simulate it by counting all decoded alarms.
def at_rest(oid):
    b = bits[oid]; w = int(BASE[(b["reg"]-0x40)*4:(b["reg"]-0x40)*4+4], 16)
    return w & b["mask"], b
decoded = [k for k in bits if 0x40 <= bits[k]["reg"] <= 0x4D]
zero = next(k for k in decoded if at_rest(k)[0] == at_rest(k)[1]["value"])
absent = next(k for k in decoded if at_rest(k)[0] == at_rest(k)[1]["mask"])
wired = next(k for k in decoded if at_rest(k)[0] not in (at_rest(k)[1]["value"], at_rest(k)[1]["mask"]))
print(f"  (using indeterminate={zero!r} absent={absent!r} wired={wired!r})")

def counting_all(profile, vals):
    b = bridge(profile, vals); b._alarm_oids = decoded; return b

vals = {k: False for k in decoded}
vals[zero] = True                      # firmware decode says asserted, but it read 0 at rest
vals[absent] = True                    # and this input is not fitted (F at rest)
d = counting_all(PROFILE, vals)._derive()
check("0-at-rest and F-at-rest conditions are NOT counted", d["active_alarm_count"], 0)
check("...and the list says None", d["active_alarms"], "None")

vals[wired] = True                     # a condition that read 1 at rest now reads asserted
b = counting_all(PROFILE, vals); d = b._derive()
check("a real 1 -> 0 transition IS counted", d["active_alarm_count"], 1)
check("...and named", b._pretty(wired) in d["active_alarms"], True)

# Old firmware: no profile -> raw decode, all three count (the pre-existing behaviour).
d = counting_all(None, vals)._derive()
check("without a profile the raw decode is unchanged", d["active_alarm_count"], 3)

# A truncated or malformed baseline must fall back to the raw decode, never crash.
d = counting_all({**PROFILE, "base": "1100"}, vals)._derive()
check("malformed baseline falls back to raw decode", d["active_alarm_count"], 3)
d = counting_all({**PROFILE, "base": "zz" * 28}, vals)._derive()
check("non-hex baseline falls back to raw decode", d["active_alarm_count"], 3)

# --- per-alarm input states shape the entities ------------------------------
st = bridge(PROFILE, {})._profile_fields()["alarm_input_states"]
check("indeterminate input classified", st[zero], "indeterminate")
check("absent input classified", st[absent], "absent")
check("wired input classified ok", st[wired], "ok")
check("status-word bits have no rest state", any(bits[k]["reg"] > 0x4D and k in st for k in bits), False)
unk = bridge({**PROFILE, "fam":"unknown"}, {})
check("unknown family: every decoded alarm is untrusted", set(unk._profile_fields()["alarm_input_states"].values()), {"untrusted"})
unk._alarm_oids = decoded; unk._values.update(vals)
check("unknown family: nothing is counted even when asserted", unk._derive()["active_alarm_count"], 0)

# --- alarm entities wait for commissioning on capable firmware ---------------
pending = bridge(None, {"controller_profile": "not commissioned", "bus_healthy": True, "bus_age": 1.0})
pending._last_rx = __import__("time").monotonic()
ps = pending.snapshot() or {}
check("capable but not yet commissioned -> alarm entities pending", (ps.get("commissioning_supported"), ps.get("alarm_entities_pending")), (True, True))
old = bridge(None, {"bus_healthy": True, "bus_age": 1.0}); old._last_rx = __import__("time").monotonic()
os_ = old.snapshot() or {}
check("old firmware (no profile entity) -> not pending", (os_.get("commissioning_supported"), os_.get("alarm_entities_pending")), (False, False))

# --- the derived keys ride in the snapshot --------------------------------
live = bridge(PROFILE, {"bus_healthy": True, "bus_age": 1.0})
live._last_rx = __import__("time").monotonic()
snap = live.snapshot() or {}
check("snapshot carries commissioned/family/registers/indeterminate",
      (snap.get("commissioned"), snap.get("controller_family"), snap.get("registers_present"), snap.get("alarm_inputs_indeterminate")),
      (True, "gc103x", 87, 17))

print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
