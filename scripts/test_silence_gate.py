"""LocalBridge.available must reject silence, not just disconnection."""
import sys, types, importlib.util, pathlib
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

from time import monotonic
b = ls.LocalBridge.__new__(ls.LocalBridge)
b._connected=True; b._values={"bus_healthy":True}; b._health_oid="bus_healthy"
b._last_rx=monotonic(); b.host="192.168.1.62"

ok=True
def check(label, got, want):
    global ok; good=got==want; ok&=good
    print(f"  {'PASS' if good else 'FAIL'}  {label:54} {got}")

check("connected + healthy + just heard from it", b.available, True)
b._last_rx = monotonic() - (ls.RX_SILENCE_LIMIT + 5)
check("connected + healthy but SILENT past the limit", b.available, False)
b._last_rx = monotonic() - (ls.RX_SILENCE_LIMIT - 20)
check("silent but still inside the limit", b.available, True)
b._last_rx = 0.0
check("never heard anything at all", b.available, False)
b._last_rx = monotonic(); b._connected=False
check("fresh messages but socket down", b.available, False)
b._connected=True; b._values={"bus_healthy":False}
check("fresh messages but generator silent on the bus", b.available, False)
print("\nALL PASS" if ok else "\nFAILURES"); sys.exit(0 if ok else 1)
