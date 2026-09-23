import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerts import DETECTION, AlertMonitor  # noqa: E402
from display import lcd_lines  # noqa: E402
from vision import Detection, VisionResult  # noqa: E402


class FakeRfid:
    def __init__(self, last_tap=None, enrolling=None):
        self._state = {"last_tap": last_tap, "enrolling": enrolling}

    def state(self):
        return self._state


class FakeFollow:
    enabled = False
    target_name = ""


class FakeVision:
    def __init__(self, persons=0):
        self.persons = persons

    def status(self):
        return {"persons": [{}] * self.persons}


class LcdLinesTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.alerts = AlertMonitor(os.path.join(tempfile.mkdtemp(), "s.json"), clock=lambda: self.now)

    def lines(self, rfid=None, follow=None, vision=None, camera_online=True):
        return lcd_lines(self.alerts, rfid or FakeRfid(), follow or FakeFollow(), vision or FakeVision(), camera_online)

    def test_safe_mode(self):
        self.assertEqual(self.lines(), ("SAFE MODE", "Rover ready"))

    def test_countdown_then_intruder(self):
        self.alerts.set_mode(DETECTION)
        self.alerts.update(VisionResult(1, 0, 640, 480, [], [Detection((0, 0, 40, 40), 0.9)]))
        self.now += 3
        self.assertEqual(self.lines(), ("UNKNOWN PERSON", "Tap card: 7s"))
        self.now += 8
        self.assertEqual(self.lines(), ("!! INTRUDER !!", "Alert sent"))

    def test_card_taps_show_briefly(self):
        granted = FakeRfid(last_tap={"name": "Asha", "authorised": True, "enrolled": False, "age": 1.0})
        denied = FakeRfid(last_tap={"name": None, "authorised": False, "enrolled": False, "age": 1.0})
        old = FakeRfid(last_tap={"name": "Asha", "authorised": True, "enrolled": False, "age": 9.0})
        self.assertEqual(self.lines(rfid=granted), ("ACCESS GRANTED", "Asha"))
        self.assertEqual(self.lines(rfid=denied), ("ACCESS DENIED", "Unknown card"))
        self.assertEqual(self.lines(rfid=old), ("SAFE MODE", "Rover ready"))

    def test_enrolling(self):
        rfid = FakeRfid(enrolling={"name": "Ravi", "seconds_left": 14})
        self.assertEqual(self.lines(rfid=rfid), ("ADD CARD", "Tap card: 14s"))

    def test_detection_mode_idle_and_camera_offline(self):
        self.alerts.set_mode(DETECTION)
        self.assertEqual(self.lines(vision=FakeVision(2)), ("DETECTION MODE", "2 persons in view"))
        self.assertEqual(self.lines(camera_online=False), ("CAMERA OFFLINE", "Check webcam"))

    def test_lines_fit_the_display(self):
        for line in self.lines(rfid=FakeRfid(last_tap={"name": "Asha", "authorised": True, "enrolled": False, "age": 0})):
            self.assertLessEqual(len(line), 16)


if __name__ == "__main__":
    unittest.main()
