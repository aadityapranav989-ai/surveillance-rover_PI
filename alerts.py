import json
import os
import threading
import time

SAFE = "safe"
DETECTION = "detection"
MODES = (SAFE, DETECTION)


class AlertMonitor:
    """Unknown-person alerts with two modes.

    Safe mode: no alerts. Detection mode: an alert starts on the first vision
    result that contains an unrecognized face, and stays active until no
    unknown face has been seen for `clear_after` seconds. Each new appearance
    gets a new event number so the dashboard can sound once per person.
    The mode is saved to `settings_path` and survives restarts.
    """

    def __init__(self, settings_path, clear_after=2.0, clock=time.monotonic):
        self.settings_path = settings_path
        self.clear_after = clear_after
        self.clock = clock
        self._lock = threading.Lock()
        self.mode = self._load_mode()
        self.event = 0
        self._reset()

    def _reset(self):
        self.active = False
        self.faces = 0
        self._started = None
        self._last_seen = None

    def _load_mode(self):
        try:
            with open(self.settings_path, encoding="utf-8") as file:
                mode = json.load(file).get("alert_mode", SAFE)
            return mode if mode in MODES else SAFE
        except (OSError, ValueError):
            return SAFE

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        with self._lock:
            self.mode = mode
            self._reset()
        try:
            settings = {}
            if os.path.exists(self.settings_path):
                with open(self.settings_path, encoding="utf-8") as file:
                    settings = json.load(file)
            settings["alert_mode"] = mode
            with open(self.settings_path, "w", encoding="utf-8") as file:
                json.dump(settings, file)
        except (OSError, ValueError) as error:
            print(f"Alerts: could not save mode: {error}")
        print(f"Alerts: {mode} mode")

    def update(self, result):
        """Called with every vision result."""
        unknown = sum(1 for face in result.faces if face.name is None)
        now = self.clock()
        with self._lock:
            if self.mode != DETECTION:
                return
            if unknown:
                if not self.active:
                    self.active = True
                    self.event += 1
                    self._started = now
                    print(f"Alerts: unknown person detected ({unknown} unknown face{'s' if unknown != 1 else ''})")
                self.faces = unknown
                self._last_seen = now
            elif self.active and now - self._last_seen >= self.clear_after:
                self.active = False
                self.faces = 0

    def status(self):
        now = self.clock()
        with self._lock:
            return {
                "mode": self.mode,
                "active": self.active,
                "faces": self.faces,
                "event": self.event,
                # Ages instead of clock times: the Pi has no clock battery, so its time can be wrong.
                "since": round(now - self._started, 1) if self._started is not None else None,
            }
