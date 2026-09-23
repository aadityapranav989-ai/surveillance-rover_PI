import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerts import DETECTION, SAFE, AlertMonitor  # noqa: E402
from vision import Detection, VisionResult  # noqa: E402

UNKNOWN = Detection((10, 10, 40, 40), 0.9)
KNOWN = Detection((100, 10, 40, 40), 0.9, name="Asha")


def result(*faces):
    return VisionResult(1, 0.0, 640, 480, [], list(faces))


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class AlertMonitorTest(unittest.TestCase):
    def setUp(self):
        self.settings = os.path.join(tempfile.mkdtemp(), "settings.json")
        self.clock = FakeClock()
        self.monitor = AlertMonitor(self.settings, clear_after=2.0, clock=self.clock)

    def test_starts_in_safe_mode_and_never_alerts(self):
        self.assertEqual(self.monitor.mode, SAFE)
        self.monitor.update(result(UNKNOWN))
        self.assertFalse(self.monitor.status()["active"])

    def test_detection_mode_alerts_on_first_unknown_face(self):
        self.monitor.set_mode(DETECTION)
        self.monitor.update(result(UNKNOWN))
        status = self.monitor.status()
        self.assertTrue(status["active"])
        self.assertEqual(status["event"], 1)
        self.assertEqual(status["faces"], 1)

    def test_known_faces_do_not_alert(self):
        self.monitor.set_mode(DETECTION)
        self.monitor.update(result(KNOWN))
        self.assertFalse(self.monitor.status()["active"])

    def test_alert_clears_after_person_leaves(self):
        self.monitor.set_mode(DETECTION)
        self.monitor.update(result(UNKNOWN))
        self.clock.now += 1.0
        self.monitor.update(result())
        self.assertTrue(self.monitor.status()["active"], "brief gaps keep the alert")
        self.clock.now += 1.5
        self.monitor.update(result())
        self.assertFalse(self.monitor.status()["active"])

    def test_new_appearance_is_a_new_event(self):
        self.monitor.set_mode(DETECTION)
        self.monitor.update(result(UNKNOWN))
        self.monitor.update(result(UNKNOWN))
        self.assertEqual(self.monitor.status()["event"], 1, "same person, same event")
        self.clock.now += 3
        self.monitor.update(result())
        self.monitor.update(result(UNKNOWN))
        self.assertEqual(self.monitor.status()["event"], 2)

    def test_switching_to_safe_mode_clears_the_alert(self):
        self.monitor.set_mode(DETECTION)
        self.monitor.update(result(UNKNOWN))
        self.monitor.set_mode(SAFE)
        self.assertFalse(self.monitor.status()["active"])

    def test_mode_survives_restart(self):
        self.monitor.set_mode(DETECTION)
        restarted = AlertMonitor(self.settings, clock=self.clock)
        self.assertEqual(restarted.mode, DETECTION)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            self.monitor.set_mode("party")


if __name__ == "__main__":
    unittest.main()
