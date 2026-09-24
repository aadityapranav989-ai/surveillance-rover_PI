import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from follow import FollowController, FollowSettings, Steering, iou, select_target  # noqa: E402
from vision import Detection, VisionResult  # noqa: E402

SETTINGS = FollowSettings(min_speed=90, max_speed=150, turn_min_speed=85, turn_max_speed=130,
                          steer_gain=110, command_ms=600, center_enter=0.15, center_exit=0.06,
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

    def test_on_the_spot_turn_hysteresis(self):
        steering = steering_for(body_at(0.62, 0.85))  # 0.12 off: inside the enter band
        self.assertIsNone(steering.command())
        steering.offset = 0.2
        self.assertIsNotNone(steering.command())
        steering.offset = 0.1  # keeps turning until within center_exit
        self.assertIsNotNone(steering.command())
        steering.offset = 0.04
        self.assertIsNone(steering.command())

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
        self.saved = esp32.drive, esp32.send_stop, esp32.resume_autopilot
        esp32.drive = lambda left, right, ms, source: self.sent.append("DRIVE") or (200, b"", "")
        esp32.send_stop = lambda *args: self.sent.append("STOP") or (200, b"", "")
        esp32.resume_autopilot = lambda: (200, b"", "")
        self.vision = FakeVision()
        self.follow = FollowController(self.vision, SETTINGS, clock=lambda: self.now)
        self.follow.enable("")
        self.frame = 0

    def tearDown(self):
        self.esp32.drive, self.esp32.send_stop, self.esp32.resume_autopilot = self.saved

    def tick(self, persons, advance=0.25):
        self.now += advance
        self.frame += 1
        self.vision.latest = result(persons, frame_id=self.frame, timestamp=self.now)
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
        self.assertEqual(self.sent, ["DRIVE", "DRIVE", "DRIVE", "DRIVE", "STOP"])
        self.assertEqual(self.follow.state, "searching")

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
