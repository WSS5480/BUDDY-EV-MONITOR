"""Tesla side: user sign-in tokens, the virtual key, partner registration and vehicle calls."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from tesla_fleet_api import TeslaFleetApi
from tesla_fleet_api.exceptions import TeslaFleetError, VehicleOffline

from . import config
from .store import store

log = logging.getLogger("bem.tesla")

K_TOKENS = "tesla:tokens"  # {access_token, refresh_token, expires_at}
K_KEY = "tesla:private_key_pem"
K_VIN = "tesla:vin"
K_PARTNER = "tesla:partner_registered"  # {domain, at}
K_PROTOCOL = "tesla:protocol:"  # + vin -> {"signed": bool, "checked": ts}


class NotConnected(Exception):
    """Tesla account not signed in yet (or the sign-in expired)."""


class CarError(Exception):
    """A readable failure to show Claude / the owner."""


_refresh_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None
_private_key: ec.EllipticCurvePrivateKey | None = None


# ---------- virtual key ----------


async def load_or_create_key() -> ec.EllipticCurvePrivateKey:
    """Load the P-256 virtual key from the store, creating it on first run."""
    global _private_key
    if _private_key is not None:
        return _private_key
    pem = await store.get(K_KEY)
    if pem:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        await store.set(K_KEY, pem)
        log.info("Created a new virtual key")
    _private_key = key
    return key


async def public_key_pem() -> str:
    key = await load_or_create_key()
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def pairing_link() -> str:
    return f"https://tesla.com/_ak/{config.APP_DOMAIN}"


# ---------- user OAuth tokens ----------


def authorize_url(state: str) -> str:
    q = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": config.TESLA_CLIENT_ID,
            "redirect_uri": config.TESLA_REDIRECT_URI,
            "scope": config.TESLA_SCOPES,
            "state": state,
            "prompt_missing_scopes": "true",
        }
    )
    return f"{config.TESLA_AUTH_URL}?{q}"


async def _token_post(data: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(config.TESLA_TOKEN_URL, data=data)
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:200]}
    if r.status_code >= 400 or "access_token" not in body:
        err = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
        raise CarError(f"Tesla sign-in failed: {err}")
    return body


async def _save_tokens(body: dict[str, Any], old_refresh: str | None = None) -> dict[str, Any]:
    tokens = {
        "access_token": body["access_token"],
        # Tesla refresh tokens are single use; always keep the newest one.
        "refresh_token": body.get("refresh_token") or old_refresh,
        "expires_at": time.time() + int(body.get("expires_in", 3600)) - 120,
        "saved_at": time.time(),
    }
    await store.set(K_TOKENS, tokens)
    return tokens


async def exchange_code(code: str) -> None:
    body = await _token_post(
        {
            "grant_type": "authorization_code",
            "client_id": config.TESLA_CLIENT_ID,
            "client_secret": config.TESLA_CLIENT_SECRET,
            "code": code,
            "audience": config.TESLA_API_BASE,
            "redirect_uri": config.TESLA_REDIRECT_URI,
        }
    )
    await _save_tokens(body)


async def access_token() -> str:
    tokens = await store.get(K_TOKENS)
    if not tokens or not tokens.get("refresh_token"):
        raise NotConnected("Tesla account isn't connected yet. Sign in with Tesla on the app's home page.")
    if tokens["expires_at"] > time.time():
        return tokens["access_token"]
    async with _refresh_lock:
        tokens = await store.get(K_TOKENS)  # another request may have refreshed already
        if tokens and tokens["expires_at"] > time.time():
            return tokens["access_token"]
        try:
            body = await _token_post(
                {
                    "grant_type": "refresh_token",
                    "client_id": config.TESLA_CLIENT_ID,
                    "refresh_token": tokens["refresh_token"],
                }
            )
        except CarError as e:
            raise NotConnected(f"Tesla sign-in expired, sign in with Tesla again on the app's home page. ({e})")
        tokens = await _save_tokens(body, tokens["refresh_token"])
        return tokens["access_token"]


async def is_connected() -> bool:
    tokens = await store.get(K_TOKENS)
    return bool(tokens and tokens.get("refresh_token"))


async def disconnect() -> None:
    await store.delete(K_TOKENS)


# ---------- partner registration (one time per domain) ----------


async def register_partner() -> str:
    await load_or_create_key()
    body = await _token_post(
        {
            "grant_type": "client_credentials",
            "client_id": config.TESLA_CLIENT_ID,
            "client_secret": config.TESLA_CLIENT_SECRET,
            "scope": "openid vehicle_device_data vehicle_cmds vehicle_charging_cmds vehicle_location",
            "audience": config.TESLA_API_BASE,
        }
    )
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{config.TESLA_API_BASE}/api/1/partner_accounts",
            headers=headers,
            json={"domain": config.APP_DOMAIN},
        )
    if r.status_code >= 400:
        raise CarError(f"Tesla registration failed (HTTP {r.status_code}): {r.text[:300]}")
    await store.set(K_PARTNER, {"domain": config.APP_DOMAIN, "at": time.time()})
    return "Registered with Tesla."


async def partner_registered() -> bool:
    rec = await store.get(K_PARTNER)
    return bool(rec and rec.get("domain") == config.APP_DOMAIN)


# ---------- vehicle API ----------


async def startup() -> None:
    await startup_session_only()
    await load_or_create_key()


async def startup_session_only() -> None:
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()


async def shutdown() -> None:
    if _session is not None:
        await _session.close()


async def _api() -> TeslaFleetApi:
    token = await access_token()
    assert _session is not None
    api = TeslaFleetApi(session=_session, access_token=token, server=config.TESLA_API_BASE)
    api.private_key = await load_or_create_key()
    return api


def _err(e: BaseException) -> CarError:
    msg = getattr(e, "message", None) or str(e) or e.__class__.__name__
    return CarError(f"Tesla said: {msg}")


async def list_vehicles() -> list[dict[str, Any]]:
    api = await _api()
    try:
        resp = await api.vehicles.list()
    except TeslaFleetError as e:
        raise _err(e) from None
    out = []
    for v in resp.get("response", []):
        out.append(
            {
                "vin": v.get("vin"),
                "name": v.get("display_name"),
                "state": v.get("state"),
            }
        )
    return out


async def resolve_vin(vin: str | None) -> str:
    if vin:
        return vin.strip().upper()
    if config.TESLA_VIN:
        return config.TESLA_VIN
    saved = await store.get(K_VIN)
    if saved:
        return saved
    cars = await list_vehicles()
    if not cars:
        raise CarError("No cars found on this Tesla account.")
    await store.set(K_VIN, cars[0]["vin"])
    return cars[0]["vin"]


async def _needs_signing(api: TeslaFleetApi, vin: str) -> bool:
    cached = await store.get(K_PROTOCOL + vin)
    if cached is not None:
        return bool(cached["signed"])
    signed = True
    try:
        resp = await api.vehicles.createFleet(vin).fleet_status([vin])
        info = (resp.get("response", {}).get("vehicle_info") or {}).get(vin) or {}
        if "vehicle_command_protocol_required" in info:
            signed = bool(info["vehicle_command_protocol_required"])
    except TeslaFleetError as e:
        log.warning("fleet_status failed, assuming signed commands: %s", e)
    await store.set(K_PROTOCOL + vin, {"signed": signed, "checked": time.time()}, ttl=7 * 24 * 3600)
    return signed


async def key_paired(vin: str | None = None) -> bool | None:
    """True/False if Tesla reports whether our key is on the car, None if unknown."""
    try:
        api = await _api()
        vin = await resolve_vin(vin)
        resp = await api.vehicles.createFleet(vin).fleet_status([vin])
    except (TeslaFleetError, CarError, NotConnected):
        return None
    r = resp.get("response", {})
    if vin in (r.get("key_paired_vins") or []):
        return True
    if vin in (r.get("unpaired_vins") or []):
        return False
    return None


async def vehicle_data(vin: str | None) -> dict[str, Any]:
    api = await _api()
    vin = await resolve_vin(vin)
    car = api.vehicles.createFleet(vin)
    try:
        resp = await car.vehicle_data(
            endpoints=["charge_state", "climate_state", "drive_state", "location_data", "vehicle_state", "vehicle_config"]
        )
    except VehicleOffline:
        return {"vin": vin, "state": "asleep", "note": "The car is asleep. Wake it to get live data."}
    except TeslaFleetError as e:
        raise _err(e) from None
    return _summarize(vin, resp.get("response") or {})


def _summarize(vin: str, d: dict[str, Any]) -> dict[str, Any]:
    cs = d.get("charge_state") or {}
    cl = d.get("climate_state") or {}
    dr = d.get("drive_state") or {}
    vs = d.get("vehicle_state") or {}

    def c_to_f(c: Any) -> Any:
        return round(c * 9 / 5 + 32) if isinstance(c, (int, float)) else None

    windows_open = any(vs.get(k) for k in ("fd_window", "fp_window", "rd_window", "rp_window"))
    lat, lon = dr.get("latitude"), dr.get("longitude")
    return {
        "vin": vin,
        "name": d.get("display_name"),
        "state": d.get("state", "online"),
        "battery_percent": cs.get("battery_level"),
        "range_miles": round(cs["battery_range"]) if isinstance(cs.get("battery_range"), (int, float)) else None,
        "charging_state": cs.get("charging_state"),
        "charge_limit_percent": cs.get("charge_limit_soc"),
        "minutes_to_full": cs.get("minutes_to_full_charge"),
        "charger_power_kw": cs.get("charger_power"),
        "locked": vs.get("locked"),
        "sentry_mode": vs.get("sentry_mode"),
        "windows_open": windows_open,
        "frunk_open": bool(vs.get("ft")),
        "trunk_open": bool(vs.get("rt")),
        "odometer_miles": round(vs["odometer"]) if isinstance(vs.get("odometer"), (int, float)) else None,
        "climate_on": cl.get("is_climate_on"),
        "inside_temp_f": c_to_f(cl.get("inside_temp")),
        "outside_temp_f": c_to_f(cl.get("outside_temp")),
        "climate_set_f": c_to_f(cl.get("driver_temp_setting")),
        "shift_state": dr.get("shift_state") or "P",
        "speed_mph": dr.get("speed"),
        "latitude": lat,
        "longitude": lon,
        "map_link": f"https://maps.google.com/?q={lat},{lon}" if lat is not None and lon is not None else None,
    }


async def wake(vin: str | None) -> str:
    api = await _api()
    vin = await resolve_vin(vin)
    try:
        await api.vehicles.createFleet(vin).wake_up(wait=True, timeout=45)
    except VehicleOffline:
        return "Sent a wake-up, but the car didn't come online within 45 seconds. It may have no signal."
    except TeslaFleetError as e:
        raise _err(e) from None
    return "The car is awake."


async def command(vin: str | None, name: str, *args: Any, **kwargs: Any) -> str:
    """Run a vehicle command, signed when the car needs it, waking the car once if asleep."""
    api = await _api()
    vin = await resolve_vin(vin)
    signed = await _needs_signing(api, vin)
    car = api.vehicles.createSigned(vin) if signed else api.vehicles.createFleet(vin)
    fn = getattr(car, name)
    for attempt in (1, 2):
        try:
            resp = await fn(*args, **kwargs)
            break
        except VehicleOffline:
            if attempt == 2:
                raise CarError("The car is asleep and didn't wake up in time. Try again in a minute.") from None
            try:
                await api.vehicles.createFleet(vin).wake_up(wait=True, timeout=45)
            except VehicleOffline:
                raise CarError("The car is asleep and didn't wake up in time. Try again in a minute.") from None
            except TeslaFleetError as e:
                raise _err(e) from None
        except TeslaFleetError as e:
            raise _err(e) from None
    r = resp.get("response") if isinstance(resp, dict) else None
    if isinstance(r, dict) and r.get("result") is False:
        raise CarError(f"The car refused: {r.get('reason') or 'no reason given'}")
    return "Done."
