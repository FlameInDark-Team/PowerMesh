import asyncio
from contextlib import asynccontextmanager
import io
import json
import math
import os
import random
import socket
import sys
import time
from typing import Dict, List, Optional, Set

import base64
import qrcode
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

LOCAL_IP = get_local_ip()
PORT = int(os.getenv("PORT", "8000"))
MOBILE_URL = f"http://{LOCAL_IP}:{PORT}/"

# Multi-Node & System State
class PowerMeshHub:
    def __init__(self):
        self.boot_time = time.time()
        self.seq = 0
        self.tx = 0
        self.rx = 0
        self.dropped = 0
        self.sd_buffered = 0
        self.rssi = -72
        self.batt_v = 12.6
        self.batt_pct = 100
        self.peers = 0
        self.sd_ok = True
        self.ap_ssid = "PowerMesh_Rescue"
        self.ip = "192.168.4.1"
        
        # Hardware modes
        self.beacon_mode = "1hz" # off, 1hz, torch, morse_sos
        self.beacon_on = False
        self.rf_led = False
        self.usb_led = True
        self.radio_mode = "dual" # dual, 433mhz, wifi
        
        # Serial and SD card log
        self.serial_logs = []
        if os.environ.get("VERCEL"):
            self.log_file = "/tmp/queue.log"
        else:
            self.log_file = os.path.join(os.path.dirname(__file__), "queue.log")
        
        # Mesh Network Nodes (SIH Multi-Node Architecture)
        self.mesh_nodes = [
            {
                "id": "PM-01",
                "name": "Rescue Basecamp (Command Hub)",
                "role": "Coordinator / Gateway",
                "ip": "192.168.4.1",
                "status": "Online (Local Host)",
                "rssi": -72,
                "batt": 100,
                "hops": 0,
                "distance": "0 km (Local)",
                "radio": "Dual (433MHz + 2.4GHz)"
            },
            {
                "id": "PM-02",
                "name": "Relief Staging Depot / High School",
                "role": "Repeater / Node #2",
                "ip": "192.168.4.15",
                "status": "Linked (Sub-GHz)",
                "rssi": -78,
                "batt": 88,
                "hops": 1,
                "distance": "1.2 km",
                "radio": "SA618F30 433MHz (1W)"
            },
            {
                "id": "PM-03",
                "name": "River Patrol / Field Search Boat",
                "role": "Mobile Evacuation Unit",
                "ip": "192.168.4.27",
                "status": "Linked (Sub-GHz)",
                "rssi": -69,
                "batt": 92,
                "hops": 2,
                "distance": "1.9 km",
                "radio": "SA618F30 433MHz (1W)"
            }
        ]
        
        # Triage Incidents Queue (Red, Yellow, Green)
        self.incidents: List[Dict] = []
        self.image_store: Dict[str, Dict] = {}
        self.shared_photos: List[Dict] = []
        
        # Init SD log
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"# PowerMesh queue.log boot {int(time.time()*1000)}\n")
        except Exception:
            pass
        self.log_serial(f"[SD] mounted ({self.log_file})")
        self.log_serial(f"[WiFi] softAP {self.ap_ssid} OK IP={self.ip}")
        self.log_serial(f"[RF] NiceRF SA618F30-FD initialized on Serial1 (115200 8N1)")
        self.log_serial(f"[MESH] Discovered 2 remote peer nodes (PM-02, PM-03)")

    def log_serial(self, line: str):
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {line}"
        print(entry)
        self.serial_logs.append(entry)
        if len(self.serial_logs) > 250:
            self.serial_logs.pop(0)

    def read_battery(self, pot_val: float):
        v = 9.0 + pot_val * 3.6
        self.batt_v = round(v, 2)
        if v >= 12.6:
            self.batt_pct = 100
        elif v <= 9.0:
            self.batt_pct = 0
        else:
            self.batt_pct = int(((v - 9.0) / 3.6) * 100)
        self.mesh_nodes[0]["batt"] = self.batt_pct

    def crc16(self, data: bytes) -> int:
        c = 0xFFFF
        for b in data:
            c ^= (b << 8)
            for _ in range(8):
                c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
        return c

    def rf_send(self, payload_str: str, ttl=3, from_node="PM-01"):
        self.seq += 1
        self.tx += 1
        meta = {
            "seq": self.seq,
            "ttl": ttl,
            "from": from_node,
            "rssi": self.rssi,
            "batt": self.batt_pct,
            "p": {},
            "raw": payload_str
        }
        out = json.dumps(meta)
        crc = self.crc16(out.encode('utf-8'))
        self.log_serial(f"[RF TX] seq={self.seq} from={from_node} len={len(out)} crc={crc:04X} ttl={ttl} payload={out[:100]}")
        
        # Store in virtual SD card log
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"{int(time.time()*1000)} TX {self.seq} {from_node} {out}\n")
        self.sd_buffered += 1
        
        # Check if SOS payload and add to Triage queue
        try:
            parsed = json.loads(payload_str)
            p_type = parsed.get("type", "")
            if any(k in p_type for k in ["SOS", "Immediate", "Delayed", "Minor", "Medical", "Emergency", "Food", "Water", "Rescue"]):
                # determine severity
                cat = "Immediate (Red)" if ("Immediate" in p_type or "Medical" in p_type or "SOS" in p_type) else ("Delayed (Yellow)" if "Delayed" in p_type else "Minor (Green)")
                incident = {
                    "id": f"INC-{self.seq:03d}",
                    "seq": self.seq,
                    "type": p_type,
                    "category": cat,
                    "from_node": from_node,
                    "count": parsed.get("count", "Unknown victims"),
                    "msg": parsed.get("msg", ""),
                    "lat": parsed.get("lat") or 26.1445,
                    "lon": parsed.get("lon") or 91.6022,
                    "ts": time.strftime("%H:%M:%S"),
                    "status": "Active Rescue"
                }
                self.incidents.insert(0, incident)
        except Exception:
            pass

        self.rf_led = True
        return out

hub = PowerMeshHub()
connected_websockets: Set[WebSocket] = set()
twin_websockets: Set[WebSocket] = set()

# Background task for beacon, RSSI drift, and battery simulation
async def background_simulation():
    last_rssi = time.time()
    morse_step = 0
    morse_sos = [1,0,1,0,1,0,0,1,1,0,1,1,0,1,1,0,0,1,0,1,0,1,0,0,0,0] # ... --- ...
    while True:
        await asyncio.sleep(0.5)
        # Beacon modes
        if hub.beacon_mode == "1hz":
            hub.beacon_on = not hub.beacon_on
        elif hub.beacon_mode == "torch":
            hub.beacon_on = True
        elif hub.beacon_mode == "morse_sos":
            hub.beacon_on = bool(morse_sos[morse_step % len(morse_sos)])
            morse_step += 1
        else:
            hub.beacon_on = False

        if time.time() - last_rssi > 2.0:
            last_rssi = time.time()
            hub.rssi = -72 + random.randint(-5, 5)
            hub.mesh_nodes[0]["rssi"] = hub.rssi
            hub.mesh_nodes[1]["rssi"] = -78 + random.randint(-4, 4)
            hub.mesh_nodes[2]["rssi"] = -69 + random.randint(-4, 4)
        await broadcast_twin_state()

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_simulation())
    yield
    task.cancel()

app = FastAPI(title="PowerMesh ESP32-S3 Disaster Hub", lifespan=lifespan)

if os.environ.get("VERCEL"):
    uploads_dir = "/tmp/uploads"
    os.makedirs(uploads_dir, exist_ok=True)
    sample_src = os.path.join(os.path.dirname(__file__), "uploads", "sample_flood_rescue.jpg")
    sample_dst = os.path.join(uploads_dir, "sample_flood_rescue.jpg")
    if os.path.exists(sample_src) and not os.path.exists(sample_dst):
        import shutil
        try:
            shutil.copy(sample_src, sample_dst)
        except Exception:
            pass
else:
    uploads_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

# QR Code endpoint
@app.get("/qrcode.png")
def get_qrcode():
    img = qrcode.make(MOBILE_URL)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")

# Unified Navigation Component with White (Default) and Dark Theme Toggle
def build_top_nav(active_page="portal"):
    p_act = "active" if active_page == "portal" else ""
    t_act = "active" if active_page == "twin" else ""
    a_act = "active" if active_page == "admin" else ""
    
    return f"""
    <style>
    :root, [data-theme="light"] {{
        --pm-nav-bg: #ffffff;
        --pm-nav-border: #e2e8f0;
        --pm-nav-text: #0f172a;
        --pm-nav-btn: #f1f5f9;
        --pm-nav-btn-border: #cbd5e1;
        --pm-nav-btn-text: #334155;
        --pm-nav-badge-bg: #f1f5f9;
        --pm-nav-badge-text: #ea580c;
        --pm-modal-bg: #ffffff;
        --pm-modal-border: #cbd5e1;
        --pm-modal-text: #0f172a;
        --pm-modal-sub: #64748b;
        --pm-modal-code-bg: #f8fafc;
    }}
    [data-theme="dark"] {{
        --pm-nav-bg: #0b0d12;
        --pm-nav-border: #222733;
        --pm-nav-text: #f1f5f9;
        --pm-nav-btn: #12151c;
        --pm-nav-btn-border: #283040;
        --pm-nav-btn-text: #94a3b8;
        --pm-nav-badge-bg: #181d26;
        --pm-nav-badge-text: #ff6600;
        --pm-modal-bg: #10141c;
        --pm-modal-border: #2c3545;
        --pm-modal-text: #f1f5f9;
        --pm-modal-sub: #94a3b8;
        --pm-modal-code-bg: #090c10;
    }}
    .pm-nav {{
        background: var(--pm-nav-bg);
        border-bottom: 1px solid var(--pm-nav-border);
        padding: 8px 16px;
        position: sticky;
        top: 0;
        z-index: 1000;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        box-shadow: 0 1px 4px rgba(0,0,0,0.05);
        transition: background 0.2s, border-color 0.2s;
    }}
    .pm-nav-brand {{
        display: flex;
        align-items: center;
        gap: 8px;
    }}
    .pm-nav-brand-title {{
        font-family: 'JetBrains Mono', monospace;
        font-weight: 800;
        font-size: 13px;
        color: var(--pm-nav-text);
        letter-spacing: 0.04em;
        text-transform: uppercase;
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    .pm-nav-badge {{
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px;
        padding: 2px 6px;
        border-radius: 3px;
        background: var(--pm-nav-badge-bg);
        border: 1px solid var(--pm-nav-border);
        color: var(--pm-nav-badge-text);
        font-weight: 700;
    }}
    .pm-nav-btns {{
        display: flex;
        align-items: center;
        gap: 6px;
        flex-wrap: wrap;
    }}
    .pm-btn {{
        background: var(--pm-nav-btn);
        border: 1px solid var(--pm-nav-btn-border);
        color: var(--pm-nav-btn-text);
        padding: 5px 11px;
        border-radius: 4px;
        font-size: 11.5px;
        font-weight: 600;
        font-family: 'Inter', sans-serif;
        text-decoration: none;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        gap: 5px;
        min-height: 30px;
        transition: all 0.15s ease;
        white-space: nowrap;
    }}
    .pm-btn:hover {{
        background: var(--pm-nav-border);
        color: var(--pm-nav-text);
    }}
    .pm-btn.active {{
        background: #fff7ed;
        color: #ea580c;
        border-color: #ea580c;
        font-weight: 700;
    }}
    [data-theme="dark"] .pm-btn.active {{
        background: #18202d;
        color: #ff6600;
        border-color: #ff6600;
    }}
    .pm-btn-demo {{
        background: #fef3c7;
        border-color: #f59e0b;
        color: #b45309;
        font-weight: 700;
    }}
    [data-theme="dark"] .pm-btn-demo {{
        background: #241407;
        border-color: #b45309;
        color: #f59e0b;
    }}
    .pm-btn-phone {{
        background: #e0f2fe;
        border-color: #38bdf8;
        color: #0369a1;
        font-weight: 700;
    }}
    [data-theme="dark"] .pm-btn-phone {{
        background: #0f1c24;
        border-color: #0e7490;
        color: #38bdf8;
    }}
    .pm-btn-theme {{
        background: var(--pm-nav-btn);
        border-color: var(--pm-nav-btn-border);
        color: var(--pm-nav-text);
        font-weight: 700;
    }}
    .pm-modal-bg {{
        display: none;
        position: fixed;
        top:0;left:0;right:0;bottom:0;
        background: rgba(0,0,0,0.6);
        backdrop-filter: blur(4px);
        z-index: 10000;
        align-items: center;
        justify-content: center;
        padding: 16px;
    }}
    .pm-modal-bg.open {{ display: flex; }}
    .pm-modal-box {{
        background: var(--pm-modal-bg);
        border: 1px solid var(--pm-modal-border);
        border-radius: 6px;
        padding: 20px;
        max-width: 360px;
        width: 100%;
        text-align: center;
        color: var(--pm-modal-text);
        box-shadow: 0 16px 32px rgba(0,0,0,0.25);
    }}
    .pm-modal-box img {{
        background: #fff;
        padding: 8px;
        border-radius: 4px;
        max-width: 180px;
        margin: 14px auto;
        display: block;
        border: 1px solid #e2e8f0;
    }}
    .pm-modal-box code {{
        display: block;
        background: var(--pm-modal-code-bg);
        border: 1px solid var(--pm-modal-border);
        padding: 8px 10px;
        border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: #ea580c;
        margin-bottom: 14px;
        word-break: break-all;
    }}
    .pm-modal-close {{
        width: 100%;
        min-height: 36px;
        border: 1px solid var(--pm-modal-border);
        background: var(--pm-nav-btn);
        color: var(--pm-nav-text);
        border-radius: 4px;
        font-weight: 600;
        font-size: 12px;
        cursor: pointer;
    }}
    </style>

    <div class="pm-nav">
        <div class="pm-nav-brand">
            <div class="pm-nav-brand-title">
                <span style="color:#ea580c">&#9650;</span> POWERMESH
            </div>
            <span class="pm-nav-badge">NDRF MESH-01</span>
        </div>
        <div class="pm-nav-btns">
            <a href="/" class="pm-btn {p_act}">🆘 Help Portal</a>
            <a href="/admin" class="pm-btn {a_act}">🚨 Command Board</a>
            <a href="/twin" class="pm-btn {t_act}">🛠️ Hardware Twin</a>
            <button onclick="triggerDemoScenario()" class="pm-btn pm-btn-demo">⚡ Demo Disaster</button>
            <button onclick="document.getElementById('qr-modal').classList.add('open')" class="pm-btn pm-btn-phone">📲 Phone QR</button>
            <button class="pm-btn pm-btn-theme theme-toggle-btn" onclick="toggleTheme()" title="Toggle Light / Dark Mode">🌙 Dark</button>
        </div>
    </div>

    <div id="qr-modal" class="pm-modal-bg" onclick="if(event.target===this)this.classList.remove('open')">
        <div class="pm-modal-box">
            <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:#ea580c;letter-spacing:0.06em;font-weight:700">FIELD NETWORK GATEWAY</div>
            <h3 style="margin:4px 0 6px 0;font-size:16px;font-weight:800">Connect Mobile Device</h3>
            <p style="font-size:12px;color:var(--pm-modal-sub);margin:6px 0 10px 0;line-height:1.4">Connect phone to field Wi-Fi or scan below:</p>
            <img src="/qrcode.png" alt="Mobile QR Code">
            <code>{MOBILE_URL}</code>
            <button class="pm-modal-close" onclick="document.getElementById('qr-modal').classList.remove('open')">Dismiss</button>
        </div>
    </div>

    <script>
    function initTheme(){{
        const saved = localStorage.getItem('powermesh-theme') || 'light';
        document.documentElement.setAttribute('data-theme', saved);
        updateThemeButtons(saved);
    }}
    function toggleTheme(){{
        const current = document.documentElement.getAttribute('data-theme') || 'light';
        const next = current === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('powermesh-theme', next);
        updateThemeButtons(next);
    }}
    window.toggleTheme = toggleTheme;
    window.togglePortalTheme = toggleTheme;
    function updateThemeButtons(th){{
        const btns = document.querySelectorAll('.theme-toggle-btn, #theme-btn');
        btns.forEach(b => {{
            b.innerHTML = th === 'light' ? '🌙 Dark' : '☀️ Light';
        }});
    }}
    if(document.readyState === 'loading'){{
        document.addEventListener('DOMContentLoaded', initTheme);
    }} else {{
        initTheme();
    }}

    async function triggerDemoScenario(){{
        try{{
            const r = await fetch('/api/demo-scenario', {{method:'POST'}});
            const j = await r.json();
            alert("Multi-Hazard Disaster Scenario Injected!\\n\\n• Earthquake: 3 survivors trapped in collapsed structure\\n• Cyclone / Grid Failure: Medical clinic backup battery low\\n• Flood: River rescue unit deployed\\n• 720p Field Reconnaissance Photo received at Command Center");
            if(window.location.pathname === '/admin' || window.location.pathname === '/twin'){{
                window.location.reload();
            }}
        }}catch(e){{
            alert("Error loading demo scenario: " + e);
        }}
    }}
    </script>
    """

# Read survivor portal HTML from sketch.ino
def extract_index_html():
    sketch_path = os.path.join(os.path.dirname(__file__), "sketch.ino")
    if os.path.exists(sketch_path):
        with open(sketch_path, "r", encoding="utf-8") as f:
            content = f.read()
        start = content.find('const char INDEX_HTML[] PROGMEM = R"HTML(')
        end = content.find(')HTML";', start)
        if start != -1 and end != -1:
            html = content[start + len('const char INDEX_HTML[] PROGMEM = R"HTML(') : end]
            topbar = build_top_nav("portal")
            return html.replace('<body>', '<body>\n' + topbar)
    return "<h1>PowerMesh Portal</h1>"

@app.get("/")
def get_root():
    return HTMLResponse(extract_index_html())

@app.get("/status")
def get_status():
    uptime_ms = int((time.time() - hub.boot_time) * 1000)
    return {
        "uptime": uptime_ms,
        "seq": hub.seq,
        "tx": hub.tx,
        "rx": hub.rx,
        "dropped": hub.dropped,
        "rssi": hub.rssi,
        "battV": hub.batt_v,
        "battPct": hub.batt_pct,
        "peers": hub.peers,
        "sdBuffered": hub.sd_buffered,
        "sdOK": hub.sd_ok,
        "ap": hub.ap_ssid,
        "ip": hub.ip,
        "beacon": hub.beacon_on,
        "beaconMode": hub.beacon_mode,
        "rfLed": hub.rf_led,
        "usbLed": hub.usb_led,
        "radioMode": hub.radio_mode,
        "nodeId": "PM-01",
        "received_photos": hub.shared_photos,
        "incidents": hub.incidents,
        "meshNodes": hub.mesh_nodes,
        "serialLogs": hub.serial_logs[-35:]
    }

# Multi-Hazard Realistic Disaster Scenario (Earthquake, Cyclone, Flood, Grid Outage)
@app.post("/api/demo-scenario")
async def inject_demo_scenario(disaster_type: str = "multi"):
    hub.incidents.clear()
    
    # 1. Earthquake Structural Collapse Alert
    s1 = json.dumps({
        "type": "Immediate (Red)",
        "count": "3 survivors trapped",
        "msg": "Earthquake rubble entrapment in collapsed community center basement. Acoustic tapping confirmed. Canine search unit requested!",
        "lat": 26.1852,
        "lon": 91.7539,
        "ts": time.time()
    })
    hub.rf_send(s1, ttl=3, from_node="PM-03")
    
    # 2. Cyclone / Grid Blackout Critical Medical Need
    s2 = json.dumps({
        "type": "Medical Emergency",
        "count": "2 critical patients",
        "msg": "Cyclone power grid failure. Primary health clinic on emergency battery reserve. Road blocked by fallen trees, need portable generator.",
        "lat": 26.1720,
        "lon": 91.7410,
        "ts": time.time()
    })
    hub.rf_send(s2, ttl=2, from_node="PM-02")
    
    # 3. Flood Water Rising Alert
    s3 = json.dumps({
        "type": "Delayed (Yellow)",
        "count": "1 family (4 people)",
        "msg": "Flash flood waters cut off bridge. Family safe on concrete terrace, drinking water required within 12 hours.",
        "lat": 26.1901,
        "lon": 91.7602,
        "ts": time.time()
    })
    hub.rf_send(s3, ttl=3, from_node="PM-03")
    
    # 4. Radio dispatch from NDRF SAR Team
    chat = json.dumps({
        "type": "chat",
        "sender_name": "NDRF SAR Team #2",
        "msg": "Rubble search team on site with acoustic sensors. Mobile repeater active on PM-02.",
        "time": time.strftime("%H:%M"),
        "ts": time.time()
    })
    hub.rf_send(chat, ttl=3, from_node="PM-03")
    
    # 5. Field Reconnaissance Photo
    photo_payload = {
        "type": "image",
        "img_id": "img_demo_multihazard",
        "from": "PM-03",
        "sender_name": "NDRF All-Terrain SAR Unit",
        "img_url": "/uploads/sample_flood_rescue.jpg",
        "caption": "NDRF All-Terrain SAR Unit deployed with debris extraction gear",
        "time": time.strftime("%H:%M"),
        "ts": time.time()
    }
    hub.shared_photos.insert(0, photo_payload)
    hub.rf_send(json.dumps(photo_payload), ttl=3, from_node="PM-03")

    # Broadcast to all websockets
    for ws in list(connected_websockets):
        try:
            await ws.send_text(json.dumps(photo_payload))
            await ws.send_text(json.dumps({"type": "chat", "sender_name": "NDRF SAR Team #2", "msg": "Acoustic contact made with basement survivors. Hydraulic cutter deployed.", "time": time.strftime("%H:%M")}))
        except Exception:
            pass
            
    await broadcast_twin_state()
    return {"status": "ok", "scenario": "Multi-Hazard Disaster Response (Earthquake, Cyclone, Flood, Grid Outage)", "photo": "/uploads/sample_flood_rescue.jpg", "incidents": hub.incidents}

@app.get("/api/incidents")
def get_incidents():
    return hub.incidents

@app.post("/api/incidents/{inc_id}/resolve")
def resolve_incident(inc_id: str):
    for inc in hub.incidents:
        if inc.get("id") == inc_id:
            inc["status"] = "Rescued / Resolved"
            hub.log_serial(f"[TRIAGE] Incident {inc_id} marked as RESCUED")
            return {"status": "resolved", "id": inc_id}
    return JSONResponse(status_code=404, content={"error": "Not found"})


# Delete Photo Endpoint
@app.delete("/api/photos/{photo_id}")
@app.post("/api/photos/{photo_id}/delete")
async def delete_photo(photo_id: str):
    removed = []
    kept = []
    for p in hub.shared_photos:
        p_id = p.get("img_id")
        fname = os.path.basename(p.get("img_url", ""))
        if p_id == photo_id or fname == photo_id:
            removed.append(p)
        else:
            kept.append(p)
            
    if removed:
        hub.shared_photos = kept
        for r in removed:
            url = r.get("img_url", "")
            if url.startswith("/uploads/"):
                fname = url.replace("/uploads/", "")
                fpath = os.path.join(uploads_dir, fname)
                if os.path.exists(fpath) and "sample_flood_rescue.jpg" not in fname:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
        hub.log_serial(f"[IMAGE] Photo {photo_id} deleted from mesh buffer ({len(removed)} instances)")
        del_msg = json.dumps({"type": "delete_photo", "img_id": photo_id})
        for ws in list(connected_websockets):
            try:
                await ws.send_text(del_msg)
            except Exception:
                pass
        await broadcast_twin_state()
        return {"status": "ok", "deleted": photo_id, "count": len(removed)}
    return JSONResponse(status_code=404, content={"status": "error", "message": "Photo not found"})

# Captive Portal detection probes
@app.get("/generate_204")
@app.get("/hotspot-detect.html")
@app.get("/canonical.html")
@app.get("/ncsi.txt")
@app.get("/connecttest.txt")
@app.get("/redirect")
def captive_redirect():
    return RedirectResponse(url="/")

@app.get("/success.txt")
def captive_success():
    return HTMLResponse(content="", status_code=200)

def process_img_chunk(parsed: dict) -> Optional[dict]:
    img_id = parsed.get("img_id", "img_default")
    seq = int(parsed.get("seq", 0))
    total = int(parsed.get("total", 1))
    chunk_data = parsed.get("data", "")
    sender_name = parsed.get("sender_name", "Citizen")

    if img_id not in hub.image_store:
        hub.image_store[img_id] = {
            "total": total,
            "chunks": {},
            "sender": sender_name,
            "started": time.time()
        }
    hub.image_store[img_id]["chunks"][seq] = chunk_data
    received_len = len(hub.image_store[img_id]["chunks"])

    if received_len % 10 == 0 or received_len == total:
        hub.log_serial(f"[RF RX] Reassembling photo {img_id}: frame {received_len}/{total}")

    # If all chunks arrived, reconstruct image!
    if received_len >= total:
        full_b64 = "".join(hub.image_store[img_id]["chunks"][i] for i in range(total) if i in hub.image_store[img_id]["chunks"])
        filename = f"photo_{int(time.time()*1000)}.jpg"
        filepath = os.path.join(uploads_dir, filename)
        raw_b64 = full_b64.split(",", 1)[1] if "," in full_b64 else full_b64
        try:
            with open(filepath, "wb") as img_f:
                img_f.write(base64.b64decode(raw_b64))
            img_url = f"/uploads/{filename}"
        except Exception:
            img_url = full_b64
        
        photo_msg = {
            "type": "image",
            "img_id": img_id,
            "from": "PM-01",
            "sender_name": sender_name,
            "img_url": img_url,
            "data_url": full_b64,
            "caption": "Disaster Scene Photo (720p Mesh Transmitted)",
            "time": time.strftime("%H:%M"),
            "ts": time.time()
        }
        hub.shared_photos.insert(0, photo_msg)
        hub.log_serial(f"[IMAGE] Photo {img_id} fully reassembled ({total} frames)! Stored at {img_url}")

        hub.rf_send(json.dumps(photo_msg))
        if img_id in hub.image_store:
            del hub.image_store[img_id]
        return photo_msg
    return None

@app.post("/api/upload-photo-chunk")
async def api_upload_photo_chunk(req: dict):
    photo_msg = process_img_chunk(req)
    if photo_msg:
        broadcast_payload = json.dumps(photo_msg)
        for ws_client in list(connected_websockets):
            try:
                await ws_client.send_text(broadcast_payload)
            except Exception:
                pass
        await broadcast_twin_state()
        return {"status": "ok", "assembled": True, "photo": photo_msg}
    img_id = req.get("img_id", "")
    received = len(hub.image_store[img_id]["chunks"]) if img_id in hub.image_store else 0
    total = int(req.get("total", 1))
    return {"status": "ok", "assembled": False, "received": received, "total": total}

@app.post("/api/packet")
async def api_send_packet(req: dict):
    p_type = req.get("type", "")
    if p_type == "img_chunk":
        photo_msg = process_img_chunk(req)
        return {"status": "ok", "assembled": bool(photo_msg)}
    raw = json.dumps(req)
    out_msg = hub.rf_send(raw)
    hub.log_serial(f"[HTTP RX] {raw[:120]}")
    for ws in list(connected_websockets):
        try:
            await ws.send_text(out_msg)
        except Exception:
            pass
    await broadcast_twin_state()
    return {"status": "ok", "out": out_msg}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    hub.peers += 1
    hub.log_serial(f"[WS] survivor client connected (total peers={hub.peers})")
    connected_websockets.add(websocket)
    
    hello = json.dumps({"hello": "PowerMesh", "node": "PM-01", "seq": hub.seq})
    await websocket.send_text(hello)
    
    try:
        while True:
            data = await websocket.receive_text()
            try:
                parsed = json.loads(data)
            except Exception:
                parsed = None

            if parsed and parsed.get("type") == "img_chunk":
                photo_msg = process_img_chunk(parsed)
                if photo_msg:
                    broadcast_payload = json.dumps(photo_msg)
                    for ws_client in list(connected_websockets):
                        try:
                            await ws_client.send_text(broadcast_payload)
                        except Exception:
                            pass
            else:
                hub.log_serial(f"[WS RX] {data[:120]}")
                out_msg = hub.rf_send(data)
                for ws in list(connected_websockets):
                    try:
                        await ws.send_text(out_msg)
                    except Exception:
                        pass
            
            await broadcast_twin_state()
            await asyncio.sleep(0.06)
            hub.rf_led = False
            await broadcast_twin_state()
            
    except (WebSocketDisconnect, RuntimeError):
        connected_websockets.discard(websocket)
        if hub.peers > 0:
            hub.peers -= 1
        hub.log_serial(f"[WS] survivor client disconnected (peers={hub.peers})")
        await broadcast_twin_state()

# Digital Twin WebSocket
@app.websocket("/twin/ws")
async def twin_ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    twin_websockets.add(websocket)
    await websocket.send_json(get_twin_state())
    try:
        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")
            if action == "press_sos":
                hub.log_serial("[BTN] SOS hardware pushbutton pressed (GPIO 10)!")
                sos_payload = json.dumps({
                    "type": "Immediate (Red)",
                    "count": "Hardware Emergency Signal",
                    "msg": "Physical SOS pushbutton triggered on Node PM-01",
                    "held": 320,
                    "lat": 26.1445,
                    "lon": 91.6022
                })
                out_msg = hub.rf_send(sos_payload)
                for ws in list(connected_websockets):
                    try:
                        await ws.send_text(out_msg)
                    except Exception:
                        pass
                hub.rf_led = True
                await broadcast_twin_state()
                await asyncio.sleep(0.06)
                hub.rf_led = False
                await broadcast_twin_state()
            elif action == "set_battery":
                val = float(msg.get("value", 1.0))
                hub.read_battery(val)
                await broadcast_twin_state()
            elif action == "set_beacon":
                hub.beacon_mode = msg.get("mode", "1hz")
                hub.log_serial(f"[BEACON] Mode changed to: {hub.beacon_mode}")
                await broadcast_twin_state()
            elif action == "set_radio":
                hub.radio_mode = msg.get("mode", "dual")
                hub.log_serial(f"[RADIO] Switch mode: {hub.radio_mode}")
                await broadcast_twin_state()
            elif action == "inject_rf":
                raw = msg.get("payload", "Hello mesh")
                hub.rx += 1
                from_n = msg.get("from", "PM-02")
                hub.log_serial(f"[RF RX] SA618 packet from {from_n}: {raw}")
                try:
                    with open(hub.log_file, "a", encoding="utf-8") as f:
                        f.write(f"{int(time.time()*1000)} RX {from_n} {raw}\n")
                except Exception:
                    pass
                for ws in list(connected_websockets):
                    try:
                        await ws.send_text(raw)
                    except Exception:
                        pass
                await broadcast_twin_state()
    except (WebSocketDisconnect, RuntimeError):
        twin_websockets.discard(websocket)

def get_twin_state():
    return {
        "seq": hub.seq,
        "tx": hub.tx,
        "rx": hub.rx,
        "dropped": hub.dropped,
        "rssi": hub.rssi,
        "battV": hub.batt_v,
        "battPct": hub.batt_pct,
        "peers": hub.peers,
        "sdBuffered": hub.sd_buffered,
        "sdOK": hub.sd_ok,
        "beacon": hub.beacon_on,
        "beaconMode": hub.beacon_mode,
        "rfLed": hub.rf_led,
        "usbLed": hub.usb_led,
        "radioMode": hub.radio_mode,
        "nodes": hub.mesh_nodes,
        "incidents": hub.incidents[:10],
        "serialLogs": hub.serial_logs[-45:]
    }

async def broadcast_twin_state():
    ts = get_twin_state()
    for ws in list(twin_websockets):
        try:
            await ws.send_json(ts)
        except Exception:
            pass

@app.get("/api/twin/state")
def api_twin_state():
    return get_twin_state()

@app.post("/api/twin/action")
async def api_twin_action(req: dict):
    action = req.get("action")
    if action == "press_sos":
        hub.log_serial("[BTN] SOS hardware pushbutton pressed (GPIO 10)!")
        sos_payload = json.dumps({
            "type": "Immediate (Red)",
            "count": "Hardware Emergency Signal",
            "msg": "Physical SOS pushbutton triggered on Node PM-01",
            "held": 320,
            "lat": 26.1445,
            "lon": 91.6022
        })
        out_msg = hub.rf_send(sos_payload)
        for ws in list(connected_websockets):
            try:
                await ws.send_text(out_msg)
            except Exception:
                pass
        hub.rf_led = True
        await broadcast_twin_state()
        await asyncio.sleep(0.06)
        hub.rf_led = False
        await broadcast_twin_state()
    elif action == "set_battery":
        val = float(req.get("value", 1.0))
        hub.read_battery(val)
        hub.log_serial(f"[ADC] Battery pot adjusted: {hub.batt_v}V ({hub.batt_pct}%)")
        await broadcast_twin_state()
    elif action == "set_beacon":
        hub.beacon_mode = req.get("mode", "1hz")
        hub.log_serial(f"[BEACON] Mode changed to: {hub.beacon_mode}")
        await broadcast_twin_state()
    elif action == "set_radio":
        hub.radio_mode = req.get("mode", "dual")
        hub.log_serial(f"[RADIO] Switch mode: {hub.radio_mode}")
        await broadcast_twin_state()
    elif action == "inject_rf":
        raw = req.get("payload", "Hello mesh")
        hub.rx += 1
        from_n = req.get("from", "PM-02")
        hub.log_serial(f"[RF RX] SA618 packet from {from_n}: {raw}")
        try:
            with open(hub.log_file, "a", encoding="utf-8") as f:
                f.write(f"{int(time.time()*1000)} RX {from_n} {raw}\n")
        except Exception:
            pass
        for ws in list(connected_websockets):
            try:
                await ws.send_text(raw)
            except Exception:
                pass
        await broadcast_twin_state()
    return {"status": "ok", "state": get_twin_state()}

# ====================================================================
# INCIDENT COMMAND & MESH NETWORK PAGE (/admin)
# ====================================================================
@app.get("/admin")
def get_admin_dashboard():
    topbar = build_top_nav("admin")
    
    photos_html = ""
    if not hub.shared_photos:
        photos_html = '<div style="color:var(--text-dim);font-size:13px;grid-column:1/-1;text-align:center;padding:24px;border:1px dashed var(--border);border-radius:4px">No disaster reconnaissance photos received yet. Upload a photo via Survivor Portal to observe 433MHz mesh packet transmission.</div>'
    else:
        for p in hub.shared_photos:
            p_id = p.get('img_id') or os.path.basename(p.get('img_url', ''))
            photos_html += f'''
            <div id="photo-card-{p_id}" class="photo-card">
                <img src="{p['img_url']}" style="width:100%;height:130px;object-fit:cover;display:block;cursor:pointer" onclick="window.open('{p['img_url']}','_blank')">
                <div style="padding:10px 12px;font-size:12px">
                    <div style="font-weight:700;color:var(--text);margin-bottom:2px">{p.get('caption','Field Reconnaissance Photo')}</div>
                    <div style="color:var(--text-dim);font-size:11px;font-family:'JetBrains Mono',monospace">From: {p.get('sender_name','Survivor')} [{p.get('from','PM-01')}]</div>
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
                        <span style="color:#ea580c;font-size:10px;font-family:'JetBrains Mono',monospace">{p.get('time','Just now')} &bull; 433MHz RX</span>
                        <button onclick="deletePhoto('{p_id}')" class="btn-del-photo">🗑️ Delete</button>
                    </div>
                </div>
            </div>
            '''

    incidents_rows = ""
    if not hub.incidents:
        incidents_rows = '<tr><td colspan="6" style="text-align:center;color:var(--text-dim);padding:24px">No active emergency alerts recorded. Click "Flood Demo" to inject realistic field distress calls.</td></tr>'
    else:
        for inc in hub.incidents:
            is_urgent = "Immediate" in inc['category'] or "Medical" in inc['category']
            badge_color = "#dc2626" if is_urgent else ("#d97706" if "Delayed" in inc['category'] else "#059669")
            status_btn_style = "background:#ecfdf5;border:1px solid #10b981;color:#047857" if inc['status'] != "Rescued / Resolved" else "background:var(--surface-sub);border:1px solid var(--border);color:var(--text-dim)"
            
            incidents_rows += f"""
            <tr>
                <td><span style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text);font-weight:700">{inc['id']}</span></td>
                <td><span style="font-family:'JetBrains Mono',monospace;background:{badge_color}18;color:{badge_color};border:1px solid {badge_color}55;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:700;letter-spacing:0.04em">{inc['category'].upper()}</span></td>
                <td><span style="font-size:12px;color:var(--text);font-weight:600">{inc['count']}</span></td>
                <td style="font-size:12px;color:var(--text);line-height:1.4">{inc['msg']}</td>
                <td><code style="font-family:'JetBrains Mono',monospace;font-size:11px;color:#ea580c">{inc['lat']:.4f}, {inc['lon']:.4f}</code></td>
                <td><button onclick="resolveIncident('{inc['id']}')" style="padding:4px 10px;font-size:11px;border-radius:3px;font-weight:700;cursor:pointer;font-family:'Inter',sans-serif;{status_btn_style}">{inc['status']}</button></td>
            </tr>
            """

    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PowerMesh Incident Command — Tactical CAD</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root, [data-theme="light"] {{
            --bg: #f8fafc;
            --surface: #ffffff;
            --surface-sub: #f1f5f9;
            --border: #e2e8f0;
            --text: #0f172a;
            --text-dim: #64748b;
            --orange: #ea580c;
            --green: #059669;
            --amber: #d97706;
            --red: #dc2626;
            --card-shadow: 0 1px 3px rgba(0,0,0,0.06);
        }}
        [data-theme="dark"] {{
            --bg: #0b0d11;
            --surface: #12151c;
            --surface-sub: #0d1015;
            --border: #222733;
            --text: #f1f5f9;
            --text-dim: #828e9e;
            --orange: #ff6600;
            --green: #10b981;
            --amber: #f59e0b;
            --red: #ef4444;
            --card-shadow: none;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg);
            color: var(--text);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh;
            transition: background 0.2s, color 0.2s;
        }}
        .container {{
            max-width: 1280px;
            margin: 0 auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 18px 20px;
            box-shadow: var(--card-shadow);
        }}
        .card-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 14px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border);
        }}
        .card-title {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: var(--text);
            display: flex;
            align-items: center;
            gap: 8px;
            font-weight: 800;
        }}
        .card-title-icon {{
            color: var(--orange);
        }}
        .badge-status {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 10.5px;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            letter-spacing: 0.04em;
        }}
        .badge-green {{
            background: rgba(5,150,105,0.12);
            color: var(--green);
            border: 1px solid rgba(5,150,105,0.3);
        }}
        .badge-orange {{
            background: rgba(234,88,12,0.12);
            color: var(--orange);
            border: 1px solid rgba(234,88,12,0.3);
        }}
        /* Mesh Topology Cards */
        .mesh-rack {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 12px;
        }}
        .node-box {{
            background: var(--surface-sub);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            position: relative;
        }}
        .node-box.coordinator {{
            border-left: 4px solid var(--orange);
        }}
        .node-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .node-tag {{
            font-family: 'JetBrains Mono', monospace;
            font-weight: 800;
            font-size: 12px;
            color: var(--text);
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 2px 7px;
            border-radius: 3px;
        }}
        .node-status {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            color: var(--green);
            font-weight: 700;
        }}
        .node-name {{
            font-size: 14px;
            font-weight: 700;
            color: var(--text);
        }}
        .node-specs {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            color: var(--text-dim);
            line-height: 1.6;
        }}
        .node-specs span {{
            color: var(--text);
            font-weight: 600;
        }}
        /* Triage CAD Table */
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12.5px;
        }}
        th, td {{
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }}
        th {{
            font-family: 'JetBrains Mono', monospace;
            color: var(--text-dim);
            font-size: 10.5px;
            text-transform: uppercase;
            font-weight: 700;
            letter-spacing: 0.05em;
            background: var(--surface-sub);
        }}
        tr:hover {{ background: var(--surface-sub); }}
        
        /* Photo Card */
        .photo-card {{
            background: var(--surface-sub);
            border: 1px solid var(--border);
            border-radius: 4px;
            overflow: hidden;
            transition: opacity 0.2s;
        }}
        .btn-del-photo {{
            background: #fee2e2;
            color: #b91c1c;
            border: 1px solid #fca5a5;
            padding: 3px 8px;
            border-radius: 3px;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
        }}
        [data-theme="dark"] .btn-del-photo {{
            background: #271010;
            color: #ef4444;
            border-color: #7f1d1d;
        }}
    </style>
</head>
<body>
    {topbar}
    <div class="container">
        <!-- 433MHz Mesh Topology Rack -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="card-title-icon">&#9650;</span> 433MHz Sub-GHz Multi-Hop Topology & Transceiver Mesh
                </div>
                <span class="badge-status badge-green">3 NODES CONNECTED &bull; 0 PACKETS DROPPED</span>
            </div>
            <div class="mesh-rack">
                <div class="node-box coordinator">
                    <div class="node-header">
                        <span class="node-tag">PM-01</span>
                        <span class="node-status">GATEWAY COORDINATOR</span>
                    </div>
                    <div class="node-name">Disaster Basecamp Command Hub</div>
                    <div class="node-specs">
                        HARDWARE: <span>ESP32-S3 + SA618F30 1W Radio</span><br>
                        LINK: <span>Direct Command Terminal ({hub.rssi} dBm)</span><br>
                        POWER: <span>{hub.batt_pct}% (3S Li-ion 12.6V)</span><br>
                        DISPATCH: <span>Store & Forward Active</span>
                    </div>
                </div>

                <div class="node-box">
                    <div class="node-header">
                        <span class="node-tag">PM-02</span>
                        <span class="node-status" style="color:var(--orange)">1 HOP &bull; 3.4 KM</span>
                    </div>
                    <div class="node-name">Hilltop Mountain Relay Tower</div>
                    <div class="node-specs">
                        HARDWARE: <span>Autonomous Solar Repeater</span><br>
                        LINK: <span>Line-of-Sight (-78 dBm)</span><br>
                        POWER: <span>85% (Solar Backed)</span><br>
                        DISPATCH: <span>Forwarding PM-03 Packets</span>
                    </div>
                </div>

                <div class="node-box">
                    <div class="node-header">
                        <span class="node-tag">PM-03</span>
                        <span class="node-status" style="color:var(--amber)">2 HOPS &bull; 1.9 KM</span>
                    </div>
                    <div class="node-name">NDRF All-Terrain SAR Unit #2</div>
                    <div class="node-specs">
                        HARDWARE: <span>Mobile Rapid Tactical SAR Node</span><br>
                        LINK: <span>Relayed via PM-02 (-69 dBm)</span><br>
                        POWER: <span>92% (12V Marine Battery)</span><br>
                        MISSION: <span>Multi-Hazard Search & Evacuation</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Live Emergency Triage CAD -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="card-title-icon">&#9888;</span> Emergency Incident Dispatch Queue (Life-Safety Priority)
                </div>
                <span class="badge-status badge-orange">{len(hub.incidents)} ACTIVE INCIDENTS</span>
            </div>
            <div style="overflow-x:auto">
                <table>
                    <thead>
                        <tr>
                            <th>INCIDENT ID</th>
                            <th>TRIAGE PRIORITY</th>
                            <th>AFFECTED</th>
                            <th>SITUATION BRIEFING</th>
                            <th>GPS LOCATION</th>
                            <th>DISPATCH STATUS</th>
                        </tr>
                    </thead>
                    <tbody id="incidents-tbody">
                        {incidents_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 720p Mesh Reconnaissance Photos with Delete Option -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    <span class="card-title-icon">&#128247;</span> Disaster Field Reconnaissance Photos (433MHz Mesh Transmitted)
                </div>
                <span class="badge-status badge-green" id="photo-count-badge">{len(hub.shared_photos)} RECON PHOTOS</span>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px" id="photos-grid">
                {photos_html}
            </div>
        </div>
    </div>

    <script>
    async function resolveIncident(id){{
        await fetch(`/api/incidents/${{id}}/resolve`, {{method:'POST'}});
        window.location.reload();
    }}

    async function deletePhoto(id){{
        if(!confirm("Are you sure you want to delete this photo from the mesh archive?")) return;
        try {{
            const r = await fetch('/api/photos/' + encodeURIComponent(id) + '/delete', {{method:'POST'}});
            if(r.ok){{
                const card = document.getElementById('photo-card-' + id);
                if(card) {{
                    card.style.opacity = '0';
                    setTimeout(() => card.remove(), 250);
                }} else {{
                    window.location.reload();
                }}
            }} else {{
                alert("Could not delete photo: " + r.statusText);
            }}
        }} catch(e) {{
            alert("Error: " + e);
        }}
    }}
    </script>
</body>
</html>
"""
    return HTMLResponse(html)

# ====================================================================
# HARDWARE DIGITAL TWIN PAGE (/twin)
# ====================================================================
@app.get("/twin")
def get_twin():
    topbar = build_top_nav("twin")
    
    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>PowerMesh Hardware Digital Twin — ESP32-S3</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root, [data-theme="light"] {{
            --bg: #f8fafc;
            --surface: #ffffff;
            --surface-sub: #f1f5f9;
            --border: #e2e8f0;
            --text: #0f172a;
            --text-dim: #64748b;
            --orange: #ea580c;
            --green: #059669;
            --amber: #d97706;
            --red: #dc2626;
        }}
        [data-theme="dark"] {{
            --bg: #0b0d11;
            --surface: #12151c;
            --surface-sub: #0d1015;
            --border: #222733;
            --text: #f1f5f9;
            --text-dim: #828e9e;
            --orange: #ff6600;
            --green: #10b981;
            --amber: #f59e0b;
            --red: #ef4444;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }}
        body {{
            background: var(--bg);
            color: var(--text);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }}
        .main-container {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 16px;
            padding: 16px;
            max-width: 1280px;
            width: 100%;
            margin: 0 auto;
            flex: 1;
            padding-bottom: max(20px, env(safe-area-inset-bottom));
        }}
        @media (min-width: 960px) {{
            .main-container {{ grid-template-columns: 1.15fr 1fr; }}
        }}
        .panel {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 16px 18px;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}
        .panel-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border);
        }}
        .panel-title {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-dim);
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .board-chassis {{
            background: var(--surface-sub);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 18px 14px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 16px;
            width: 100%;
        }}
        .silkscreen-label {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            color: #ff6600;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            font-weight: 700;
        }}
        /* Monochrome 128x64 SSD1306 OLED */
        .oled-bezel {{
            background: #050608;
            border: 2px solid #2a313d;
            border-radius: 4px;
            padding: 8px;
            box-shadow: inset 0 0 10px rgba(0,0,0,0.9);
            max-width: 100%;
            display: flex;
            justify-content: center;
        }}
        .oled-display {{
            width: 256px;
            max-width: 100%;
            height: 128px;
            background: #020304;
            border: 1px solid #11141a;
            font-family: 'Courier New', Courier, monospace;
            color: #00ff66;
            padding: 8px 10px;
            font-size: 13px;
            line-height: 1.35;
            position: relative;
            box-shadow: inset 0 0 15px rgba(0,255,102,0.06);
            image-rendering: pixelated;
        }}
        .oled-batt-bar {{
            position: absolute;
            bottom: 8px;
            left: 10px;
            width: calc(100% - 20px);
            height: 14px;
            border: 1px solid #00ff66;
            padding: 1px;
        }}
        .oled-batt-fill {{
            height: 100%;
            background: #00ff66;
            width: 100%;
            transition: width 0.3s;
        }}
        .oled-beacon-dot {{
            position: absolute;
            top: 8px;
            right: 10px;
            width: 8px;
            height: 8px;
            border-radius: 1px;
            background: #00ff66;
            display: none;
        }}
        /* Status Indicators */
        .indicators-row {{
            display: flex;
            gap: 24px;
            justify-content: center;
            width: 100%;
            flex-wrap: wrap;
        }}
        .ind-item {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 6px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            color: var(--text-dim);
            letter-spacing: 0.04em;
        }}
        .ind-lamp {{
            width: 18px;
            height: 18px;
            border-radius: 3px;
            background: #181d26;
            border: 1px solid #2a3344;
            transition: all 0.1s;
        }}
        .ind-beacon.active {{ background: #ff6600; box-shadow: 0 0 12px #ff6600; border-color: #ff8533; }}
        .ind-rf.active {{ background: #10b981; box-shadow: 0 0 12px #10b981; border-color: #34d399; }}
        .ind-usb.active {{ background: #38bdf8; box-shadow: 0 0 10px #38bdf8; border-color: #7dd3fc; }}

        /* Hardware Controls */
        .controls-grid {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 12px;
            width: 100%;
        }}
        @media(min-width: 480px) {{
            .controls-grid {{ grid-template-columns: 1fr 1fr; }}
        }}
        .ctrl-card {{
            background: var(--surface-sub);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 12px 14px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
            text-align: center;
        }}
        .ctrl-label {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            color: var(--text-dim);
            font-weight: 700;
            text-transform: uppercase;
        }}
        .tactical-btn {{
            width: 100%;
            max-width: 160px;
            min-height: 44px;
            background: #1e0b0b;
            border: 2px solid #ef4444;
            color: #ef4444;
            font-family: 'JetBrains Mono', monospace;
            font-weight: 800;
            font-size: 13px;
            border-radius: 4px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            letter-spacing: 0.05em;
            transition: all 0.1s;
        }}
        .tactical-btn:hover {{
            background: #ef4444;
            color: #fff;
        }}
        .tactical-btn:active {{
            transform: scale(0.98);
        }}
        input[type=range] {{
            width: 100%;
            height: 24px;
            accent-color: #ff6600;
        }}
        .mode-select {{
            width: 100%;
            padding: 8px;
            background: #090c10;
            color: #f1f5f9;
            border-radius: 4px;
            border: 1px solid var(--border);
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
        }}
        /* Serial Monitor Console */
        .console-wrap {{
            display: flex;
            flex-direction: column;
            height: 100%;
            background: #06080b;
            border: 1px solid var(--border);
            border-radius: 4px;
            overflow: hidden;
            min-height: 260px;
        }}
        .console-header {{
            background: #0e1117;
            padding: 8px 12px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            color: var(--text-dim);
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
        }}
        .console-log {{
            flex: 1;
            padding: 10px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            line-height: 1.5;
            color: #10b981;
            overflow-y: auto;
            max-height: 340px;
            white-space: pre-wrap;
            word-break: break-all;
        }}
        .injector-row {{
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        @media(min-width: 480px) {{
            .injector-row {{ flex-direction: row; }}
        }}
        .injector-row input, .injector-row select {{
            background: #090c10;
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 8px 10px;
            color: #fff;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
        }}
        .injector-row button {{
            background: #181d26;
            color: #ff6600;
            border: 1px solid #ff6600;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-family: 'JetBrains Mono', monospace;
            font-weight: 700;
            font-size: 11px;
        }}
        .injector-row button:hover {{
            background: #ff6600;
            color: #fff;
        }}
    </style>
</head>
<body>
    {topbar}
    <div class="main-container">
        <!-- Hardware Emulation Panel -->
        <div class="panel">
            <div class="panel-header">
                <div class="panel-title">
                    <span style="color:var(--orange)">&#9650;</span> ESP32-S3 Hardware Simulation (Node PM-01)
                </div>
                <span style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--green)">RUNNING 240MHz</span>
            </div>
            <div class="board-chassis">
                <div class="silkscreen-label">ESP32-S3 DevKitC-1 &bull; SA618F30 1W Radio &bull; SSD1306 OLED</div>
                
                <!-- Monochrome OLED Display -->
                <div class="oled-bezel">
                    <div class="oled-display" id="oled">
                        <div id="oled-l1">PowerMesh 100% 12.60V</div>
                        <div id="oled-l2">RSSI -72 dBm  P:0</div>
                        <div id="oled-l3">TX:0 RX:0 DR:0</div>
                        <div id="oled-l4">SD:OK Q:0</div>
                        <div id="oled-l5">SEQ:0  AP ready</div>
                        <div class="oled-beacon-dot" id="oled-dot"></div>
                        <div class="oled-batt-bar"><div class="oled-batt-fill" id="oled-batt"></div></div>
                    </div>
                </div>

                <!-- Status LED Indicators -->
                <div class="indicators-row">
                    <div class="ind-item">
                        <div class="ind-lamp ind-beacon" id="led-beacon"></div>
                        <span>3W BEACON (D2)</span>
                    </div>
                    <div class="ind-item">
                        <div class="ind-lamp ind-rf" id="led-rf"></div>
                        <span>433MHz TX (D6)</span>
                    </div>
                    <div class="ind-item">
                        <div class="ind-lamp ind-usb active" id="led-usb"></div>
                        <span>USB 5V (D5)</span>
                    </div>
                </div>

                <!-- Hardware Controls -->
                <div class="controls-grid">
                    <div class="ctrl-card">
                        <div class="ctrl-label">Hardware SOS Trigger (D10)</div>
                        <button class="tactical-btn" onclick="pressSOS()">[ TRIGGER SOS ]</button>
                        <small style="color:var(--text-dim);font-size:10px;font-family:'JetBrains Mono',monospace">Active-low interrupt test</small>
                    </div>
                    <div class="ctrl-card">
                        <div class="ctrl-label">Battery ADC Voltage (D1)</div>
                        <div style="width:100%;display:flex;flex-direction:column;gap:6px">
                            <input type="range" id="batt-slider" min="0" max="1" step="0.01" value="1" oninput="changeBattery(this.value)">
                            <span id="batt-text" style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--orange);">12.60V (100%)</span>
                        </div>
                    </div>
                </div>

                <!-- Operating Mode Selectors -->
                <div style="width:100%;display:grid;grid-template-columns:1fr 1fr;gap:10px">
                    <div class="ctrl-card">
                        <div class="ctrl-label">3W Rescue Beacon Mode</div>
                        <select id="beacon-select" onchange="changeBeacon(this.value)" class="mode-select">
                            <option value="1hz">1 Hz Strobe (SAR Visual)</option>
                            <option value="torch">Continuous Torch</option>
                            <option value="morse_sos">Morse SOS (... --- ...)</option>
                            <option value="off">Off</option>
                        </select>
                    </div>
                    <div class="ctrl-card">
                        <div class="ctrl-label">Radio Operation Mode</div>
                        <select id="radio-select" onchange="changeRadio(this.value)" class="mode-select">
                            <option value="dual">Dual (433MHz + WiFi)</option>
                            <option value="433mhz">433MHz Mesh Only</option>
                            <option value="wifi">Local WiFi AP Only</option>
                        </select>
                    </div>
                </div>
            </div>

            <!-- RF Inbound Injector -->
            <div class="panel-header" style="margin-top:4px">
                <div class="panel-title">
                    <span style="color:var(--orange)">&#9650;</span> SA618 Radio UART Packet Injector
                </div>
            </div>
            <div class="injector-row">
                <select id="rf-sender" style="max-width:140px">
                    <option value="PM-02">From: PM-02</option>
                    <option value="PM-03">From: PM-03</option>
                </select>
                <input type="text" id="rf-input" placeholder='{{"type":"chat","msg":"Rescue boat reaching sector 4"}}' style="flex:1">
                <button onclick="injectRF()">Inject RF Frame</button>
            </div>
        </div>

        <!-- Serial Terminal -->
        <div class="panel">
            <div class="panel-header">
                <div class="panel-title">
                    <span style="color:var(--orange)">&#9650;</span> ESP32-S3 Firmware Serial (115200 Baud)
                </div>
                <span id="conn-badge" style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--green)">CONNECTED</span>
            </div>
            <div class="console-wrap">
                <div class="console-header">
                    <span>USB CDC / UART0 TELEMETRY</span>
                    <span id="node-summary">SEQ: 0 &bull; TX: 0 &bull; RX: 0</span>
                </div>
                <div class="console-log" id="serial-log">Initializing PowerMesh firmware bridge...</div>
            </div>
            <div style="font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--text-dim);display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">
                <span>MicroSD Flash Buffer: <b>queue.log [OK]</b></span>
                <span style="color:var(--orange)">MESH FREQ: 433.92 MHz</span>
            </div>
        </div>
    </div>

    <script>
        let ws;
        let localBeaconMode = "1hz";
        let morseStep = 0;
        const morseSos = [1,0,1,0,1,0,0,1,1,0,1,1,0,1,1,0,0,1,0,1,0,1,0,0,0,0];

        // 1. Continuous client-side Search & Rescue Beacon LED blinking loop
        setInterval(() => {{
            const b = document.getElementById('led-beacon');
            const dot = document.getElementById('oled-dot');
            if(!b) return;
            if(localBeaconMode === '1hz'){{
                const act = b.classList.toggle('active');
                if(dot) dot.style.display = act ? 'block' : 'none';
            }} else if(localBeaconMode === 'torch'){{
                b.classList.add('active');
                if(dot) dot.style.display = 'block';
            }} else if(localBeaconMode === 'morse_sos'){{
                const on = morseSos[morseStep % morseSos.length] === 1;
                morseStep++;
                if(on) b.classList.add('active'); else b.classList.remove('active');
                if(dot) dot.style.display = on ? 'block' : 'none';
            }} else {{
                b.classList.remove('active');
                if(dot) dot.style.display = 'none';
            }}
        }}, 480);

        // 2. Twin WebSocket & Polling Fallback
        function connect(){{
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            try {{
                ws = new WebSocket(proto + '//' + location.host + '/twin/ws');
                ws.onopen = () => {{
                    document.getElementById('conn-badge').textContent = 'WEBSOCKET ACTIVE';
                    document.getElementById('conn-badge').style.color = 'var(--green)';
                }};
                ws.onmessage = (e) => {{
                    try {{
                        const data = JSON.parse(e.data);
                        updateUI(data);
                    }} catch(err) {{ console.error(err); }}
                }};
                ws.onclose = () => {{
                    document.getElementById('conn-badge').textContent = 'CLOUD LIVE MESH';
                    document.getElementById('conn-badge').style.color = '#0284c7';
                    setTimeout(connect, 3000);
                }};
                ws.onerror = () => {{
                    document.getElementById('conn-badge').textContent = 'CLOUD LIVE MESH';
                    document.getElementById('conn-badge').style.color = '#0284c7';
                }};
            }} catch(e) {{
                document.getElementById('conn-badge').textContent = 'CLOUD LIVE MESH';
                document.getElementById('conn-badge').style.color = '#0284c7';
            }}
        }}
        connect();

        // 3. Fallback polling for serverless / cloud environments
        async function fetchState(){{
            try {{
                const r = await fetch('/api/twin/state');
                if(r.ok){{
                    const data = await r.json();
                    updateUI(data);
                }}
            }} catch(e) {{}}
        }}
        fetchState();
        setInterval(() => {{
            if(!ws || ws.readyState !== WebSocket.OPEN){{
                fetchState();
            }}
        }}, 1400);

        function updateUI(s){{
            if(!s) return;
            document.getElementById('oled-l1').textContent = `PowerMesh ${{String(s.battPct).padStart(3,' ')}}% ${{s.battV.toFixed(2)}}V`;
            document.getElementById('oled-l2').textContent = `RSSI ${{s.rssi}} dBm  P:${{s.peers}}`;
            document.getElementById('oled-l3').textContent = `TX:${{s.tx}} RX:${{s.rx}} DR:${{s.dropped}}`;
            document.getElementById('oled-l4').textContent = `SD:${{s.sdOK ? 'OK' : 'NO'}} Q:${{s.sdBuffered}}`;
            document.getElementById('oled-l5').textContent = `SEQ:${{s.seq}}  ${{s.peers ? 'STA linked' : 'AP ready'}}`;
            document.getElementById('oled-batt').style.width = `${{s.battPct}}%`;

            if(s.beaconMode) {{
                localBeaconMode = s.beaconMode;
                const sel = document.getElementById('beacon-select');
                if(sel && sel.value !== s.beaconMode) sel.value = s.beaconMode;
            }}

            const r = document.getElementById('led-rf');
            if(r) {{
                if(s.rfLed) r.classList.add('active'); else r.classList.remove('active');
            }}

            const consoleBox = document.getElementById('serial-log');
            if(consoleBox && s.serialLogs && s.serialLogs.length){{
                consoleBox.textContent = s.serialLogs.join('\\n');
                consoleBox.scrollTop = consoleBox.scrollHeight;
            }}

            const sum = document.getElementById('node-summary');
            if(sum) sum.innerHTML = `SEQ: ${{s.seq}} &bull; TX: ${{s.tx}} &bull; RX: ${{s.rx}} &bull; PEERS: ${{s.peers}}`;
        }}

        async function sendAction(data){{
            if(ws && ws.readyState === WebSocket.OPEN){{
                ws.send(JSON.stringify(data));
            }} else {{
                try {{
                    const r = await fetch('/api/twin/action', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(data)
                    }});
                    if(r.ok){{
                        const j = await r.json();
                        if(j.state) updateUI(j.state);
                    }}
                }} catch(e) {{}}
            }}
        }}

        function pressSOS(){{
            const r = document.getElementById('led-rf');
            if(r){{
                r.classList.add('active');
                setTimeout(() => r.classList.remove('active'), 250);
            }}
            const consoleBox = document.getElementById('serial-log');
            if(consoleBox){{
                consoleBox.textContent += "\\n[" + new Date().toLocaleTimeString() + "] [BTN] Physical SOS pushbutton pressed (GPIO 10)";
                consoleBox.scrollTop = consoleBox.scrollHeight;
            }}
            sendAction({{action: 'press_sos'}});
        }}

        function changeBattery(val){{
            const v = 9.0 + parseFloat(val) * 3.6;
            const pct = Math.round(parseFloat(val) * 100);
            document.getElementById('batt-text').textContent = `${{v.toFixed(2)}}V (${{pct}}%)`;
            document.getElementById('oled-l1').textContent = `PowerMesh ${{String(pct).padStart(3,' ')}}% ${{v.toFixed(2)}}V`;
            document.getElementById('oled-batt').style.width = `${{pct}}%`;
            sendAction({{action: 'set_battery', value: parseFloat(val)}});
        }}

        function changeBeacon(val){{
            localBeaconMode = val;
            const consoleBox = document.getElementById('serial-log');
            if(consoleBox){{
                consoleBox.textContent += "\\n[" + new Date().toLocaleTimeString() + "] [BEACON] Mode changed to: " + val;
                consoleBox.scrollTop = consoleBox.scrollHeight;
            }}
            sendAction({{action: 'set_beacon', mode: val}});
        }}

        function changeRadio(val){{
            const consoleBox = document.getElementById('serial-log');
            if(consoleBox){{
                consoleBox.textContent += "\\n[" + new Date().toLocaleTimeString() + "] [RADIO] Mode switched to: " + val;
                consoleBox.scrollTop = consoleBox.scrollHeight;
            }}
            sendAction({{action: 'set_radio', mode: val}});
        }}

        function injectRF(){{
            const inp = document.getElementById('rf-input');
            const sender = document.getElementById('rf-sender').value;
            const txt = inp.value.trim();
            if(!txt) return;
            const consoleBox = document.getElementById('serial-log');
            if(consoleBox){{
                consoleBox.textContent += "\\n[" + new Date().toLocaleTimeString() + "] [RF RX] SA618 packet from " + sender + ": " + txt;
                consoleBox.scrollTop = consoleBox.scrollHeight;
            }}
            const r = document.getElementById('led-rf');
            if(r){{
                r.classList.add('active');
                setTimeout(() => r.classList.remove('active'), 250);
            }}
            inp.value = '';
            sendAction({{action: 'inject_rf', from: sender, payload: txt}});
        }}
    </script>
</body>
</html>
"""
    return HTMLResponse(html)

if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
    print("\n=======================================================")
    print("  [*] PowerMesh ESP32-S3 Disaster Mesh Hub (SIH 2026)")
    print("=======================================================")
    print(f"  > Survivor Portal:   http://127.0.0.1:{PORT}/")
    print(f"  > Incident Command:  http://127.0.0.1:{PORT}/admin")
    print(f"  > Digital Twin:      http://127.0.0.1:{PORT}/twin")
    print(f"  > Mobile Wi-Fi URL:  {MOBILE_URL}")
    print("=======================================================\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
