"""async_call must actually find the bridge's actions.

Guards the bug that made every backlog acknowledgement silently fail: the
services half of list_entities_services() was bound to a throwaway local, so
self._services stayed empty and async_call returned False forever -- with no
error anywhere, and a critical notification every 30 seconds as the symptom.
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

class Svc:
    def __init__(self, name): self.name = name
called = []
class FakeClient:
    async def execute_service(self, svc, kwargs):
        called.append((svc.name, kwargs))

ok = True
def check(label, got, want):
    global ok; good = got == want; ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label:52} {got!r}")

b = ls.LocalBridge.__new__(ls.LocalBridge)
b._client = FakeClient(); b._connected = True
b._services = {s.name: s for s in [Svc("backlog_ack"), Svc("backlog_erase")]}

check("a known action is found and invoked",
      asyncio.run(b.async_call("backlog_ack", seq=11)), True)
check("  ...and actually reached the client", called, [("backlog_ack", {"seq": 11})])
check("an unknown action returns False, does not raise",
      asyncio.run(b.async_call("nope")), False)

# the regression itself
b._services = {}
check("EMPTY services map -> ack cannot work (the bug)",
      asyncio.run(b.async_call("backlog_ack", seq=1)), False)

# and a bridge that is merely disconnected
b._services = {s.name: s for s in [Svc("backlog_ack")]}
b._connected = False
check("disconnected bridge refuses rather than pretending",
      asyncio.run(b.async_call("backlog_ack", seq=1)), False)

print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
