import time
from urllib.error import HTTPError
from urllib.parse import urlencode
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


def drive(left, right, ms, source=None):
    """Drives with separate side speeds, falling back to straight/spin commands on older firmware."""
    global _drive_missing_since
    if left == 0 and right == 0:
        return send_stop(source)
    if _drive_missing_since is None or time.monotonic() - _drive_missing_since > DRIVE_RECHECK_SECONDS:
        status, body, content_type = send_drive(left, right, ms, source)
        if status != 404:
            _drive_missing_since = None
            return status, body, content_type
        _drive_missing_since = time.monotonic()
        print("ESP32 firmware has no /api/drive; using straight and spin commands (flash the new firmware for curves)")
    return send_command(*legacy_command(left, right, ms), source)


def show_lcd(line1, line2):
    """Shows two lines on the ESP32's 16x2 LCD."""
    return esp32_request("/api/lcd?" + urlencode({"line1": line1[:16], "line2": line2[:16]}), "POST")


def send_stop(source=None):
    return esp32_request("/api/stop" + ("?source=" + source if source else ""), "POST")


def resume_autopilot():
    """Re-allows follow-mode commands on the Pi gateway (a no-op 404 when talking to the ESP32 directly)."""
    return esp32_request("/api/autopilot/resume", "POST")
