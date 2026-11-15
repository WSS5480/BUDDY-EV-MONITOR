"""Check the Tesla side against a fake Fleet API (no real car or Tesla account needed).

Run: python tests/fake_tesla.py
Covers: early-rejected access token is refreshed once, sleeping car is woken, a refusal is
reported, a plain REST call refused for "command protocol" switches to signed commands, window
commands send the car's location, tokens and key are stored encrypted, the car list and VIN
lookup work with no TESLA_VIN set.
"""

import asyncio
import os
import sys
import tempfile
import time

os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "fake.db")
os.environ["TESLA_VIN"] = "VIN1"
os.environ["SECRET_KEY"] = "fake-test-secret"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402

from app import config, tesla  # noqa: E402
from app.store import store  # noqa: E402

fails = 0
calls: list[str] = []
state = {"asleep": True, "rest_needs_key": False, "signed_used": False}


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    fails += 0 if cond else 1


async def handler(req: web.Request) -> web.Response:
    p = req.path
    calls.append(p)
    if req.headers.get("Authorization") == "Bearer stale":
        return web.json_response({"error": "token expired (401)"}, status=401)
    if p == "/api/1/products":
        return web.json_response({"response": [{"energy_site_id": 1, "resource_type": "battery"},
                                               {"id": 9, "vin": "5YJ3E1EA7KF000001", "display_name": "Buddy", "state": "online"}]})
    if p == "/api/1/vehicles" or p.endswith("/vehicles/list"):
        return web.json_response({"error": "wrong endpoint"}, status=404)
    if p.endswith("fleet_status"):
        return web.json_response({"response": {"key_paired_vins": ["VIN1", "5YJ3E1EA7KF000001"], "unpaired_vins": [],
                                               "vehicle_info": {"VIN1": {"vehicle_command_protocol_required": False}}}})
    if p.endswith("wake_up"):
        state["asleep"] = False
        return web.json_response({"response": {"state": "online"}})
    if p.endswith("/vehicles/VIN1"):
        return web.json_response({"response": {"state": "online"}})
    if state["asleep"]:
        return web.json_response({"error": "vehicle unavailable"}, status=408)
    if p.endswith("vehicle_data"):
        return web.json_response({"response": {"drive_state": {"latitude": 30.1, "longitude": -97.7}}})
    if p.endswith("honk_horn"):
        return web.json_response({"response": {"result": False, "reason": "not in park"}})
    if p.endswith("window_control"):
        body = await req.json()
        ok = body.get("lat") == 30.1 and body.get("lon") == -97.7
        return web.json_response({"response": {"result": ok, "reason": "" if ok else "no location"}})
    if p.endswith("door_unlock") and state["rest_needs_key"]:
        return web.json_response({"error": "Tesla Vehicle Command Protocol required"}, status=422)
    return web.json_response({"response": {"result": True, "reason": ""}})


async def main():
    app = web.Application()
    app.router.add_route("*", "/{t:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8769).start()
    config.TESLA_API_BASE = "http://127.0.0.1:8769"
    await store.open()
    await tesla.startup()
    tesla.TeslaFleetApi.Vehicles.Fleet._wake_poll_interval = 0.2

    # token Tesla rejects early -> one refresh, then success
    await tesla._save_tokens({"access_token": "stale", "refresh_token": "r1", "expires_in": 28800})
    posts = {"n": 0}

    async def fake_post(data):
        posts["n"] += 1
        return {"access_token": "good", "refresh_token": "r2", "expires_in": 28800}

    tesla._token_post = fake_post
    out = await tesla.command(None, "door_lock")
    check("asleep car woken and command sent after token refresh", out == "Done." and posts["n"] == 1, f"{out} {posts}")
    stored = await store.get(tesla.K_TOKENS)
    check("tokens stored encrypted", set(stored) == {"enc"} and "good" not in str(stored))
    check("newest refresh token kept", (await tesla._load_tokens())["refresh_token"] == "r2")
    check("key stored encrypted", set(await store.get(tesla.K_KEY)) == {"enc"})

    try:
        await tesla.command(None, "honk_horn")
        check("refusal reported", False)
    except tesla.CarError as e:
        check("refusal reported", "not in park" in str(e), str(e))

    out = await tesla.command(None, "window_control", "close")
    check("window command sends the car's location", out == "Done.", out)

    state["rest_needs_key"] = True
    try:
        await tesla.command(None, "door_unlock")
    except BaseException as e:  # the fake can't speak the signed protocol; we only check the switch
        print("   (signed call against fake ended with", type(e).__name__, ")")
    check("REST refusal switches the car to signed commands", (await store.get(tesla.K_PROTOCOL + "VIN1"))["signed"] is True)

    # no TESLA_VIN: the app finds the car itself (this is the path that broke live)
    config.TESLA_VIN = ""
    cars = await tesla.list_vehicles()
    check("car list read from /api/1/products, energy products skipped",
          cars == [{"vin": "5YJ3E1EA7KF000001", "name": "Buddy", "state": "online"}], str(cars))
    await store.delete(tesla.K_VIN)
    check("VIN found without TESLA_VIN", await tesla.resolve_vin(None) == "5YJ3E1EA7KF000001")
    paired = await tesla.key_paired()
    check("key pairing check runs end to end for the found car", paired is True, str(paired))

    await tesla.shutdown()
    await store.close()
    await runner.cleanup()
    print("\nFAILED:" if fails else "\nALL PASSED", fails or "")
    sys.exit(1 if fails else 0)


asyncio.run(main())
