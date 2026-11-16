# Buddy EV Monitor

Lets Claude check and control your Tesla through Tesla's official Fleet API. It runs as a Claude
custom connector (remote MCP server) on Render's free plan, with a free Supabase database to keep
the Tesla sign-in and the car key.

Claude can read battery, range, charging, locks, climate and location, and can lock, set climate
and temperature, start/stop charging, set the charge limit, honk and flash. It also unlocks, opens
the trunk or frunk, vents the windows and turns on keyless driving when you ask it to, with no
extra approval step. Only you can connect Claude to the app (owner password).

## Setup
1. Render → this service → Environment: add `TESLA_CLIENT_ID`, `TESLA_CLIENT_SECRET`,
   `OWNER_PASSWORD` (pick one, 12+ characters), `DATABASE_URL` (Supabase → Connect → Session
   pooler) and `SECRET_KEY` (press Generate; keep it, changing it means signing in to Tesla again).
2. Tesla developer app: allowed origin = the app URL, redirect URI = `<app URL>/auth/callback`,
   turn on vehicle commands, charging commands and location; add a payment method and a billing
   limit above $0.
3. Open the app, sign in with the owner password, press **Register**, then **Sign in with Tesla**,
   then **Add key to car** on your phone and approve it in the Tesla app.
4. Claude → Settings → Connectors → Add custom connector → paste `<app URL>/mcp`, then enter the
   owner password when asked.

See HANDOFF.md for how it's built, decisions and open items.
