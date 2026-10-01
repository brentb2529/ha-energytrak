#!/usr/bin/env python3
"""Install a release of this integration on a Home Assistant instance via HACS.

    python3 scripts/hacs_install.py <token-file> <version> [--host http://host:8123] [--no-restart]

HACS caches its view of the repository: straight after a release it still
reports the previous version as the newest and shows no update. So this
refreshes first, then downloads the named version, then restarts Home
Assistant (custom component code is only reloaded by a restart), and finally
waits for the integration's config entry to report loaded.

Needs aiohttp. The token file holds a long-lived access token, nothing else.
"""
import argparse
import asyncio
import sys
import time

import aiohttp

REPO_ID = "1316262403"  # brentb2529/ha-energytrak in HACS


async def ws_call(ws, counter, **msg):
    counter[0] += 1
    msg["id"] = counter[0]
    await ws.send_json(msg)
    while True:
        r = await ws.receive_json()
        if r.get("id") == counter[0] and r.get("type") == "result":
            return r


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("token_file")
    ap.add_argument("version")
    ap.add_argument("--host", default="http://192.168.2.250:8123")
    ap.add_argument("--no-restart", action="store_true")
    a = ap.parse_args()
    token = open(a.token_file).read().strip()
    ws_url = a.host.replace("http", "ws", 1) + "/api/websocket"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    counter = [0]
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.ws_connect(ws_url) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            if (await ws.receive_json()).get("type") != "auth_ok":
                print("auth failed"); return 1
            r = await ws_call(ws, counter, type="hacs/repository/refresh", repository=REPO_ID)
            print("refresh:", r.get("success"), r.get("error", ""))
            r = await ws_call(ws, counter, type="hacs/repository/download", repository=REPO_ID, version=a.version)
            print("download:", r.get("success"), r.get("error", ""))
            if not r.get("success"):
                return 1
        if a.no_restart:
            return 0
        try:
            async with s.post(f"{a.host}/api/services/homeassistant/restart") as r:
                print("restart:", r.status)
        except Exception as e:  # noqa: BLE001 - the socket drops mid-restart; that is success
            print("restart: requested (connection closed:", type(e).__name__ + ")")
        t0 = time.time()
        await asyncio.sleep(45)
        while time.time() - t0 < 240:
            try:
                async with s.ws_connect(ws_url) as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": token})
                    await ws.receive_json()
                    r = await ws_call(ws, counter, type="config_entries/get", domain="energytrak")
                    states = [e.get("state") for e in r.get("result", [])]
                    if states and all(st == "loaded" for st in states):
                        print(f"energytrak loaded after {int(time.time() - t0)} s")
                        return 0
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(10)
        print("timed out waiting for the config entry to load")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
