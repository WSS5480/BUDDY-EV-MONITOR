"""HTML for the owner pages. Phone-first: wrapped cards, no sideways tables, tiles drill down."""

from __future__ import annotations

from html import escape

CSS = """
:root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--text:#eef1f6;--muted:#9aa3b2;--ok:#3ecf8e;--warn:#f5a524;--bad:#f26d6d;--accent:#e82127}
@media (prefers-color-scheme: light){:root{--bg:#f4f5f8;--card:#fff;--line:#e2e5ec;--text:#14171c;--muted:#5d6675}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:4px 0 12px}h2{font-size:17px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:0 0 12px}
.row{display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--line)}.row:first-of-type{border-top:0}
.dot{flex:none;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:700;color:#000;margin-top:1px}
.ok{background:var(--ok)}.warn{background:var(--warn)}.bad{background:var(--bad)}
.muted{color:var(--muted);font-size:14px}.grow{flex:1;min-width:0;overflow-wrap:anywhere}
button,.btn{appearance:none;border:0;border-radius:10px;padding:10px 14px;font:inherit;font-weight:600;background:var(--accent);color:#fff;cursor:pointer;text-decoration:none;display:inline-block}
.btn2{background:transparent;color:var(--text);border:1px solid var(--line)}
input[type=password]{width:100%;padding:12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);font:inherit;margin:8px 0 12px}
code,.copy{display:block;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:14px ui-monospace,Menlo,monospace;overflow-wrap:anywhere;margin-top:6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;text-align:left;color:var(--text);font-weight:400}
.tile b{display:block;font-size:24px}.tile span{color:var(--muted);font-size:13px}
.kv{display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 12px;padding:8px 0;border-top:1px solid var(--line)}.kv:first-of-type{border-top:0}
.kv .v{font-weight:600;text-align:right}
.msg{background:var(--card);border-left:4px solid var(--accent);padding:10px 12px;border-radius:8px;margin-bottom:12px}
.hidden{display:none}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


def _msg(msg: str) -> str:
    return f"<div class=msg>{escape(msg)}</div>" if msg else ""


def login_page(locked: bool = False, msg: str = "", short_pw: bool = False) -> str:
    note = "<p class=muted>Too many wrong tries. Sign-in is paused for up to an hour.</p>" if locked else ""
    if short_pw:
        note += "<p style='color:var(--bad)'>Sign-in is off: OWNER_PASSWORD in Render must be at least 12 characters.</p>"
    return _page(
        "Buddy EV Monitor",
        f"<h1>Buddy EV Monitor</h1>{_msg(msg)}<div class=card><h2>Owner sign-in</h2>{note}"
        "<form method=post action=/login>"
        "<input type=password name=password placeholder='Owner password' "
        "autocomplete=current-password required autofocus><button>Sign in</button></form></div>",
    )


def connect_page(
    req_id: str, pending: dict, error: str = "", locked: bool = False, short_pw: bool = False, owner: bool = False
) -> str:
    err = f"<p style='color:var(--bad)'>{escape(error)}</p>" if error else ""
    note = "<p class=muted>Too many wrong tries. Sign-in is paused for up to an hour.</p>" if locked else ""
    if short_pw:
        note += "<p style='color:var(--bad)'>Sign-in is off: OWNER_PASSWORD in Render must be at least 12 characters.</p>"
    client_name = pending.get("client_name") or "An app"
    host = pending.get("redirect_host") or "?"
    return _page(
        "Connect Claude to your Tesla",
        "<h1>Connect Claude to your Tesla</h1><div class=card>"
        f"<p><b>{escape(client_name)}</b> is asking to check and control your car through Buddy EV Monitor. "
        "It can unlock, open and start the car when you ask, with no extra approval step.</p>"
        f"<p class=muted>It will send you back to <b>{escape(host)}</b>. Only allow this if you just "
        "added the connector in Claude yourself.</p>"
        f"{err}{'' if owner else note}<form method=post action=/connect><input type=hidden name=req value='{escape(req_id)}'>"
        + (
            "<p class=muted>You're signed in on this site, so no password is needed.</p>"
            if owner
            else "<input type=password name=password placeholder='Owner password' autocomplete=current-password required autofocus>"
        )
        + "<button>Allow</button></form></div>",
    )


def message_page(text: str) -> str:
    return _page("Buddy EV Monitor", f"<h1>Buddy EV Monitor</h1><div class=card>{escape(text)}</div>")


def _step(state: str, title: str, detail: str = "") -> str:
    mark = {"ok": "✓", "warn": "!", "bad": "✕"}[state]
    return f"<div class=row><div class='dot {state}'>{mark}</div><div class=grow><b>{title}</b>{detail}</div></div>"


def dashboard(
    *,
    missing: list[str],
    storage: str,
    connected: bool,
    registered: bool,
    paired: bool | None,
    pairing_link: str,
    connector_url: str,
    msg: str = "",
) -> str:
    steps = []
    if missing:
        names = "".join(f"<span class=copy>{escape(m)}</span>" for m in missing)
        steps.append(_step("bad", "Add settings in Render → Environment", f"<div class=muted>Still missing:</div>{names}"))
    else:
        steps.append(_step("ok", "Settings are in place"))
    if storage != "postgres":
        steps.append(
            _step("warn", "No database yet", "<div class=muted>Add DATABASE_URL first. Without it the Tesla sign-in and car key are lost on every restart, so the next steps are hidden.</div>")
        )
    has_db = storage == "postgres"
    if has_db:
        steps.append(
            _step("ok", "Registered with Tesla")
            if registered
            else _step(
                "bad",
                "Register this site with Tesla",
                "<div class=muted>One time, after the Tesla settings are in.</div>"
                "<form method=post action=/setup/register style='margin-top:8px'><button>Register</button></form>",
            )
        )
        steps.append(
            _step(
                "ok",
                "Tesla account connected",
                "<form method=post action=/setup/disconnect style='margin-top:8px'><button class='btn btn2'>Disconnect</button></form>",
            )
            if connected
            else _step("bad", "Sign in with Tesla", "<div style='margin-top:8px'><a class=btn href=/auth/login>Sign in with Tesla</a></div>")
        )
    if not has_db:
        pass
    elif paired is True:
        steps.append(_step("ok", "Key is on the car"))
    else:
        detail = (
            "<div class=muted>Open this link on your phone and approve it in the Tesla app, near the car. "
            "Needed for lock, unlock, climate and charging.</div>"
            f"<div style='margin-top:8px'><a class=btn href='{escape(pairing_link)}'>Add key to car</a></div>"
        )
        steps.append(_step("warn" if paired is None else "bad", "Add the key to your car", detail))
    steps.append(
        _step(
            "ok" if connected else "warn",
            "Add to Claude",
            "<div class=muted>Claude → Settings → Connectors → Add custom connector, paste this URL, "
            "then sign in with your owner password.</div>"
            f"<span class=copy>{escape(connector_url)}</span>",
        )
    )

    car = ""
    if connected:
        car = """
<div class=card id=carcard><h2>Your car</h2>
<p class=muted id=carnote>Checking costs a fraction of a cent and won't wake the car.</p>
<button id=check onclick="loadCar()">Check car</button>
<div id=tiles class="tiles hidden" style="margin-top:12px"></div>
<div id=detail class=hidden><button class="btn btn2" onclick="back()">← Back</button><h2 id=dtitle style="margin-top:12px"></h2><div id=dbody></div></div>
</div>
<script>
let D=null;
const groups={
 battery:["Battery",[["battery_percent","Battery","%"],["range_miles","Range"," mi"],["charging_state","Charging",""],["charge_limit_percent","Charge limit","%"],["minutes_to_full","Minutes to full",""],["charger_power_kw","Charger power"," kW"]]],
 security:["Locks & doors",[["locked","Locked",""],["windows_open","Windows open",""],["trunk_open","Trunk open",""],["frunk_open","Frunk open",""],["sentry_mode","Sentry mode",""]]],
 climate:["Climate",[["climate_on","Climate on",""],["inside_temp_f","Inside","°F"],["outside_temp_f","Outside","°F"],["climate_set_f","Set to","°F"]]],
 location:["Location",[["shift_state","Gear",""],["speed_mph","Speed"," mph"],["odometer_miles","Odometer"," mi"],["latitude","Latitude",""],["longitude","Longitude",""]]]
};
function fmt(v,u){if(v===null||v===undefined)return"–";if(v===true)return"Yes";if(v===false)return"No";return v+u}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
async function loadCar(){const n=document.getElementById('carnote');n.textContent='Checking…';
 try{const r=await fetch('/api/status');D=await r.json()}catch(e){n.textContent='Could not reach the app.';return}
 if(D.error){n.textContent=D.error;return}
 if(D.state==='asleep'){n.textContent=D.note;return}
 n.textContent=(D.name||'Your Tesla')+' is '+D.state+'.';
 const t=document.getElementById('tiles');t.classList.remove('hidden');
 t.innerHTML=[["battery",fmt(D.battery_percent,'%'),"Battery · "+fmt(D.range_miles,' mi')],
  ["security",D.locked?"Locked":"Unlocked","Locks & doors"],
  ["climate",fmt(D.inside_temp_f,'°F'),"Inside · climate "+(D.climate_on?"on":"off")],
  ["location",D.shift_state||"P","Location & driving"]]
  .map(([k,b,s])=>'<button class=tile onclick="show(\\''+k+'\\')"><b>'+esc(b)+'</b><span>'+esc(s)+'</span></button>').join('')}
function show(k){const g=groups[k];document.getElementById('tiles').classList.add('hidden');document.getElementById('check').classList.add('hidden');
 document.getElementById('detail').classList.remove('hidden');document.getElementById('dtitle').textContent=g[0];
 let h=g[1].map(([f,l,u])=>'<div class=kv><span>'+esc(l)+'</span><span class=v>'+esc(fmt(D[f],u))+'</span></div>').join('');
 if(k==='location'&&D.map_link)h+='<div style="margin-top:10px"><a class=btn href="'+esc(D.map_link)+'" target=_blank rel=noopener>Open map</a></div>';
 document.getElementById('dbody').innerHTML=h}
function back(){document.getElementById('detail').classList.add('hidden');document.getElementById('tiles').classList.remove('hidden');document.getElementById('check').classList.remove('hidden')}
</script>"""

    return _page(
        "Buddy EV Monitor",
        f"<h1>Buddy EV Monitor</h1>{_msg(msg)}"
        f"<div class=card><h2>Setup</h2>{''.join(steps)}</div>{car}"
        "<div class=card><h2>Security</h2>"
        "<p class=muted>Cut off Claude (you can add the connector again later), or sign out of this page on every device.</p>"
        "<form method=post action=/setup/disconnect-claude style='display:inline'><button class='btn btn2'>Disconnect Claude</button></form> "
        "<form method=post action=/setup/signout-all style='display:inline'><button class='btn btn2'>Sign out everywhere</button></form> "
        "<form method=post action=/setup/clear-pause style='display:inline'><button class='btn btn2'>Clear sign-in pause</button></form>"
        "<form method=post action=/setup/new-key style='margin-top:12px'>"
        "<p class=muted>Only if the car key can't be read (for example SECRET_KEY was lost): make a new one. "
        "You'll have to press Register and pair the car again.</p>"
        "<label class=muted><input type=checkbox name=confirm value=yes> I understand</label> "
        "<button class='btn btn2'>Make a new car key</button></form></div>"
        "<form method=post action=/logout><button class='btn btn2'>Sign out</button></form>",
    )
