import threading
import time
from urllib.error import URLError

import esp32
from alerts import ALARM, CHECKING, DETECTION, GRANTED

TAP_MESSAGE_SECONDS = 3
WELCOME_HOLD_SECONDS = 3


class RecentNames:
    """Known people seen in the last few seconds, so a missed frame does not blank the welcome."""

    def __init__(self, hold=WELCOME_HOLD_SECONDS, clock=time.monotonic):
        self.hold = hold
        self.clock = clock
        self._last_seen = {}

    def update(self, faces):
        """Takes the vision status' face list; returns names seen recently, most recent first."""
        now = self.clock()
        for face in faces:
            if face.get("name"):
                self._last_seen[face["name"]] = now
        self._last_seen = {name: t for name, t in self._last_seen.items() if now - t <= self.hold}
        return sorted(self._last_seen, key=self._last_seen.get, reverse=True)


def welcome_line(names):
    """One LCD line of names: "Asha", "Asha, Ravi", or "Asha +2" when they do not fit."""
    text = ", ".join(names)
    return text if len(text) <= 16 else f"{names[0][:12]} +{len(names) - 1}"


def lcd_lines(alerts, rfid, follow, vision, camera_online, known_names=()):
    """The two 16-character lines the ESP32's LCD should show, most urgent first."""
    a = alerts.status()
    tap = rfid.state()["last_tap"] if rfid else None
    enrolling = rfid.state()["enrolling"] if rfid else None
    if tap and tap["age"] < TAP_MESSAGE_SECONDS:
        if tap.get("enrolled"):
            return "CARD ADDED", tap["name"]
        return ("ACCESS GRANTED", tap["name"]) if tap["authorised"] else ("ACCESS DENIED", "Unknown card")
    if enrolling:
        return "ADD CARD", f"Tap card: {enrolling['seconds_left']}s"
    if a["mode"] == DETECTION:
        if a["state"] == ALARM:
            return "!! INTRUDER !!", "Alert sent"
        if a["state"] == CHECKING:
            return "UNKNOWN PERSON", f"Tap card: {a['seconds_left']}s"
    if known_names:
        return "WELCOME", welcome_line(list(known_names))
    if a["mode"] == DETECTION:
        if a["state"] == GRANTED:
            return "AUTHORISED", a["granted_name"]
    if follow and follow.enabled:
        return "FOLLOWING", follow.target_name or "Nearest person"
    if not camera_online:
        return "CAMERA OFFLINE", "Check webcam"
    if a["mode"] == DETECTION:
        people = len(vision.status()["persons"])
        return "DETECTION MODE", f"{people} person{'s' if people != 1 else ''} in view" if people else "Watching"
    return "SAFE MODE", "Rover ready"


class LcdDisplay(threading.Thread):
    """Keeps the ESP32's 16x2 LCD in step with the Pi's security state.

    Sends only when the text changes, plus a refresh every `keepalive`
    seconds; if these stop, the ESP32 shows its own status after 10 s.
    """

    def __init__(self, lines, interval=0.5, keepalive=5.0):
        super().__init__(daemon=True, name="lcd")
        self.lines = lines
        self.interval = interval
        self.keepalive = keepalive
        self._shown = None
        self._sent_at = 0.0
        self._paused_until = 0.0

    def run(self):
        while True:
            time.sleep(self.interval)
            now = time.monotonic()
            if now < self._paused_until:
                continue
            try:
                lines = self.lines()
            except Exception as error:  # never let a display bug stop the thread
                print(f"LCD: {error}")
                continue
            if lines == self._shown and now - self._sent_at < self.keepalive:
                continue
            try:
                status, _, _ = esp32.show_lcd(*lines)
            except (URLError, OSError):
                continue  # rover unreachable; try again next time
            if status == 404:
                print("LCD: ESP32 firmware has no /api/lcd yet; flash the latest firmware")
                self._paused_until = now + 60
                continue
            self._shown, self._sent_at = lines, now
