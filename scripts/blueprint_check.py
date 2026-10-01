#!/usr/bin/env python3
"""Prove the shipped blueprints on a real Home Assistant, then clean up.

    python3 scripts/blueprint_check.py <token-file> [--host URL] [--keep-blueprints]

For each blueprint under blueprints/automation/energytrak/: import it from its
source_url (HA parses and validates the blueprint), save it into the instance,
create a throwaway automation from it bound to the first EnergyTrak device
with notify.persistent_notification, confirm the automation entity came up
(a bad template or trigger leaves it in 'unavailable' or an error in the
system log), then delete the throwaway automation. The saved blueprints are
left in place (they are harmless and useful) unless --remove-blueprints.
"""
import argparse, asyncio, glob, os, sys, time, yaml
import aiohttp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

async def ws_call(ws, c, **m):
    c[0] += 1; m["id"] = c[0]; await ws.send_json(m)
    while True:
        r = await ws.receive_json()
        if r.get("id") == c[0] and r.get("type") == "result": return r

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("token_file"); ap.add_argument("--host", default="http://192.168.2.250:8123"); ap.add_argument("--remove-blueprints", action="store_true")
    a = ap.parse_args(); tok = open(a.token_file).read().strip(); H = a.host; c = [0]; failures = 0
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    class L(yaml.SafeLoader): pass
    L.add_constructor("!input", lambda l, n: None)
    files = sorted(glob.glob(os.path.join(ROOT, "blueprints/automation/energytrak/*.yaml")))
    async with aiohttp.ClientSession(headers=hdr) as s:
        async with s.ws_connect(H.replace("http", "ws", 1) + "/api/websocket") as ws:
            await ws.receive_json(); await ws.send_json({"type": "auth", "access_token": tok}); await ws.receive_json()
            r = await ws_call(ws, c, type="config/device_registry/list")
            dev = next(d for d in r["result"] if any(i[0] == "energytrak" for i in d.get("identifiers", [])))
            print("device:", dev["name"], dev["id"])
            r = await ws_call(ws, c, type="system_log/list"); before = len(r["result"])
            for f in files:
                name = os.path.basename(f); path = f"energytrak/{name}"
                url = yaml.load(open(f), L)["blueprint"]["source_url"]
                r = await ws_call(ws, c, type="blueprint/import", url=url)
                if not r.get("success"):
                    print(f"  FAIL import {name}: {r.get('error')}"); failures += 1; continue
                raw = r["result"]["raw_data"]
                r = await ws_call(ws, c, type="blueprint/save", domain="automation", path=path, yaml=raw, allow_override=True)
                if not r.get("success"):
                    print(f"  FAIL save {name}: {r.get('error')}"); failures += 1; continue
                aid = "bptest_" + name.replace(".yaml", "")
                cfg = {"id": aid, "alias": f"[blueprint test] {name}", "use_blueprint": {"path": path, "input": {"generator": dev["id"], "notify": "notify.persistent_notification"}}}
                async with s.post(f"{H}/api/config/automation/config/{aid}", json=cfg) as rr:
                    body = await rr.text()
                    if rr.status != 200:
                        print(f"  FAIL create {name}: {rr.status} {body[:200]}"); failures += 1; continue
                await asyncio.sleep(4)
                async with s.get(f"{H}/api/states") as rr:
                    st = await rr.json()
                ent = next((e for e in st if e["attributes"].get("id") == aid), None)
                state = ent["state"] if ent else "MISSING"
                ok = state == "on"
                print(f"  {'ok  ' if ok else 'FAIL'} {name:42} automation state={state}")
                failures += 0 if ok else 1
                async with s.delete(f"{H}/api/config/automation/config/{aid}") as rr:
                    pass
                if a.remove_blueprints:
                    await ws_call(ws, c, type="blueprint/delete", domain="automation", path=path)
            await asyncio.sleep(3)
            r = await ws_call(ws, c, type="system_log/list")
            new = [" ".join(i.get("message") or [])[:160] for i in r["result"][: max(0, len(r["result"]) - before)]]
            errs = [m for m in new if "blueprint" in m.lower() or "template" in m.lower() or "automation" in m.lower()]
            print("  new log entries mentioning automation/template/blueprint:", len(errs)); [print("    ", m) for m in errs[:8]]
            failures += len(errs)
    print("PASS" if failures == 0 else f"FAIL ({failures})"); return 0 if failures == 0 else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
