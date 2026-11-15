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
- Anything that unlocks, opens or starts the car needs an explicit confirmation step (done as an
  owner approval link, see Decisions).
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
  - `app/pages.py` — owner pages (sign-in, setup checklist, car tiles that drill down, approval
    page, security buttons).
  - `app/crypt.py` — Fernet encryption (key from SECRET_KEY) for the Tesla tokens and car key.
- Tesla's library errors subclass **BaseException**, not Exception: catch `TeslaFleetError`.
- tesla-fleet-api 1.23.1 (pinned): the car list is `api.products()` (GET /api/1/products, filter
  entries with a `vin`). `api.vehicles` only has createFleet/createSigned; there is no
  `api.vehicles.list()`.

### Endpoints
- `/` owner page (password = OWNER_PASSWORD). `/auth/login` + `/auth/callback` Tesla sign-in.
- `/setup/register` (POST) one-time Tesla partner registration for this domain.
- `/.well-known/appspecific/com.tesla.3p.public-key.pem` public virtual key.
- `/mcp` the connector (Bearer token from our OAuth). `/authorize`, `/token`, `/register`,
  `/revoke`, `/.well-known/oauth-*` from the SDK. `/connect` owner password page during connect.
- `/approve?id=` owner approves/denies an unlock-type action (needs owner sign-in).
- `/setup/disconnect-claude`, `/setup/signout-all` (POST) owner kill switches.
- `/setup/clear-pause` (POST) owner lifts the sign-in pause and per-address lockouts.
- `/healthz` (`?db=1` also touches the database: used by the keep-alive).

### Tools
Read: list_vehicles, vehicle_status. Actions: wake_vehicle, lock_doors, climate_on/off,
set_temperature (F), start/stop_charging, set_charge_limit, honk_horn, flash_lights,
close_windows. Need owner approval (first call returns an /approve link, owner taps Approve on
the site, second call passes approval_id): unlock_doors, open_trunk, open_frunk, vent_windows,
remote_start.

## Settings (Render → Environment)
`TESLA_CLIENT_ID`, `TESLA_CLIENT_SECRET`, `OWNER_PASSWORD` (12+ chars; shorter = sign-in
refused), `DATABASE_URL` (Supabase session pooler string), `SECRET_KEY` (Render Generate). If
SECRET_KEY changes, saved data can't be read: put the old one back, or use "Make a new car key"
on the owner page and then Register, sign in with Tesla and pair the car again. Optional: `TESLA_VIN` (pick a car), `TESLA_REGION` (na default), `APP_URL`
(defaults to Render's RENDER_EXTERNAL_URL).

Tesla developer app (developer.tesla.com) must have: allowed origin = app URL, redirect URI =
`<app URL>/auth/callback`, scopes incl. vehicle commands + charging commands + location, a
payment method and a billing limit above $0 (default $0 disables the app).

## Run / test / ship
- Local: `pip install -r requirements.txt` then
  `APP_URL=http://localhost:8765 OWNER_PASSWORD=testpass-long-1 TESLA_CLIENT_ID=x TESLA_CLIENT_SECRET=y SECRET_KEY=test TESLA_VIN=TESTVIN123 SQLITE_PATH=/tmp/bem.db uvicorn app.main:app --port 8765`
  and `python tests/smoke_flow.py` (54 checks: connector OAuth, redirect allow-list, tools,
  approvals, owner pages, headers, cross-site POSTs, VIN check, host check, disconnect,
  app-wide password cap and the owner clearing it; run it on a fresh DB). Add `DATABASE_URL=postgresql://...`
  to test the Postgres path. `python tests/fake_tesla.py` checks the Tesla side against a fake
  Fleet API (token refresh, wake, refusals, signed switch, window location, encryption, car list
  and VIN lookup with no TESLA_VIN). `python tests/test_library_api.py` checks every Tesla library
  call the app makes against the real installed tesla-fleet-api (method exists, arguments fit).
  Run all three before every push.
- Ship: push `main`; Render auto-deploys. Service buddy-ev-monitor (srv-dbsvma7f3r2c73bh1q8g),
  MY APPS workspace, free plan, https://buddy-ev-monitor-gk37.onrender.com (created 2026-11-15
  via the Render API, so render.yaml isn't applied: health check path and SECRET_KEY generate
  are set by hand in the dashboard if wanted). MY APPS is at the hobby 25-service limit.

## Free-plan limits
- Sleeps after 15 min idle; first request takes about a minute (Claude may need a retry).
- No background jobs (no alerts). Supabase free pauses after ~7 days without use: restore it
  in Supabase; the owner page says "Can't reach the database" meanwhile. Paused past 90 days =
  data gone (sign in to Tesla and pair the car again). Tesla refresh tokens also lapse after
  ~3 months unused. A keep-alive that hits `/healthz?db=1` every few days avoids both pauses.

## Decisions
- 2026-11-15 — Rebuilt in Python, not the old Node: `tesla-fleet-api` signs vehicle commands
  natively (no Go proxy). Reason: unlock/climate/charging need signed commands on newer cars.
- 2026-11-15 — Own repo BUDDY-EV-MONITOR, history copied from Karaoke (commits b07da3a..83ff43a).
  Reason: Steve asked to move it out without touching karaoke.
- 2026-11-15 — Free Render + free Supabase for storage. Reason: Steve wants the free plan;
  Render free has no disk and Tesla refresh tokens rotate, so they must live in a database.
- 2026-11-15 — Connector protected by our own OAuth + owner password. Reason: only Steve may use
  it; Claude custom connectors support OAuth with dynamic registration.
- 2026-11-15 — Unlock/trunk/frunk/vent/remote start need the owner to tap Approve on the app's
  own page (signed in), not a code Claude repeats back. Reason: Steve's confirmation rule; a code
  in the chat could be replayed by Claude itself if a web page or email tricked it.
- 2026-11-15 — Table `bem_kv` gets RLS on and no grants for Supabase's anon/authenticated roles,
  and Tesla tokens + car key are Fernet-encrypted with SECRET_KEY. Reason: second review found
  Supabase's REST API would otherwise expose the table to anyone with the anon key.
- 2026-11-15 — Connector redirect allow-list (claude.ai / claude.com callbacks only), 25-client
  cap, per-address password lockout plus one wrong guess per second overall (the forwarded
  address can be faked), fixed notice codes, security headers, stateless MCP transport (Render
  free sleeps). Reason: first and second reviews.
- 2026-11-15 — Car status on the web page loads only when he taps "Check car". Reason: each
  read costs Tesla API money; don't wake or poll on page load.

## Change log
- 2026-11-15 — Moved Tesla monitor code out of Karaoke with history; rewrote as Python MCP
  connector with OAuth, signed commands, Postgres storage, owner setup page, smoke tests.

- 2026-11-15 — Security pass after two independent reviews (see Decisions): owner approvals,
  RLS + encryption, redirect allow-list, lockout, headers, kill switches, token-rejected refresh,
  25 s wake, REST-refused switch to signed, window commands send location, key race fix.

- 2026-11-15 — Third review (of the security pass): fixed owner lock-out by slow parallel
  guesses (delay now per request, not global), short passwords refused, client address from
  CF-Connecting-IP / last X-Forwarded-For, cross-site POST guard on every owner form, Tesla
  errors from the wake step no longer escape, "Make a new car key" recovery, VIN format check,
  "already used" message, Tesla outage vs expired sign-in, HSTS + no-store, plaintext key
  re-encrypted once SECRET_KEY is set. Live on Render at buddy-ev-monitor-gk37.

- 2026-11-15 — Fourth review: app-wide cap of 50 wrong owner passwords per hour (then new
  sign-ins pause; existing sessions and Claude keep working), client address must be a real IP,
  IPv6 grouped by /64, failed sign-ins logged with which header gave the address, cross-site
  check trusts Sec-Fetch-Site and otherwise compares Origin with Host, register errors caught.

- 2026-11-15 — Final review of the password cap: signed-in owner can reconnect Claude without a
  password and has a "Clear sign-in pause" button (so a guesser can't keep him out), right
  password during a pause says "paused" not "wrong", X-Forwarded-For uses the last entry (Render
  appends the real address), IPv4-mapped IPv6 counts as the IPv4 address.
- 2026-11-15 — Steve chose Supabase: free project buddy-ev-monitor (ref mpnvxhpcxzdpxvuhobuw,
  us-east-2) created by the main chat. The app creates its own table `bem_kv` (with RLS and
  revokes) on startup, so nothing has to be created in Supabase by hand.

- 2026-11-15 — Live bug after Steve's first Tesla sign-in: home page 503 with AttributeError
  ('Vehicles' object has no attribute 'list'). The car list now uses `api.products()`; the home
  page shows a note instead of failing if the car check errors; approvals catch Tesla errors from
  the VIN lookup; new tests/test_library_api.py and fake-Tesla checks would have caught it
  (tests had always set TESLA_VIN, which skipped the car list).

## Recovery
- Locked out of sign-in on a new device with no signed-in device left: in Supabase SQL editor,
  `DELETE FROM bem_kv WHERE k LIKE 'web:fails%';` then sign in.

## Open items
- Confirm on Render which header carries the real client address: look for "Wrong owner
  password ... (via cf-connecting-ip)" in the logs after one wrong try.
- After "Make a new car key", Tesla may still list the car as paired until it's paired again.
- Signed vehicle sessions are made per command (an extra handshake each time). Caching one per
  VIN would be faster; not done yet.
- Two instances overlapping during a deploy each have their own refresh lock; Tesla's 24 h grace
  on the old refresh token covers the rare clash.
- Render env names are set (SECRET_KEY random; the other four say ADD_YOURS until Steve fills
  them; DATABASE_URL = Supabase Session pooler string). Steve: new Tesla developer credentials, then enter env vars; register; sign in with Tesla;
  add key to car; add connector in Claude.
- Old Tesla public key in Karaoke history is unused now (the app makes a new key).
- Untested against a real car until Steve signs in. First live check: vehicle_status, then lock.
