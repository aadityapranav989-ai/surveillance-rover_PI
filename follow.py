import threading
import time
from dataclasses import dataclass
from urllib.error import URLError

import esp32

VISION_STALE_AFTER = 1.0
OPERATOR_STOP = 423  # "Locked": the Pi gateway refuses follow commands after an operator STOP


@dataclass
class FollowSettings:
    hfov_deg: float
    min_speed: int
    max_speed: int
    turn_speed: int
    step_cm: float
    max_turn_deg: float
    center_tolerance: float
    stop_body_height: float
    stop_face_height: float
    track_memory: float
    interval: float


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap_w = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_h = max(0, min(ay + ah, by + bh) - max(ay, by))
    overlap = overlap_w * overlap_h
    union = aw * ah + bw * bh - overlap
    return overlap / union if union else 0.0


def select_target(result, target_name, previous_box):
    """Picks the detection to follow. Returns (detection, kind) or (None, None).

    With a target name, only a person whose face was recognized as that name is
    accepted, or the body that continues the previous track (face turned away).
    Without a name, the previously tracked body is preferred, then the largest.
    """
    persons = result.persons
    if target_name:
        named = [p for p in persons if p.name == target_name]
        if named:
            return max(named, key=lambda d: d.area), "body"
        faces = [f for f in result.faces if f.name == target_name]
        if faces:
            return max(faces, key=lambda d: d.area), "face"
    if previous_box is not None and persons:
        best = max(persons, key=lambda p: iou(p.box, previous_box))
        if iou(best.box, previous_box) >= 0.3:
            return best, "body"
    if target_name:
        return None, None
    if persons:
        return max(persons, key=lambda d: d.area), "body"
    if result.faces:
        return max(result.faces, key=lambda d: d.area), "face"
    return None, None


def decide(box, kind, frame_width, frame_height, settings):
    """Returns the (direction, speed, value) command to move towards box, or None to hold still."""
    x, y, w, h = box
    offset = (x + w / 2) / frame_width - 0.5  # -0.5 (far left) .. 0.5 (far right)
    if abs(offset) > settings.center_tolerance:
        degrees = min(settings.max_turn_deg, max(3.0, abs(offset) * settings.hfov_deg))
        return ("RIGHT" if offset > 0 else "LEFT"), settings.turn_speed, round(degrees, 1)
    stop_height = settings.stop_body_height if kind == "body" else settings.stop_face_height
    size = h / frame_height
    if size >= stop_height:
        return None
    closeness = size / stop_height  # 0 = far away, 1 = close enough
    speed = round(settings.max_speed - (settings.max_speed - settings.min_speed) * closeness)
    return "FORWARD", speed, settings.step_cm


class FollowController(threading.Thread):
    def __init__(self, vision, settings):
        super().__init__(daemon=True, name="follow")
        self.vision = vision
        self.settings = settings
        self.enabled = False
        self.target_name = ""
        self.state = "off"
        self._box = None
        self._last_seen = 0.0
        self._moving = False
        self._lock = threading.Lock()

    def enable(self, target_name):
        # Starting follow mode is an explicit operator decision, so lift the
        # Pi gateway's operator-stop latch.
        self._send(esp32.resume_autopilot)
        with self._lock:
            self.enabled = True
            self.target_name = target_name
            self._box = None
            self._last_seen = 0.0
            self.state = "searching"

    def disable(self, reason="off"):
        """Stops follow mode. Any in-flight follow command completes before this returns."""
        with self._lock:
            was_moving = self._moving
            self.enabled = False
            self._moving = False
            self._box = None
            self.vision.target_box = None
            self.state = reason
        if was_moving:
            self._send(esp32.send_stop, esp32.AUTO)

    def status(self):
        return {"enabled": self.enabled, "target": self.target_name, "state": self.state}

    def _send(self, function, *args):
        """Returns the HTTP status, or None when the rover could not be reached."""
        try:
            status, body, _ = function(*args)
        except (URLError, OSError) as error:
            print(f"Follow: rover unavailable: {error}")
            return None
        if status >= 400 and status not in (404, OPERATOR_STOP):
            print(f"Follow: rover rejected {function.__name__}{args}: {status} {body[:200]!r}")
        return status

    def run(self):
        while True:
            time.sleep(self.settings.interval)
            with self._lock:
                if self.enabled:
                    self._step()

    def _step(self):
        now = time.monotonic()
        result = self.vision.latest
        if result is None or now - result.timestamp > VISION_STALE_AFTER:
            self._hold("waiting for camera")
            return
        previous = self._box if now - self._last_seen < self.settings.track_memory else None
        target, kind = select_target(result, self.target_name, previous)
        if target is None:
            self._box = previous
            self.vision.target_box = None
            self._hold("searching")
            return
        self._box = target.box
        self._last_seen = now
        self.vision.target_box = target.box
        command = decide(target.box, kind, result.width, result.height, self.settings)
        if command is None:
            self._hold("reached target")
            return
        direction, speed, value = command
        self.state = f"tracking ({direction.lower()} {value})"
        self._moving = True
        if self._send(esp32.send_command, direction, speed, value, esp32.AUTO) == OPERATOR_STOP:
            # Someone pressed STOP or drove manually on the Pi dashboard.
            self.enabled = False
            self._moving = False
            self._box = None
            self.vision.target_box = None
            self.state = "stopped from the Pi dashboard"

    def _hold(self, state):
        self.state = state
        if self._moving:
            self._moving = False
            self._send(esp32.send_stop, esp32.AUTO)
