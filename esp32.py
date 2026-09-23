from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import ESP32_URL, REQUEST_TIMEOUT

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


def send_stop(source=None):
    return esp32_request("/api/stop" + ("?source=" + source if source else ""), "POST")


def resume_autopilot():
    """Re-allows follow-mode commands on the Pi gateway (a no-op 404 when talking to the ESP32 directly)."""
    return esp32_request("/api/autopilot/resume", "POST")
