import threading
import time
from dataclasses import dataclass
from urllib.error import URLError

import numpy as np

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
    aim_ahead: float = 1.0  # turn to where a walking person will be this many seconds later


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap_w = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_h = max(0, min(ay + ah, by + bh) - max(ay, by))
    overlap = overlap_w * overlap_h
    union = aw * ah + bw * bh - overlap
    return overlap / union if union else 0.0


SAME_PERSON = 0.6  # clothing similarity needed to accept someone as the picked person again
CLEAR_MARGIN = 0.1  # ...and by this much more than anyone else in view


def similarity(a, b):
    """How alike two clothing signatures are: 0 (nothing in common) to 1 (identical)."""
    if a is None or b is None:
        return 0.0
    return float(np.minimum(a, b).sum() / 2)  # histogram intersection; each half sums to 1


def pick_person(result, x, y):
    """The person at (x, y), fractions of the picture: the one whose box contains the
    point (the smallest, if boxes overlap), else the nearest within 10% of the width."""
    px, py = x * result.width, y * result.height
    inside = [p for p in result.persons
              if p.box[0] <= px <= p.box[0] + p.box[2] and p.box[1] <= py <= p.box[1] + p.box[3]]
    if inside:
        return min(inside, key=lambda p: p.area)

    def distance(p):
        bx, by, bw, bh = p.box
        return max(bx - px, 0, px - bx - bw) + max(by - py, 0, py - by - bh)
    near = [p for p in result.persons if distance(p) <= 0.1 * result.width]
    return min(near, key=distance) if near else None


def select_target(result, target_name, previous_box, signature=None, locked=False):
    """Picks the detection to follow. Returns (detection, kind) or (None, None).

    With a target name, only a person whose face was recognized as that name is
    accepted, or the body that continues the previous track (face turned away).
    Locked on a picked person, only the body that continues the track is accepted,
    or, once they were out of view, someone whose clothes clearly match theirs.
    Otherwise the previously tracked body is preferred, then the largest.
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
        overlapping = [p for p in persons if iou(p.box, previous_box) >= 0.3]
        if len(overlapping) > 1 and signature is not None:
            # People crossing paths: stay with the one dressed like the picked person.
            return max(overlapping, key=lambda p: similarity(p.signature, signature)), "body"
        if overlapping:
            return max(overlapping, key=lambda p: iou(p.box, previous_box)), "body"
    if locked:
        if signature is not None and persons:
            scores = sorted(((similarity(p.signature, signature), i) for i, p in enumerate(persons)), reverse=True)
            best, index = scores[0]
            runner_up = scores[1][0] if len(scores) > 1 else 0.0
            if best >= SAME_PERSON and best - runner_up >= CLEAR_MARGIN:
                return persons[index], "body"
        return None, None
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

    # Starting guess for how far a burst turns: fraction of the picture per ms of burst
    # beyond its dead time. Deliberately high, so the first bursts are short and undershoot
    # rather than spin past the person; learning then lengthens them.
    TURN_GAIN_START = 0.0025  # about 150 degrees per second
    # The motors ramp up over MOTOR_RAMP_MS and this rover only turns near full power,
    # so roughly the first 60% of the ramp does not turn it: a 400 ms burst turns
    # more than twice as far as a 200 ms one.
    DEAD_TIME_SHARE = 0.6
    MAX_AIM_TURN = 0.6  # never plan to turn further than this (fraction of the picture) at once
    # Walking speed is the trend over the newest WALK_PICTURES pictures; below WALK_THRESHOLD
    # (pictures per second) it is taken as the box wobbling, not walking.
    WALK_PICTURES = 4
    WALK_THRESHOLD = 0.15
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
        self._raw = None  # (offset, timestamp) of the last detection
        self._track = []  # recent (timestamp, offset) while the rover stood or drove straight, for the velocity
        self._aimed = None  # (offset, velocity, time) before the last aiming burst, and its direction and ms
        self._after_turn = None  # (offset, time) of the first steady picture after a burst
        if not hasattr(self, "turn_gain"):
            self.turn_gain = self.TURN_GAIN_START  # kept across resets: it describes the rover
            self.learned = False  # turn_gain has been measured, not just the starting guess

    def learn(self, offset_after, time_after=None):
        """Updates how far a burst turns, from where the person is after the last aiming burst."""
        before, _, time_before, direction, ms, learnable = self._aimed
        self._aimed = None
        if not learnable:
            return
        if time_after is not None and time_before is not None:
            before += self.walking() * (time_after - time_before)  # where they walked meanwhile
        turned = (before - offset_after) * direction  # positive: turned towards the person
        if turned > 0.03:
            # Trust a clear measurement more than the old value; the first one replaces the guess.
            weight = 0.5 if self.learned else 1.0
            self.turn_gain = (1 - weight) * self.turn_gain + weight * turned / self._turning_ms(ms)
            self.learned = True
        else:
            self.turn_gain *= 0.7  # hardly turned: make the next burst longer
        self.turn_gain = min(max(self.turn_gain, self.TURN_GAIN_RANGE[0]), self.TURN_GAIN_RANGE[1])

    def learn_lost(self):
        """The person left the picture during an aiming burst: usually it turned right past them.

        Returns (side, sure): the side to look (+1 right, -1 left), or 0 if the last burst
        was not aiming, and whether the guess is based on a measured turn.
        """
        if self._aimed is None:
            return 0, True
        sure = self.learned
        before, walking, _, direction, ms, learnable = self._aimed
        self._aimed = None
        # Where the person should be now if the burst turned as far as expected.
        expected = before + walking * self.settings.aim_ahead - direction * self.turn_gain * self._turning_ms(ms)
        sure = sure and learnable
        if expected * direction > 0.4 or (walking * direction > 0 and expected * direction > -0.3):
            # They were walking the way it turned and got ahead of it (a burst planned
            # from a late picture falls short of a walker more easily than it overshoots).
            return direction, sure
        if not sure and abs(walking) > 0.1:
            # The turn size is only a guess, but they were walking: look where they were going.
            return (1 if walking > 0 else -1), False
        # It turned at least past the edge of the picture on the other side.
        if sure:
            self.turn_gain = min(max(self.turn_gain * 1.5, (abs(before) + 0.5) / self._turning_ms(ms)),
                                 self.TURN_GAIN_RANGE[1])
        return -direction, sure
    def observe(self, box, kind, frame_width, frame_height, timestamp=None, fresh=False):
        """Adds a detection. fresh=True drops the history, e.g. after the rover has turned."""
        x, y, w, h = box
        offset = (x + w / 2) / frame_width - 0.5
        size = h / frame_height
        if self.offset is None or kind != self.kind:
            self.offset, self.size, self.velocity = offset, size, 0.0
            self._track = []
        elif fresh:
            # After a turn the old position is meaningless, but the person is still walking
            # the same way: keep the velocity (measured while the rover stood still).
            self.offset, self.size = offset, size
            self._track = []
            if self._aimed is not None:
                self._after_turn = (offset, timestamp)
        else:
            weight = self.settings.smoothing
            self._measure_walking(offset, timestamp)
            self.offset = weight * offset + (1 - weight) * self.offset
            self.size = weight * size + (1 - weight) * self.size
            if self._after_turn is not None and self._aimed is not None:
                # Second steady picture after a burst: the walking speed is up to date,
                # so the person's own movement can be told apart from the turn.
                self.learn(*self._after_turn)
            self._after_turn = None
        self._raw = (offset, timestamp) if timestamp is not None else None
        if timestamp is not None and not self._track:
            self._track = [(timestamp, offset)]
        self.kind = kind

    def _measure_walking(self, offset, timestamp):
        """Sideways speed from the trend over the last second of pictures.

        Detection boxes wobble by a few percent from frame to frame; a straight-line fit over
        several pictures tells a walking person from a wobbling box. With fewer than three
        pictures (just after a turn) the previous speed is kept.
        """
        if timestamp is None:
            return
        self._track = [(t, o) for t, o in self._track if timestamp - t <= 1.2 and t < timestamp]
        self._track = self._track[-(self.WALK_PICTURES - 1):]
        self._track.append((timestamp, offset))
        if len(self._track) < 3:
            return
        mean_t = sum(t for t, _ in self._track) / len(self._track)
        mean_o = sum(o for _, o in self._track) / len(self._track)
        spread = sum((t - mean_t) ** 2 for t, _ in self._track)
        if spread > 0:
            slope = sum((t - mean_t) * (o - mean_o) for t, o in self._track) / spread
            self.velocity = max(-1.5, min(1.5, slope))

    def command(self, age=0.0):
        """Returns (left, right) wheel speeds, or None when the rover should hold still.

        age is how old the newest picture is. For a turn on the spot, pulse_ms is the
        burst length; otherwise it is None.
        """
        s = self.settings
        self.pulse_ms = None
        stop_height = s.stop_body_height if self.kind == "body" else s.stop_face_height
        limit = stop_height * (1 - s.resume_margin) if self.holding else stop_height
        self.holding = self.size >= limit
        # Where the person will be after one aiming burst, if they keep walking. Turning
        # towards that, not where they are now, keeps a walking person in the centre
        # instead of always catching up once they are nearly out of the picture.
        walking = self.walking()
        if not self.holding and walking * self.offset < 0:
            # While driving, drifting towards the centre is mostly the rover's own curve.
            walking = 0.0
        predicted = max(-0.9, min(0.9, self.predict(age + s.aim_ahead, walking)))
        if self.holding:
            self.base = 0
            return self._aim(predicted) if abs(predicted) > s.center_enter else None
        # Someone walking across the picture is kept centred by turning, as when close;
        # someone standing is approached in a smooth curve unless they are at the edge.
        aim_limit = s.center_enter * 2 if walking else s.aim_offset
        if abs(predicted) > aim_limit:
            # Person at (or walking out of) the edge of the picture: face them first, then drive.
            # A burst that starts while driving first has to reverse one side, so it turns
            # less than one from standstill: do not learn the turn size from it.
            driving = self.base
            self.base = 0
            return self._aim(predicted, learn=driving == 0, reverse_from=driving)

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

    def walking(self):
        """The person's sideways speed (fraction of the picture per second), ignoring box jitter."""
        return self.velocity if abs(self.velocity) > self.WALK_THRESHOLD else 0.0

    def predict(self, seconds, walking=None):
        """Where the person will be `seconds` after their newest picture."""
        if walking is None:
            walking = self.walking()
        if walking == 0 or self._raw is None:
            return self.offset
        # The smoothed position lags a walking person; start from the newest one.
        return self._raw[0] + walking * seconds

    def exit_side(self):
        """The side (+1 right, -1 left) a person is leaving the picture on, or 0."""
        if abs(self.offset) > self.settings.search_edge:
            return 1 if self.offset > 0 else -1
        ahead = self.offset + self.walking()  # where they will be in a second
        return (1 if ahead > 0 else -1) if abs(ahead) > 0.5 else 0

    def _aim(self, target, learn=True, reverse_from=0):
        """A burst of turning on the spot, sized to bring target most of the way to the centre."""
        s = self.settings
        strength = min(1.0, max(0.0, abs(target) - s.center_enter) / (0.5 - s.center_enter))
        speed = round(s.turn_min_speed + (s.turn_max_speed - s.turn_min_speed) * strength)
        direction = 1 if target > 0 else -1
        # Aim for 80% of the way: a little short is one more small burst, too far loses them.
        self.pulse_ms = self._burst_ms(min(self.MAX_AIM_TURN, 0.8 * abs(target)))
        if reverse_from:
            # One side first has to ramp down from its driving speed before it turns backwards.
            s_max = s.pulse_max_ms + s.ramp_ms
            self.pulse_ms = min(s_max, round(self.pulse_ms + reverse_from / 255 * s.ramp_ms))
        time = self._raw[1] if self._raw is not None else None
        self._aimed = (self.offset, self.walking(), time, direction, self.pulse_ms, learn)
        return (speed, -speed) if direction > 0 else (-speed, speed)

    def search(self, side, extra=0.0):
        """A burst of turning towards side (+1 right, -1 left) to find a person who left the picture.

        Turns about half a picture, so each new view overlaps the last one, plus `extra`
        (fraction of the picture), e.g. to sweep back past where it already looked.
        """
        s = self.settings
        self._aimed = None
        # Half a picture, plus how far a walking person gets during the burst.
        self.pulse_ms = self._burst_ms(0.5 + extra + min(0.5, abs(self.walking()) * 2 * s.aim_ahead))
        return (s.turn_max_speed, -s.turn_max_speed) if side > 0 else (-s.turn_max_speed, s.turn_max_speed)

    def _dead_ms(self):
        return self.DEAD_TIME_SHARE * self.settings.ramp_ms

    def _turning_ms(self, ms):
        """The part of a burst that actually turns the rover."""
        return max(30.0, ms - self._dead_ms())

    def _burst_ms(self, turn):
        """Burst length expected to turn by `turn` (fraction of the picture width)."""
        s = self.settings
        return round(min(s.pulse_max_ms, max(s.pulse_min_ms, self._dead_ms() + turn / self.turn_gain)))

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
        self.picked = False  # following a person picked on the video, not anyone or a name
        self.signature = None  # the picked person's clothing colours
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
        self._misses = 0  # steady pictures without the person since the last burst
        self._seen_after_burst = 0  # steady pictures with the person since the last burst
        self._frame_time = 0.0  # when the newest picture of the person was taken
        self._sweep_back = False  # if the first search burst finds nobody, search the other way

    def enable(self, target_name):
        # Starting follow mode is an explicit operator decision, so lift the
        # Pi gateway's operator-stop latch.
        self._send(esp32.resume_autopilot)
        with self._lock:
            self.enabled = True
            self.target_name = target_name
            self.picked = False
            self.signature = None
            self._reset_track()
            self.steering.reset()
            self.state = "searching"

    def pick(self, person):
        """Follows this detected person (picked on the video) and nobody else.

        A recognized face also identifies them; otherwise they are kept apart from
        others by their track and clothing colours.
        """
        self._send(esp32.resume_autopilot)
        with self._lock:
            self.enabled = True
            self.target_name = person.name or ""
            self.picked = True
            self.signature = person.signature
            self._reset_track()
            self._box = person.box
            self._last_seen = self._frame_time = self.clock()
            self.vision.target_box = person.box
            self.steering.reset()
            self.state = "locked on"

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
        return {"enabled": self.enabled, "target": self.target_name, "picked": self.picked, "state": self.state}

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
        after_burst = self.steering.pulse_ms is not None  # waiting for steady pictures after a turn
        if after_burst:
            self._moving = False  # the burst has ended on the ESP32
        if result.frame_id != self._last_frame and result.timestamp >= self._settled_at:
            # Only new detections move the smoothed target; repeated ticks reuse it. Frames
            # taken while the rover was still turning are skipped: the person has moved in them.
            self._last_frame = result.frame_id
            previous = self._box if now - self._last_seen < s.track_memory else None
            target, kind = select_target(result, self.target_name, previous, self.signature, self.picked)
            self._visible = target is not None
            if target is None and after_burst:
                self._misses += 1
            if target is not None:
                self._misses = 0
                self._learn_appearance(target, result)
                self._box = target.box
                self._last_seen = now
                self._frame_time = result.timestamp
                self._searches = 0
                self._sweep_back = False
                self.vision.target_box = target.box
                self.steering.observe(target.box, kind, result.width, result.height, result.timestamp,
                                      fresh=after_burst and self._seen_after_burst == 0)
                if after_burst:
                    # Decide after two steady pictures: the second shows how fast they are walking.
                    self._seen_after_burst += 1
                    after_burst = self._seen_after_burst < 2
                self._last_side = self.steering.exit_side()
        if now - self._last_seen > s.lost_grace or self._misses >= 2:
            # Lost; after a turn, two steady pictures without them is enough to know.
            self._lost()
            return
        if after_burst:
            self.state = "aiming · looking for the person again"
            return
        if self.steering.offset is None:
            # Just picked on the video, and not in a new picture yet.
            self._hold("locked on · looking for them")
            return
        # Detections flicker, so while the person is briefly not detected the rover keeps
        # driving on its last steering (and the yellow box stays) instead of stopping.
        command = self.steering.command(now - self._frame_time)
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

    def _learn_appearance(self, target, result):
        """Slowly follows the picked person's colours as the light changes, but only while
        nobody else is near them, so the signature never drifts onto someone else."""
        if self.signature is None or target.signature is None:
            return
        others = [p for p in result.persons if p is not target and iou(p.box, target.box) > 0]
        if not others and similarity(target.signature, self.signature) >= SAME_PERSON:
            self.signature = 0.9 * self.signature + 0.1 * target.signature

    def _lost(self):
        """The person has been gone longer than a detection blip."""
        self.vision.target_box = None
        guess, sure = self.steering.learn_lost()
        if guess:
            # Lost during an aiming burst: it turned past them, or they outpaced it.
            self._last_side = guess
            self._searches = 0
            self._sweep_back = not sure
        extra = 0.0
        if self._sweep_back and self._searches == 1:
            # The first guess was a guess (the turn size is not learned yet) and they were
            # not there: sweep back the other way, past where the rover started.
            self._sweep_back = False
            self._last_side = -self._last_side
            self._searches = 0
            extra = 1.0
        if self._last_side and self._searches < self.settings.search_bursts:
            # They walked out of the side of the picture: turn that way to find them.
            self._searches += 1
            left, right = self.steering.search(self._last_side, extra)
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
        self._misses = 0
        self._seen_after_burst = 0
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
