"""Check every Tesla library call the app makes against the real installed tesla-fleet-api.

Run: python tests/test_library_api.py
Catches calls to methods that don't exist or take different arguments (a live bug once came from
calling api.vehicles.list(), which doesn't exist on the account-level object).
"""

import ast
import inspect
import os
import sys

from tesla_fleet_api import TeslaFleetApi
from tesla_fleet_api.tesla.vehicle.fleet import VehicleFleet
from tesla_fleet_api.tesla.vehicle.signed import VehicleSigned
from tesla_fleet_api.tesla.vehicle.vehicles import Vehicles

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    fails += 0 if cond else 1


def binds(fn, args, kwargs):
    try:
        inspect.signature(fn).bind(None, *args, **kwargs)
        return True
    except TypeError:
        return False


# 1. Every command name main.py sends through tesla.command(), with its arguments, exists on both
#    the plain (Fleet) and the signed vehicle objects.
tree = ast.parse(open(os.path.join(ROOT, "app", "main.py")).read())
sent = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "command" and len(node.args) >= 2:
        name = node.args[1].value
        extra = [a.value if isinstance(a, ast.Constant) else 21.0 for a in node.args[2:]]
        sent.append((name, extra))
check("found the app's car commands", len(sent) >= 13, str(len(sent)))
for name, extra in sent:
    for cls in (VehicleFleet, VehicleSigned):
        fn = getattr(cls, name, None)
        kw = {"lat": 1.0, "lon": 2.0} if name == "window_control" else {}
        check(f"{cls.__name__}.{name}{tuple(extra)}", fn is not None and binds(fn, extra, kw))

# 2. The other calls tesla.py makes on the real objects.
check("TeslaFleetApi.products()", binds(TeslaFleetApi.products, [], {}))
check("vehicles.createFleet / createSigned", hasattr(Vehicles, "createFleet") and hasattr(Vehicles, "createSigned"))
check("Vehicles has no list() (don't call it)", not hasattr(Vehicles, "list"))
check("fleet_status([vin])", binds(VehicleFleet.fleet_status, [["V"]], {}))
check("vehicle_data(endpoints=...)", binds(VehicleFleet.vehicle_data, [], {"endpoints": ["charge_state"]}))
check("wake_up(wait, timeout)", binds(VehicleFleet.wake_up, [], {"wait": True, "timeout": 25}))

# 3. No call in tesla.py goes to api.vehicles.<method> other than the create* factories.
src = open(os.path.join(ROOT, "app", "tesla.py")).read()
bad = []
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "vehicles":
        if node.attr not in ("createFleet", "createSigned", "Fleet"):
            bad.append(node.attr)
check("tesla.py only uses vehicles.createFleet/createSigned", not bad, str(bad))

print("\nFAILED:" if fails else "\nALL PASSED", fails or "")
sys.exit(1 if fails else 0)
