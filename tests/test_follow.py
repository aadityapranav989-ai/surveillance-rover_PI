import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from follow import FollowSettings, decide, iou, select_target  # noqa: E402
from vision import Detection, VisionResult  # noqa: E402

SETTINGS = FollowSettings(hfov_deg=60, min_speed=90, max_speed=150, turn_speed=110, step_cm=15,
                          max_turn_deg=25, center_tolerance=0.12, stop_body_height=0.8,
                          stop_face_height=0.25, track_memory=3, interval=0.3)


def result(persons=(), faces=()):
    return VisionResult(1, 0.0, 640, 480, list(persons), list(faces))


class DecideTest(unittest.TestCase):
    def test_turns_towards_person_on_the_right(self):
        direction, speed, degrees = decide((500, 100, 80, 200), "body", 640, 480, SETTINGS)
        self.assertEqual(direction, "RIGHT")
        self.assertEqual(speed, 110)
        self.assertAlmostEqual(degrees, (540 / 640 - 0.5) * 60, places=1)

    def test_turns_left_and_caps_turn_angle(self):
        direction, _, degrees = decide((0, 100, 20, 200), "body", 640, 480, SETTINGS)
        self.assertEqual(direction, "LEFT")
        self.assertEqual(degrees, 25)

    def test_drives_forward_faster_when_far(self):
        far = decide((300, 200, 40, 60), "body", 640, 480, SETTINGS)
        near = decide((260, 50, 120, 350), "body", 640, 480, SETTINGS)
        self.assertEqual(far[0], "FORWARD")
        self.assertEqual(near[0], "FORWARD")
        self.assertGreater(far[1], near[1])
        self.assertEqual(far[2], 15)

    def test_stops_when_close(self):
        self.assertIsNone(decide((200, 0, 240, 480), "body", 640, 480, SETTINGS))
        self.assertIsNone(decide((280, 100, 100, 130), "face", 640, 480, SETTINGS))


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
