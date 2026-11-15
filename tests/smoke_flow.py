"""End-to-end smoke test of the connector sign-in and the tools, without a real Tesla.

Start the app first:
  APP_URL=http://localhost:8765 OWNER_PASSWORD=testpass TESLA_VIN=TESTVIN123 \
  SQLITE_PATH=/tmp/bem-test.db uvicorn app.main:app --port 8765
Then: python tests/smoke_flow.py
"""

import base64
import hashlib
import json
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx

B = "http://localhost:8765"
PW = "testpass"
REDIRECT = "http://localhost:9999/callback"
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails += 1


c = httpx.Client(base_url=B, follow_redirects=False, timeout=30)

# 1. dynamic client registration (what Claude does)
r = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude", "token_endpoint_auth_method": "none",
                               "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
check("register client", r.status_code in (200, 201), r.text)
client = r.json()
cid = client["client_id"]

# 2. authorize with PKCE -> our password page
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
r = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz", "scope": "car"})
check("authorize redirects to /connect", r.status_code in (302, 303, 307) and "/connect?req=" in r.headers.get("location", ""), r.text)
req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]

r = c.get("/connect", params={"req": req})
check("connect page shows", r.status_code == 200 and "Owner password" in r.text)

# 3. wrong password rejected, right one gives a code
r = c.post("/connect", data={"req": req, "password": "nope"})
check("wrong password rejected", r.status_code == 401)
r = c.post("/connect", data={"req": req, "password": PW})
loc = r.headers.get("location", "")
check("right password redirects back with code", r.status_code == 303 and loc.startswith(REDIRECT) and "code=" in loc, r.text)
q = parse_qs(urlparse(loc).query)
check("state preserved", q.get("state") == ["xyz"])
code = q["code"][0]

# 4. token exchange (bad verifier fails, good works, code single use)
r = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                           "client_id": cid, "code_verifier": "wrong" * 10})
check("bad PKCE verifier rejected", r.status_code == 400)
r = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                           "client_id": cid, "code_verifier": verifier})
check("token issued", r.status_code == 200 and "access_token" in r.json(), r.text)
tok = r.json()
r = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                           "client_id": cid, "code_verifier": verifier})
check("code can't be reused", r.status_code == 400)

# 5. refresh rotates
r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
check("refresh works", r.status_code == 200, r.text)
tok2 = r.json()
r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
check("old refresh token can't be reused", r.status_code == 400)
access = tok2["access_token"]

# 6. MCP calls
H = {"Authorization": f"Bearer {access}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method, params=None, rid=1, sid=None):
    h = dict(H)
    if sid:
        h["mcp-session-id"] = sid
    r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    body = r.text
    data = None
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        for line in body.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:])
    elif body:
        data = r.json()
    return r, data


r = c.post("/mcp", headers={**H, "Authorization": "Bearer junk"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
check("bad token refused", r.status_code == 401)

r, d = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}})
check("initialize", r.status_code == 200 and d and "result" in d, r.text[:300])
sid = r.headers.get("mcp-session-id")
c.post("/mcp", headers={**H, **({"mcp-session-id": sid} if sid else {})}, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

r, d = rpc("tools/list", rid=2, sid=sid)
names = [t["name"] for t in (d or {}).get("result", {}).get("tools", [])]
print("tools:", names)
check("tools listed", {"vehicle_status", "unlock_doors", "lock_doors", "set_charge_limit"} <= set(names), r.text[:300])


def call(name, args, rid):
    r, d = rpc("tools/call", {"name": name, "arguments": args}, rid=rid, sid=sid)
    content = (d or {}).get("result", {}).get("content", [])
    return " ".join(x.get("text", "") for x in content)


out = call("vehicle_status", {}, 3)
check("status says not connected", "isn't connected" in out, out)
out = call("unlock_doors", {}, 4)
check("unlock asks for confirmation", "CONFIRMATION NEEDED" in out and "confirm_code" in out, out)
code4 = out.split('confirm_code="')[1][:4]
out = call("unlock_doors", {"confirm_code": "0000" if code4 != "0000" else "1111"}, 5)
check("wrong confirm code refused", "wrong or expired" in out, out)
out = call("unlock_doors", {}, 6)
code6 = out.split('confirm_code="')[1][:4]
out = call("unlock_doors", {"confirm_code": code6}, 7)
check("right code proceeds to Tesla (not connected here)", "isn't connected" in out, out)
out = call("unlock_doors", {"confirm_code": code6}, 8)
check("confirm code is single use", "wrong or expired" in out, out)
out = call("set_charge_limit", {"percent": 30}, 9)
check("charge limit range checked", "between 50 and 100" in out, out)

# 7. owner pages
r = c.get("/auth/login")
check("Tesla sign-in needs owner session", r.status_code == 303 and r.headers["location"] == "/")
r = c.get("/api/status")
check("status API needs owner session", r.status_code == 401)
r = c.post("/login", data={"password": PW})
check("owner login sets cookie", r.status_code == 303 and "bem_session" in r.headers.get("set-cookie", ""))
c.cookies.set("bem_session", r.cookies.get("bem_session"))
r = c.get("/")
check("dashboard shows setup", r.status_code == 200 and "Setup" in r.text and "/mcp" in r.text)
r = c.get("/auth/login")
check("Tesla sign-in redirects to Tesla", r.status_code == 303 and r.headers["location"].startswith("https://auth.tesla.com/"), r.headers.get("location"))
r = c.get("/auth/callback", params={"state": "forged", "code": "x"})
check("forged Tesla state refused", r.status_code == 303 and "expired" in r.headers["location"])

r = httpx.post(f"{B}/mcp", headers={**H, "Host": "evil.example.com"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
check("foreign Host header refused", r.status_code in (400, 421), str(r.status_code))

print("\nFAILED:" if fails else "\nALL PASSED", fails or "")
sys.exit(1 if fails else 0)
