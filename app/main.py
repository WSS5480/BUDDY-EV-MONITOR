"""Buddy EV Monitor: a Claude connector (remote MCP server) for Steve's Tesla.

Run locally:  uvicorn app.main:app --port 8000
On Render:    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.provider import AuthorizeError
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import auth, config, pages, tesla
from .store import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bem")

K_TESLA_STATE = "tesla:oauth_state:"
K_CONFIRM = "confirm:"


@contextlib.asynccontextmanager
async def lifespan(_server: MCPServer):
    await store.open()
    try:
        if store.error:
            raise RuntimeError(store.error)
        await store.purge_expired()
        await tesla.startup()
    except Exception as e:  # keep serving so the owner page can explain the problem
        log.error("Startup problem (will retry on first request): %s", e)
        await tesla.startup_session_only()
    log.info(
        "Started. Storage: %s%s. Missing settings: %s",
        store.kind,
        f" ({store.error})" if store.error else "",
        config.missing_settings() or "none",
    )
    try:
        yield {}
    finally:
        await tesla.shutdown()
        await store.close()


mcp = MCPServer(
    name="Buddy EV Monitor",
    instructions=(
        "Check and control the owner's Tesla. Reading status costs a fraction of a cent; waking the "
        "car costs more, so only wake it when needed. Actions that unlock, open or start the car "
        "(unlock_doors, open_trunk, open_frunk, vent_windows, remote_start) return a confirmation "
        "code first: tell the owner exactly what you are about to do, and only call the tool again "
        "with that code after the owner clearly says yes in this conversation."
    ),
    auth_server_provider=auth.OwnerOAuthProvider(),
    auth=AuthSettings(
        issuer_url=config.APP_URL,
        resource_server_url=f"{config.APP_URL}/mcp",
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[config.CONNECTOR_SCOPE],
            default_scopes=[config.CONNECTOR_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[config.CONNECTOR_SCOPE],
    ),
    lifespan=lifespan,
)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
RISKY = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


async def _run(coro) -> str:
    try:
        result = await coro
    except (tesla.NotConnected, tesla.CarError) as e:
        return str(e)
    except Exception as e:  # database or network trouble: say so instead of a bare error
        log.exception("Tool failed")
        return f"Something went wrong on the app's side ({e.__class__.__name__}). {store.error or ''}".strip()
    return result if isinstance(result, str) else json.dumps(result, indent=1)


async def _confirmed(action: str, vin: str | None, code: str, what: str) -> str | None:
    """Two-step guard. Returns a message asking for confirmation, or None when the code matches."""
    try:
        real_vin = await tesla.resolve_vin(vin)
    except (tesla.NotConnected, tesla.CarError) as e:
        return str(e)
    except Exception as e:
        log.exception("Confirmation step failed")
        return f"Something went wrong on the app's side ({e.__class__.__name__}). Nothing was done."
    key = f"{K_CONFIRM}{action}:{real_vin}"
    if code:
        saved = await store.take(key)
        if saved and secrets.compare_digest(str(saved.get("code")), code.strip()):
            return None
        return "That confirmation code is wrong or expired. Call the tool again without a code to get a new one."
    new_code = f"{secrets.randbelow(10000):04d}"
    await store.set(key, {"code": new_code}, ttl=config.CONFIRM_TTL)
    return (
        f"CONFIRMATION NEEDED. Nothing has happened yet. Ask the owner: \"{what}?\" "
        f"Only if they clearly say yes, call this tool again with confirm_code=\"{new_code}\". "
        "The code expires in 5 minutes."
    )


# ---------------- read tools ----------------


@mcp.tool(annotations=READ)
async def list_vehicles() -> str:
    """List the cars on the Tesla account with their VIN and whether they're awake."""
    return await _run(tesla.list_vehicles())


@mcp.tool(annotations=READ)
async def vehicle_status(vin: str = "") -> str:
    """Battery, range, charging, lock state, doors/windows, climate, temperatures and location.
    Doesn't wake a sleeping car (it says it's asleep instead). vin is optional with one car."""
    return await _run(tesla.vehicle_data(vin or None))


# ---------------- everyday actions ----------------


@mcp.tool(annotations=ACT)
async def wake_vehicle(vin: str = "") -> str:
    """Wake the car so it can report live data or take commands (waits up to 45 seconds)."""
    return await _run(tesla.wake(vin or None))


@mcp.tool(annotations=ACT)
async def lock_doors(vin: str = "") -> str:
    """Lock the car."""
    return await _run(tesla.command(vin or None, "door_lock"))


@mcp.tool(annotations=ACT)
async def climate_on(vin: str = "") -> str:
    """Turn on climate control (precondition the cabin)."""
    return await _run(tesla.command(vin or None, "auto_conditioning_start"))


@mcp.tool(annotations=ACT)
async def climate_off(vin: str = "") -> str:
    """Turn off climate control."""
    return await _run(tesla.command(vin or None, "auto_conditioning_stop"))


@mcp.tool(annotations=ACT)
async def set_temperature(fahrenheit: float, vin: str = "") -> str:
    """Set the cabin temperature for driver and passenger, in Fahrenheit (59 to 82)."""
    if not 59 <= fahrenheit <= 82:
        return "Pick a temperature between 59 and 82 degrees Fahrenheit."
    c = round((fahrenheit - 32) * 5 / 9 * 2) / 2
    return await _run(tesla.command(vin or None, "set_temps", c, c))


@mcp.tool(annotations=ACT)
async def start_charging(vin: str = "") -> str:
    """Start charging (the car must be plugged in)."""
    return await _run(tesla.command(vin or None, "charge_start"))


@mcp.tool(annotations=ACT)
async def stop_charging(vin: str = "") -> str:
    """Stop charging."""
    return await _run(tesla.command(vin or None, "charge_stop"))


@mcp.tool(annotations=ACT)
async def set_charge_limit(percent: int, vin: str = "") -> str:
    """Set the charge limit, 50 to 100 percent."""
    if not 50 <= percent <= 100:
        return "Pick a charge limit between 50 and 100 percent."
    return await _run(tesla.command(vin or None, "set_charge_limit", percent))


@mcp.tool(annotations=ACT)
async def honk_horn(vin: str = "") -> str:
    """Honk the horn once (car must be in park)."""
    return await _run(tesla.command(vin or None, "honk_horn"))


@mcp.tool(annotations=ACT)
async def flash_lights(vin: str = "") -> str:
    """Flash the headlights (car must be in park)."""
    return await _run(tesla.command(vin or None, "flash_lights"))


# ---------------- actions that need the owner's yes ----------------


@mcp.tool(annotations=RISKY)
async def unlock_doors(vin: str = "", confirm_code: str = "") -> str:
    """Unlock the car. Needs a confirmation code: call once without a code, ask the owner, then
    call again with the code only after they say yes."""
    ask = await _confirmed("unlock", vin or None, confirm_code, "Unlock the car")
    return ask or await _run(tesla.command(vin or None, "door_unlock"))


@mcp.tool(annotations=RISKY)
async def open_trunk(vin: str = "", confirm_code: str = "") -> str:
    """Open (or close, on cars with a powered trunk) the rear trunk. Needs a confirmation code."""
    ask = await _confirmed("trunk", vin or None, confirm_code, "Open the rear trunk")
    return ask or await _run(tesla.command(vin or None, "actuate_trunk", "rear"))


@mcp.tool(annotations=RISKY)
async def open_frunk(vin: str = "", confirm_code: str = "") -> str:
    """Open the front trunk (frunk). It can't be closed remotely. Needs a confirmation code."""
    ask = await _confirmed("frunk", vin or None, confirm_code, "Open the frunk (it can't be closed remotely)")
    return ask or await _run(tesla.command(vin or None, "actuate_trunk", "front"))


@mcp.tool(annotations=RISKY)
async def vent_windows(vin: str = "", confirm_code: str = "") -> str:
    """Crack all windows open a little (vent). Needs a confirmation code."""
    ask = await _confirmed("vent", vin or None, confirm_code, "Vent (crack open) the windows")
    return ask or await _run(tesla.command(vin or None, "window_control", "vent"))


@mcp.tool(annotations=ACT)
async def close_windows(vin: str = "") -> str:
    """Close all windows. Some models only allow this when your phone is near the car."""
    return await _run(tesla.command(vin or None, "window_control", "close"))


@mcp.tool(annotations=RISKY)
async def remote_start(vin: str = "", confirm_code: str = "") -> str:
    """Enable keyless driving for 2 minutes so someone at the car can drive without a key.
    Needs a confirmation code."""
    ask = await _confirmed(
        "remote_start", vin or None, confirm_code, "Turn on keyless driving so anyone at the car can drive it for 2 minutes"
    )
    return ask or await _run(tesla.command(vin or None, "remote_start_drive"))


# ---------------- web pages ----------------


async def _is_owner(request: Request) -> bool:
    return await auth.valid_session(request.cookies.get(auth.SESSION_COOKIE))


def _set_session(resp: Response, token: str) -> None:
    resp.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_TTL,
        httponly=True,
        secure=config.APP_URL.startswith("https://"),
        samesite="lax",
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> Response:
    return PlainTextResponse("ok")


@mcp.custom_route("/.well-known/appspecific/com.tesla.3p.public-key.pem", methods=["GET"])
async def public_key(_request: Request) -> Response:
    return PlainTextResponse(await tesla.public_key_pem(), media_type="application/x-pem-file")


@mcp.custom_route("/", methods=["GET"])
async def home(request: Request) -> Response:
    try:
        return await _home(request)
    except Exception as e:
        log.exception("Home page failed")
        return HTMLResponse(
            pages.message_page(store.error or f"The app hit a problem ({e.__class__.__name__}). Check the Render logs."),
            status_code=503,
        )


async def _home(request: Request) -> Response:
    if not await _is_owner(request):
        return HTMLResponse(pages.login_page(locked=await auth.locked_out(), msg=request.query_params.get("msg", "")))
    connected = await tesla.is_connected()
    paired = await tesla.key_paired() if connected else None
    return HTMLResponse(
        pages.dashboard(
            missing=config.missing_settings(),
            storage=store.kind,
            connected=connected,
            registered=await tesla.partner_registered(),
            paired=paired,
            pairing_link=tesla.pairing_link(),
            connector_url=f"{config.APP_URL}/mcp",
            msg=request.query_params.get("msg", ""),
        )
    )


@mcp.custom_route("/login", methods=["POST"])
async def login(request: Request) -> Response:
    form = await request.form()
    if await auth.check_password(str(form.get("password", ""))):
        resp = RedirectResponse("/", status_code=303)
        _set_session(resp, await auth.new_session())
        return resp
    return RedirectResponse("/?msg=Wrong+password", status_code=303)


@mcp.custom_route("/logout", methods=["POST"])
async def logout(request: Request) -> Response:
    await auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@mcp.custom_route("/auth/login", methods=["GET"])
async def tesla_login(request: Request) -> Response:
    if not await _is_owner(request):
        return RedirectResponse("/", status_code=303)
    if not config.TESLA_CLIENT_ID or not config.TESLA_CLIENT_SECRET:
        return RedirectResponse("/?msg=Add+TESLA_CLIENT_ID+and+TESLA_CLIENT_SECRET+in+Render+first", status_code=303)
    state = secrets.token_urlsafe(24)
    await store.set(K_TESLA_STATE + state, {"ok": True}, ttl=10 * 60)
    return RedirectResponse(tesla.authorize_url(state), status_code=303)


@mcp.custom_route("/auth/callback", methods=["GET"])
async def tesla_callback(request: Request) -> Response:
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if not await _is_owner(request) or not state or await store.take(K_TESLA_STATE + state) is None:
        return RedirectResponse("/?msg=Tesla+sign-in+expired,+try+again", status_code=303)
    if request.query_params.get("error") or not code:
        return RedirectResponse("/?msg=Tesla+sign-in+was+cancelled", status_code=303)
    try:
        await tesla.exchange_code(code)
    except tesla.CarError as e:
        log.warning("Tesla code exchange failed: %s", e)
        return RedirectResponse("/?msg=Tesla+sign-in+failed,+check+the+client+ID+and+secret", status_code=303)
    return RedirectResponse("/?msg=Tesla+account+connected", status_code=303)


@mcp.custom_route("/setup/register", methods=["POST"])
async def setup_register(request: Request) -> Response:
    if not await _is_owner(request):
        return RedirectResponse("/", status_code=303)
    try:
        await tesla.register_partner()
    except tesla.CarError as e:
        log.warning("Partner registration failed: %s", e)
        return RedirectResponse("/?msg=Tesla+registration+failed,+see+Render+logs", status_code=303)
    return RedirectResponse("/?msg=Registered+with+Tesla", status_code=303)


@mcp.custom_route("/setup/disconnect", methods=["POST"])
async def setup_disconnect(request: Request) -> Response:
    if not await _is_owner(request):
        return RedirectResponse("/", status_code=303)
    await tesla.disconnect()
    return RedirectResponse("/?msg=Tesla+account+disconnected", status_code=303)


@mcp.custom_route("/api/status", methods=["GET"])
async def api_status(request: Request) -> Response:
    if not await _is_owner(request):
        return JSONResponse({"error": "sign in first"}, status_code=401)
    try:
        data: dict[str, Any] = await tesla.vehicle_data(None)
    except (tesla.NotConnected, tesla.CarError) as e:
        return JSONResponse({"error": str(e)})
    return JSONResponse(data)


@mcp.custom_route("/connect", methods=["GET", "POST"])
async def connect(request: Request) -> Response:
    """The page Claude sends Steve to when he adds the connector."""
    if request.method == "GET":
        req_id = request.query_params.get("req", "")
        pending = await auth.pending_request(req_id)
        if not pending:
            return HTMLResponse(pages.message_page("This link expired. Start again from Claude."), status_code=400)
        return HTMLResponse(pages.connect_page(req_id, pending.get("client_name") or "Claude", locked=await auth.locked_out()))
    form = await request.form()
    req_id = str(form.get("req", ""))
    if not await auth.pending_request(req_id):
        return HTMLResponse(pages.message_page("This link expired. Start again from Claude."), status_code=400)
    if not await auth.check_password(str(form.get("password", ""))):
        return HTMLResponse(
            pages.connect_page(req_id, "Claude", error="Wrong password.", locked=await auth.locked_out()), status_code=401
        )
    try:
        return RedirectResponse(await auth.approve_request(req_id), status_code=303)
    except AuthorizeError:
        return HTMLResponse(pages.message_page("This link expired. Start again from Claude."), status_code=400)


app = mcp.streamable_http_app(
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[config.APP_DOMAIN, f"{config.APP_DOMAIN}:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=[config.APP_URL, "https://claude.ai", "https://claude.com"],
    ),
)
