import threading
import time
from dataclasses import dataclass
from urllib.error import URLError

import esp32

OPERATOR_STOP = 423  # "Locked": the Pi gateway refuses follow commands after an operator STOP


@dataclass
class FollowSettings:
    min_speed: int
    max_speed: int
    turn_min_speed: int  # turning on the spot, used once close to the person
    turn_max_speed: int
    full_steer_offset: float  # offset (fraction of width) at which the curve is at its tightest
    command_ms: int  # each command's run time; outlasts the interval so motion is continuous
    center_enter: float  # when close, turn to face the person once they are this far off-center
    center_exit: float  # steering dead zone while approaching
    stop_body_height: float
    stop_face_height: float
    resume_margin: float  # after stopping close to the target, drive again once it shrinks by this fraction
    smoothing: float  # 0..1 weight of each new detection; lower is smoother but slower to react
    max_speed_change: int  # largest speed change between consecutive commands
    lost_grace: float  # keep driving on the last steering this long when the target is briefly not detected
    vision_timeout: float  # stop if the newest detection result is older than this
    track_memory: float
    interval: float
    lead: float = 0.4  # steer by where the person will be this many seconds ahead (vision is late)
    aim_offset: float = 0.35  # further off-center than this, stop and turn to face them first
    pulse_min_ms: int = 200  # shortest and longest turn-on-the-spot burst
    pulse_max_ms: int = 700
    ramp_ms: int = 250  # the ESP32's MOTOR_RAMP_MS: how long the motors take to spin down
    settle: float = 0.2  # after a burst, wait this long before trusting a new frame
    search_edge: float = 0.25  # a person last seen further off-center than this left the picture that way
    search_bursts: int = 3  # turn bursts towards that side before giving up


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

    While approaching, the rover drives in a curve: the side away from the person
    runs faster, so it bends towards them in one motion. The curve is steered by
    where the person is about to be, not where they were: vision sees each frame
    a fraction of a second late, and without this look-ahead the rover would keep
    turning after it already faces the person, then swing back (weaving).

    Turning on the spot needs full power on this rover, which spins it too fast
    to steer by a late camera. So those turns are short bursts ("aim"): turn,
    stop, look at a fresh frame, and turn again only if still needed. How far a
    burst turns depends on the floor, battery and load, so the rover learns it
    from each burst (how far the person moved in the picture) and sizes the next
    one to match.
    """

    # Starting guess for how far a burst turns: fraction of the picture per ms of burst.
    # Deliberately high, so the first bursts are short and undershoot rather than spin
    # past the person; learning then lengthens them.
    TURN_GAIN_START = 0.0015
    TURN_GAIN_RANGE = (0.0001, 0.01)

    def __init__(self, settings):
        self.settings = settings
        self.reset()

    def reset(self):
        self.offset = None  # -0.5 (far left) .. 0.5 (far right), smoothed
        self.size = None  # target height as a fraction of the frame, smoothed
        self.velocity = 0.0  # how fast the offset is changing, per second
        self.kind = None
        self.holding = False
        self.base = 0  # current forward speed
        self.pulse_ms = None  # set when the last command was a turn-on-the-spot burst
        self._raw = None  # (offset, timestamp) of the last detection, for the velocity
        self._aimed = None  # (offset before, direction, ms) of the last aiming burst, to learn from
        if not hasattr(self, "turn_gain"):
            self.turn_gain = self.TURN_GAIN_START  # kept across resets: it describes the rover

    def learn(self, offset_after):
        """Updates how far a burst turns, from where the person is after the last aiming burst."""
        before, direction, ms = self._aimed
        self._aimed = None
        turned = (before - offset_after) * direction  # positive: turned towards the person
        if turned > 0.03:
            self.turn_gain = 0.5 * self.turn_gain + 0.5 * turned / ms
        else:
            self.turn_gain *= 0.7  # hardly turned: make the next burst longer
        self.turn_gain = min(max(self.turn_gain, self.TURN_GAIN_RANGE[0]), self.TURN_GAIN_RANGE[1])

    def learn_lost(self):
        """The person left the picture during an aiming burst: it turned right past them.

        Returns the side to look (+1 right, -1 left), or 0 if the last burst was not aiming.
        """
        if self._aimed is None:
            return 0
        before, direction, ms = self._aimed
        self._aimed = None
        # It turned at least past the edge of the picture on the other side.
        self.turn_gain = min(max(self.turn_gain * 1.5, (abs(before) + 0.5) / ms), self.TURN_GAIN_RANGE[1])
        return -direction
    def observe(self, box, kind, frame_width, frame_height, timestamp=None, fresh=False):
        """Adds a detection. fresh=True drops the history, e.g. after the rover has turned."""
        x, y, w, h = box
        offset = (x + w / 2) / frame_width - 0.5
        size = h / frame_height
        if fresh and self._aimed is not None:
            self.learn(offset)
        if fresh or self.offset is None or kind != self.kind:
            self.offset, self.size, self.velocity = offset, size, 0.0
        else:
            weight = self.settings.smoothing
            if timestamp is not None and self._raw is not None and timestamp > self._raw[1]:
                speed = (offset - self._raw[0]) / (timestamp - self._raw[1])
                speed = max(-1.5, min(1.5, speed))  # one bad box must not throw the steering
                self.velocity = weight * speed + (1 - weight) * self.velocity
            self.offset = weight * offset + (1 - weight) * self.offset
            self.size = weight * size + (1 - weight) * self.size
        self._raw = (offset, timestamp) if timestamp is not None else None
        self.kind = kind

    def command(self):
        """Returns (left, right) wheel speeds, or None when the rover should hold still.

        For a turn on the spot, pulse_ms is the burst length; otherwise it is None.
        """
        s = self.settings
        self.pulse_ms = None
        stop_height = s.stop_body_height if self.kind == "body" else s.stop_face_height
        limit = stop_height * (1 - s.resume_margin) if self.holding else stop_height
        self.holding = self.size >= limit
        if self.holding:
            self.base = 0
            return self._aim() if abs(self.offset) > s.center_enter else None
        if abs(self.offset) > s.aim_offset:
            # Person at the edge of the picture: face them first, then drive.
            self.base = 0
            return self._aim()

        closeness = min(1.0, self.size / stop_height)  # 0 = far away, 1 = close enough
        target = s.max_speed - (s.max_speed - s.min_speed) * closeness
        # Where the person will be once this command takes effect.
        ahead = self.offset + s.lead * self.velocity
        # Ease off when the person is far to one side, so the curve can be tighter.
        sharpness = min(1.0, max(0.0, abs(ahead) - 0.2) / 0.3)
        base = self._ramp(target * (1 - 0.4 * sharpness))
        # Skid steering only turns with a large wheel-speed difference, so the side away
        # from the person ramps up to full turn power while the near side slows down.
        steer = 0.0
        if abs(ahead) >= s.center_exit:
            steer = min(1.0, (abs(ahead) - s.center_exit) / (s.full_steer_offset - s.center_exit))
        outer = round(base + (max(s.turn_max_speed, base) - base) * steer)
        inner = round(base * (1 - steer))
        return (outer, inner) if ahead > 0 else (inner, outer)

    def _aim(self):
        """A burst of turning on the spot, sized to bring the person most of the way to the center."""
        s = self.settings
        strength = min(1.0, max(0.0, abs(self.offset) - s.center_enter) / (0.5 - s.center_enter))
        speed = round(s.turn_min_speed + (s.turn_max_speed - s.turn_min_speed) * strength)
        direction = 1 if self.offset > 0 else -1
        # Aim for 80% of the way: a little short is one more small burst, too far loses them.
        self.pulse_ms = self._burst_ms(0.8 * abs(self.offset))
        self._aimed = (self.offset, direction, self.pulse_ms)
        return (speed, -speed) if direction > 0 else (-speed, speed)

    def search(self, side):
        """A burst of turning towards side (+1 right, -1 left) to find a person who left the picture.

        Turns about half a picture, so each new view overlaps the last one.
        """
        s = self.settings
        self._aimed = None
        self.pulse_ms = self._burst_ms(0.5)
        return (s.turn_max_speed, -s.turn_max_speed) if side > 0 else (-s.turn_max_speed, s.turn_max_speed)

    def _burst_ms(self, turn):
        """Burst length expected to turn by `turn` (fraction of the picture width)."""
        s = self.settings
        return round(min(s.pulse_max_ms, max(s.pulse_min_ms, turn / self.turn_gain)))

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
        self._reset_track()
        self._moving = False
        self._lock = threading.Lock()

    def _reset_track(self):
        self._box = None
        self._last_seen = 0.0
        self._last_frame = None
        self._visible = False  # target found in the latest vision result
        self._settled_at = 0.0  # after a turn burst, only frames taken after this are used
        self._last_side = 0  # which edge the person was near when last seen (+1 right, -1 left)
        self._searches = 0  # search bursts since the person was lost

    def enable(self, target_name):
        # Starting follow mode is an explicit operator decision, so lift the
        # Pi gateway's operator-stop latch.
        self._send(esp32.resume_autopilot)
        with self._lock:
            self.enabled = True
            self.target_name = target_name
            self._reset_track()
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
        s = self.settings
        if now < self._settled_at:
            return  # a turn burst is running: wait for it, then look again
        result = self.vision.latest
        if result is None or now - result.timestamp > s.vision_timeout:
            self._hold("waiting for camera (vision too slow or stopped)")
            return
        after_burst = self.steering.pulse_ms is not None  # the smoothed position is from before the turn
        if after_burst:
            self._moving = False  # the burst has ended on the ESP32
        if result.frame_id != self._last_frame and result.timestamp >= self._settled_at:
            # Only new detections move the smoothed target; repeated ticks reuse it. Frames
            # taken while the rover was still turning are skipped: the person has moved in them.
            self._last_frame = result.frame_id
            previous = self._box if now - self._last_seen < s.track_memory else None
            target, kind = select_target(result, self.target_name, previous)
            self._visible = target is not None
            if target is not None:
                self._box = target.box
                self._last_seen = now
                self._searches = 0
                self.vision.target_box = target.box
                self.steering.observe(target.box, kind, result.width, result.height, result.timestamp,
                                      fresh=after_burst)
                after_burst = False
                edge = self.steering.offset
                self._last_side = (1 if edge > 0 else -1) if abs(edge) > s.search_edge else 0
        if now - self._last_seen > s.lost_grace:
            self._lost()
            return
        if after_burst:
            self.state = "aiming · looking for the person again"
            return
        # Detections flicker, so while the person is briefly not detected the rover keeps
        # driving on its last steering (and the yellow box stays) instead of stopping.
        command = self.steering.command()
        detail = self._describe()
        if command is None:
            self._hold(f"reached target · {detail}")
            return
        left, right = command
        if self.steering.pulse_ms is not None:
            self._burst(left, right, f"aiming · {detail} · turn {self.steering.pulse_ms} ms")
            return
        hidden = "" if self._visible else " · briefly hidden"
        self.state = f"tracking · {detail} · left {left} / right {right}{hidden}"
        self._drive(left, right, s.command_ms)

    def _lost(self):
        """The person has been gone longer than a detection blip."""
        self.vision.target_box = None
        overshot = self.steering.learn_lost()
        if overshot:
            # The last aiming burst turned right past them: look back the other way.
            self._last_side = overshot
            self._searches = 0
        if self._last_side and self._searches < self.settings.search_bursts:
            # They walked out of the side of the picture: turn that way to find them.
            self._searches += 1
            left, right = self.steering.search(self._last_side)
            side = "right" if self._last_side > 0 else "left"
            self._burst(left, right, f"searching {side} ({self._searches}/{self.settings.search_bursts})")
            return
        self._last_side = 0
        self.steering.reset()
        self._hold("searching")

    def _burst(self, left, right, state):
        """Turns on the spot for pulse_ms, then waits until the picture is steady again."""
        s = self.settings
        ms = self.steering.pulse_ms
        self.state = state
        if self._drive(left, right, ms):
            # The motors ramp down after the burst (MOTOR_RAMP_MS on the ESP32), then settle.
            self._settled_at = self.clock() + (ms + s.ramp_ms) / 1000 + s.settle
            # The person cannot be seen during the burst, so the lost-person timer starts after it.
            self._last_seen = max(self._last_seen, self._settled_at)

    def _drive(self, left, right, ms):
        self._moving = True
        if self._send(esp32.drive, left, right, ms, esp32.AUTO) == OPERATOR_STOP:
            # Someone pressed STOP or drove manually on the Pi dashboard.
            self.enabled = False
            self._moving = False
            self._box = None
            self.steering.reset()
            self.vision.target_box = None
            self.state = "stopped from the Pi dashboard"
            return False
        return True

    def _describe(self):
        """The numbers behind a decision, for tuning: target size, offset and vision rate."""
        offset = self.steering.offset
        side = "centered" if abs(offset) < 0.03 else f"{abs(offset) * 100:.0f}% {'right' if offset > 0 else 'left'}"
        fps = getattr(self.vision, "fps", 0) or 0
        return f"target {self.steering.size * 100:.0f}% tall, {side}, vision {fps:.1f} fps"

    def _hold(self, state):
        self.state = state
        if self._moving:
            self._moving = False
            # Ease to a stop rather than braking hard; the red STOP button still brakes at once.
            self._send(esp32.ease_stop, esp32.AUTO)
