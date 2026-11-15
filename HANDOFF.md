# HANDOFF — Buddy EV Monitor (start here)

Buddy EV Monitor lets Claude check and control Steve's Tesla. It's a remote MCP server (a Claude
custom connector) on Tesla's official Fleet API, plus a small owner web page for setup.

Update this file **in the same commit as every change**: change log, decisions, open items.

## How Steve works (his rules)
- Terse, informal, often voice. Lead with the result; short, practical, with the reason.
- End every reply with the links: app, Render dashboard, GitHub repo.
- **Free plan only** for now (Render free + Supabase free). Nothing paid without his yes.
- He enters every secret himself in Render. Never ask for, read or type his Tesla client secret,
  tokens or passwords. Give exact env var names, each in its own copy box, no stray spaces.
- Three roles every session: coder, first reviewer, second reviewer. At the first sign of
  degradation the coder retires to first reviewer, the first reviewer moves to second reviewer and
  the second reviewer falls off. Independent reviewers check the code before anything ships;
  reviewers don't push code themselves (they report, the coder verifies and pushes).
- UI: wrap info (cards, no sideways tables), headers over their data, summary tiles drill down
  with a back button, phone first.
- Anything that unlocks, opens or starts the car needs an explicit confirmation step.
- Keep the Karaoke repo (WSS5480/Karaoke) and its Render service untouched. This app's code
  started there in Aug 2026 and was moved out with its history on Nov 15 2026.

## How it's built
- Python 3.13, `app/` package, run by uvicorn.
  - `app/main.py` — MCP server (`mcp` SDK 2.3, MCPServer) with the tools, OAuth for the connector,
    and the owner web routes (custom routes).
  - `app/auth.py` — OAuth provider for Claude (dynamic client registration, PKCE, single-use codes
    and rotating refresh tokens, stored hashed) and the owner password / web session.
  - `app/tesla.py` — Tesla sign-in tokens (refresh tokens are single use; always save the newest),
    the P-256 virtual key (created on first run, stored in the DB), partner registration, vehicle
    data and commands via `tesla-fleet-api` (signs commands itself when the car requires the
    Vehicle Command protocol, else plain REST; checked with fleet_status, cached a week).
  - `app/store.py` — key-value table `bem_kv` in Postgres (DATABASE_URL) or a SQLite file when no
    DATABASE_URL (local only; on Render free that's wiped on restart).
  - `app/pages.py` — owner pages (sign-in, setup checklist, car tiles that drill down).
- Tesla's library errors subclass **BaseException**, not Exception: catch `TeslaFleetError`.

### Endpoints
- `/` owner page (password = OWNER_PASSWORD). `/auth/login` + `/auth/callback` Tesla sign-in.
- `/setup/register` (POST) one-time Tesla partner registration for this domain.
- `/.well-known/appspecific/com.tesla.3p.public-key.pem` public virtual key.
- `/mcp` the connector (Bearer token from our OAuth). `/authorize`, `/token`, `/register`,
  `/revoke`, `/.well-known/oauth-*` from the SDK. `/connect` owner password page during connect.
- `/healthz`.

### Tools
Read: list_vehicles, vehicle_status. Actions: wake_vehicle, lock_doors, climate_on/off,
set_temperature (F), start/stop_charging, set_charge_limit, honk_horn, flash_lights,
close_windows. Need a confirmation code (two calls, owner's yes in between): unlock_doors,
open_trunk, open_frunk, vent_windows, remote_start.

## Settings (Render → Environment)
`TESLA_CLIENT_ID`, `TESLA_CLIENT_SECRET`, `OWNER_PASSWORD`, `DATABASE_URL` (Supabase session
pooler string). Optional: `TESLA_VIN` (pick a car), `TESLA_REGION` (na default), `APP_URL`
(defaults to Render's RENDER_EXTERNAL_URL).

Tesla developer app (developer.tesla.com) must have: allowed origin = app URL, redirect URI =
`<app URL>/auth/callback`, scopes incl. vehicle commands + charging commands + location, a
payment method and a billing limit above $0 (default $0 disables the app).

## Run / test / ship
- Local: `pip install -r requirements.txt` then
  `APP_URL=http://localhost:8765 OWNER_PASSWORD=testpass TESLA_VIN=TESTVIN123 SQLITE_PATH=/tmp/bem.db uvicorn app.main:app --port 8765`
  and `python tests/smoke_flow.py` (30 checks: connector OAuth, tools, confirmation codes, owner
  pages, host check). Add `DATABASE_URL=postgresql://...` to test the Postgres path.
- Ship: push `main`; Render auto-deploys (service buddy-ev-monitor, MY APPS workspace, free).

## Free-plan limits
- Sleeps after 15 min idle; first request takes about a minute (Claude may need a retry).
- No background jobs (no alerts). Supabase free pauses after ~7 days without use: restore it
  in Supabase; the owner page says "Can't reach the database" meanwhile.

## Decisions
- 2026-11-15 — Rebuilt in Python, not the old Node: `tesla-fleet-api` signs vehicle commands
  natively (no Go proxy). Reason: unlock/climate/charging need signed commands on newer cars.
- 2026-11-15 — Own repo BUDDY-EV-MONITOR, history copied from Karaoke (commits b07da3a..83ff43a).
  Reason: Steve asked to move it out without touching karaoke.
- 2026-11-15 — Free Render + free Supabase for storage. Reason: Steve wants the free plan;
  Render free has no disk and Tesla refresh tokens rotate, so they must live in a database.
- 2026-11-15 — Connector protected by our own OAuth + owner password. Reason: only Steve may use
  it; Claude custom connectors support OAuth with dynamic registration.
- 2026-11-15 — Confirmation codes for unlock/trunk/frunk/vent/remote start. Reason: Steve's rule.
- 2026-11-15 — Car status on the web page loads only when he taps "Check car". Reason: each
  read costs Tesla API money; don't wake or poll on page load.

## Change log
- 2026-11-15 — Moved Tesla monitor code out of Karaoke with history; rewrote as Python MCP
  connector with OAuth, signed commands, Postgres storage, owner setup page, smoke tests.

## Open items
- Steve: new Tesla developer credentials, then enter env vars; register; sign in with Tesla;
  add key to car; add connector in Claude.
- Old Tesla public key in Karaoke history is unused now (the app makes a new key).
- Untested against a real car until Steve signs in. First live check: vehicle_status, then lock.
