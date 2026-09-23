import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerts import ALARM, CHECKING, CLEAR, DETECTION, GRANTED, SAFE, AlertMonitor  # noqa: E402
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
        self.monitor = AlertMonitor(self.settings, clear_after=2.0, auth_timeout=10.0, grant_seconds=120.0,
                                    clock=self.clock)

    def see(self, *faces, advance=0.0):
        self.clock.now += advance
        self.monitor.update(result(*faces))
        return self.monitor.status()

    def test_safe_mode_never_checks(self):
        self.assertEqual(self.monitor.mode, SAFE)
        self.assertEqual(self.see(UNKNOWN)["state"], CLEAR)
        self.monitor.card_tapped("Asha")
        self.assertEqual(self.monitor.status()["state"], CLEAR)

    def test_unknown_face_asks_for_a_card(self):
        self.monitor.set_mode(DETECTION)
        status = self.see(UNKNOWN)
        self.assertEqual(status["state"], CHECKING)
        self.assertEqual(status["seconds_left"], 10)
        self.assertFalse(status["active"], "no alarm yet")
        self.assertEqual(status["check"], 1)

    def test_no_card_within_timeout_raises_alarm(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.assertEqual(self.see(UNKNOWN, advance=9.5)["state"], CHECKING)
        status = self.see(UNKNOWN, advance=0.6)
        self.assertEqual(status["state"], ALARM)
        self.assertTrue(status["active"])
        self.assertEqual(status["event"], 1)

    def test_alarm_fires_even_if_vision_pauses(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.clock.now += 11
        self.assertEqual(self.monitor.status()["state"], ALARM)

    def test_authorised_card_within_timeout_grants_access(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.clock.now += 5
        self.monitor.card_tapped("Asha")
        status = self.see(UNKNOWN, advance=10)  # the same unknown face is now allowed
        self.assertEqual(status["state"], GRANTED)
        self.assertEqual(status["granted_name"], "Asha")
        self.assertEqual(status["event"], 0, "no alarm")

    def test_card_after_alarm_clears_it(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.see(UNKNOWN, advance=11)
        self.monitor.card_tapped("Asha")
        self.assertEqual(self.monitor.status()["state"], GRANTED)

    def test_unknown_card_changes_nothing(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.monitor.card_tapped(None)
        self.assertEqual(self.see(UNKNOWN, advance=11)["state"], ALARM)

    def test_grant_expires_and_checks_again(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.monitor.card_tapped("Asha")
        status = self.see(UNKNOWN, advance=121)
        self.assertEqual(status["state"], CHECKING)
        self.assertEqual(status["check"], 2)

    def test_person_leaving_during_countdown_cancels_the_check(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.assertEqual(self.see(advance=1.0)["state"], CHECKING, "brief gap keeps the check")
        self.assertEqual(self.see(advance=1.5)["state"], CLEAR)
        self.assertEqual(self.see(advance=10)["event"], 0, "no alarm")

    def test_alarm_clears_after_person_leaves(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.see(UNKNOWN, advance=11)
        self.assertEqual(self.see(advance=2.5)["state"], CLEAR)

    def test_known_faces_are_never_challenged(self):
        self.monitor.set_mode(DETECTION)
        self.assertEqual(self.see(KNOWN)["state"], CLEAR)

    def test_switching_to_safe_mode_clears_everything(self):
        self.monitor.set_mode(DETECTION)
        self.see(UNKNOWN)
        self.see(UNKNOWN, advance=11)
        self.monitor.set_mode(SAFE)
        self.assertEqual(self.monitor.status()["state"], CLEAR)

    def test_mode_survives_restart(self):
        self.monitor.set_mode(DETECTION)
        restarted = AlertMonitor(self.settings, clock=self.clock)
        self.assertEqual(restarted.mode, DETECTION)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            self.monitor.set_mode("party")


if __name__ == "__main__":
    unittest.main()
