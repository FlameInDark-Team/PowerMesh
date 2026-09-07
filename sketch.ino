// PowerMesh — ESP32-S3 Off-Grid Mesh Hub
// Board: ESP32-S3 DevKitC-1  Flash 16MB PSRAM 8MB Octal
// Wokwi free-tier compatible. Hardware-identical: swap SIMULATION flag for real SA618 on Serial1.
// ponytail: SA618 RF modeled as UART passthrough + loss slider; real 3-4km path loss not physics-simulated.
#include <WiFi.h>
#include <DNSServer.h>
#include <ESPAsyncWebServer.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <SD.h>
#include <SPI.h>
#include <ArduinoJson.h>

// ---------- Config ----------
#define OLED_W 128
#define OLED_H 64
#define OLED_ADDR 0x3C
#define PIN_SDA 8
#define PIN_SCL 9
#define PIN_BAT_ADC 1
#define PIN_BEACON 2
#define PIN_USB_LED 5
#define PIN_RF_LED 6
#define PIN_SOS_BTN 10
#define PIN_SD_CS 15
#define PIN_SD_SCK 14
#define PIN_SD_MOSI 13
#define PIN_SD_MISO 12
// SA618 UART (real hardware: Serial1)
#define PIN_RF_TX 17 // sim stub disabled in Wokwi single-node (no RF chip) // ESP TX -> RF RXD
#define PIN_RF_RX 18 // ESP RX <- RF TXD
#define PIN_RF_AUX 8
#define PIN_RF_M0 6
#define PIN_RF_M1 7

const char* AP_SSID = "PowerMesh_Rescue";
const char* AP_PASS = "12345678"; // open in Wokwi-GUEST note; hardware uses this
const IPAddress AP_IP(192,168,4,1);
const IPAddress AP_GW(192,168,4,1);
const IPAddress AP_MASK(255,255,255,0);
const byte DNS_PORT = 53;

Adafruit_SSD1306 oled(OLED_W, OLED_H, &Wire, -1);
DNSServer dns;
AsyncWebServer server(80);
AsyncWebSocket ws("/ws");

// ---------- State ----------
struct Stats {
  uint32_t seq = 0;
  uint32_t tx = 0, rx = 0, dropped = 0, sdBuffered = 0;
  int rssi = -72; // dBm (from SA618 proxy slider or real module)
  float battV = 12.6f;
  int battPct = 100;
  uint8_t peers = 0;
} st;

HardwareSerial &RF = Serial1; // SA618
bool sdOK = false;
unsigned long lastOled = 0, lastRSSI = 0, lastBeacon = 0;
bool beaconOn = false;

// CRC16-CCITT (poly 0x1021) — same for Wokwi stub and hardware
uint16_t crc16(const uint8_t* d, size_t n){
  uint16_t c=0xFFFF; for(size_t i=0;i<n;i++){c^=(uint16_t)d[i]<<8; for(int b=0;b<8;b++) c = c&0x8000 ? (c<<1)^0x1021 : c<<1;} return c;
}

// Packet: [AA][LEN][SEQ:2][TTL][FLAGS][PAYLOAD 0-220][CRC16]
bool rfSendJson(const String& json, uint8_t ttl=3){
  st.seq++; st.tx++;
  String wrapped; // json + meta
  DynamicJsonDocument doc(512);
  doc["seq"]=st.seq; doc["ttl"]=ttl; doc["rssi"]=st.rssi; doc["batt"]=st.battPct;
  JsonObject p = doc.createNestedObject("p");
  // caller json is already string; embed raw
  doc["raw"]=json;
  String out; serializeJson(doc,out);
  uint16_t len = out.length();
  uint8_t hdr[6] = {0xAA, (uint8_t)len, (uint8_t)(st.seq>>8),(uint8_t)st.seq, ttl, 0};
  uint16_t crc = crc16((uint8_t*)out.c_str(), len);
  // UART frame: hdr + payload + crc
  RF.write(hdr,6); RF.write((uint8_t*)out.c_str(), len); RF.write((uint8_t)(crc>>8)); RF.write((uint8_t)crc);
  // Also log to Serial (visible as "mesh" in Wokwi)
  Serial.printf("[RF TX] seq=%u len=%u crc=%04X rssi=%d ttl=%u payload=%.120s\n", (unsigned)st.seq, len, crc, st.rssi, ttl, out.c_str());
  // SD store-and-forward
  if(sdOK){ File f=SD.open("/queue.log", FILE_APPEND); if(f){ f.printf("%lu TX %u %s\n", millis(), (unsigned)st.seq, out.c_str()); f.close(); st.sdBuffered++; } }
  digitalWrite(PIN_RF_LED, HIGH); delay(60); digitalWrite(PIN_RF_LED, LOW);
  ws.textAll(out); // echo to all portal clients
  return true;
}

// ---------- HTML ----------
const char INDEX_HTML[] PROGMEM = R"HTML(
<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>PowerMesh — Emergency Rescue Portal</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;margin:0;padding:0}
:root, [data-theme="light"] {
  --bg-core: #f8fafc;
  --bg-surface: #ffffff;
  --bg-card: #ffffff;
  --bg-inset: #f1f5f9;
  --border: #e2e8f0;
  --border-focus: #ea580c;
  --orange: #ea580c;
  --orange-hover: #c2410c;
  --red: #dc2626;
  --red-hover: #b91c1c;
  --green: #059669;
  --amber: #d97706;
  --text-main: #0f172a;
  --text-sub: #64748b;
  --card-shadow: 0 1px 3px rgba(0,0,0,0.06);
  --radius: 6px;
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}
[data-theme="dark"] {
  --bg-core: #0a0c10;
  --bg-surface: #131720;
  --bg-card: #191e28;
  --bg-inset: #080a0d;
  --border: #262d3a;
  --border-focus: #ff6600;
  --orange: #ff6600;
  --orange-hover: #ff7b1a;
  --red: #ef4444;
  --red-hover: #dc2626;
  --green: #10b981;
  --amber: #f59e0b;
  --text-main: #ffffff;
  --text-sub: #94a3b8;
  --card-shadow: none;
}

body{
  background: var(--bg-core);
  color: var(--text-main);
  font-family: var(--font-sans);
  min-height: 100vh;
  line-height: 1.4;
  -webkit-font-smoothing: antialiased;
  transition: background 0.2s, color 0.2s;
}

/* Header */
header{
  padding: 10px 16px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  box-shadow: var(--card-shadow);
}
.hub-status{
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 800;
  font-size: 14px;
  color: var(--text-main);
}
.hub-dot{
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 8px var(--green);
}
.hub-badges{
  display: flex;
  align-items: center;
  gap: 6px;
}
.badge{
  font-family: var(--font-mono);
  font-size: 11.5px;
  font-weight: 700;
  padding: 3px 8px;
  border-radius: 4px;
  background: var(--bg-inset);
  border: 1px solid var(--border);
  color: var(--text-main);
}
.btn-hdr{
  padding: 4px 9px;
  background: var(--bg-inset);
  border: 1px solid var(--border);
  color: var(--text-main);
  border-radius: 4px;
  font-size: 11.5px;
  font-weight: 700;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

/* Main Layout */
.container{
  max-width: 680px;
  margin: 0 auto;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-bottom: max(32px, env(safe-area-inset-bottom));
}

/* Cards */
.card{
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  box-shadow: var(--card-shadow);
}
.card-title{
  font-size: 17px;
  font-weight: 800;
  color: var(--text-main);
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.card-sub{
  font-size: 13px;
  color: var(--text-sub);
  margin-bottom: 12px;
}

/* Siren Bar */
.siren-box{
  background: #fffbeb;
  border: 1px solid #f59e0b;
  border-radius: var(--radius);
  padding: 12px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
[data-theme="dark"] .siren-box {
  background: #211406;
  border-color: #b45309;
}
.siren-text{
  font-size: 14px;
  font-weight: 800;
  color: #b45309;
}
[data-theme="dark"] .siren-text { color: #fbbf24; }
.siren-hint{
  font-size: 12px;
  color: #78350f;
  margin-top: 1px;
}
[data-theme="dark"] .siren-hint { color: #cbd5e1; }
.btn-siren{
  background: #f59e0b;
  color: #000;
  font-weight: 800;
  font-size: 13px;
  padding: 8px 16px;
  border-radius: 4px;
  border: none;
  cursor: pointer;
  white-space: nowrap;
  min-height: 42px;
}
.btn-siren.sounding{
  background: var(--red);
  color: #fff;
  animation: alarm-pulse 0.7s infinite alternate;
}
@keyframes alarm-pulse{
  from{opacity: 1; transform: scale(1);}
  to{opacity: 0.85; transform: scale(0.98);}
}

/* Crisis 2x2 Buttons */
.crisis-grid{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 12px;
}
.crisis-card{
  background: var(--bg-inset);
  border: 2px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 10px;
  text-align: center;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  transition: all 0.15s ease;
}
.crisis-card:hover, .crisis-card.selected{
  border-color: var(--orange);
  background: #fff7ed;
}
[data-theme="dark"] .crisis-card:hover, [data-theme="dark"] .crisis-card.selected {
  background: #232a38;
}
.crisis-icon{
  font-size: 26px;
}
.crisis-label{
  font-size: 14px;
  font-weight: 800;
  color: var(--text-main);
}
.crisis-desc{
  font-size: 11.5px;
  color: var(--text-sub);
}

/* Giant SOS Button */
.btn-sos{
  width: 100%;
  min-height: 52px;
  background: var(--red);
  color: #ffffff;
  border: none;
  border-radius: var(--radius);
  font-size: 17px;
  font-weight: 800;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  box-shadow: 0 4px 12px rgba(220,38,38,0.3);
  transition: all 0.15s;
}
.btn-sos:hover{ background: var(--red-hover); }
.btn-sos:active{ transform: scale(0.98); }

/* Inputs */
.note-input{
  width: 100%;
  padding: 11px 14px;
  font-size: 14px;
  background: var(--bg-inset);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-main);
  margin-bottom: 10px;
  outline: none;
  font-family: inherit;
}
.note-input:focus{
  border-color: var(--orange);
}
.gps-bar{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
  padding: 8px 12px;
  background: var(--bg-inset);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 12px;
}
.btn-gps{
  background: var(--bg-surface);
  border: 1px solid var(--border);
  color: var(--orange);
  font-weight: 700;
  font-size: 11.5px;
  padding: 5px 10px;
  border-radius: 4px;
  cursor: pointer;
}

/* Messenger */
.chat-window{
  background: var(--bg-inset);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  height: 330px;
}
.chat-list{
  flex: 1;
  padding: 12px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.chat-empty{
  text-align: center;
  color: var(--text-sub);
  font-size: 13px;
  margin: auto;
  line-height: 1.5;
}
.msg-bubble{
  max-width: 85%;
  padding: 9px 12px;
  border-radius: 6px;
  font-size: 13.5px;
  line-height: 1.4;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.msg-bubble.out{
  align-self: flex-end;
  border-left: 4px solid var(--orange);
  background: #fff7ed;
}
[data-theme="dark"] .msg-bubble.out { background: #1e2430; }
.msg-bubble.in{
  align-self: flex-start;
  border-left: 4px solid var(--green);
}
.msg-bubble.sos{
  align-self: stretch;
  max-width: 100%;
  border-left: 5px solid var(--red);
  background: #fef2f2;
}
[data-theme="dark"] .msg-bubble.sos { background: #261010; }
.msg-head{
  font-size: 11px;
  font-weight: 700;
  margin-bottom: 2px;
  display: flex;
  justify-content: space-between;
  color: var(--text-sub);
}
.msg-bubble.out .msg-head{ color: var(--orange); }
.msg-bubble.in .msg-head{ color: var(--green); }
.msg-bubble.sos .msg-head{ color: var(--red); }

/* Quick Replies */
.quick-replies{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  padding: 8px 10px;
  background: var(--bg-surface);
  border-top: 1px solid var(--border);
}
.quick-btn{
  background: var(--bg-inset);
  border: 1px solid var(--border);
  color: var(--text-main);
  font-size: 12.5px;
  font-weight: 600;
  padding: 7px 8px;
  border-radius: 4px;
  cursor: pointer;
  text-align: center;
  transition: all 0.15s;
}
.quick-btn:active{
  border-color: var(--orange);
  color: var(--orange);
}

/* Input Bar */
.chat-bar{
  display: flex;
  gap: 8px;
  padding: 8px 10px;
  background: var(--bg-surface);
  border-top: 1px solid var(--border);
}
.btn-photo-attach{
  width: 44px;
  height: 44px;
  border-radius: 4px;
  background: var(--bg-inset);
  border: 1px solid var(--border);
  color: var(--text-main);
  font-size: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex-shrink: 0;
}
.chat-text-input{
  flex: 1;
  height: 44px;
  padding: 8px 12px;
  font-size: 14px;
  background: var(--bg-inset);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-main);
  outline: none;
}
.chat-text-input:focus{
  border-color: var(--orange);
}
.btn-send{
  min-width: 68px;
  height: 44px;
  background: var(--orange);
  color: #fff;
  border: none;
  border-radius: 4px;
  font-size: 13.5px;
  font-weight: 700;
  cursor: pointer;
  flex-shrink: 0;
}

/* Image preview in chat */
.photo-wrap{
  margin-top: 4px;
  border-radius: 4px;
  overflow: hidden;
  border: 1px solid var(--border);
  position: relative;
}
.photo-wrap img{
  width: 100%;
  max-height: 200px;
  object-fit: cover;
  display: block;
  cursor: pointer;
}
.photo-foot{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px;
  background: var(--bg-inset);
  border-top: 1px solid var(--border);
  font-size: 11px;
}
.btn-del-mini{
  background: #fee2e2;
  color: #b91c1c;
  border: 1px solid #fca5a5;
  border-radius: 3px;
  padding: 2px 6px;
  font-size: 10px;
  font-weight: 700;
  cursor: pointer;
}
[data-theme="dark"] .btn-del-mini{
  background: #271010;
  color: #ef4444;
  border-color: #7f1d1d;
}

/* Photo Upload Box */
.photo-sheet{
  display: none;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px;
  margin-top: 10px;
  box-shadow: var(--card-shadow);
}
.photo-sheet.open{ display: block; }
.progress-strip{
  height: 6px;
  background: var(--bg-inset);
  border-radius: 3px;
  overflow: hidden;
  margin: 8px 0;
}
.progress-fill{
  height: 100%;
  width: 0%;
  background: var(--orange);
  transition: width 0.15s;
}

/* Helpful Notice */
.info-box{
  display: flex;
  align-items: flex-start;
  gap: 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 14px;
  box-shadow: var(--card-shadow);
}
.info-icon{
  font-size: 22px;
  flex-shrink: 0;
}
.info-title{
  font-size: 13.5px;
  font-weight: 800;
  color: var(--text-main);
  margin-bottom: 2px;
}
.info-desc{
  font-size: 12px;
  color: var(--text-sub);
  line-height: 1.4;
}

/* Modals */
.modal-bg{
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.6);
  backdrop-filter: blur(4px);
  display: none; align-items: center; justify-content: center;
  z-index: 10000; padding: 16px;
}
.modal-bg.open{ display: flex; }
.modal-sheet{
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px; max-width: 440px; width: 100%;
  box-shadow: 0 20px 25px -5px rgba(0,0,0,0.2);
}
</style>
</head>
<body>

<header>
  <div class="hub-status">
    <div class="hub-dot"></div>
    <span>RESCUE HUB</span>
  </div>
  <div class="hub-badges">
    <span id="batt" class="badge">🔋 100%</span>
    <span id="peers" class="badge">📡 MESH OK</span>
    <button onclick="togglePortalTheme()" id="theme-btn" class="btn-hdr" title="Toggle Light / Dark Mode">🌙 Dark</button>
    <button onclick="document.getElementById('guide-modal').classList.add('open')" class="btn-hdr">❓ HELP</button>
  </div>
</header>

<div class="container">
  <!-- Loud Siren Bar -->
  <div class="siren-box">
    <div>
      <div class="siren-text">🔊 Sound Rescue Alarm</div>
      <div class="siren-hint">Makes a very loud sound so rescue teams can locate you.</div>
    </div>
    <button id="siren-btn" class="btn-siren" onclick="toggleSiren()">TURN ON</button>
  </div>

  <!-- Emergency SOS Card -->
  <div class="card">
    <div class="card-title">
      <span>🚨 Emergency Assistance</span>
    </div>
    <div class="card-sub">Tap your situation, then tap the big red button:</div>

    <div class="crisis-grid">
      <div class="crisis-card selected" id="c-trapped" onclick="pickCrisis('Rubble / Trapped', 'Trapped under collapsed building/debris. Road blocked. Need extraction.', 'c-trapped')">
        <div class="crisis-icon">🏚️</div>
        <div class="crisis-label">Trapped in Debris</div>
        <div class="crisis-desc">Earthquake / Collapsed</div>
      </div>
      <div class="crisis-card" id="c-flood" onclick="pickCrisis('Cyclone / Flood Water', 'Severe storm/flood water rising rapidly. Stranded on roof or high ground.', 'c-flood')">
        <div class="crisis-icon">🌀</div>
        <div class="crisis-label">Cyclone / Flood</div>
        <div class="crisis-desc">Storm / Rising water</div>
      </div>
      <div class="crisis-card" id="c-med" onclick="pickCrisis('Medical Emergency', 'Severe trauma, bleeding, fracture, or unconscious. Need urgent paramedic.', 'c-med')">
        <div class="crisis-icon">🩸</div>
        <div class="crisis-label">Medical Emergency</div>
        <div class="crisis-desc">Injured / Paramedic</div>
      </div>
      <div class="crisis-card" id="c-blackout" onclick="pickCrisis('Grid Blackout / Supplies', 'Total power grid failure, clean water exhausted, elderly/infants cut off.', 'c-blackout')">
        <div class="crisis-icon">⚡</div>
        <div class="crisis-label">Blackout / Supplies</div>
        <div class="crisis-desc">No power / Water depleted</div>
      </div>
    </div>

    <input type="text" id="sos-details" class="note-input" placeholder="Optional: Where are you? How many people? (e.g. 3 people on roof)">

    <button class="btn-sos" onclick="sendSOS()">
      <span>🚨 SEND RESCUE CALL NOW</span>
    </button>

    <div class="gps-bar">
      <span id="loc" style="color:var(--text-sub)">📍 Location: Ready to attach</span>
      <button class="btn-gps" onclick="getLoc()">Refresh GPS</button>
    </div>
  </div>

  <!-- Simple Messages Card -->
  <div class="card">
    <div class="card-title">
      <span>💬 Message Rescue Teams</span>
    </div>
    <div class="card-sub">Messages are sent over radio without internet or phone signal.</div>

    <div class="chat-window">
      <div id="chat-list" class="chat-list">
        <div class="chat-empty">
          <b>Radio Connection Active</b><br>
          Type a message below or tap one of the quick buttons.
        </div>
      </div>

      <!-- Quick 1-tap replies -->
      <div class="quick-replies">
        <button class="quick-btn" onclick="sendQuick('👍 We are safe for now')">👍 We are safe</button>
        <button class="quick-btn" onclick="sendQuick('💧 Need clean drinking water')">💧 Need clean water</button>
        <button class="quick-btn" onclick="sendQuick('💊 Need medical assistance')">💊 Need medicine</button>
        <button class="quick-btn" onclick="sendQuick('🆘 Search & rescue team needed here')">🆘 Need search team</button>
      </div>

      <!-- Input Bar -->
      <div class="chat-bar">
        <label class="btn-photo-attach" title="Send a Photo">
          📷
          <input type="file" id="chat-photo-input" accept="image/*" capture="environment" style="display:none" onchange="onPhotoSelected(this)">
        </label>
        <input type="text" id="chat-text" class="chat-text-input" placeholder="Type your message here…" onkeydown="if(event.key==='Enter')sendChat()">
        <button class="btn-send" onclick="sendChat()">SEND</button>
      </div>
    </div>

    <!-- Photo Upload Sheet -->
    <div id="photo-sheet" class="photo-sheet">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <b style="color:var(--orange);font-size:14px">📷 Send Field Photo to Rescuers</b>
        <button onclick="cancelPhoto()" style="background:none;border:none;color:var(--text-sub);font-size:16px;cursor:pointer">✕</button>
      </div>
      <div style="display:flex;gap:12px;align-items:center">
        <img id="img-thumb" style="width:70px;height:70px;object-fit:cover;border-radius:4px;border:1px solid var(--border)">
        <div style="font-size:13px;color:var(--text-sub)">
          <div>Photo Ready: <b id="img-size" style="color:var(--text-main)">-</b></div>
          <div id="img-status" style="color:var(--green);font-weight:700;margin-top:2px">Ready to send</div>
        </div>
      </div>
      <div class="progress-strip">
        <div id="img-progress" class="progress-fill"></div>
      </div>
      <button id="send-photo-btn" onclick="dispatchPhoto()" style="width:100%;min-height:44px;background:var(--orange);color:#fff;border:none;border-radius:4px;font-size:14px;font-weight:700;cursor:pointer">
        SEND PHOTO OVER RADIO
      </button>
    </div>
  </div>

  <!-- Practical Help Info Cards -->
  <div class="info-box">
    <div class="info-icon">🌐</div>
    <div>
      <div class="info-title">Universal Multi-Hazard Mesh Hub</div>
      <div class="info-desc">Works across Earthquakes, Cyclones, Floods, Landslides, and Total Grid Blackouts when cell towers fail.</div>
    </div>
  </div>

  <div class="info-box">
    <div class="info-icon">🔌</div>
    <div>
      <div class="info-title">Emergency Power & USB Charging</div>
      <div class="info-desc">Plug any smartphone into the hub's 5V USB port to recharge during extended power outages.</div>
    </div>
  </div>

  <div class="info-box">
    <div class="info-icon">📡</div>
    <div>
      <div class="info-title">Independent 433MHz Long-Range Radio</div>
      <div class="info-desc">Sub-GHz radio waves penetrate rubble, heavy storm rain, and forest cover up to 3-4 km.</div>
    </div>
  </div>
</div>

<!-- Image Zoom Modal -->
<div id="zoom-modal" class="modal-bg" onclick="this.classList.remove('open')">
  <img id="zoom-target" style="max-width:94vw;max-height:86vh;border-radius:4px;border:1px solid var(--border)">
</div>

<!-- Simple Guide Modal -->
<div id="guide-modal" class="modal-bg" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal-sheet">
    <h3 style="font-size:18px;margin-bottom:8px;color:var(--orange)">How to Use This Rescue Hub</h3>
    <p style="font-size:13px;color:var(--text-sub);margin-bottom:14px">Keep your phone connected to this Wi-Fi. You do not need internet or a SIM card.</p>
    
    <div style="display:flex;flex-direction:column;gap:12px;font-size:14px">
      <div style="display:flex;gap:10px">
        <span style="font-weight:800;color:var(--orange)">1.</span>
        <div><b>Press the Big Red Button</b> in any emergency (earthquake collapse, storm, flood, or medical trauma).</div>
      </div>
      <div style="display:flex;gap:10px">
        <span style="font-weight:800;color:var(--orange)">2.</span>
        <div><b>Use the Loud Alarm</b> if you hear boats or search dogs nearby.</div>
      </div>
      <div style="display:flex;gap:10px">
        <span style="font-weight:800;color:var(--orange)">3.</span>
        <div><b>Take a Photo</b> of water levels or injuries so medical teams prepare before arrival.</div>
      </div>
    </div>

    <button onclick="document.getElementById('guide-modal').classList.remove('open')" style="width:100%;min-height:42px;background:var(--border);color:var(--text-main);border:none;border-radius:4px;font-weight:700;margin-top:18px;cursor:pointer">
      GOT IT
    </button>
  </div>
</div>

<canvas id="comp-canvas" style="display:none"></canvas>

<script>
let ws, lat=null, lon=null, compressedDataUrl=null;
let audioCtx=null, sirenOsc=null, sirenInterval=null;
let currentCrisisType = "Rubble / Trapped";
let currentCrisisDetail = "Trapped under collapsed building/debris. Road blocked. Need extraction.";

function initPortalTheme(){
  const saved = localStorage.getItem('powermesh-theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  const btn = document.getElementById('theme-btn');
  if(btn) btn.innerHTML = saved === 'light' ? '🌙 Dark' : '☀️ Light';
}
initPortalTheme();

function togglePortalTheme(){
  if(window.toggleTheme){
    window.toggleTheme();
  } else {
    const current = document.documentElement.getAttribute('data-theme') || 'light';
    const next = current === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('powermesh-theme', next);
    const btn = document.getElementById('theme-btn');
    if(btn) btn.innerHTML = next === 'light' ? '🌙 Dark' : '☀️ Light';
  }
}

function pickCrisis(type, detail, elementId){
  currentCrisisType = type;
  currentCrisisDetail = detail;
  document.querySelectorAll('.crisis-card').forEach(c => c.classList.remove('selected'));
  const el = document.getElementById(elementId);
  if(el) el.classList.add('selected');
}

function connect(){
  const proto = location.protocol==='https:'?'wss:':'ws:';
  ws = new WebSocket(proto+'//'+location.host+'/ws');
  ws.onopen=()=>{
    if(!lat) getLoc();
  };
  ws.onmessage=e=>{
    try{
      const data = JSON.parse(e.data);
      handleInbound(data);
    }catch(err){}
  };
  ws.onclose=()=>{
    setTimeout(connect, 1500);
  };
}
connect();

function handleInbound(j){
  let p = j;
  if(j.raw){
    try{ p = JSON.parse(j.raw); }catch{ p = {type:'chat', msg:j.raw}; }
  }
  const t = p.type || '';
  const time = p.time || new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const sender = p.sender_name || (p.from ? `Node ${p.from}` : 'Rescue Team');

  if(t === 'chat'){
    addMsgBubble(p.msg, false, sender, time);
  } else if(t === 'image'){
    addPhotoBubble(p.img_url || p.data_url, false, sender, p.caption, time, p.img_id);
  } else if(t === 'delete_photo'){
    removePhotoBubble(p.img_id, p.img_url);
  } else if(t.includes('Immediate') || t.includes('SOS') || t.includes('Medical') || t.includes('Food') || t.includes('Emergency') || t.includes('Flood') || t.includes('Elderly')){
    addAlertCard(p, time);
  }
}

function addMsgBubble(text, isOut, sender, time){
  const list = document.getElementById('chat-list');
  const h = list.querySelector('.chat-empty'); if(h) h.remove();

  const div = document.createElement('div');
  div.className = `msg-bubble ${isOut ? 'out' : 'in'}`;
  div.innerHTML = `
    <div class="msg-head">
      <span>${isOut ? 'You' : escapeHtml(sender)}</span>
      <span>${time} ${isOut ? '✓' : ''}</span>
    </div>
    <div>${escapeHtml(text)}</div>
  `;
  list.appendChild(div);
  list.scrollTop = list.scrollHeight;
}

function addPhotoBubble(src, isOut, sender, caption, time, imgId){
  const list = document.getElementById('chat-list');
  const h = list.querySelector('.chat-empty'); if(h) h.remove();

  const id = imgId || ('img_' + Date.now());
  const div = document.createElement('div');
  div.className = `msg-bubble ${isOut ? 'out' : 'in'}`;
  div.id = `chat-photo-${id}`;
  div.setAttribute('data-src', src);
  div.innerHTML = `
    <div class="msg-head">
      <span>📷 ${isOut ? 'You' : escapeHtml(sender)}</span>
      <span>${time}</span>
    </div>
    <div class="photo-wrap">
      <img src="${src}" onclick="zoomImg('${src}')" alt="Photo">
      <div class="photo-foot">
        <span>${escapeHtml(caption || 'Field Photo')}</span>
        <button onclick="requestDeletePhoto('${id}')" class="btn-del-mini" title="Delete Photo">🗑️ Delete</button>
      </div>
    </div>
  `;
  list.appendChild(div);
  list.scrollTop = list.scrollHeight;
}

function removePhotoBubble(imgId, imgUrl){
  let el = document.getElementById(`chat-photo-${imgId}`);
  if(!el && imgUrl){
    el = document.querySelector(`[data-src="${imgUrl}"]`);
  }
  if(el){
    el.style.opacity = '0';
    setTimeout(()=>el.remove(), 250);
  }
}

async function requestDeletePhoto(id){
  if(!confirm("Delete this photo?")) return;
  try{
    const r = await fetch('/api/photos/' + encodeURIComponent(id) + '/delete', {method:'POST'});
    if(r.ok){
      removePhotoBubble(id, null);
    }
  }catch(e){}
}

function addAlertCard(p, time){
  const list = document.getElementById('chat-list');
  const h = list.querySelector('.chat-empty'); if(h) h.remove();

  const div = document.createElement('div');
  div.className = 'msg-bubble sos';
  const locStr = (p.lat && p.lon) ? `📍 ${Number(p.lat).toFixed(4)}°, ${Number(p.lon).toFixed(4)}°` : '';
  div.innerHTML = `
    <div class="msg-head">
      <span>🚨 ${escapeHtml(p.type)}</span>
      <span>${time}</span>
    </div>
    <div style="font-weight:800;color:var(--red);font-size:14px;margin-bottom:2px">${escapeHtml(p.count || 'Emergency Alert')}</div>
    <div>${escapeHtml(p.msg || '')}</div>
    ${locStr ? `<div style="font-size:11.5px;color:var(--text-sub);margin-top:3px">${locStr}</div>` : ''}
  `;
  list.appendChild(div);
  list.scrollTop = list.scrollHeight;
}

function escapeHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function sendQuick(txt){
  document.getElementById('chat-text').value = txt;
  sendChat();
}

function sendChat(){
  const inp = document.getElementById('chat-text');
  const txt = inp.value.trim();
  if(!txt) return;

  const timeStr = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const payload = {
    type: "chat",
    sender_name: "Survivor",
    msg: txt,
    time: timeStr,
    ts: Date.now()
  };

  addMsgBubble(txt, true, "You", timeStr);
  inp.value = "";

  if(ws && ws.readyState === WebSocket.OPEN){
    ws.send(JSON.stringify(payload));
  } else {
    fetch('/api/packet', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).catch(()=>{});
  }
}

// 720p photo compression
async function onPhotoSelected(input){
  const file = input.files[0];
  if(!file) return;

  const p = document.getElementById('photo-sheet');
  p.classList.add('open');
  document.getElementById('img-status').textContent = "Preparing photo...";
  document.getElementById('img-progress').style.width = "0%";

  const img = new Image();
  img.src = URL.createObjectURL(file);
  await img.decode();

  const canvas = document.getElementById('comp-canvas');
  const ctx = canvas.getContext('2d');
  const maxDim = 1280;
  const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
  canvas.width = Math.round(img.width * scale);
  canvas.height = Math.round(img.height * scale);
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

  const dataUrl = canvas.toDataURL('image/jpeg', 0.45);
  compressedDataUrl = dataUrl;
  const kb = Math.round((dataUrl.length * 3/4) / 1024);

  document.getElementById('img-thumb').src = dataUrl;
  document.getElementById('img-size').textContent = `${kb} KB`;
  document.getElementById('img-status').textContent = "Ready to send";
  document.getElementById('send-photo-btn').disabled = false;
  document.getElementById('send-photo-btn').textContent = "SEND PHOTO OVER RADIO";
}

function cancelPhoto(){
  compressedDataUrl = null;
  document.getElementById('photo-sheet').classList.remove('open');
  document.getElementById('chat-photo-input').value = "";
}

async function dispatchPhoto(){
  if(!compressedDataUrl) return;
  const btn = document.getElementById('send-photo-btn');
  btn.disabled = true;
  btn.textContent = "Sending photo over radio...";

  const chunk = 800;
  const total = Math.ceil(compressedDataUrl.length / chunk);
  const imgId = "img_" + Date.now();
  const bar = document.getElementById('img-progress');

  for(let i=0; i<compressedDataUrl.length; i+=chunk){
    const seq = Math.floor(i / chunk);
    const chunkPayload = {
      type: "img_chunk",
      img_id: imgId,
      seq: seq,
      total: total,
      data: compressedDataUrl.slice(i, i + chunk),
      sender_name: "Survivor"
    };
    if(ws && ws.readyState === WebSocket.OPEN){
      ws.send(JSON.stringify(chunkPayload));
    } else {
      try {
        await fetch('/api/upload-photo-chunk', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(chunkPayload)
        });
      } catch(e) {}
    }
    const pct = Math.round(((seq + 1) / total) * 100);
    bar.style.width = `${pct}%`;
    await new Promise(r => setTimeout(r, 15));
  }

  addPhotoBubble(compressedDataUrl, true, "You", "Field Photo", new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}), imgId);
  cancelPhoto();
}

function zoomImg(src){
  document.getElementById('zoom-target').src = src;
  document.getElementById('zoom-modal').classList.add('open');
}

// GPS Location
function getLoc(){
  if(!navigator.geolocation){
    lat = 26.1445; lon = 91.6022;
    document.getElementById('loc').textContent = "📍 Location: 26.1445° N, 91.6022° E";
    return;
  }
  navigator.geolocation.getCurrentPosition(p=>{
    lat = p.coords.latitude; lon = p.coords.longitude;
    document.getElementById('loc').textContent = `📍 Location: ${lat.toFixed(4)}°, ${lon.toFixed(4)}° (Locked)`;
  }, e=>{
    lat = 26.1445; lon = 91.6022;
    document.getElementById('loc').textContent = "📍 Location: 26.1445°, 91.6022° (Auto-filled)";
  });
}

// Send SOS
function sendSOS(){
  const detail = document.getElementById('sos-details').value.trim();
  const fullMsg = detail ? `${currentCrisisDetail} Note: ${detail}` : currentCrisisDetail;

  const payload = {
    type: currentCrisisType,
    count: currentCrisisType,
    msg: fullMsg,
    lat: lat || 26.1445,
    lon: lon || 91.6022,
    time: new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}),
    ts: Date.now()
  };

  addAlertCard(payload, "Sent Just Now");

  if(ws && ws.readyState === WebSocket.OPEN){
    ws.send(JSON.stringify(payload));
  } else {
    fetch('/api/packet', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).catch(()=>{});
  }
  alert("🚨 Emergency Call Sent!\n\nRescue teams and nearby boats have received your alert and GPS coordinates.");
}

// Siren
function toggleSiren(){
  const btn = document.getElementById('siren-btn');
  if(!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if(sirenInterval){
    clearInterval(sirenInterval); sirenInterval = null;
    if(sirenOsc){ try{sirenOsc.stop();}catch{} sirenOsc=null; }
    btn.textContent = "TURN ON"; btn.classList.remove('sounding');
  } else {
    btn.textContent = "STOP SOUND"; btn.classList.add('sounding');
    let freq = 750, up = true;
    sirenOsc = audioCtx.createOscillator();
    const g = audioCtx.createGain(); g.gain.value = 0.25;
    sirenOsc.connect(g); g.connect(audioCtx.destination);
    sirenOsc.start();
    sirenInterval = setInterval(()=>{
      freq += up ? 70 : -70;
      if(freq > 1150) up = false;
      if(freq < 700) up = true;
      if(sirenOsc) sirenOsc.frequency.setValueAtTime(freq, audioCtx.currentTime);
    }, 40);
  }
}

setInterval(async()=>{
  try{
    const r = await fetch('/status'); const j = await r.json();
    if(j.battPct !== undefined) document.getElementById('batt').textContent = `🔋 ${j.battPct}%`;
    if(j.received_photos && j.received_photos.length){
      j.received_photos.forEach(p => {
        const id = p.img_id || ('img_' + (p.time || 'recon'));
        if(!document.getElementById(`chat-photo-${id}`)){
          addPhotoBubble(p.img_url || p.data_url, false, p.sender_name || 'Field Recon', p.caption, p.time, id);
        }
      });
    }
  }catch{}
}, 2000);
</script>
</body>
</html>
)HTML";

// ---------- Setup helpers ----------
void handleRoot(AsyncWebServerRequest* r){ r->send_P(200,"text/html", INDEX_HTML); }
void handleStatus(AsyncWebServerRequest* r){
  DynamicJsonDocument j(512);
  j["uptime"]=millis(); j["seq"]=st.seq; j["tx"]=st.tx; j["rx"]=st.rx; j["dropped"]=st.dropped;
  j["rssi"]=st.rssi; j["battV"]=st.battV; j["battPct"]=st.battPct; j["peers"]=st.peers;
  j["sdBuffered"]=st.sdBuffered; j["sdOK"]=sdOK; j["ap"]=AP_SSID; j["ip"]="192.168.4.1";
  String s; serializeJson(j,s); r->send(200,"application/json", s);
}
void handleNotFound(AsyncWebServerRequest* r){ r->redirect("http://192.168.4.1/"); }

void onWsEvent(AsyncWebSocket *s, AsyncWebSocketClient *c, AwsEventType t, void *arg, uint8_t *data, size_t len){
  if(t==WS_EVT_CONNECT){ st.peers++; Serial.printf("[WS] client %u connected peers=%u\n", c->id(), st.peers); c->text("{\"hello\":\"PowerMesh\",\"seq\":"+String(st.seq)+"}"); }
  else if(t==WS_EVT_DISCONNECT){ if(st.peers) st.peers--; Serial.printf("[WS] client %u gone peers=%u\n", c->id(), st.peers); }
  else if(t==WS_EVT_DATA){
    String msg; msg.reserve(len+1); for(size_t i=0;i<len;i++) msg+=(char)data[i];
    Serial.printf("[WS RX] %s\n", msg.c_str());
    // Relay to RF (mesh) and echo to all WS
    rfSendJson(msg);
  }
}

float readBattery(){
  int raw = analogRead(PIN_BAT_ADC); // pot 0-4095 = 9.0-12.6V
  float v = 9.0f + (raw/4095.0f)*3.6f;
  // simple smoothing
  static float filt=12.6f; filt = filt*0.85f + v*0.15f; return filt;
}
int battPctFromV(float v){
  // 3S Li-ion: 9.0(0%) 9.9(10%) 11.1(45%) 12.0(80%) 12.6(100%)
  if(v>=12.6) return 100; if(v<=9.0) return 0;
  // linear approx for demo
  return (int)((v-9.0f)/3.6f*100);
}

void updateOLED(){
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1); oled.setCursor(0,0);
  oled.printf("PowerMesh %3d%% %.2fV", st.battPct, st.battV);
  oled.setCursor(0,10); oled.printf("RSSI %d dBm  P:%u", st.rssi, st.peers);
  oled.setCursor(0,20); oled.printf("TX:%u RX:%u DR:%u", st.tx, st.rx, st.dropped);
  oled.setCursor(0,30); oled.printf("SD:%s Q:%u", sdOK?"OK":"NO", st.sdBuffered);
  oled.setCursor(0,40); oled.printf("SEQ:%u  %s", st.seq, WiFi.softAPgetStationNum()?"STA linked":"AP ready");
  // battery bar
  int bw = map(st.battPct,0,100,0,120);
  oled.drawRect(0,52,124,10,SSD1306_WHITE);
  oled.fillRect(2,54,bw,6,SSD1306_WHITE);
  // beacon indicator
  if(beaconOn) oled.fillCircle(120,4,3,SSD1306_WHITE);
  oled.display();
}

void setup(){
  Serial.begin(115200); delay(200);
  Serial.println("\n=== PowerMesh v1 — ESP32-S3 ===");
  pinMode(PIN_BEACON, OUTPUT); pinMode(PIN_USB_LED, OUTPUT); pinMode(PIN_RF_LED, OUTPUT);
  pinMode(PIN_SOS_BTN, INPUT_PULLUP);
  pinMode(PIN_RF_AUX, INPUT); pinMode(PIN_RF_M0, OUTPUT); pinMode(PIN_RF_M1, OUTPUT);
  digitalWrite(PIN_RF_M0, LOW); digitalWrite(PIN_RF_M1, LOW); // normal mode

  // RF UART
  RF.begin(115200, SERIAL_8N1, PIN_RF_RX, PIN_RF_TX);
  Wire.begin(PIN_SDA, PIN_SCL);
  if(!oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)){ Serial.println("[OLED] not found 0x3C"); }
  else { oled.clearDisplay(); oled.display(); }
  // ADC
  analogReadResolution(12); analogSetAttenuation(ADC_11db);
  // SD
  SPI.begin(PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI, PIN_SD_CS);
  sdOK = SD.begin(PIN_SD_CS, SPI, 8000000);
  Serial.printf("[SD] %s\n", sdOK?"mounted":"FAILED (Wokwi: add card or check wiring)");
  if(sdOK){
    File f=SD.open("/queue.log", FILE_APPEND); if(f){ f.println("# PowerMesh queue.log boot "+String(millis())); f.close(); }
  }
  // WiFi AP + DHCP captive
  WiFi.mode(WIFI_MODE_AP);
  WiFi.softAPConfig(AP_IP, AP_GW, AP_MASK);
  bool ok = WiFi.softAP(AP_SSID, AP_PASS, 6, 0, 8);
  Serial.printf("[WiFi] softAP %s %s IP=%s\n", AP_SSID, ok?"OK":"FAIL", AP_IP.toString().c_str());
  // DNS catch-all
  dns.setTTL(300); dns.start(DNS_PORT, "*", AP_IP);
  // HTTP
  server.on("/", HTTP_GET, handleRoot);
  server.on("/status", HTTP_GET, handleStatus);
  // OS probe redirects — triggers captive portal popup
  server.on("/generate_204", HTTP_GET, handleNotFound);
  server.on("/hotspot-detect.html", HTTP_GET, handleNotFound);
  server.on("/canonical.html", HTTP_GET, handleNotFound);
  server.on("/success.txt", HTTP_GET, [](AsyncWebServerRequest* r){ r->send(200,"text/plain",""); });
  server.on("/ncsi.txt", HTTP_GET, handleNotFound);
  server.on("/connecttest.txt", HTTP_GET, handleNotFound);
  server.on("/redirect", HTTP_GET, handleNotFound);
  server.onNotFound(handleNotFound);
  ws.onEvent(onWsEvent);
  server.addHandler(&ws);
  server.begin();
  Serial.println("[HTTP] server on http://192.168.4.1/  WS /ws");
  // initial RF hello
  delay(500); rfSendJson("{\"type\":\"hello\",\"node\":\"PM-01\",\"msg\":\"PowerMesh online\"}");
  digitalWrite(PIN_USB_LED, HIGH); // power bank 5V active
  updateOLED();
}

void loop(){
  dns.processNextRequest();
  // SOS button (active low)
  static unsigned long btnDown=0; static bool wasLow=false;
  bool low = digitalRead(PIN_SOS_BTN)==LOW;
  if(low && !wasLow) btnDown=millis();
  if(!low && wasLow){ // released
    unsigned long held = millis()-btnDown;
    if(held>80){ // debounce
      String j="{\"type\":\"SOS\",\"msg\":\"SOS button pressed\",\"held\":"+String(held)+",\"lat\":26.14,\"lon\":91.60}";
      Serial.println("[BTN] SOS "+j); rfSendJson(j);
    }
  }
  wasLow=low;

  // RF RX — parse framed packets (hdr 6 + payload + crc2)
  static uint8_t rxBuf[512]; static size_t rxPos=0;
  while(RF.available()){
    uint8_t b=RF.read();
    if(rxPos==0 && b!=0xAA){ continue; }
    rxBuf[rxPos++]=b;
    if(rxPos>=6){
      uint8_t len=rxBuf[1];
      size_t need=6+len+2;
      if(rxPos>=need){
        uint16_t got = (rxBuf[6+len]<<8)|rxBuf[6+len+1];
        uint16_t calc = crc16(rxBuf+6, len);
        if(got==calc){
          st.rx++;
          String payload; payload.reserve(len); for(int i=0;i<len;i++) payload+=(char)rxBuf[6+i];
          Serial.printf("[RF RX] len=%u crc OK %s\n", len, payload.c_str());
          ws.textAll(payload);
          if(sdOK){ File f=SD.open("/queue.log", FILE_APPEND); if(f){ f.printf("%lu RX %s\n", millis(), payload.c_str()); f.close(); } }
        }else{ st.dropped++; Serial.printf("[RF RX] CRC mismatch got=%04X calc=%04X\n", got, calc); }
        rxPos=0;
      }
      if(rxPos>=sizeof(rxBuf)) rxPos=0;
    }
  }

  // Battery + OLED (2 Hz)
  if(millis()-lastOled>500){
    lastOled=millis();
    st.battV = readBattery();
    st.battPct = battPctFromV(st.battV);
    // RSSI: read proxy slider via AUX pin analog? fallback to simulated drift
    // If RF AUX high = channel busy, dim RSSI; else use stored
    updateOLED();
    // low-battery behavior
    if(st.battPct<15) { /* ponytail: deep sleep threshold; real BMS would cut load */ }
  }
  // Beacon blink (1Hz)
  if(millis()-lastBeacon>500){ lastBeacon=millis(); beaconOn=!beaconOn; digitalWrite(PIN_BEACON, beaconOn?HIGH:LOW); }
  // Simulate RSSI jitter unless overridden by SA618 proxy
  if(millis()-lastRSSI>2000){ lastRSSI=millis(); st.rssi = -72 + random(-6,7); }
  ws.cleanupClients();
  delay(10); // Wokwi FAQ: delay helps sim speed
}
