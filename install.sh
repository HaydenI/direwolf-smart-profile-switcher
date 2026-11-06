#!/usr/bin/env bash
# =============================================================================
# Direwolf Smart Profile Switcher — Automated Installer for Raspberry Pi OS
# IMPORTANT: Run this setup FIRST while the filesystem is still read-write.
# Once setup and testing are complete, enable the Read-Only Overlay File System
# via `sudo raspi-config`.
# =============================================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: Please run this installer as root (e.g., sudo ./install.sh)"
  exit 1
fi

# Check if Overlay File System is already active on /
ROOT_FSTYPE=$(findmnt -no FSTYPE / || true)
if [[ "$ROOT_FSTYPE" == "overlay" ]]; then
  echo "======================================================================"
  echo "WARNING: Raspberry Pi Overlay File System (Read-Only Mode) is ACTIVE!"
  echo "Any changes made right now are written only to RAM and will disappear"
  echo "when you reboot."
  echo ""
  echo "Please disable the Overlay File System first:"
  echo "  1. Run: sudo raspi-config"
  echo "  2. Go to: Performance Options -> Overlay File System"
  echo "  3. Select <No> to disable the overlay, then reboot."
  echo "  4. Re-run: sudo ./install.sh"
  echo "======================================================================"
  read -rp "Continue anyway in temporary RAM mode? [y/N]: " CONT
  if [[ "${CONT,,}" != "y" ]]; then
    exit 1
  fi
fi

echo "=== Direwolf Smart Profile Switcher Setup ==="
read -rp "Enter your Mobile Station Callsign + SSID (e.g., W1AW-9) [N0CALL-9]: " STATION_CALL
STATION_CALL=${STATION_CALL:-N0CALL-9}
STATION_CALL=${STATION_CALL^^}

BASE_CALL="${STATION_CALL%%-*}"
read -rp "Enter your 5-digit APRS-IS Passcode for ${BASE_CALL} [12345]: " APRS_PASS
APRS_PASS=${APRS_PASS:-12345}

read -rp "Enter your Handheld Callsign + SSID to whitelist for RF commands (e.g., W1AW-7) [N0CALL-7]: " HT_CALL
HT_CALL=${HT_CALL:-N0CALL-7}
HT_CALL=${HT_CALL^^}

echo ""
echo "Detected USB Serial devices in /dev/serial/by-id/:"
ls -1 /dev/serial/by-id/ 2>/dev/null || echo "  (None currently plugged in)"
read -rp "Enter full Digirig PTT device path [/dev/ttyUSB0]: " PTT_DEV
PTT_DEV=${PTT_DEV:-/dev/ttyUSB0}

echo ""
echo "[1/4] Installing and personalizing Direwolf profile configs in /boot/firmware..."
install -m 0644 config/direwolf-igate.conf /boot/firmware/direwolf-igate.conf
install -m 0644 config/direwolf-digi.conf /boot/firmware/direwolf-digi.conf

for cfg in /boot/firmware/direwolf-igate.conf /boot/firmware/direwolf-digi.conf; do
  sed -i "s/^MYCALL .*/MYCALL ${STATION_CALL}/" "$cfg"
  sed -i "s/^IGLOGIN .*/IGLOGIN ${BASE_CALL} ${APRS_PASS}/" "$cfg"
  sed -i "s|^PTT .*|PTT ${PTT_DEV} RTS|" "$cfg"
done

echo "[2/4] Creating default symlink /etc/direwolf.conf -> /boot/firmware/direwolf-igate.conf..."
ln -sfn /boot/firmware/direwolf-igate.conf /etc/direwolf.conf

echo "[3/4] Installing smart profile daemon to /usr/local/bin/dw-switch-daemon.py..."
install -m 0755 src/dw-switch-daemon.py /usr/local/bin/dw-switch-daemon.py
sed -i "s/STATION_CALLSIGN = \"N0CALL-9\"/STATION_CALLSIGN = \"${STATION_CALL}\"/" /usr/local/bin/dw-switch-daemon.py
sed -i "s/ALLOWED_RF_SENDERS = {\"N0CALL-7\"}/ALLOWED_RF_SENDERS = {\"${HT_CALL}\"}/" /usr/local/bin/dw-switch-daemon.py
sed -i "s/N0CALL-9/${STATION_CALL}/g" /usr/local/bin/dw-switch-daemon.py
sed -i "s/N0CALL-7/${HT_CALL}/g" /usr/local/bin/dw-switch-daemon.py

echo "[4/4] Installing, enabling, and starting systemd services..."
install -m 0644 systemd/dw-switch.service /etc/systemd/system/dw-switch.service
install -m 0644 systemd/direwolf.service /etc/systemd/system/direwolf.service
systemctl daemon-reload
systemctl enable dw-switch.service
systemctl enable direwolf.service
systemctl restart dw-switch.service
systemctl restart direwolf.service

sync
echo ""
echo "======================================================================"
echo "Setup Complete! Services are now enabled and running."
echo ""
echo "NEXT STEP — Enable the Read-Only Overlay File System:"
echo "  1. Verify your setup works:"
echo "     curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool"
echo "  2. Run: sudo raspi-config"
echo "  3. Navigate to: Performance Options -> Overlay File System"
echo "  4. Choose <Yes> to enable the overlay."
echo "  5. Choose <Yes> to write-protect the boot partition."
echo "  6. Select <Finish> and reboot the Raspberry Pi."
echo "======================================================================"
