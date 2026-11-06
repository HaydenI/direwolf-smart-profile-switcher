# Direwolf APRS Smart Profile Switcher (Raspberry Pi Read-Only OverlayFS)

> **Disclaimer:** This project was vibecoded with **Gemini** using **Argon** and is **human-vetted**.


Dynamic dual-profile manager for a headless **Raspberry Pi Zero 2W** running the **Direwolf** APRS software TNC in an off-road 4x4 or mobile satellite (Starlink) iGate/digipeater installation protected by Raspberry Pi OS's **Read-Only Overlay File System**.

Instead of relying on a physical toggle switch wired to GPIO headers, this daemon switches between **Profile A (iGate Only)** and **Profile B (Trail Relay Digipeater + iGate)** entirely in software using three integrated methods:

1. **Automatic RF Traffic Density Sensor (Default on Boot)**: Starts in **Profile A (iGate Only)** and monitors decoded AX.25 frames over Direwolf's local KISS TCP port (`127.0.0.1:8001`). When driving into a radio-quiet canyon or backcountry trail where mountaintop digipeater traffic drops to `<= 2` repeated packets per 15 minutes, it automatically switches to **Profile B (Digi/iGate Combo)**. When returning to a metro area with strong wide-area repeater coverage (`>= 12` repeated packets per 15 minutes), it automatically reverts to **iGate Only**.
2. **Over-the-Air APRS RF Command Listener**: Send standard APRS text messages (`DIGI ON`, `DIGI OFF`, `AUTO`, `STATUS`) from your whitelisted handheld radio directly to your vehicle's APRS callsign over 144.390 MHz RF. The daemon transmits an over-the-air `ack` and confirmation message before switching profiles.
3. **In-Cab Mobile Web UI & One-Tap Phone Shortcut API**: Serves a dark-mode control dashboard and REST endpoints on port `8080` (`http://raspberrypi.local:8080`) over your local vehicle Wi-Fi LAN for instant manual overrides from a phone, tablet, Apple Shortcut, or Android HTTP widget.

## Repository Structure

```text
direwolf-smart-profile-switcher/
├── README.md
├── LICENSE
├── .gitignore
├── install.sh                     # Interactive setup & callsign personalization script
├── config/
│   ├── direwolf-igate.conf        # Profile A: Pure bidirectional APRS-IS iGate (DIGIPEAT disabled)
│   └── direwolf-digi.conf         # Profile B: Tactical trail relay digipeater + APRS-IS iGate
├── src/
│   └── dw-switch-daemon.py        # Zero-dependency Python 3 daemon (KISS TCP + HTTP Server)
└── systemd/
    ├── dw-switch.service          # Systemd unit for the smart profile daemon
    └── direwolf.service           # Systemd unit for Direwolf pointing to /etc/direwolf.conf
```

## Hardware & Software Prerequisites

* **Single-Board Computer**: Raspberry Pi Zero 2W (or Pi 3/4/5) running Raspberry Pi OS (Bullseye or Bookworm).
* **Audio & PTT Interface**: Digirig Mobile (C-Media USB audio + Silicon Labs CP2102N hardware RTS PTT) or equivalent soundcard interface.
* **GPS Receiver**: USB GPS receiver managed by `gpsd` (`localhost:2947`) or direct NMEA serial for `SMARTBEACONING` and `TBEACON`.
* **Software**:
  * `direwolf` installed at `/usr/local/bin/direwolf`
  * `python3` (uses Python standard library only; zero `pip` or external dependencies required)
  * `gpsd` (if using `GPSD localhost 2947`)

## Deployment Workflow Overview

> **IMPORTANT — Setup First, Read-Only Second:**
> You **must** perform the installation and configuration steps below **first** while your Raspberry Pi is still in normal **Read-Write** mode. Once all files, callsigns, and systemd services are installed and tested, you will enable the **Overlay File System** via `raspi-config` as the final step so your SD card is locked against sudden vehicle power cuts.

### Phase 1: Gather Your Station Identifiers

Before running the installer or editing the configuration files, gather four values for your station:

1. **Your Station & Handheld Callsigns**:
   * **Vehicle Station Callsign (`MYCALL`)**: e.g., `N0CALL-9` (SSID `-9` is standard for mobile VHF stations).
   * **Authorized Handheld Callsign**: e.g., `N0CALL-7` (SSID `-7` is standard for handhelds allowed to send RF mode commands).
2. **Your 5-Digit APRS-IS Passcode (`IGLOGIN`)**:
   * Required to gate packets into APRS-IS (`noam.aprs2.net`). Generate your callsign's passcode using `callpass <YOUR_CALLSIGN>` or a standard APRS-IS passcode generator.
3. **Your USB Audio Card & Device Number (`ADEVICE`)**:
   * Run `aplay -l` and `arecord -l` on the Raspberry Pi. If your Digirig USB audio codec shows as `card 1: Device [USB PnP Sound Device], device 0`, keep `ADEVICE plughw:1,0`.
4. **Your Persistent Digirig Serial PTT Path (`PTT`)**:
   * Always use the persistent `/dev/serial/by-id/...` path rather than `/dev/ttyUSB0` so Linux never swaps your Digirig PTT serial port with your USB GPS on reboot:
     ```bash
     ls -l /dev/serial/by-id/
     ```
   * Copy the full path beginning with `/dev/serial/by-id/usb-Silicon_Labs_CP2102...` for your `PTT` line.
   * Also set `USBAUTO="false"` in `/etc/default/gpsd` so `gpsd` does not probe your Digirig serial port and key your transmitter on boot.

### Phase 2: Install & Configure (While Filesystem is Read-Write)

#### Option A: Automated Interactive Installer (`install.sh`)

Clone this repository onto your Raspberry Pi and run `install.sh`. It verifies that OverlayFS is not active yet, prompts for your callsigns, APRS-IS passcode, and Digirig PTT path, installs the configs to `/boot/firmware`, creates the `/etc/direwolf.conf` symlink, and enables both systemd services:

```bash
git clone https://github.com/YOUR_USERNAME/direwolf-smart-profile-switcher.git
cd direwolf-smart-profile-switcher
chmod +x install.sh
sudo ./install.sh
```

#### Option B: Manual Configuration & Installation

If you prefer to configure and install every file manually while in read-write mode:

1. **Personalize `config/direwolf-igate.conf` and `config/direwolf-digi.conf`**:

| Directive | Placeholder in Repo | Replace With |
| :--- | :--- | :--- |
| `MYCALL` | `MYCALL N0CALL-9` | Your mobile callsign and SSID (e.g., `MYCALL W1AW-9`) |
| `PTT` | `PTT /dev/serial/by-id/...YOUR_SERIAL_ID... RTS` | Your actual `/dev/serial/by-id/...` path + `RTS` |
| `IGLOGIN` | `IGLOGIN N0CALL 12345` | Your base callsign and 5-digit APRS-IS passcode |
| `IGSERVER` | `IGSERVER noam.aprs2.net` | Regional Tier 2 server (`noam.aprs2.net`, `euro.aprs2.net`, etc.) |

2. **Personalize `src/dw-switch-daemon.py`**:
   Update the top settings block in `src/dw-switch-daemon.py` with your callsigns (and optionally adjust the RF density thresholds):

```python
STATION_CALLSIGN = "N0CALL-9"       # Replace with your mobile station callsign-SSID
ALLOWED_RF_SENDERS = {"N0CALL-7"}   # Replace with your handheld callsign-SSID(s)
```

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `WINDOW_SECONDS` | `900` (15 min) | Sliding time window used to count heard RF packets |
| `WARMUP_SECONDS` | `300` (5 min) | Initial listening period after boot before auto-switching is allowed |
| `COOLDOWN_SECONDS` | `600` (10 min) | Minimum wait time between automatic profile switches |
| `QUIET_MAX_REPEATED_PKTS` | `2` pkts | Switches `iGate Only` $\rightarrow$ `Digi/iGate` when repeated packets in 15 min $\le 2$ |
| `BUSY_MIN_REPEATED_PKTS` | `12` pkts | Switches `Digi/iGate` $\rightarrow$ `iGate Only` when repeated packets in 15 min $\ge 12$ |

3. **Copy Files and Enable Services**:

```bash
# 1. Install both profile configs to /boot/firmware and create the default symlink
sudo install -m 0644 config/direwolf-igate.conf /boot/firmware/direwolf-igate.conf
sudo install -m 0644 config/direwolf-digi.conf /boot/firmware/direwolf-digi.conf
sudo ln -sfn /boot/firmware/direwolf-igate.conf /etc/direwolf.conf

# 2. Install the daemon script and systemd service files
sudo install -m 0755 src/dw-switch-daemon.py /usr/local/bin/dw-switch-daemon.py
sudo install -m 0644 systemd/dw-switch.service /etc/systemd/system/dw-switch.service
sudo install -m 0644 systemd/direwolf.service /etc/systemd/system/direwolf.service

# 3. Enable and start both services
sudo systemctl daemon-reload
sudo systemctl enable dw-switch.service direwolf.service
sudo systemctl restart dw-switch.service direwolf.service
sync
```

### Phase 3: Test Before Locking the Filesystem

Verify that both services start cleanly and that the Web UI and symlink transitions work before enabling Read-Only mode:

```bash
# 1. Check service logs
sudo journalctl -u dw-switch.service -u direwolf.service -n 40 --no-pager

# 2. Test the local HTTP status API
curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool
```

### Phase 4: Enable the Read-Only Overlay File System

Once setup and testing are complete, enable Raspberry Pi OS's native **Overlay File System** so the SD card is mounted read-only and all runtime changes (including `/etc/direwolf.conf` symlink swaps and service restarts) happen safely in RAM:

#### Method A: Using the Command Line (Headless / All Versions)

1. Open a terminal and run the configuration tool:
   ```bash
   sudo raspi-config
   ```
2. Navigate to **Performance Options** (or *Advanced Options* on older versions).
3. Select **Overlay File System**.
4. Choose **Yes** to enable the overlay.
5. Choose **Yes** when prompted to write-protect the boot partition.
6. Select **Finish** and reboot the Raspberry Pi.

#### Method B: Using the Graphical Desktop (GUI)

1. Open the application menu (Raspberry icon) $\rightarrow$ **Preferences** $\rightarrow$ **Raspberry Pi Configuration**.
2. Click on the **Performance** tab.
3. Find **Overlay File System** and click **Configure...**.
4. Set the Overlay to **Enabled** and the Boot Partition to **Read-only**.
5. Click **OK** and reboot the device.

#### Making Permanent Configuration Changes Later

* **Full System Updates**: Run `sudo raspi-config`, navigate to **Performance Options** $\rightarrow$ **Overlay File System**, disable the overlay, and reboot. Make your changes, then re-enable the Overlay File System and reboot.
* **Quick Edits to `/boot/firmware/direwolf-*.conf` Only**: Because the two Direwolf config files live on `/boot/firmware`, you can edit them without disabling the root overlay by temporarily remounting `/boot/firmware` as read-write:
  ```bash
  sudo mount -o remount,rw /boot/firmware
  sudo nano /boot/firmware/direwolf-digi.conf
  sync
  sudo mount -o remount,ro /boot/firmware
  sudo systemctl restart direwolf.service
  ```

## Operating the Station

### 1. In-Cab Web Dashboard & Phone Shortcuts (Port `8080`)

Connect your phone or tablet to the same Wi-Fi LAN as your Raspberry Pi and open:

* **Dashboard URL**: `http://raspberrypi.local:8080` (or `http://<PI_IP_ADDRESS>:8080`)
* **REST API Endpoints** (accept both `GET` and `POST` for iOS Shortcuts or Android HTTP Shortcuts):
  * `/api/status` — Returns JSON with current mode, active profile, and 15-minute packet counts
  * `/api/mode/auto` — Enables automatic RF traffic density switching
  * `/api/mode/igate` — Forces **Profile A (iGate Only)**
  * `/api/mode/digi` — Forces **Profile B (Digi/iGate Combo)**

### 2. Over-the-Air APRS RF Commands

From your whitelisted handheld (`ALLOWED_RF_SENDERS`), send a standard APRS message addressed to your mobile station (`STATION_CALLSIGN`) over 144.390 MHz within 1 RF hop:

| APRS Message Text | Station Action |
| :--- | :--- |
| `DIGI ON` or `MODE DIGI` | Sends RF `ack` + confirmation, then locks **Profile B (`Digi/iGate Combo`)** |
| `DIGI OFF` or `MODE IGATE` | Sends RF `ack` + confirmation, then locks **Profile A (`iGate Only`)** |
| `AUTO` or `MODE AUTO` | Sends RF `ack` + confirmation, then enables **Automatic RF Density** mode |
| `STATUS` or `?` | Sends RF `ack` + current mode, active profile, and 15-minute repeated packet count |
