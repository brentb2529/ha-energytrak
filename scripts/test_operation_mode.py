"""operation_mode must answer "will it start on its own?" in cloud vocabulary.

Guards the false alarm of 2026-09-26: the controller reports a running
scheduled exercise in the same field as the selector position, so the bridge
said "Scheduled Exercise in Progress", the alias table had no entry, and the
NOT IN AUTO alert paged three minutes into the weekly exercise.
"""
import sys, types, importlib.util
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
    print(f"  {'PASS' if good else 'FAIL'}  {label:56} {got!r}")

t = ls._to_cloud_vocabulary
check("a running scheduled exercise still reads AUTO",
      t("operation_mode", "Scheduled Exercise in Progress"), "AUTO")
check("plain AUTO is unchanged", t("operation_mode", "Automatic"), "AUTO")
check("MANUAL still reads MANUAL", t("operation_mode", "Manual"), "MANUAL")
check("an unmapped value passes through, so it is never silently AUTO",
      t("operation_mode", "Generator Initialized"), "Generator Initialized")
check("ignition still folds to the cloud's 0/1", t("ignition_status", "Running"), 1)

print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
