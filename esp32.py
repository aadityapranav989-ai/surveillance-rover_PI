import json
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from config import ESP32_MS_PER_CM, ESP32_MS_PER_DEGREE, ESP32_URL, REQUEST_TIMEOUT

DIRECTIONS = {"FORWARD", "BACKWARD", "LEFT", "RIGHT"}
# Commands sent by follow mode carry source=auto so the Pi gateway can refuse
# them after an operator presses STOP or drives manually.
AUTO = "auto"


def esp32_request(path, method="GET"):
    """Returns (status, body, content_type). Raises URLError/OSError when the target is unreachable.

    ESP32_URL is the ESP32 itself on the Pi, or the Pi gateway on the laptop;
    both answer the same /api/status, /api/command and /api/stop endpoints.
    """
    request = Request(ESP32_URL + path, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read(), response.headers.get_content_type()
    except HTTPError as error:
        # The target answered with an error status; pass it through unchanged.
        return error.code, error.read(), error.headers.get_content_type()


def send_command(direction, speed, value, source=None):
    """value is centimeters for FORWARD/BACKWARD and degrees for LEFT/RIGHT."""
    query = {"direction": direction, "speed": speed, "value": value}
    if source:
        query["source"] = source
    return esp32_request("/api/command?" + urlencode(query), "POST")


def send_drive(left, right, ms, source=None):
    """Runs each side at its own speed (-255..255) for ms milliseconds; unequal speeds curve."""
    query = {"left": left, "right": right, "ms": ms}
    if source:
        query["source"] = source
    return esp32_request("/api/drive?" + urlencode(query), "POST")


# Firmware from before /api/drive answers 404. Remember that for a while so every
# command does not pay for a failed request, then check again in case it was reflashed.
_drive_missing_since = None
DRIVE_RECHECK_SECONDS = 30


def legacy_command(left, right, ms):
    """The closest straight-or-spin command for older firmware: (direction, speed, value)."""
    forward, turn = (left + right) / 2, (left - right) / 2
    if abs(turn) > abs(forward):
        return ("RIGHT" if turn > 0 else "LEFT"), max(1, round(abs(turn))), round(ms / ESP32_MS_PER_DEGREE, 1)
    return ("FORWARD" if forward >= 0 else "BACKWARD"), max(1, round(abs(forward))), round(ms / ESP32_MS_PER_CM, 1)


# Drive commands over UDP. Each HTTP command opens a new TCP connection, and on the
# rover's busy Wi-Fi a lost connection-setup packet delays it by a full second, so the
# previous pulse runs out and the rover jerks. A lost UDP packet costs nothing: the
# next command replaces it. Used only when ESP32_URL is the ESP32 itself and its
# firmware reports udpPort; a laptop talking to the Pi gateway keeps using HTTP.
UDP_RECHECK_SECONDS = 30
_esp32_host = urlparse(ESP32_URL).hostname
_udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_udp = {"port": None, "lcd": False, "checked": None, "refreshing": False}


def refresh_udp_port():
    """Asks the ESP32 whether it takes UDP commands; returns the port or None."""
    try:
        status, body, _ = esp32_request("/api/status")
        info = json.loads(body) if status == 200 else {}
    except (OSError, ValueError):
        info = {}
    # A Pi gateway proxies the ESP32's status too, so check the answer came from the ESP32 itself.
    port = info.get("udpPort") if info.get("wifi") == _esp32_host else None
    if port != _udp["port"]:
        print(f"ESP32 drive commands: {'UDP port ' + str(port) if port else 'HTTP'}")
    _udp.update(port=port, lcd=bool(port and info.get("udpLcd")), checked=time.monotonic(), refreshing=False)
    return port


def udp_port():
    """The cached UDP port (or None). Rechecks in the background so no command waits for it."""
    stale = _udp["checked"] is None or time.monotonic() - _udp["checked"] > UDP_RECHECK_SECONDS
    if stale and not _udp["refreshing"]:
        _udp["refreshing"] = True
        threading.Thread(target=refresh_udp_port, daemon=True, name="udp-check").start()
    return _udp["port"]


def send_udp(text, port):
    _udp_socket.sendto(text.encode("ascii"), (_esp32_host, port))


def drive(left, right, ms, source=None):
    """Drives with separate side speeds: UDP when available, else HTTP, else straight/spin commands."""
    global _drive_missing_since
    if left == 0 and right == 0:
        return send_stop(source)
    port = udp_port()
    if port:
        try:
            send_udp(f"DRIVE {left} {right} {ms}", port)
            return 200, b'{"ok":true,"via":"udp"}', "application/json"
        except OSError as error:
            print(f"ESP32 UDP send failed ({error}); using HTTP")
    if _drive_missing_since is None or time.monotonic() - _drive_missing_since > DRIVE_RECHECK_SECONDS:
        status, body, content_type = send_drive(left, right, ms, source)
        if status != 404:
            _drive_missing_since = None
            return status, body, content_type
        _drive_missing_since = time.monotonic()
        print("ESP32 firmware has no /api/drive; using straight and spin commands (flash the new firmware for curves)")
    return send_command(*legacy_command(left, right, ms), source)


def ease_stop(source=None):
    """Stops the rover gently: the motors ramp down (MOTOR_RAMP_MS) instead of braking at once.

    Falls back to a normal STOP when UDP is not available. If the packet is lost, the
    last command still ends by itself within FOLLOW_COMMAND_MS.
    """
    port = udp_port()
    if port:
        try:
            send_udp("DRIVE 0 0 1", port)
            return 200, b'{"ok":true,"via":"udp"}', "application/json"
        except OSError:
            pass
    return send_stop(source)


def show_lcd(line1, line2):
    """Shows two lines on the ESP32's 16x2 LCD: over UDP when the firmware supports it."""
    port = udp_port()
    if port and _udp["lcd"]:
        try:
            clean = [line[:16].replace("|", "/") for line in (line1, line2)]
            send_udp(f"LCD {clean[0]}|{clean[1]}", port)
            return 200, b'{"ok":true,"via":"udp"}', "application/json"
        except OSError:
            pass
    return esp32_request("/api/lcd?" + urlencode({"line1": line1[:16], "line2": line2[:16]}), "POST")


class StatusCache(threading.Thread):
    """Fetches the ESP32's status (GPS etc.) once a second and shares it with every viewer.

    Each open dashboard used to ask the ESP32 itself every second; on a congested Wi-Fi
    those extra requests slowed the ESP32 down. Now it gets one request per second.
    """

    def __init__(self, interval=1.0, max_age=5.0):
        super().__init__(daemon=True, name="esp32-status")
        self.interval = interval
        self.max_age = max_age
        self._latest = None  # (status, body, content_type, time)

    def run(self):
        while True:
            try:
                self._latest = (*esp32_request("/api/status"), time.monotonic())
            except OSError:
                pass  # keep the last answer until it is too old
            time.sleep(self.interval)

    def get(self):
        """(status, body, content_type), or None when there is no recent answer."""
        latest = self._latest
        if latest is None or time.monotonic() - latest[3] > self.max_age:
            return None
        return latest[:3]


def send_stop(source=None):
    """Stops the rover. Sent over UDP first (fastest), then over HTTP (confirmed delivery)."""
    port = udp_port()
    if port:
        try:
            send_udp("STOP", port)
        except OSError:
            pass
    return esp32_request("/api/stop" + ("?source=" + source if source else ""), "POST")


def resume_autopilot():
    """Re-allows follow-mode commands on the Pi gateway (a no-op 404 when talking to the ESP32 directly)."""
    return esp32_request("/api/autopilot/resume", "POST")
