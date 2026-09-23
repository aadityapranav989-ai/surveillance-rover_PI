import json
import os
import threading
import time

SAFE = "safe"
DETECTION = "detection"
MODES = (SAFE, DETECTION)

CLEAR = "clear"  # no unknown person in view
CHECKING = "checking"  # unknown person seen, waiting for an RFID card
ALARM = "alarm"  # no authorised card in time: intruder alert
GRANTED = "granted"  # an authorised card was tapped


class AlertMonitor:
    """Unknown-person checks with RFID authorisation.

    Safe mode: nothing happens. Detection mode: when an unrecognized face
    appears, the rover asks for an RFID card. An authorised card within
    `auth_timeout` seconds grants access for `grant_seconds`; otherwise the
    intruder alarm starts. The check ends once no unknown face has been seen
    for `clear_after` seconds. The mode is saved to `settings_path`.
    """

    def __init__(self, settings_path, clear_after=2.0, auth_timeout=10.0, grant_seconds=120.0,
                 clock=time.monotonic):
        self.settings_path = settings_path
        self.clear_after = clear_after
        self.auth_timeout = auth_timeout
        self.grant_seconds = grant_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self.mode = self._load_mode()
        self.event = 0  # counts alarms, so the dashboard can sound once per new alarm
        self.check = 0  # counts authorisation requests
        self.granted_name = None
        self._granted_until = None
        self._reset()

    def _reset(self):
        self.state = CLEAR
        self.faces = 0
        self._deadline = None
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

    def _granted(self, now):
        return self._granted_until is not None and now < self._granted_until

    def _advance(self, now):
        """Moves timed states along; called with the lock held."""
        if self.state == GRANTED and not self._granted(now):
            self._reset()
        if self.state == CHECKING and now >= self._deadline:
            self.state = ALARM
            self.event += 1
            print(f"Alerts: no authorised card within {self.auth_timeout:.0f} s: intruder alarm")

    def update(self, result):
        """Called with every vision result."""
        unknown = sum(1 for face in result.faces if face.name is None)
        now = self.clock()
        with self._lock:
            if self.mode != DETECTION:
                return
            self._advance(now)
            if self.state == GRANTED:
                return
            if unknown:
                self.faces = unknown
                self._last_seen = now
                if self.state == CLEAR:
                    self.state = CHECKING
                    self.check += 1
                    self._deadline = now + self.auth_timeout
                    print(f"Alerts: unknown person: waiting {self.auth_timeout:.0f} s for an RFID card")
            elif self.state in (CHECKING, ALARM) and now - self._last_seen >= self.clear_after:
                self._reset()

    def card_tapped(self, name):
        """Called by the RFID reader: name of the authorised card holder, or None for an unknown card."""
        if name is None:
            return
        now = self.clock()
        with self._lock:
            if self.mode != DETECTION:
                return  # safe mode: nothing to authorise (the LCD still shows the tap)
            self.granted_name = name
            self._granted_until = now + self.grant_seconds
            self._reset()
            self.state = GRANTED
        print(f"Alerts: access granted to {name} for {self.grant_seconds:.0f} s")

    def status(self):
        now = self.clock()
        with self._lock:
            self._advance(now)
            return {
                "mode": self.mode,
                "state": self.state,
                "active": self.state == ALARM,
                "faces": self.faces,
                "event": self.event,
                "check": self.check,
                "auth_timeout": self.auth_timeout,
                "seconds_left": max(0, round(self._deadline - now)) if self.state == CHECKING else None,
                "granted_name": self.granted_name if self.state == GRANTED else None,
                "granted_left": round(self._granted_until - now) if self.state == GRANTED else None,
            }
