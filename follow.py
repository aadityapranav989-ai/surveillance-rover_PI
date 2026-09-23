import threading
import time
from dataclasses import dataclass
from urllib.error import URLError

import esp32

VISION_STALE_AFTER = 1.0
OPERATOR_STOP = 423  # "Locked": the Pi gateway refuses follow commands after an operator STOP


@dataclass
class FollowSettings:
    min_speed: int
    max_speed: int
    turn_min_speed: int  # turning on the spot, used once close to the person
    turn_max_speed: int
    steer_gain: int  # wheel speed difference when the person is at the edge of the frame
    command_ms: int  # each command's run time; outlasts the interval so motion is continuous
    center_enter: float  # when close, start turning on the spot at this offset (fraction of width)
    center_exit: float  # ...and stop once back within this; also the steering dead zone
    stop_body_height: float
    stop_face_height: float
    resume_margin: float  # after stopping close to the target, drive again once it shrinks by this fraction
    smoothing: float  # 0..1 weight of each new detection; lower is smoother but slower to react
    max_speed_change: int  # largest speed change between consecutive commands
    lost_grace: float  # keep going this long when the target is briefly not detected
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


class Steering:
    """Turns noisy target boxes into smooth wheel speeds.

    While approaching, the rover drives in a curve: both sides move forward and
    the side away from the person runs faster, so it bends towards them in one
    motion. Once close enough it only turns on the spot to keep facing them.
    The target's position and size are smoothed across frames, stopping and
    turning use hysteresis, and forward speed changes gradually.
    """

    def __init__(self, settings):
        self.settings = settings
        self.reset()

    def reset(self):
        self.offset = None  # -0.5 (far left) .. 0.5 (far right)
        self.size = None  # target height as a fraction of the frame
        self.kind = None
        self.turning = False
        self.holding = False
        self.base = 0  # current forward speed

    def observe(self, box, kind, frame_width, frame_height):
        x, y, w, h = box
        offset = (x + w / 2) / frame_width - 0.5
        size = h / frame_height
        if self.offset is None or kind != self.kind:
            self.offset, self.size = offset, size
        else:
            weight = self.settings.smoothing
            self.offset = weight * offset + (1 - weight) * self.offset
            self.size = weight * size + (1 - weight) * self.size
        self.kind = kind

    def command(self):
        """Returns (left, right) wheel speeds, or None when the rover should hold still."""
        s = self.settings
        stop_height = s.stop_body_height if self.kind == "body" else s.stop_face_height
        limit = stop_height * (1 - s.resume_margin) if self.holding else stop_height
        self.holding = self.size >= limit
        if self.holding:
            self.base = 0
            return self._face_on_the_spot()
        self.turning = False

        closeness = min(1.0, self.size / stop_height)  # 0 = far away, 1 = close enough
        target = s.max_speed - (s.max_speed - s.min_speed) * closeness
        # Ease off when the person is far to one side, so the curve can be tighter.
        sharpness = min(1.0, max(0.0, abs(self.offset) - 0.2) / 0.3)
        base = self._ramp(target * (1 - 0.4 * sharpness))
        steer = 0.0 if abs(self.offset) < s.center_exit else s.steer_gain * self.offset / 0.5
        left = max(-255, min(255, round(base + steer)))
        right = max(-255, min(255, round(base - steer)))
        return left, right

    def _face_on_the_spot(self):
        s = self.settings
        limit = s.center_exit if self.turning else s.center_enter
        self.turning = abs(self.offset) > limit
        if not self.turning:
            return None
        strength = min(1.0, (abs(self.offset) - s.center_exit) / (0.5 - s.center_exit))
        speed = round(s.turn_min_speed + (s.turn_max_speed - s.turn_min_speed) * strength)
        return (speed, -speed) if self.offset > 0 else (-speed, speed)

    def _ramp(self, target):
        """Moves the forward speed gradually towards target, never below the speed that moves the rover."""
        s = self.settings
        if self.base == 0:
            speed = min(target, s.min_speed + s.max_speed_change)  # starting: begin gently
        else:
            speed = max(self.base - s.max_speed_change, min(self.base + s.max_speed_change, target))
        self.base = max(s.min_speed, round(speed))
        return self.base


class FollowController(threading.Thread):
    def __init__(self, vision, settings, clock=time.monotonic):
        super().__init__(daemon=True, name="follow")
        self.vision = vision
        self.settings = settings
        self.clock = clock
        self.steering = Steering(settings)
        self.enabled = False
        self.target_name = ""
        self.state = "off"
        self._box = None
        self._last_seen = 0.0
        self._last_frame = None
        self._visible = False  # target found in the latest vision result
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
            self._last_frame = None
            self._visible = False
            self.steering.reset()
            self.state = "searching"

    def disable(self, reason="off"):
        """Stops follow mode. Any in-flight follow command completes before this returns."""
        with self._lock:
            was_moving = self._moving
            self.enabled = False
            self._moving = False
            self._box = None
            self.steering.reset()
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
        now = self.clock()
        result = self.vision.latest
        if result is None or now - result.timestamp > VISION_STALE_AFTER:
            self._hold("waiting for camera")
            return
        if result.frame_id != self._last_frame:
            # Only new detections move the smoothed target; repeated ticks reuse it.
            self._last_frame = result.frame_id
            previous = self._box if now - self._last_seen < self.settings.track_memory else None
            target, kind = select_target(result, self.target_name, previous)
            self._visible = target is not None
            if target is not None:
                self._box = target.box
                self._last_seen = now
                self.vision.target_box = target.box
                self.steering.observe(target.box, kind, result.width, result.height)
        if now - self._last_seen > self.settings.lost_grace:
            # Lost for longer than a detection blip: stop and wait for the person.
            self.vision.target_box = None
            self.steering.reset()
            self._hold("searching")
            return
        if not self._visible:
            # Briefly not detected: let the current command run on instead of stopping.
            self.state = "tracking (target briefly hidden)"
            return
        command = self.steering.command()
        if command is None:
            self._hold("reached target")
            return
        left, right = command
        self.state = f"tracking (left {left} / right {right})"
        self._moving = True
        if self._send(esp32.drive, left, right, self.settings.command_ms, esp32.AUTO) == OPERATOR_STOP:
            # Someone pressed STOP or drove manually on the Pi dashboard.
            self.enabled = False
            self._moving = False
            self._box = None
            self.steering.reset()
            self.vision.target_box = None
            self.state = "stopped from the Pi dashboard"

    def _hold(self, state):
        self.state = state
        if self._moving:
            self._moving = False
            self._send(esp32.send_stop, esp32.AUTO)
