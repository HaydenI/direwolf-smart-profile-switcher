#!/usr/bin/env python3
"""
Direwolf APRS Smart Profile Daemon (/usr/local/bin/dw-switch-daemon.py)

Combines 3 switchless profile control methods for a Read-Only Root FS Raspberry Pi Zero 2W:
  1. In-Cab Web UI & Phone Shortcut API (http://<pi-ip>:8080)
  2. Over-the-Air APRS RF Command Listener via KISS TCP (127.0.0.1:8001)
     - Whitelisted Sender: N0CALL-7 (addressed to N0CALL-9)
  3. Automatic RF Traffic Density Sensor (KISS Channel 0 sliding window)
     - Boots into Profile A (iGate Only) by default.
     - Automatically switches to Profile B (Digi/iGate Combo) when a quiet RF area
       is detected after the warmup window, and reverts to iGate Only in high-traffic metro RF.
"""

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

# =============================================================================
#                          STATION & DAEMON SETTINGS
# =============================================================================
STATION_CALLSIGN = "N0CALL-9"
ALLOWED_RF_SENDERS = {"N0CALL-7"}  # Only accept over-the-air commands from these callsigns
MAX_COMMAND_RF_HOPS = 1            # Reject commands that traversed more than 1 digipeater hop

# Direwolf KISS TCP Server & HTTP Dashboard
KISS_HOST = "127.0.0.1"
KISS_PORT = 8001
HTTP_BIND_HOST = "0.0.0.0"
HTTP_PORT = 8080

# Auto RF Density Thresholds (Sliding Window)
WINDOW_SECONDS = 900               # 15-minute sliding window for RF traffic evaluation
WARMUP_SECONDS = 300               # Listen for 5 minutes after boot/auto-reset before auto-switching
COOLDOWN_SECONDS = 600             # Minimum 10 minutes between automatic profile transitions
EVAL_INTERVAL_SECONDS = 15         # How often the density evaluator checks thresholds

# Hysteresis thresholds (number of digipeated/infrastructure packets heard in 15 minutes):
# - In iGate Only: if repeated packets <= QUIET_MAX_REPEATED_PKTS after warmup -> switch to Digi/iGate
# - In Digi/iGate: if repeated packets >= BUSY_MIN_REPEATED_PKTS -> switch back to iGate Only
QUIET_MAX_REPEATED_PKTS = 2
BUSY_MIN_REPEATED_PKTS = 12

# Filesystem & Service Paths
BOOT_MOUNT = "/boot/firmware"
ROOT_MOUNT = "/"
PROFILE_IGATE = Path("/boot/firmware/direwolf-igate.conf")
PROFILE_DIGI = Path("/boot/firmware/direwolf-digi.conf")
SYMLINK_PATH = Path("/etc/direwolf.conf")
DIREWOLF_SERVICE = "direwolf.service"

# KISS Protocol Constants
FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("dw-switch")


# =============================================================================
#                        READ-ONLY FILESYSTEM & SYMLINK
# =============================================================================
def is_mounted_ro(mountpoint: str) -> bool:
    """Check /proc/mounts to determine if mountpoint is currently read-only."""
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == mountpoint:
                    return "ro" in parts[3].split(",")
    except OSError as exc:
        logger.warning("Could not inspect /proc/mounts for %s: %s", mountpoint, exc)
    return True


def remount(mountpoint: str, mode: str) -> bool:
    """Remount a partition as 'rw' or 'ro'."""
    try:
        subprocess.run(
            ["/usr/bin/mount", "-o", f"remount,{mode}", mountpoint],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Remounted %s as %s.", mountpoint, mode.upper())
        return True
    except subprocess.CalledProcessError as exc:
        logger.error(
            "Failed to remount %s as %s (exit %d): %s",
            mountpoint,
            mode.upper(),
            exc.returncode,
            exc.stderr.strip(),
        )
        return False


def update_symlink_atomic(target: Path, link_path: Path) -> None:
    """Atomically replace link_path with a symlink to target."""
    tmp_link = link_path.parent / f".{link_path.name}.tmp"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(str(target), str(tmp_link))
    os.replace(str(tmp_link), str(link_path))
    os.sync()


# =============================================================================
#                       AX.25 & KISS ENCODER / DECODER
# =============================================================================
def kiss_unescape(data: bytes) -> bytes:
    """Unescape KISS payload bytes between FEND (0xC0) delimiters."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == FESC and i + 1 < n:
            nxt = data[i + 1]
            if nxt == TFEND:
                out.append(FEND)
            elif nxt == TFESC:
                out.append(FESC)
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def kiss_escape(data: bytes) -> bytes:
    """Escape raw AX.25 payload and wrap in KISS data frame on Channel 0."""
    out = bytearray([FEND, 0x00])  # 0x00 = Data frame, TNC port/channel 0
    for b in data:
        if b == FEND:
            out.extend([FESC, TFEND])
        elif b == FESC:
            out.extend([FESC, TFESC])
        else:
            out.append(b)
    out.append(FEND)
    return bytes(out)


def decode_ax25_address(addr_bytes: bytes) -> tuple[str, bool, bool]:
    """Decode a 7-byte AX.25 address field into (CALL-SSID, has_been_repeated, is_last)."""
    call = "".join(chr(b >> 1) for b in addr_bytes[:6]).strip()
    ssid_byte = addr_bytes[6]
    ssid = (ssid_byte >> 1) & 0x0F
    has_repeated = bool(ssid_byte & 0x80)
    is_last = bool(ssid_byte & 0x01)
    full_call = f"{call}-{ssid}" if ssid > 0 else call
    return full_call.upper(), has_repeated, is_last


def encode_ax25_address(callsign: str, is_last: bool = False, has_repeated: bool = False) -> bytes:
    """Encode CALL-SSID into a 7-byte AX.25 address field."""
    parts = callsign.upper().split("-", 1)
    call = parts[0][:6].ljust(6)
    ssid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    out = bytearray(ord(c) << 1 for c in call)
    ssid_byte = 0x60 | ((ssid & 0x0F) << 1)
    if has_repeated:
        ssid_byte |= 0x80
    if is_last:
        ssid_byte |= 0x01
    out.append(ssid_byte)
    return bytes(out)


def parse_kiss_ax25_frame(kiss_payload: bytes) -> dict | None:
    """Parse an unescaped KISS frame into AX.25 addresses and APRS information text."""
    if len(kiss_payload) < 17:
        return None
    cmd_byte = kiss_payload[0]
    if (cmd_byte & 0x0F) != 0x00:
        return None  # Not a KISS data frame

    ax25 = kiss_payload[1:]
    dest, _, dest_last = decode_ax25_address(ax25[0:7])
    if dest_last:
        return None

    src, _, src_last = decode_ax25_address(ax25[7:14])
    idx = 14
    path = []
    repeated_hops = 0
    is_last = src_last

    while not is_last and idx + 7 <= len(ax25) and len(path) < 8:
        digi_call, h_bit, is_last = decode_ax25_address(ax25[idx : idx + 7])
        path.append((digi_call, h_bit))
        if h_bit:
            repeated_hops += 1
        idx += 7

    # Expect UI frame Control (0x03) and PID (0xF0)
    if idx + 2 > len(ax25) or ax25[idx] != 0x03 or ax25[idx + 1] != 0xF0:
        return None

    info_bytes = ax25[idx + 2 :]
    info_text = info_bytes.decode("ascii", errors="replace").rstrip("\r\n")
    return {
        "dest": dest,
        "src": src,
        "path": path,
        "repeated_hops": repeated_hops,
        "info": info_text,
    }


def build_aprs_message_kiss_frame(src_call: str, recipient_call: str, message_text: str) -> bytes:
    """Build a KISS-wrapped AX.25 UI frame sending an APRS message via WIDE1-1."""
    dest_bytes = encode_ax25_address("APDW17", is_last=False)
    src_bytes = encode_ax25_address(src_call, is_last=False)
    via_bytes = encode_ax25_address("WIDE1-1", is_last=True)
    header = dest_bytes + src_bytes + via_bytes + bytes([0x03, 0xF0])
    padded_recipient = recipient_call.upper()[:9].ljust(9)
    info = f":{padded_recipient}:{message_text}".encode("ascii", errors="replace")
    return kiss_escape(header + info)


# =============================================================================
#                         STATE & CONTROLLER ENGINE
# =============================================================================
class StationController:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "AUTO"             # "AUTO", "FORCE_IGATE", or "FORCE_DIGI"
        self.active_profile = "igate"  # "igate" or "digi"
        self.boot_or_auto_start = time.monotonic()
        self.last_switch_time = 0.0
        self.kiss_connected = False
        self.kiss_sock: socket.socket | None = None

        # Sliding window of (monotonic_timestamp, is_repeated_packet, src_call)
        self.rf_window: deque[tuple[float, bool, str]] = deque()
        self.recent_cmd_ids: deque[tuple[str, str]] = deque(maxlen=32)
        self.last_event = "Initialized in AUTO mode (Default: Profile A iGate Only)"

    def prune_window(self, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        cutoff = now - WINDOW_SECONDS
        while self.rf_window and self.rf_window[0][0] < cutoff:
            self.rf_window.popleft()

    def get_metrics(self) -> dict:
        with self.lock:
            now = time.monotonic()
            self.prune_window(now)
            total_pkts = len(self.rf_window)
            repeated_pkts = sum(1 for _, is_rep, _ in self.rf_window if is_rep)
            direct_pkts = total_pkts - repeated_pkts
            warmup_rem = max(0, int(WARMUP_SECONDS - (now - self.boot_or_auto_start)))
            cooldown_rem = (
                max(0, int(COOLDOWN_SECONDS - (now - self.last_switch_time)))
                if self.last_switch_time > 0
                else 0
            )
            return {
                "station": STATION_CALLSIGN,
                "mode": self.mode,
                "active_profile": self.active_profile,
                "active_config": str(PROFILE_DIGI if self.active_profile == "digi" else PROFILE_IGATE),
                "kiss_connected": self.kiss_connected,
                "window_minutes": WINDOW_SECONDS // 60,
                "total_rf_packets": total_pkts,
                "repeated_rf_packets": repeated_pkts,
                "direct_rf_packets": direct_pkts,
                "quiet_threshold": QUIET_MAX_REPEATED_PKTS,
                "busy_threshold": BUSY_MIN_REPEATED_PKTS,
                "warmup_remaining_sec": warmup_rem if self.mode == "AUTO" else 0,
                "cooldown_remaining_sec": cooldown_rem if self.mode == "AUTO" else 0,
                "last_event": self.last_event,
            }

    def apply_profile(self, profile_key: str, reason: str, restart_service: bool = True) -> bool:
        """Atomically update /etc/direwolf.conf and restart direwolf.service."""
        target = PROFILE_DIGI if profile_key == "digi" else PROFILE_IGATE
        label = "Profile B (Digi/iGate Combo)" if profile_key == "digi" else "Profile A (iGate Only)"

        with self.lock:
            try:
                if SYMLINK_PATH.is_symlink() and Path(os.readlink(SYMLINK_PATH)) == target:
                    self.active_profile = profile_key
                    self.last_event = f"{label} already active ({reason})"
                    logger.info("%s is already active (%s).", label, reason)
                    return True
            except OSError:
                pass

            logger.info("Switching to %s [%s] -> %s", label, reason, target)
            # When Raspberry Pi OverlayFS is enabled via raspi-config, / is mounted RW in RAM
            # (overlay) while /boot/firmware is RO on the SD card. If / is instead mounted RO
            # via fstab, we briefly remount / RW to update /etc/direwolf.conf and restore RO.
            root_was_ro = is_mounted_ro(ROOT_MOUNT)
            updated = False

            try:
                if root_was_ro and not remount(ROOT_MOUNT, "rw"):
                    raise RuntimeError("Root FS (/) failed to remount RW.")
                if not target.exists():
                    raise FileNotFoundError(f"Missing target profile: {target}")

                update_symlink_atomic(target, SYMLINK_PATH)
                updated = True
                self.active_profile = profile_key
                self.last_switch_time = time.monotonic()
                self.last_event = f"Switched to {label} ({reason})"
            except Exception as exc:
                logger.error("Failed switching profile: %s", exc)
                self.last_event = f"ERROR switching profile: {exc}"
                return False
            finally:
                os.sync()
                if root_was_ro:
                    remount(ROOT_MOUNT, "ro")

        if updated and restart_service:
            try:
                subprocess.run(
                    ["/usr/bin/systemctl", "restart", DIREWOLF_SERVICE],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                logger.info("Restarted %s (%s).", DIREWOLF_SERVICE, reason)
            except subprocess.CalledProcessError as exc:
                logger.error("Failed to restart %s: %s", DIREWOLF_SERVICE, exc.stderr.strip())
        return updated

    def set_mode(self, new_mode: str, source: str) -> None:
        """Set mode to AUTO, FORCE_IGATE, or FORCE_DIGI."""
        new_mode = new_mode.upper()
        with self.lock:
            self.mode = new_mode
            if new_mode == "AUTO":
                self.boot_or_auto_start = time.monotonic()
                self.last_event = f"Mode set to AUTO via {source}"

        if new_mode == "FORCE_IGATE":
            self.apply_profile("igate", f"Manual iGate Only via {source}")
        elif new_mode == "FORCE_DIGI":
            self.apply_profile("digi", f"Manual Digi/iGate via {source}")
        elif new_mode == "AUTO":
            logger.info("Entered AUTO RF density mode via %s.", source)

    def record_rf_packet(self, parsed: dict) -> None:
        """Record an external RF packet in the sliding density window."""
        src = parsed["src"]
        # Ignore our own transmissions or frames we digipeated
        if src == STATION_CALLSIGN.upper():
            return
        for digi_call, h_bit in parsed["path"]:
            if digi_call == STATION_CALLSIGN.upper() and h_bit:
                return
            # Ignore TCP/IP gated third-party packets if any appear on KISS
            if digi_call in ("TCPIP", "TCPXX", "NOGATE", "RFONLY"):
                return
        if parsed["info"].startswith("}"):
            return  # Third-party gated packet from APRS-IS

        is_repeated = parsed["repeated_hops"] > 0
        with self.lock:
            now = time.monotonic()
            self.rf_window.append((now, is_repeated, src))
            self.prune_window(now)

    def evaluate_auto_density(self) -> None:
        """Check sliding window metrics and switch profiles when in AUTO mode."""
        with self.lock:
            if self.mode != "AUTO":
                return
            now = time.monotonic()
            self.prune_window(now)

            if (now - self.boot_or_auto_start) < WARMUP_SECONDS:
                return
            if self.last_switch_time > 0 and (now - self.last_switch_time) < COOLDOWN_SECONDS:
                return

            repeated_pkts = sum(1 for _, is_rep, _ in self.rf_window if is_rep)
            current = self.active_profile

        if current == "igate" and repeated_pkts <= QUIET_MAX_REPEATED_PKTS:
            self.apply_profile(
                "digi",
                f"Auto RF quiet area detected ({repeated_pkts} repeated pkts/{WINDOW_SECONDS // 60}m <= {QUIET_MAX_REPEATED_PKTS})",
            )
        elif current == "digi" and repeated_pkts >= BUSY_MIN_REPEATED_PKTS:
            self.apply_profile(
                "igate",
                f"Auto RF metro traffic detected ({repeated_pkts} repeated pkts/{WINDOW_SECONDS // 60}m >= {BUSY_MIN_REPEATED_PKTS})",
            )

    def send_kiss_aprs_message(self, recipient: str, text: str) -> None:
        """Transmit an APRS message frame over the connected KISS socket."""
        sock = self.kiss_sock
        if not sock:
            return
        try:
            frame = build_aprs_message_kiss_frame(STATION_CALLSIGN, recipient, text)
            sock.sendall(frame)
            logger.info("TX RF APRS Msg to %s: %s", recipient, text)
        except OSError as exc:
            logger.warning("Failed sending KISS frame to %s: %s", recipient, exc)

    def handle_aprs_command_if_any(self, parsed: dict) -> None:
        """Inspect decoded frame for an APRS message command from N0CALL-7 to N0CALL-9."""
        src = parsed["src"].upper()
        if src not in ALLOWED_RF_SENDERS:
            return
        if parsed["repeated_hops"] > MAX_COMMAND_RF_HOPS:
            logger.warning(
                "Ignored RF command from %s: hop count %d > %d",
                src,
                parsed["repeated_hops"],
                MAX_COMMAND_RF_HOPS,
            )
            return

        info = parsed["info"]
        # Standard APRS message format: :RECIPIENT:message text{msg_id
        if not info.startswith(":") or len(info) < 12 or info[10] != ":":
            return

        recipient = info[1:10].strip().upper()
        if recipient != STATION_CALLSIGN.upper():
            return

        body = info[11:].strip()
        msg_id = None
        if "{" in body:
            body, msg_id = body.rsplit("{", 1)
            body = body.strip()
            msg_id = msg_id.strip()[:5]

        # Always ACK valid messages from our whitelisted HT first
        if msg_id:
            self.send_kiss_aprs_message(src, f"ack{msg_id}")
            if (src, msg_id) in self.recent_cmd_ids:
                logger.info("Duplicate RF message ID %s from %s; sent ACK only.", msg_id, src)
                return
            self.recent_cmd_ids.append((src, msg_id))

        cmd = re.sub(r"\s+", " ", body.upper()).strip()
        logger.info("Received authorized APRS RF command from %s: '%s'", src, cmd)

        if cmd in ("DIGI ON", "MODE DIGI", "DIGI"):
            self.send_kiss_aprs_message(src, "4x4: Switching to FORCE_DIGI (Digi+iGate)")
            time.sleep(2.5)  # Allow IC-2200H PTT cycle to finish transmitting ACK + reply
            self.set_mode("FORCE_DIGI", f"APRS RF ({src})")
        elif cmd in ("DIGI OFF", "MODE IGATE", "IGATE", "IGATE ONLY"):
            self.send_kiss_aprs_message(src, "4x4: Switching to FORCE_IGATE (iGate Only)")
            time.sleep(2.5)
            self.set_mode("FORCE_IGATE", f"APRS RF ({src})")
        elif cmd in ("MODE AUTO", "AUTO"):
            self.send_kiss_aprs_message(src, "4x4: Enabled AUTO RF Density Mode")
            self.set_mode("AUTO", f"APRS RF ({src})")
        elif cmd in ("STATUS", "?"):
            m = self.get_metrics()
            self.send_kiss_aprs_message(
                src,
                f"Mode={m['mode']} Prof={m['active_profile'].upper()} Rep={m['repeated_rf_packets']}/{m['window_minutes']}m",
            )
        else:
            self.send_kiss_aprs_message(
                src,
                "Cmds: DIGI ON | DIGI OFF | AUTO | STATUS",
            )


# =============================================================================
#                         KISS TCP CLIENT WORKER THREAD
# =============================================================================
def kiss_client_worker(controller: StationController, stop_event: threading.Event) -> None:
    """Maintain connection to Direwolf KISSPORT 8001 and parse incoming AX.25 frames."""
    while not stop_event.is_set():
        sock = None
        try:
            sock = socket.create_connection((KISS_HOST, KISS_PORT), timeout=10.0)
            sock.settimeout(2.0)
            with controller.lock:
                controller.kiss_connected = True
                controller.kiss_sock = sock
            logger.info("Connected to Direwolf KISS server at %s:%d", KISS_HOST, KISS_PORT)

            buf = bytearray()
            in_frame = False

            while not stop_event.is_set():
                try:
                    chunk = sock.recv(1024)
                    if not chunk:
                        raise ConnectionError("KISS server closed connection.")
                except socket.timeout:
                    continue

                for b in chunk:
                    if b == FEND:
                        if in_frame and len(buf) > 0:
                            raw_kiss = kiss_unescape(bytes(buf))
                            parsed = parse_kiss_ax25_frame(raw_kiss)
                            if parsed:
                                controller.record_rf_packet(parsed)
                                controller.handle_aprs_command_if_any(parsed)
                        buf.clear()
                        in_frame = True
                    elif in_frame:
                        buf.append(b)

        except Exception as exc:
            with controller.lock:
                controller.kiss_connected = False
                controller.kiss_sock = None
            logger.debug("KISS connection unavailable (%s); retrying in 5s...", exc)
            stop_event.wait(5.0)
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass


# =============================================================================
#                       IN-CAB WEB UI & JSON API SERVER
# =============================================================================
HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>N0CALL-9 APRS Control</title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8;
    --green: #10b981; --amber: #f59e0b; --blue: #3b82f6; --border: #334155;
  }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { margin: 0; padding: 16px; background: var(--bg); color: var(--text); max-width: 520px; margin-inline: auto; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px; margin-bottom: 14px; }
  h1 { font-size: 1.25rem; margin: 0 0 4px 0; display: flex; justify-content: space-between; align-items: center; }
  .sub { font-size: 0.85rem; color: var(--muted); margin-bottom: 12px; }
  .badge { display: inline-block; padding: 6px 12px; border-radius: 999px; font-weight: 700; font-size: 0.85rem; }
  .badge-igate { background: rgba(59,130,246,0.2); color: #60a5fa; border: 1px solid #3b82f6; }
  .badge-digi  { background: rgba(245,158,11,0.2); color: #fbbf24; border: 1px solid #f59e0b; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
  .stat { background: rgba(15,23,42,0.6); padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border); }
  .stat-label { font-size: 0.75rem; color: var(--muted); }
  .stat-val { font-size: 1.15rem; font-weight: 700; margin-top: 2px; }
  .btn-group { display: flex; flex-direction: column; gap: 10px; }
  button {
    width: 100%; padding: 15px; border-radius: 12px; border: 1px solid var(--border);
    background: #334155; color: var(--text); font-size: 1rem; font-weight: 700; cursor: pointer;
  }
  button.active-auto  { background: var(--green); color: #052e16; border-color: var(--green); }
  button.active-igate { background: var(--blue); color: #fff; border-color: var(--blue); }
  button.active-digi  { background: var(--amber); color: #1e1b4b; border-color: var(--amber); }
  .event { font-size: 0.8rem; color: var(--muted); margin-top: 8px; word-break: break-word; }
</style>
</head>
<body>
  <div class="card">
    <h1>
      <span>N0CALL-9 APRS</span>
      <span id="profileBadge" class="badge badge-igate">Loading...</span>
    </h1>
    <div class="sub">Starlink In-Cab Control &bull; Whitelisted HT: <b>N0CALL-7</b></div>
    <div class="grid">
      <div class="stat">
        <div class="stat-label">Control Mode</div>
        <div class="stat-val" id="modeVal">-</div>
      </div>
      <div class="stat">
        <div class="stat-label">KISS Port 8001</div>
        <div class="stat-val" id="kissVal">-</div>
      </div>
      <div class="stat">
        <div class="stat-label">Repeated RF (15m)</div>
        <div class="stat-val" id="repVal">-</div>
      </div>
      <div class="stat">
        <div class="stat-label">Direct RF (15m)</div>
        <div class="stat-val" id="dirVal">-</div>
      </div>
    </div>
    <div class="event" id="timerInfo"></div>
    <div class="event" id="lastEvent"></div>
  </div>

  <div class="card">
    <div class="sub" style="margin-bottom:10px;">Tap to Switch Operational Mode</div>
    <div class="btn-group">
      <button id="btnAuto" onclick="setMode('auto')">AUTO (RF Traffic Sensor)</button>
      <button id="btnIgate" onclick="setMode('igate')">FORCE Profile A: iGate Only</button>
      <button id="btnDigi" onclick="setMode('digi')">FORCE Profile B: Trail Digi + iGate</button>
    </div>
  </div>

<script>
async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    const badge = document.getElementById('profileBadge');
    if (s.active_profile === 'digi') {
      badge.textContent = 'DIGI + iGATE';
      badge.className = 'badge badge-digi';
    } else {
      badge.textContent = 'iGATE ONLY';
      badge.className = 'badge badge-igate';
    }
    document.getElementById('modeVal').textContent = s.mode;
    document.getElementById('kissVal').textContent = s.kiss_connected ? 'Connected' : 'Reconnecting';
    document.getElementById('repVal').textContent = s.repeated_rf_packets + ' pkts';
    document.getElementById('dirVal').textContent = s.direct_rf_packets + ' pkts';

    let tInfo = `Auto thresholds: <=${s.quiet_threshold} rep/15m -> Digi | >=${s.busy_threshold} rep/15m -> iGate`;
    if (s.warmup_remaining_sec > 0) tInfo += ` (Warmup: ${s.warmup_remaining_sec}s)`;
    else if (s.cooldown_remaining_sec > 0) tInfo += ` (Cooldown: ${s.cooldown_remaining_sec}s)`;
    document.getElementById('timerInfo').textContent = tInfo;
    document.getElementById('lastEvent').textContent = 'Last: ' + s.last_event;

    document.getElementById('btnAuto').className = s.mode === 'AUTO' ? 'active-auto' : '';
    document.getElementById('btnIgate').className = s.mode === 'FORCE_IGATE' ? 'active-igate' : '';
    document.getElementById('btnDigi').className = s.mode === 'FORCE_DIGI' ? 'active-digi' : '';
  } catch (e) {}
}

async function setMode(m) {
  await fetch('/api/mode/' + m, { method: 'POST' });
  await refreshStatus();
}

refreshStatus();
setInterval(refreshStatus, 3000);
</script>
</body>
</html>
"""


def make_http_handler(controller: StationController):
    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Keep journalctl clean from periodic HTTP polling

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_mode_route(self, path: str) -> bool:
            if path in ("/api/mode/auto", "/mode/auto"):
                controller.set_mode("AUTO", "Web UI")
                self._send_json(controller.get_metrics())
                return True
            if path in ("/api/mode/igate", "/mode/igate"):
                controller.set_mode("FORCE_IGATE", "Web UI")
                self._send_json(controller.get_metrics())
                return True
            if path in ("/api/mode/digi", "/mode/digi"):
                controller.set_mode("FORCE_DIGI", "Web UI")
                self._send_json(controller.get_metrics())
                return True
            return False

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                body = HTML_DASHBOARD.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path in ("/api/status", "/status"):
                self._send_json(controller.get_metrics())
            elif not self._handle_mode_route(path):
                self._send_json({"error": "Not found"}, 404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path.rstrip("/")
            if not self._handle_mode_route(path):
                self._send_json({"error": "Not found"}, 404)

    return DashboardHandler


# =============================================================================
#                                MAIN ENTRYPOINT
# =============================================================================
def main() -> None:
    logger.info("Starting Direwolf Smart Profile Daemon (Web UI + APRS RF + Auto Density)...")
    controller = StationController()
    stop_event = threading.Event()

    # 1. Always initialize into Profile A (iGate Only) on boot, in AUTO mode
    controller.apply_profile("igate", "Boot Default (iGate Only)", restart_service=False)

    # 2. Start KISS TCP client thread (handles Method 3 RF Commands + Method 6 RF Density)
    kiss_thread = threading.Thread(
        target=kiss_client_worker,
        args=(controller, stop_event),
        daemon=True,
    )
    kiss_thread.start()

    # 3. Start In-Cab Web UI & API server thread (Method 1)
    httpd = ThreadingHTTPServer((HTTP_BIND_HOST, HTTP_PORT), make_http_handler(controller))
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    logger.info("Web UI & Shortcut API listening on http://%s:%d", HTTP_BIND_HOST, HTTP_PORT)

    # 4. Graceful shutdown on SIGTERM / SIGINT
    def shutdown_handler(signum, _frame):
        logger.info("Received signal %d; shutting down cleanly...", signum)
        stop_event.set()
        httpd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # 5. Periodic Auto RF Density evaluation loop
    while not stop_event.is_set():
        stop_event.wait(EVAL_INTERVAL_SECONDS)
        if not stop_event.is_set():
            controller.evaluate_auto_density()


if __name__ == "__main__":
    main()
