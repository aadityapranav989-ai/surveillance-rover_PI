import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import esp32  # noqa: E402


class DriveTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.drive_status = 200
        self.saved = esp32.esp32_request
        esp32.esp32_request = self.fake_request
        esp32._drive_missing_since = None

    def tearDown(self):
        esp32.esp32_request = self.saved
        esp32._drive_missing_since = None

    def fake_request(self, path, method="GET"):
        self.requests.append(path)
        if path.startswith("/api/drive"):
            return self.drive_status, b"", "application/json"
        return 200, b"{}", "application/json"

    def test_uses_drive_on_new_firmware(self):
        esp32.drive(150, 90, 600)
        self.assertEqual(self.requests, ["/api/drive?left=150&right=90&ms=600"])

    def test_falls_back_on_old_firmware_and_remembers(self):
        self.drive_status = 404
        esp32.drive(150, 150, 612)
        esp32.drive(150, 150, 612)
        self.assertEqual(self.requests[0], "/api/drive?left=150&right=150&ms=612")
        self.assertEqual(self.requests[1], "/api/command?direction=FORWARD&speed=150&value=40.0")
        self.assertEqual(self.requests[2], "/api/command?direction=FORWARD&speed=150&value=40.0",
                         "does not retry /api/drive on every command")

    def test_zero_speeds_send_stop(self):
        esp32.drive(0, 0, 300, esp32.AUTO)
        self.assertEqual(self.requests, ["/api/stop?source=auto"])

    def test_legacy_translation(self):
        self.assertEqual(esp32.legacy_command(150, 150, 612), ("FORWARD", 150, 40.0))
        self.assertEqual(esp32.legacy_command(-120, -120, 306), ("BACKWARD", 120, 20.0))
        self.assertEqual(esp32.legacy_command(120, -120, 570), ("RIGHT", 120, 50.0))
        self.assertEqual(esp32.legacy_command(-100, 100, 570), ("LEFT", 100, 50.0))
        self.assertEqual(esp32.legacy_command(180, 60, 612), ("FORWARD", 120, 40.0), "gentle curve: forward")


if __name__ == "__main__":
    unittest.main()
