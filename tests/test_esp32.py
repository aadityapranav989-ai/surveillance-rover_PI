import json
import os
import sys
import time
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
        esp32._udp.update(port=None, checked=time.monotonic(), refreshing=False)  # HTTP only

    def tearDown(self):
        esp32.esp32_request = self.saved
        esp32._drive_missing_since = None
        esp32._udp.update(port=None, checked=None, refreshing=False)

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


class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendto(self, data, address):
        self.sent.append((data.decode(), address))


class UdpTest(unittest.TestCase):
    def setUp(self):
        self.http = []
        self.delay = 0.0
        self.status = {"wifi": esp32._esp32_host, "udpPort": 4210}
        self.saved = esp32.esp32_request, esp32._udp_socket
        esp32.esp32_request = self.fake_request
        esp32._udp_socket = self.socket = FakeSocket()
        esp32._udp.update(port=None, checked=None, refreshing=False)

    def tearDown(self):
        esp32.esp32_request, esp32._udp_socket = self.saved
        esp32._udp.update(port=None, checked=None, refreshing=False)

    def fake_request(self, path, method="GET"):
        self.http.append(path)
        if path == "/api/status":
            time.sleep(self.delay)
            return 200, json.dumps(self.status).encode(), "application/json"
        return 200, b"{}", "application/json"

    def test_drives_over_udp_when_firmware_supports_it(self):
        self.assertEqual(esp32.refresh_udp_port(), 4210)
        status, body, _ = esp32.drive(150, 90, 400)
        self.assertEqual(status, 200)
        self.assertEqual(self.socket.sent, [("DRIVE 150 90 400", (esp32._esp32_host, 4210))])
        self.assertEqual(self.http, ["/api/status"], "no HTTP request per drive command")

    def test_stop_goes_over_udp_and_http(self):
        esp32.refresh_udp_port()
        esp32.send_stop()
        self.assertEqual(self.socket.sent[0][0], "STOP")
        self.assertIn("/api/stop", self.http)

    def test_old_firmware_uses_http(self):
        del self.status["udpPort"]
        self.assertIsNone(esp32.refresh_udp_port())
        esp32.drive(150, 90, 400)
        self.assertEqual(self.socket.sent, [])
        self.assertIn("/api/drive?left=150&right=90&ms=400", self.http)

    def test_not_used_through_a_pi_gateway(self):
        self.status["wifi"] = "10.0.0.99"  # the answer was proxied from an ESP32 elsewhere
        self.assertIsNone(esp32.refresh_udp_port())

    def test_check_runs_in_the_background(self):
        self.delay = 0.2  # a slow status reply must not hold up a drive command
        started = time.monotonic()
        self.assertIsNone(esp32.udp_port(), "first call does not wait for the check")
        self.assertLess(time.monotonic() - started, 0.1)
        for _ in range(100):
            if esp32._udp["checked"]:
                break
            time.sleep(0.01)
        self.assertEqual(esp32.udp_port(), 4210)


if __name__ == "__main__":
    unittest.main()
