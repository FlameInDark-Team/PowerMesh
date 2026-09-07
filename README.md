# ⚡ PowerMesh — ESP32-S3 Off-Grid Mesh Communication Hub

> **Smart India Hackathon (SIH 2026) / FusioniX 2026 Submission**  
> **Team:** Tech Yoddhas — PowerMesh  
> **Original Wokwi Prototype:** [https://wokwi.com/projects/474510804154140673](https://wokwi.com/projects/474510804154140673)  
> **Hardware:** ESP32-S3 DevKitC-1 (Flash 16MB / PSRAM 8MB Octal) + NiceRF SA618F30-FD (1W, 433MHz)

---

## 🎯 Executive Overview — Universal Multi-Hazard Disaster & Off-Grid Mesh

**PowerMesh** is an autonomous, ruggedized off-grid communication and emergency power hub designed for **all major disaster and connectivity-loss situations**:
- 🏚️ **Earthquakes & Structural Collapses**: Penetrates concrete rubble with 433MHz Sub-GHz radio; acoustic audio siren guides search dogs and rescue crews to trapped survivors.
- 🌀 **Cyclones, Typhoons & Severe Storms**: High-durability store-and-forward radio relay above storm tree canopies when power lines and fiber links snap.
- 🌊 **Flash Floods & Tsunamis**: Instant high-ground Wi-Fi hotspot linking stranded rooftops and water rescue boats.
- ⚡ **Total Grid Blackouts & Infrastructure Sabotage**: Built-in 9000mAh 3S battery provides emergency 5V USB charging for survivor phones and runs 48+ hours autonomously.
- 🌲 **Remote Telecom Dead Zones, Landslides & Mountain SAR**: Connects isolated rural villages and rescue expeditions up to 3-4 km per hop without cellular towers or satellite subscriptions.

---

## 🚀 How to Run and Demo

### 1. Launch the System
Run either command in your terminal:
```powershell
python server.py
```
*or double-click `run.bat` or execute `.\run.ps1`.*

The server will automatically start on port `8000` and display your local Wi-Fi IP (e.g. `http://192.168.0.102:8000/`).

---

### 2. Available Interfaces

| Interface | URL | Purpose |
|---|---|---|
| **📱 Survivor Portal** | [http://localhost:8000/](http://localhost:8000/) | Zero-install emergency triage, 1-tap SOS cards, 720p photo dispatch, offline chat, and acoustic siren. |
| **🚨 Incident Command & Mesh Map** | [http://localhost:8000/admin](http://localhost:8000/admin) | Real-time triage board (Red/Yellow/Green), 3-node 433MHz topology map, victim status. |
| **🛠️ Hardware Digital Twin** | [http://localhost:8000/twin](http://localhost:8000/twin) | Live 128×64 SSD1306 OLED emulator, battery ADC pot slider, physical SOS pushbutton, 3W beacon modes, and live 115200 baud Serial output. |

---

## ⏱️ 60-Second Judge / Demonstration Script

If you are presenting this to judges or an unfamiliar user, follow this exact sequence:

1. **Step 1: Open the Survivor Portal ([http://localhost:8000/](http://localhost:8000/))**
   - Click `❓ Guide` to show the interactive 3-step disaster onboarding tutorial.
   - Tap any **1-Tap Crisis Card** (e.g. `🌊 Flood Trapped` or `🩸 Severe Injury`).
   - Notice the instantaneous broadcast and GPS coordinate attachment.
   - Click `🚨 Siren ON` to demonstrate the acoustic search-and-rescue audio beacon.

2. **Step 2: Trigger the Multi-Hazard Disaster Demo**
   - In the top bar, click **`⚡ Demo Disaster`**.
   - Notice:
     - Real-time emergency alerts injected across multiple disaster vectors:
       - **Earthquake**: 3 survivors trapped under collapsed community center basement.
       - **Cyclone & Grid Blackout**: Medical clinic running on backup battery reserve.
       - **Flash Flood**: Trapped family on terrace terrace needing clean water.
     - Live radio transmission from *NDRF All-Terrain SAR Unit #2*.
     - 720p field reconnaissance photo received and cataloged in the incident CAD.

3. **Step 3: Open the Incident Command Dashboard ([http://localhost:8000/admin](http://localhost:8000/admin))**
   - Show the **Active 433MHz Mesh Topology** diagram (*Basecamp PM-01 <-> Relay Tower PM-02 <-> Search Boat PM-03*).
   - Review the **Incident Triage Queue** sorted by medical severity.
   - Click the green button to acknowledge and mark victims as *Rescued*.

4. **Step 4: Open the Hardware Digital Twin ([http://localhost:8000/twin](http://localhost:8000/twin))**
   - Show the live **128×64 SSD1306 OLED screen** updating in real time.
   - Drag the **Battery ADC Slider** to demonstrate 3S Li-ion voltage decay (12.6V down to 9.0V).
   - Click the red physical **SOS button** (GPIO 10) to show hardware interrupt handling.
   - Change the **3W Beacon Mode** to *Morse SOS (`... --- ...`)* and show the strobe flashing.
   - Show the live **115200 baud USB CDC Serial Monitor** and virtual SD card [`queue.log`](file:///e:/Hackathon/SIH/DEMO/queue.log).

5. **Step 5: Connect a Real Mobile Phone**
   - Click **`📲 Connect Phone`** in the top bar.
   - Point your phone camera at the QR code on screen.
   - The exact mobile web portal opens on your smartphone with full touch controls!

---

## 📌 Hardware Pinout & Schematics ([diagram.json](file:///e:/Hackathon/SIH/DEMO/diagram.json))

| Component | ESP32-S3 Pin | Function |
|---|---|---|
| **SSD1306 OLED (128x64)** | SDA: `GPIO 8`, SCL: `GPIO 9` | I2C Display (0x3C) |
| **Battery ADC** | `GPIO 1` | Potentiometer input (9.0V - 12.6V 3S Li-ion) |
| **SOS Pushbutton** | `GPIO 10` | Active-low switch with pullup |
| **3W Optical Beacon** | `GPIO 2` | Optical emergency strobe |
| **USB 5V Power Active LED** | `GPIO 5` | Power bank active indicator |
| **RF TX LED** | `GPIO 6` | Blue LED flashes on radio packet transmission |
| **MicroSD Module** | CS: `15`, SCK: `14`, MOSI: `13`, MISO: `12` | SPI Flash storage for `/queue.log` |
| **SA618F30-FD RF** | TX: `GPIO 17`, RX: `GPIO 18`, AUX: `8`, M0: `6`, M1: `7` | Serial1 433MHz UART mesh radio link |
