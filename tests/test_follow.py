import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from follow import FollowController, FollowSettings, Steering, iou, select_target  # noqa: E402
from vision import Detection, VisionResult  # noqa: E402

SETTINGS = FollowSettings(min_speed=90, max_speed=150, turn_min_speed=255, turn_max_speed=255,
                          full_steer_offset=0.3, command_ms=600, center_enter=0.15, center_exit=0.06,
                          stop_body_height=0.8, stop_face_height=0.25, resume_margin=0.12,
                          smoothing=0.5, max_speed_change=20, lost_grace=0.8, vision_timeout=2.5, track_memory=3,
                          interval=0.25)
W, H = 640, 480


def result(persons=(), faces=(), frame_id=1, timestamp=0.0):
    return VisionResult(frame_id, timestamp, W, H, list(persons), list(faces))


def body_at(center_fraction, height_fraction):
    """A body box centered at center_fraction of the width and height_fraction of the frame tall."""
    h = int(H * height_fraction)
    w = 80
    return (int(W * center_fraction - w / 2), H - h, w, h)


def steering_for(box):
    steering = Steering(SETTINGS)
    steering.observe(box, "body", W, H)
    return steering


class SteeringTest(unittest.TestCase):
    def test_drives_straight_when_person_is_centered(self):
        left, right = steering_for(body_at(0.52, 0.4)).command()
        self.assertEqual(left, right, "inside the dead zone: no steering")
        self.assertGreater(left, 0)

    def test_curves_towards_person_off_center(self):
        left, right = steering_for(body_at(0.75, 0.4)).command()
        self.assertGreater(left, right, "person on the right: left side runs faster")
        self.assertGreater(right, 0, "still moving forward on both sides: a curve, not a spin")
        left, right = steering_for(body_at(0.25, 0.4)).command()
        self.assertGreater(right, left)

    def test_curve_uses_full_turn_power_on_the_outside(self):
        left, right = steering_for(body_at(0.85, 0.4)).command()  # 35% right: tightest curve
        self.assertEqual(left, 255, "the far side runs at full turn power")
        self.assertEqual(right, 0, "the near side stops, so the rover pivots towards the person")

    def test_on_the_spot_turns_use_full_power(self):
        left, right = steering_for(body_at(0.8, 0.85)).command()
        self.assertEqual((left, right), (255, -255))

    def test_curve_tightens_with_offset(self):
        gentle = steering_for(body_at(0.6, 0.4)).command()
        sharp = steering_for(body_at(0.9, 0.4)).command()
        self.assertGreater(sharp[0] - sharp[1], gentle[0] - gentle[1])

    def test_faster_when_far(self):
        far, near = steering_for(body_at(0.5, 0.2)), steering_for(body_at(0.5, 0.6))
        for _ in range(6):
            far_speeds, near_speeds = far.command(), near.command()
        self.assertGreater(far_speeds[0], near_speeds[0])

    def test_speed_ramps_up_gradually(self):
        steering = steering_for(body_at(0.5, 0.1))
        speeds = [steering.command()[0] for _ in range(5)]
        self.assertEqual(speeds[0], 110, "starts gently: minimum plus one step")
        self.assertTrue(all(b - a <= 20 for a, b in zip(speeds, speeds[1:])))
        self.assertEqual(speeds[-1], 142, "settles at the target speed for this distance")

    def test_close_and_centered_holds_still(self):
        self.assertIsNone(steering_for(body_at(0.5, 0.85)).command())

    def test_close_and_off_center_turns_on_the_spot(self):
        left, right = steering_for(body_at(0.8, 0.85)).command()
        self.assertEqual(left, -right)
        self.assertGreater(left, 0, "person on the right: spin right")

    def test_on_the_spot_turns_are_short_bursts(self):
        steering = steering_for(body_at(0.62, 0.85))  # 12% off: close enough to centered
        self.assertIsNone(steering.command())
        steering.offset = 0.2
        self.assertEqual(steering.command(), (255, -255))
        small = steering.pulse_ms
        steering.offset = -0.45
        self.assertEqual(steering.command(), (-255, 255))
        self.assertGreater(steering.pulse_ms, small, "further off-center: a longer burst")
        self.assertLessEqual(steering.pulse_ms, 600)

    def test_person_at_the_edge_is_faced_before_driving(self):
        steering = steering_for(body_at(0.92, 0.4))  # 42% right while approaching
        self.assertEqual(steering.command(), (255, -255), "turns on the spot instead of a wide pivot")
        self.assertIsNotNone(steering.pulse_ms)

    def test_driving_command_is_not_a_burst(self):
        steering = steering_for(body_at(0.7, 0.4))
        steering.command()
        self.assertIsNone(steering.pulse_ms)

    def test_steers_by_where_the_person_will_be(self):
        steady = Steering(SETTINGS)
        steady.observe(body_at(0.7, 0.4), "body", W, H, 0.0)
        steady.observe(body_at(0.7, 0.4), "body", W, H, 0.25)
        closing = Steering(SETTINGS)  # the rover is already turning towards them
        closing.observe(body_at(0.8, 0.4), "body", W, H, 0.0)
        closing.observe(body_at(0.6, 0.4), "body", W, H, 0.25)
        self.assertAlmostEqual(closing.offset, steady.offset, places=3, msg="same average position")
        steady_left, steady_right = steady.command()
        closing_left, closing_right = closing.command()
        self.assertLess(closing_left - closing_right, steady_left - steady_right,
                        "eases off the turn before it overshoots")
        leaving = Steering(SETTINGS)  # the person is walking off to the right
        leaving.observe(body_at(0.6, 0.4), "body", W, H, 0.0)
        leaving.observe(body_at(0.8, 0.4), "body", W, H, 0.25)
        leaving_left, leaving_right = leaving.command()
        self.assertGreater(leaving_left - leaving_right, steady_left - steady_right, "turns harder to keep up")

    def test_learns_how_far_a_burst_turns(self):
        steering = steering_for(body_at(0.85, 0.85))  # close, 35% right
        steering.command()
        first = steering.pulse_ms
        # The burst only moved the person from 35% to 30% right: bursts turn less than guessed.
        steering.observe(body_at(0.8, 0.85), "body", W, H, 1.0, fresh=True)
        steering.command()
        self.assertGreater(steering.pulse_ms, first, "the next burst is longer")
        # That one swung them from 30% right to 20% left: too far, so shorter next time.
        longer = steering.pulse_ms
        steering.observe(body_at(0.3, 0.85), "body", W, H, 2.0, fresh=True)
        self.assertEqual(steering.command(), (-255, 255), "turns back towards them")
        self.assertLess(steering.pulse_ms, longer)

    def test_learning_survives_losing_the_person(self):
        steering = steering_for(body_at(0.85, 0.85))
        steering.command()
        gain = steering.turn_gain
        steering.reset()
        self.assertEqual(steering.turn_gain, gain, "how the rover turns does not change when the person is lost")

    def test_fresh_observation_forgets_the_old_position(self):
        steering = steering_for(body_at(0.9, 0.4))
        steering.observe(body_at(0.5, 0.4), "body", W, H, 1.0, fresh=True)
        self.assertAlmostEqual(steering.offset, 0.0, places=2)
        self.assertEqual(steering.velocity, 0.0)

    def test_stop_distance_hysteresis(self):
        steering = steering_for(body_at(0.5, 0.85))
        self.assertIsNone(steering.command(), "close enough: hold")
        steering.size = 0.75  # stepped back a little: still holding
        self.assertIsNone(steering.command())
        steering.size = 0.65  # clearly further away: drive again
        self.assertIsNotNone(steering.command())

    def test_smooths_jittery_detections(self):
        steering = steering_for(body_at(0.5, 0.4))
        steering.observe(body_at(0.9, 0.4), "body", W, H)  # one jumpy frame
        self.assertAlmostEqual(steering.offset, 0.2, places=2, msg="moves halfway, not all the way")


class FakeVision:
    def __init__(self):
        self.latest = None
        self.target_box = None


class FollowControllerTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.now = 100.0
        import esp32
        self.esp32 = esp32
        self.commands = []
        self.saved = esp32.drive, esp32.send_stop, esp32.ease_stop, esp32.resume_autopilot
        esp32.drive = lambda left, right, ms, source: (self.sent.append("DRIVE"), self.commands.append(
            (left, right, ms))) and (200, b"", "")
        esp32.send_stop = lambda *args: self.sent.append("STOP") or (200, b"", "")
        esp32.ease_stop = lambda *args: self.sent.append("EASE") or (200, b"", "")
        esp32.resume_autopilot = lambda: (200, b"", "")
        self.vision = FakeVision()
        self.follow = FollowController(self.vision, SETTINGS, clock=lambda: self.now)
        self.follow.enable("")
        self.frame = 0

    def tearDown(self):
        self.esp32.drive, self.esp32.send_stop, self.esp32.ease_stop, self.esp32.resume_autopilot = self.saved

    def tick(self, persons, advance=0.25, age=0.0):
        """A new vision result, taken age seconds ago, then one follow step."""
        self.now += advance
        self.frame += 1
        self.vision.latest = result(persons, frame_id=self.frame, timestamp=self.now - age)
        self.follow._step()

    def test_brief_detection_gap_keeps_driving(self):
        person = Detection(body_at(0.5, 0.3), 0.9)
        self.tick([person])
        self.tick([])  # one missed frame: keeps driving on the last steering
        self.assertIn("briefly hidden", self.follow.state)
        self.assertIsNotNone(self.vision.target_box, "the yellow box stays")
        self.tick([person])
        self.assertEqual(self.sent, ["DRIVE", "DRIVE", "DRIVE"])

    def test_stops_once_person_is_really_gone(self):
        person = Detection(body_at(0.5, 0.3), 0.9)
        self.tick([person])
        for _ in range(4):  # 1 s without the person; the grace period is 0.8 s
            self.tick([])
        self.assertEqual(self.sent, ["DRIVE", "DRIVE", "DRIVE", "DRIVE", "EASE"], "eases to a stop")
        self.assertEqual(self.follow.state, "searching")

    def test_aims_in_bursts_and_looks_again(self):
        self.tick([Detection(body_at(0.85, 0.85), 0.9)])  # close, 35% right
        self.assertEqual(self.commands, [(255, -255, self.follow.steering.pulse_ms)])
        self.assertIn("aiming", self.follow.state)
        settled = self.follow._settled_at
        self.tick([Detection(body_at(0.85, 0.85), 0.9)], advance=0.1)  # burst still running
        self.assertEqual(len(self.commands), 1)
        # A frame taken while the rover was still turning shows the person in the old place: ignored.
        self.tick([Detection(body_at(0.85, 0.85), 0.9)], advance=settled - self.now + 0.05, age=0.3)
        self.assertEqual(len(self.commands), 1, "no second burst from a stale frame")
        self.tick([Detection(body_at(0.52, 0.85), 0.9)])  # steady picture: now facing the person
        self.assertEqual(len(self.commands), 1)
        self.assertIn("reached target", self.follow.state)
        self.assertNotIn("EASE", self.sent, "the burst already ended; nothing to stop")

    def test_turns_to_find_a_person_who_walked_out_of_the_side(self):
        self.tick([Detection(body_at(0.8, 0.3), 0.9)])  # 30% right, approaching
        for _ in range(4):
            self.tick([])
        self.assertEqual(self.commands[-1][:2], (255, -255), "turns right, where they were last seen")
        self.assertIn("searching right (1/3)", self.follow.state)
        for _ in range(30):
            self.tick([])
        bursts = [c for c in self.commands if c[:2] == (255, -255)]
        self.assertEqual(len(bursts), 3, "gives up after three search bursts")
        self.assertEqual(self.follow.state, "searching")
        self.tick([Detection(body_at(0.5, 0.3), 0.9)])  # found again
        self.assertIn("tracking", self.follow.state)

    def test_looks_back_after_turning_past_the_person(self):
        self.tick([Detection(body_at(0.85, 0.85), 0.9)])  # close, 35% right: aim right
        self.assertEqual(self.commands[-1][:2], (255, -255))
        for _ in range(12):  # the burst swung them out of the picture on the left
            self.tick([])
        self.assertEqual(self.commands[-1][:2], (-255, 255), "searches left, back the way it came")
        self.assertIn("searching left", self.follow.state)

    def test_status_shows_the_numbers(self):
        self.tick([Detection(body_at(0.7, 0.4), 0.9)])
        self.assertIn("target 40% tall, 20% right", self.follow.state)

    def test_slow_vision_is_tolerated(self):
        person = Detection(body_at(0.5, 0.3), 0.9)
        self.tick([person])
        self.now += 2.0  # vision result is 2 s old: still within the 2.5 s timeout
        self.follow._step()
        self.assertNotIn("waiting for camera", self.follow.state)

    def test_sends_every_tick_while_tracking(self):
        person = Detection(body_at(0.5, 0.3), 0.9)
        for _ in range(4):
            self.tick([person])
        self.assertEqual(self.sent, ["DRIVE"] * 4)


class SelectTargetTest(unittest.TestCase):
    def test_anyone_prefers_largest_person(self):
        small, big = Detection((0, 0, 50, 100), 0.9), Detection((300, 0, 100, 300), 0.9)
        self.assertIs(select_target(result([small, big]), "", None)[0], big)

    def test_anyone_keeps_previous_track(self):
        small, big = Detection((0, 0, 50, 100), 0.9), Detection((300, 0, 100, 300), 0.9)
        self.assertIs(select_target(result([small, big]), "", (2, 0, 50, 100))[0], small)

    def test_named_target_ignores_strangers(self):
        stranger = Detection((0, 0, 100, 300), 0.9)
        self.assertEqual(select_target(result([stranger]), "Asha", None), (None, None))

    def test_named_target_follows_recognized_body(self):
        stranger = Detection((0, 0, 200, 400), 0.9)
        asha = Detection((400, 0, 100, 300), 0.9, name="Asha")
        self.assertEqual(select_target(result([stranger, asha]), "Asha", None), (asha, "body"))

    def test_named_target_continues_track_when_face_turns_away(self):
        body = Detection((405, 5, 100, 300), 0.9)
        self.assertEqual(select_target(result([body]), "Asha", (400, 0, 100, 300)), (body, "body"))

    def test_named_target_falls_back_to_face(self):
        face = Detection((300, 50, 40, 40), 0.9, name="Asha")
        self.assertEqual(select_target(result([], [face]), "Asha", None), (face, "face"))

    def test_iou(self):
        self.assertEqual(iou((0, 0, 10, 10), (20, 20, 10, 10)), 0)
        self.assertAlmostEqual(iou((0, 0, 10, 10), (5, 0, 10, 10)), 50 / 150)


if __name__ == "__main__":
    unittest.main()
